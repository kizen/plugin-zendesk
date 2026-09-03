import json
import time
from datetime import datetime
from urllib.parse import urlparse

# Preview-qualified while this plugin's PR is open; switch to plain "zendesk" once merged.
PLUGIN_API_NAME = "zendesk_preview_kzn_18120_spike_explore_zendesk_integration"
BASE_URL = f"/external-integrations/proxy/{PLUGIN_API_NAME}/zendesk_api"

VALID_STATUSES = {"new", "open", "pending", "hold", "solved", "closed"}
VALID_PRIORITIES = {"urgent", "high", "normal", "low"}
VALID_TYPES = {"problem", "incident", "question", "task"}

MAX_PER_PAGE = 100
DEFAULT_LIMIT = 100
MAX_RESULTS = 1000  # Zendesk's own hard cap on /search (100/page * 10 pages) — 422s beyond this.
MAX_PAGES = MAX_RESULTS // MAX_PER_PAGE

# HELPERS


def check_response(resp, context):
    """Raise if the proxy or Zendesk returned an error."""
    if not resp.ok:
        try:
            payload = resp.json()
        except Exception:
            raise Exception(f"Proxy error {context} — HTTP {resp.status_code}")
        detail = payload.get("error") or payload.get("detail") if isinstance(payload, dict) else None
        raise Exception(f"Proxy error {context} — {detail or f'HTTP {resp.status_code}'}")

    try:
        payload = resp.json()
    except Exception:
        raise Exception(f"Unexpected non-JSON response {context} — HTTP {resp.status_code}")

    if not isinstance(payload, dict):
        return

    status_code = payload.get("status_code")
    if isinstance(status_code, int) and not (200 <= status_code < 300):
        body = payload.get("body")
        if isinstance(body, dict):
            error = body.get("error")
            description = body.get("description")
            details = body.get("details")
            if error or description:
                label = error.get("title") if isinstance(error, dict) else error
                message = f"Zendesk error {context}: {label or 'unknown_error'}"
                if description:
                    message += f" — {description}"
                if isinstance(details, dict) and details:
                    detail_bits = []
                    for field, issues in details.items():
                        for issue in issues if isinstance(issues, list) else [issues]:
                            text = issue.get("description") if isinstance(issue, dict) else str(issue)
                            if text:
                                detail_bits.append(f"{field}: {text}")
                    if detail_bits:
                        message += " (" + "; ".join(detail_bits) + ")"
                raise Exception(message)
        raise Exception(f"Zendesk error {context}: unknown_error — HTTP {status_code}")

    if not isinstance(status_code, int):
        kizen_error = payload.get("error") or payload.get("detail")
        if kizen_error:
            raise Exception(f"Zendesk error {context}: proxy_error — {kizen_error}")


def zendesk_request_with_retry(method, url, **kwargs):
    resp = method(url, **kwargs)
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After")
        if not retry_after or int(retry_after) > 20:
            raise Exception(f"Zendesk error: rate_limited — retry after {retry_after or 'unknown'}s.")
        time.sleep(int(retry_after))
        resp = method(url, **kwargs)
    return resp


def as_search_date(value, label):
    # Zendesk's search date filters are date-only (YYYY-MM-DD) — accept a bare date or ISO datetime.
    if not value:
        return None
    date_part = value[:10]
    try:
        datetime.strptime(date_part, "%Y-%m-%d")
    except ValueError:
        raise Exception(f"Zendesk error: invalid_{label} — expected YYYY-MM-DD (or an ISO datetime), got {value!r}.")
    return date_part


# MAIN LOGIC

requester_email = getattr(inputs, "requester_email", None)
status = (getattr(inputs, "ticket_status", None) or "").strip().lower() or None
priority = (getattr(inputs, "priority", None) or "").strip().lower() or None
ticket_type = (getattr(inputs, "type", None) or "").strip().lower() or None
tags = getattr(inputs, "ticket_tags", None)
organization_id = getattr(inputs, "organization_id", None)
created_after = getattr(inputs, "created_after", None)
updated_after = getattr(inputs, "updated_after", None)
external_id = getattr(inputs, "external_id", None)
raw_query = getattr(inputs, "raw_query", None)
limit = getattr(inputs, "limit", None)

