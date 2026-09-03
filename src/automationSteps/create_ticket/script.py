import json
import time
from urllib.parse import urlparse

# Preview-qualified while this plugin's PR is open; switch to plain "zendesk" once merged.
PLUGIN_API_NAME = "zendesk_preview_kzn_18120_spike_explore_zendesk_integration"
BASE_URL = f"/external-integrations/proxy/{PLUGIN_API_NAME}/zendesk_api"

VALID_PRIORITIES = {"urgent", "high", "normal", "low"}
VALID_TYPES = {"problem", "incident", "question", "task"}
VALID_STATUSES = {"new", "open", "pending", "hold", "solved", "closed"}

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


# MAIN LOGIC

subject = inputs.subject
comment_body = inputs.comment_body
requester_email = getattr(inputs, "requester_email", None)
requester_name = getattr(inputs, "requester_name", None)
priority = (getattr(inputs, "priority", None) or "").strip().lower() or None
ticket_type = (getattr(inputs, "type", None) or "").strip().lower() or None
status = (getattr(inputs, "ticket_status", None) or "").strip().lower() or None
tags = getattr(inputs, "ticket_tags", None)
assignee_id = getattr(inputs, "assignee_id", None)
group_id = getattr(inputs, "group_id", None)
external_id = getattr(inputs, "external_id", None)
custom_fields = getattr(inputs, "custom_fields", None)

if priority and priority not in VALID_PRIORITIES:
    raise Exception(
        f"Zendesk error: invalid_priority — must be one of {', '.join(sorted(VALID_PRIORITIES))}, got {priority!r}."
    )
if ticket_type and ticket_type not in VALID_TYPES:
    raise Exception(
        f"Zendesk error: invalid_type — must be one of {', '.join(sorted(VALID_TYPES))}, got {ticket_type!r}."
    )
if status and status not in VALID_STATUSES:
    raise Exception(
        f"Zendesk error: invalid_status — must be one of {', '.join(sorted(VALID_STATUSES))}, got {status!r}."
    )
# "pending" requires an assignee or Zendesk rejects the ticket with RecordInvalid — confirmed empirically.
if status == "pending" and not assignee_id:
    raise Exception("Zendesk error: invalid_status — status 'pending' requires Assignee ID to be set.")

ticket = {
    "subject": subject,
    "comment": {"body": comment_body, "public": True},
}

if requester_email:
    requester = {"email": requester_email}
    if requester_name:
        requester["name"] = requester_name
    ticket["requester"] = requester

if priority:
    ticket["priority"] = priority
if ticket_type:
    ticket["type"] = ticket_type
if status:
    ticket["status"] = status
if tags:
    ticket["tags"] = [t.strip() for t in tags.split(",") if t.strip()]

if assignee_id:
    try:
        ticket["assignee_id"] = int(assignee_id)
    except ValueError:
        raise Exception(f"Zendesk error: invalid_assignee_id — must be numeric, got {assignee_id!r}.")

if group_id:
    try:
        ticket["group_id"] = int(group_id)
    except ValueError:
        raise Exception(f"Zendesk error: invalid_group_id — must be numeric, got {group_id!r}.")

if external_id:
    ticket["external_id"] = external_id

if custom_fields:
    try:
        parsed_custom_fields = json.loads(custom_fields)
    except json.JSONDecodeError as exc:
        raise Exception(f"Zendesk error: invalid_custom_fields — must be valid JSON: {exc}")
    if not isinstance(parsed_custom_fields, list) or not all(
        isinstance(item, dict) and "id" in item and "value" in item for item in parsed_custom_fields
    ):
        raise Exception("Zendesk error: invalid_custom_fields — must be a JSON array of {id, value} objects.")
    ticket["custom_fields"] = parsed_custom_fields

# zendesk_subdomain Integration Secret — same name as the OAuth-templating secret, separate value registration.
subdomain_secret_key = next((key for key in secrets if key.endswith("zendesk_subdomain")), None)
if not subdomain_secret_key:
    raise Exception("zendesk_subdomain secret is not set for this business — set it before running this action.")
zendesk_subdomain = secrets[subdomain_secret_key]

full_domain = f"{zendesk_subdomain}.zendesk.com"

resp = zendesk_request_with_retry(
    kizen.api.post,
    f"{BASE_URL}/api/v2/tickets.json",
    json={"ticket": ticket},
    params={"full_domain": full_domain},
)
check_response(resp, "creating ticket")

result = resp.json().get("body", {}).get("ticket", {})

ticket_id = result.get("id")

# Agent-facing URL derived from Zendesk's own returned host rather than a hardcoded subdomain.
agent_url = ""
ticket_api_url = result.get("url", "")
if ticket_api_url and ticket_id is not None:
    parsed = urlparse(ticket_api_url)
    agent_url = f"{parsed.scheme}://{parsed.netloc}/agent/tickets/{ticket_id}"

outputs.ticket_id = str(ticket_id) if ticket_id is not None else ""
outputs.ticket_url = agent_url
outputs.ticket_status = result.get("status", "")
outputs.created_at = result.get("created_at", "")
