import json
import time
from urllib.parse import urlparse

# NOTE: while this plugin's PR preview is live, api_name is preview-qualified
# (zendesk_preview_<branch-slug>) instead of the plain "zendesk" — see the PR's
# plugin-wizard bot comment for the current value. Update once merged/published.
BASE_URL = "/external-integrations/proxy/zendesk_preview_kzn_18120_spike_explore_zendesk_integration/zendesk_api"

VALID_PRIORITIES = {"urgent", "high", "normal", "low"}
VALID_TYPES = {"problem", "incident", "question", "task"}
VALID_STATUSES = {"new", "open", "pending", "hold", "solved", "closed"}

# HELPERS


def raise_zendesk_error(resp, context):
    # Kizen's proxy wraps a successful upstream call as {"status_code", "response_headers",
    # "body": <upstream response>} — a relayed Zendesk error lives at payload["body"]["error"]/
    # ["description"]. A proxy-level error (routing/auth/content-type) is Kizen's own flat,
    # unwrapped shape.
    try:
        payload = resp.json()
    except Exception:
        raise Exception(f"Zendesk error {context}: unknown_error — HTTP {resp.status_code}")

    body = payload.get("body")
    if isinstance(body, dict):
        error = body.get("error")
        description = body.get("description")
        if error or description:
            label = error.get("title") if isinstance(error, dict) else error
            message = f"Zendesk error {context}: {label or 'unknown_error'}"
            if description:
                message += f" — {description}"
            raise Exception(message)

    kizen_error = payload.get("error") or payload.get("detail")
    if kizen_error:
        raise Exception(f"Zendesk error {context}: proxy_error — {kizen_error}")

    raise Exception(f"Zendesk error {context}: unknown_error — HTTP {resp.status_code}")


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
priority = getattr(inputs, "priority", None)
ticket_type = getattr(inputs, "type", None)
status = getattr(inputs, "ticket_status", None)
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
    if not isinstance(parsed_custom_fields, list):
        raise Exception("Zendesk error: invalid_custom_fields — must be a JSON array of {id, value} objects.")
    ticket["custom_fields"] = parsed_custom_fields

resp = zendesk_request_with_retry(kizen.api.post, f"{BASE_URL}/tickets.json", json={"ticket": ticket})
if not resp.ok:
    raise_zendesk_error(resp, "creating ticket")

result = resp.json().get("body", {}).get("ticket", {})

ticket_id = result.get("id")

# Zendesk's ticket payload only carries its own API URL (e.g. ".../api/v2/tickets/35.json"),
# not an agent-facing UI URL — derive the latter from the former's host instead of hardcoding
# our dev subdomain, so this keeps working if base_service_url ever moves to a per-business value.
agent_url = ""
ticket_api_url = result.get("url", "")
if ticket_api_url and ticket_id is not None:
    parsed = urlparse(ticket_api_url)
    agent_url = f"{parsed.scheme}://{parsed.netloc}/agent/tickets/{ticket_id}"

outputs.ticket_id = str(ticket_id) if ticket_id is not None else ""
outputs.ticket_url = agent_url
outputs.ticket_status = result.get("status", "")
outputs.created_at = result.get("created_at", "")