if status and status not in VALID_STATUSES:
    raise Exception(
        f"Zendesk error: invalid_status — must be one of {', '.join(sorted(VALID_STATUSES))}, got {status!r}."
    )
if priority and priority not in VALID_PRIORITIES:
    raise Exception(
        f"Zendesk error: invalid_priority — must be one of {', '.join(sorted(VALID_PRIORITIES))}, got {priority!r}."
    )
if ticket_type and ticket_type not in VALID_TYPES:
    raise Exception(
        f"Zendesk error: invalid_type — must be one of {', '.join(sorted(VALID_TYPES))}, got {ticket_type!r}."
    )

if not limit:
    target_count = DEFAULT_LIMIT
else:
    try:
        target_count = int(limit)
    except (TypeError, ValueError):
        raise Exception(f"Zendesk error: invalid_limit — must be a number, got {limit!r}.")
target_count = min(target_count, MAX_RESULTS)

if raw_query:
    # Always scoped to tickets — a raw fragment is combined with a fixed type:ticket, not trusted alone.
    query = f"type:ticket {raw_query}"
else:
    clauses = ["type:ticket"]
    if requester_email:
        clauses.append(f"requester:{requester_email}")
    if status:
        clauses.append(f"status:{status}")
    if priority:
        clauses.append(f"priority:{priority}")
    if ticket_type:
        clauses.append(f"ticket_type:{ticket_type}")
    if tags:
        clauses.extend(f"tags:{t.strip()}" for t in tags.split(",") if t.strip())
    if organization_id:
        clauses.append(f"organization:{organization_id}")
    created_date = as_search_date(created_after, "created_after")
    if created_date:
        clauses.append(f"created>{created_date}")
    updated_date = as_search_date(updated_after, "updated_after")
    if updated_date:
        clauses.append(f"updated>{updated_date}")
    if external_id:
        clauses.append(f"external_id:{external_id}")
    query = " ".join(clauses)

# zendesk_subdomain Integration Secret — same name as the OAuth-templating secret, separate value registration.
subdomain_secret_key = next((key for key in secrets if key.endswith("zendesk_subdomain")), None)
if not subdomain_secret_key:
    raise Exception("zendesk_subdomain secret is not set for this business — set it before running this action.")
zendesk_subdomain = secrets[subdomain_secret_key]

full_domain = f"{zendesk_subdomain}.zendesk.com"

tickets = []
page = 1
while True:
    resp = zendesk_request_with_retry(
        kizen.api.get,
        f"{BASE_URL}/api/v2/search.json",
        params={
            "query": query,
            "sort_by": "created_at",
            "sort_order": "desc",
            "per_page": MAX_PER_PAGE,
            "page": page,
            "full_domain": full_domain,
        },
    )
    check_response(resp, "searching tickets")

    payload = resp.json().get("body", {})
    results = payload.get("results") or []
    tickets.extend(results)

    reached_last_page = len(results) < MAX_PER_PAGE
    if len(tickets) >= target_count or reached_last_page or page >= MAX_PAGES:
        break
    page += 1

tickets = tickets[:target_count]


def ticket_summary(ticket):
    ticket_id = ticket.get("id")
    ticket_api_url = ticket.get("url", "")
    agent_url = ""
    if ticket_api_url and ticket_id is not None:
        parsed = urlparse(ticket_api_url)
        agent_url = f"{parsed.scheme}://{parsed.netloc}/agent/tickets/{ticket_id}"
    requester_id = ticket.get("requester_id")
    return {
        "id": str(ticket_id) if ticket_id is not None else "",
        "subject": ticket.get("subject") or "",
        "status": ticket.get("status") or "",
        "priority": ticket.get("priority") or "",
        "requester_id": str(requester_id) if requester_id is not None else "",
        "updated_at": ticket.get("updated_at") or "",
        "ticket_url": agent_url,
    }


outputs.tickets = json.dumps([ticket_summary(t) for t in tickets])
outputs.count = len(tickets)
