import time

# NOTE: while this plugin's PR preview is live, api_name is preview-qualified
# (zendesk_preview_<branch-slug>) instead of the plain "zendesk" — see the PR's
# plugin-wizard bot comment for the current value. Update once merged/published.
PLUGIN_API_NAME = "zendesk_preview_kzn_18120_spike_explore_zendesk_integration"
BASE_URL = f"/external-integrations/proxy/{PLUGIN_API_NAME}/zendesk_api"

VALID_STATUSES = {"new", "open", "pending", "hold", "solved", "closed"}

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
comment_body = inputs.body
is_public = bool(inputs.is_public)
status = getattr(inputs, "ticket_status", None)

if status and status not in VALID_STATUSES:
    raise Exception(
        f"Zendesk error: invalid_status — must be one of {', '.join(sorted(VALID_STATUSES))}, got {status!r}."
    )

ticket = {"comment": {"body": comment_body, "public": is_public}}
if status:
    ticket["status"] = status

# Read this business's configured Zendesk subdomain and build a full_domain override from it,
# rather than relying on base_service_url's own (fixed, single-tenant) host. See get_ticket's
# script.py for the fuller explanation of this mechanism and its limits.
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
    kizen.api.put,
    f"{BASE_URL}/api/v2/tickets/{ticket_id}.json",
    json={"ticket": ticket},
    params={"full_domain": full_domain},
)
if is_upstream_error(resp):
    raise_zendesk_error(resp, "adding ticket comment")

# Zendesk's ticket-update response only echoes back the ticket, not the comment that was
# just added — fetch the comment list newest-first and take the top row instead of assuming
# any particular field on the update response.
comments_resp = zendesk_request_with_retry(
    kizen.api.get,
    f"{BASE_URL}/api/v2/tickets/{ticket_id}/comments.json",
    params={"sort_order": "desc", "per_page": 1, "full_domain": full_domain},
)
if is_upstream_error(comments_resp):
    raise_zendesk_error(comments_resp, "fetching new comment")

comments = comments_resp.json().get("body", {}).get("comments", [])
comment = comments[0] if comments else {}

outputs.comment_id = str(comment.get("id", ""))
outputs.created_at = comment.get("created_at", "")
outputs.is_public = comment.get("public", is_public)
