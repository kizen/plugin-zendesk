import time

# NOTE: while this plugin's PR preview is live, api_name is preview-qualified
# (zendesk_preview_<branch-slug>) instead of the plain "zendesk" — see the PR's
# plugin-wizard bot comment for the current value. Update once merged/published.
BASE_URL = "/external-integrations/proxy/zendesk_preview_kzn_18120_spike_explore_zendesk_integration/zendesk_api"

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

ticket_id = inputs.ticket_id
comment_body = inputs.body
is_public = bool(inputs.is_public)
status = getattr(inputs, "status", None)

if status and status not in VALID_STATUSES:
    raise Exception(
        f"Zendesk error: invalid_status — must be one of {', '.join(sorted(VALID_STATUSES))}, got {status!r}."
    )

ticket = {"comment": {"body": comment_body, "public": is_public}}
if status:
    ticket["status"] = status

resp = zendesk_request_with_retry(kizen.api.put, f"{BASE_URL}/tickets/{ticket_id}.json", json={"ticket": ticket})
if not resp.ok:
    raise_zendesk_error(resp, "adding ticket comment")

# Zendesk's ticket-update response only echoes back the ticket, not the comment that was
# just added — fetch the comment list newest-first and take the top row instead of assuming
# any particular field on the update response.
comments_resp = zendesk_request_with_retry(
    kizen.api.get,
    f"{BASE_URL}/tickets/{ticket_id}/comments.json",
    params={"sort_order": "desc", "per_page": 1},
)
if not comments_resp.ok:
    raise_zendesk_error(comments_resp, "fetching new comment")

comments = comments_resp.json().get("body", {}).get("comments", [])
comment = comments[0] if comments else {}

outputs.comment_id = str(comment.get("id", ""))
outputs.created_at = comment.get("created_at", "")
outputs.is_public = comment.get("public", is_public)
