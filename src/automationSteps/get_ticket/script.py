import time
from urllib.parse import urlparse

# NOTE: while this plugin's PR preview is live, api_name is preview-qualified
# (zendesk_preview_<branch-slug>) instead of the plain "zendesk" — see the PR's
# plugin-wizard bot comment for the current value. Update once merged/published.
BASE_URL = "/external-integrations/proxy/zendesk_preview_kzn_18120_spike_explore_zendesk_integration/zendesk_api"

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

ticket_id = inputs.ticket_id

resp = zendesk_request_with_retry(kizen.api.get, f"{BASE_URL}/tickets/{ticket_id}.json")
if not resp.ok:
    raise_zendesk_error(resp, "fetching ticket")

ticket = resp.json().get("body", {}).get("ticket", {})

# The ticket object only carries requester_id, not the requester's email — a second lookup
# against the user record is required. If that lookup fails, leave requester_email blank
# rather than failing the whole Get Ticket call over a secondary piece of data.
requester_id = ticket.get("requester_id")
requester_email = ""
if requester_id:
    user_resp = zendesk_request_with_retry(kizen.api.get, f"{BASE_URL}/users/{requester_id}.json")
    if user_resp.ok:
        requester_email = user_resp.json().get("body", {}).get("user", {}).get("email") or ""

# Same host-derivation approach as Create Ticket's ticket_url — avoids hardcoding our dev
# subdomain, so this keeps working if base_service_url ever moves to a per-business value.
agent_url = ""
ticket_api_url = ticket.get("url", "")
if ticket_api_url:
    parsed = urlparse(ticket_api_url)
    agent_url = f"{parsed.scheme}://{parsed.netloc}/agent/tickets/{ticket_id}"


def as_id_string(value):
    return str(value) if value is not None else ""


outputs.subject = ticket.get("subject") or ""
outputs.description = ticket.get("description") or ""
outputs.ticket_status = ticket.get("status") or ""
outputs.priority = ticket.get("priority") or ""
outputs.type = ticket.get("type") or ""
outputs.ticket_tags = ", ".join(ticket.get("tags") or [])
outputs.requester_email = requester_email
outputs.requester_id = as_id_string(requester_id)
outputs.assignee_id = as_id_string(ticket.get("assignee_id"))
outputs.group_id = as_id_string(ticket.get("group_id"))
outputs.organization_id = as_id_string(ticket.get("organization_id"))
outputs.external_id = ticket.get("external_id") or ""
outputs.created_at = ticket.get("created_at") or ""
outputs.updated_at = ticket.get("updated_at") or ""
outputs.ticket_url = agent_url
