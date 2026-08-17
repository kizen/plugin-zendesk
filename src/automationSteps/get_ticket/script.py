import time
from urllib.parse import urlparse

# NOTE: while this plugin's PR preview is live, api_name is preview-qualified
# (zendesk_preview_<branch-slug>) instead of the plain "zendesk" — see the PR's
# plugin-wizard bot comment for the current value. Update once merged/published.
PLUGIN_API_NAME = "zendesk_preview_kzn_18120_spike_explore_zendesk_integration"
BASE_URL = f"/external-integrations/proxy/{PLUGIN_API_NAME}/zendesk_api"

# HELPERS


def is_upstream_error(resp):
    # The proxy sometimes returns its OWN 200 while wrapping a failed upstream call — e.g. a
    # connection failure (bad/unreachable subdomain) comes back as {"status_code": 503,
    # "response_headers": {}, "body": ""} with resp.ok True. Checking resp.ok alone misses this.
    if not resp.ok:
        return True
    try:
        payload = resp.json()
    except Exception:
        return False
    status_code = payload.get("status_code") if isinstance(payload, dict) else None
    return isinstance(status_code, int) and not (200 <= status_code < 300)


def raise_zendesk_error(resp, context):
    # Kizen's proxy wraps a successful upstream call as {"status_code", "response_headers",
    # "body": <upstream response>} — a relayed Zendesk error lives at payload["body"]["error"]/
    # ["description"]. A proxy-level error (routing/auth/content-type) is Kizen's own flat,
    # unwrapped shape.
    try:
        payload = resp.json()
    except Exception:
        raise Exception(f"Zendesk error {context}: unknown_error — HTTP {resp.status_code}")

    upstream_status = payload.get("status_code", resp.status_code) if isinstance(payload, dict) else resp.status_code
    body = payload.get("body") if isinstance(payload, dict) else None
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

    kizen_error = payload.get("error") or payload.get("detail") if isinstance(payload, dict) else None
    if kizen_error:
        raise Exception(f"Zendesk error {context}: proxy_error — {kizen_error}")

    # TEMPORARY — surfaces the actual constructed/resolved base domain when the upstream
    # response is a redirect (e.g. Zendesk's own root domain redirecting to www.zendesk.com
    # when base_service_url has no real account subdomain). Without this, a 3xx just reports
    # "unknown_error — HTTP 3xx" with no visibility into where it actually went. Remove once
    # concluded testing base_service_url configurations.
    response_headers = payload.get("response_headers", {}) if isinstance(payload, dict) else {}
    redirect_location = response_headers.get("location") or response_headers.get("Location") if isinstance(response_headers, dict) else None
    if redirect_location:
        raise Exception(
            f"Zendesk error {context}: unknown_error — HTTP {upstream_status}, "
            f"constructed base domain redirected to: {redirect_location}"
        )

    raise Exception(f"Zendesk error {context}: unknown_error — HTTP {upstream_status}")


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

# Read this business's configured Zendesk subdomain and build a full_domain override from it,
# rather than relying on base_service_url's own (fixed, single-tenant) host. Config values
# aren't reachable from any Python attribute (kizen.config/kizen.business_config don't exist)
# but ARE reachable via this real Kizen platform endpoint — confirmed live. full_domain then
# genuinely overrides the request's destination host per-call, e.g.
# .../proxy/<plugin_api_name>/zendesk_api/api/v2/tickets/3.json?full_domain=<subdomain>.zendesk.com
# Note the /api/v2 segment must be included explicitly here — a full_domain override doesn't
# carry through base_service_url's own baked-in path prefix.
business_config_resp = kizen.api.get(f"/external-integrations/business-plugin-apps/{PLUGIN_API_NAME}")
if not business_config_resp.ok:
    raise Exception(f"Failed to read business config: HTTP {business_config_resp.status_code}")

zendesk_subdomain = (
    business_config_resp.json().get("config", {}).get("__kizen_clean_config", {}).get("zendesk_subdomain")
)
if not zendesk_subdomain:
    raise Exception("zendesk_subdomain is not configured for this business — set it in Configuration first.")

full_domain = f"{zendesk_subdomain}.zendesk.com"

resp = zendesk_request_with_retry(
    kizen.api.get,
    f"{BASE_URL}/api/v2/tickets/{ticket_id}.json",
    params={"full_domain": full_domain},
)
if is_upstream_error(resp):
    raise_zendesk_error(resp, "fetching ticket")

ticket = resp.json().get("body", {}).get("ticket", {})

# The ticket object only carries requester_id, not the requester's email — a second lookup
# against the user record is required. If that lookup fails, leave requester_email blank
# rather than failing the whole Get Ticket call over a secondary piece of data.
requester_id = ticket.get("requester_id")
requester_email = ""
if requester_id:
    user_resp = zendesk_request_with_retry(
        kizen.api.get,
        f"{BASE_URL}/api/v2/users/{requester_id}.json",
        params={"full_domain": full_domain},
    )
    if not is_upstream_error(user_resp):
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
