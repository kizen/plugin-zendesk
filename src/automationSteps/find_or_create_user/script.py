import time

# NOTE: while this plugin's PR preview is live, api_name is preview-qualified
# (zendesk_preview_<branch-slug>) instead of the plain "zendesk" — see the PR's
# plugin-wizard bot comment for the current value. Update once merged/published.
BASE_URL = "/external-integrations/proxy/zendesk_preview_kzn_18120_spike_explore_zendesk_integration/zendesk_api"

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

email = inputs.email
name = getattr(inputs, "name", None)
phone = getattr(inputs, "phone", None)
external_id = getattr(inputs, "external_id", None)
organization_id = getattr(inputs, "organization_id", None)
create_if_missing = bool(getattr(inputs, "create_if_missing", False))

resp = zendesk_request_with_retry(
    kizen.api.get,
    f"{BASE_URL}/users/search.json",
    params={"query": f"email:{email}"},
)
if is_upstream_error(resp):
    raise_zendesk_error(resp, "searching for user")

users = resp.json().get("body", {}).get("users") or []
user = users[0] if users else None
was_created = False

if not user and create_if_missing:
    # Same lesson learned from Create Ticket: Zendesk requires a name to create a new user —
    # checked proactively here since it's deterministic once we already know no match exists.
    if not name:
        raise Exception("Zendesk error: invalid_name — Name is required to create a new user (no existing user matched Email).")

    new_user = {"email": email, "name": name}
    if phone:
        new_user["phone"] = phone
    if external_id:
        new_user["external_id"] = external_id
    if organization_id:
        try:
            new_user["organization_id"] = int(organization_id)
        except ValueError:
            raise Exception(f"Zendesk error: invalid_organization_id — must be numeric, got {organization_id!r}.")

    create_resp = zendesk_request_with_retry(kizen.api.post, f"{BASE_URL}/users.json", json={"user": new_user})
    if is_upstream_error(create_resp):
        raise_zendesk_error(create_resp, "creating user")

    user = create_resp.json().get("body", {}).get("user", {})
    was_created = True

user_organization_id = user.get("organization_id") if user else None

outputs.user_id = str(user.get("id")) if user and user.get("id") is not None else ""
outputs.name = (user.get("name") if user else "") or ""
outputs.email = (user.get("email") if user else "") or ""
outputs.organization_id = str(user_organization_id) if user_organization_id is not None else ""
outputs.role = (user.get("role") if user else "") or ""
outputs.was_created = was_created
