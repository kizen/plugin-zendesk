import time

# Preview-qualified while this plugin's PR is open; switch to plain "zendesk" once merged.
PLUGIN_API_NAME = "zendesk_preview_kzn_18120_spike_explore_zendesk_integration"
BASE_URL = f"/external-integrations/proxy/{PLUGIN_API_NAME}/zendesk_api"

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

ticket_id = inputs.ticket_id
comment_body = inputs.body
is_public = bool(inputs.is_public)
status = (getattr(inputs, "ticket_status", None) or "").strip().lower() or None

if status and status not in VALID_STATUSES:
    raise Exception(
        f"Zendesk error: invalid_status — must be one of {', '.join(sorted(VALID_STATUSES))}, got {status!r}."
    )

ticket = {"comment": {"body": comment_body, "public": is_public}}
if status:
    ticket["status"] = status

# zendesk_subdomain Integration Secret — same name as the OAuth-templating secret, separate value registration.
subdomain_secret_key = next((key for key in secrets if key.endswith("zendesk_subdomain")), None)
if not subdomain_secret_key:
    raise Exception("zendesk_subdomain secret is not set for this business — set it before running this action.")
zendesk_subdomain = secrets[subdomain_secret_key]

full_domain = f"{zendesk_subdomain}.zendesk.com"

resp = zendesk_request_with_retry(
    kizen.api.put,
    f"{BASE_URL}/api/v2/tickets/{ticket_id}.json",
    json={"ticket": ticket},
    params={"full_domain": full_domain},
)
check_response(resp, "adding ticket comment")

# Update response only echoes the ticket, not the new comment — fetch newest-first and take the top row.
comments_resp = zendesk_request_with_retry(
    kizen.api.get,
    f"{BASE_URL}/api/v2/tickets/{ticket_id}/comments.json",
    params={"sort_order": "desc", "per_page": 1, "full_domain": full_domain},
)
check_response(comments_resp, "fetching new comment")

comments = comments_resp.json().get("body", {}).get("comments", [])
comment = comments[0] if comments else {}

outputs.comment_id = str(comment.get("id", ""))
outputs.created_at = comment.get("created_at", "")
outputs.is_public = comment.get("public", is_public)
