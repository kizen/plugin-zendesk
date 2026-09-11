import time
from urllib.parse import urlparse

PLUGIN_API_NAME = "zendesk"
BASE_URL = f"/external-integrations/proxy/{PLUGIN_API_NAME}/zendesk_api"

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

ticket_id = inputs.ticket_id

# zendesk_subdomain Integration Secret — same name as the OAuth-templating secret, separate value registration.
subdomain_secret_key = next((key for key in secrets if key.endswith("zendesk_subdomain")), None)
if not subdomain_secret_key:
    raise Exception("zendesk_subdomain secret is not set for this business — set it before running this action.")
zendesk_subdomain = secrets[subdomain_secret_key]

full_domain = f"{zendesk_subdomain}.zendesk.com"

resp = zendesk_request_with_retry(
    kizen.api.get,
    f"{BASE_URL}/api/v2/tickets/{ticket_id}.json",
    params={"full_domain": full_domain},
)
check_response(resp, "fetching ticket")

ticket = resp.json().get("body", {}).get("ticket", {})

# Ticket object only carries requester_id; a second lookup resolves email, left blank on failure.
requester_id = ticket.get("requester_id")
requester_email = ""
if requester_id:
    user_resp = zendesk_request_with_retry(
        kizen.api.get,
        f"{BASE_URL}/api/v2/users/{requester_id}.json",
        params={"full_domain": full_domain},
    )
    try:
        check_response(user_resp, "resolving requester email")
        requester_email = user_resp.json().get("body", {}).get("user", {}).get("email") or ""
    except Exception:
        pass

# Agent-facing URL derived from Zendesk's own returned host rather than a hardcoded subdomain.
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
