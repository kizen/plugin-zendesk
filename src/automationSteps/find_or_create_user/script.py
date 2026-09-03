import time

# Preview-qualified while this plugin's PR is open; switch to plain "zendesk" once merged.
PLUGIN_API_NAME = "zendesk_preview_kzn_18120_spike_explore_zendesk_integration"
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


def upstream_status_code(resp):
    try:
        payload = resp.json()
    except Exception:
        return resp.status_code
    if isinstance(payload, dict) and isinstance(payload.get("status_code"), int):
        return payload["status_code"]
    return resp.status_code


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

zendesk_user_id = getattr(inputs, "zendesk_user_id", None)
email = getattr(inputs, "user_email", None)
name = getattr(inputs, "user_name", None)
phone = getattr(inputs, "phone", None)
external_id = getattr(inputs, "external_id", None)
organization_id = getattr(inputs, "organization_id", None)
create_if_missing = bool(getattr(inputs, "create_if_missing", False))

if not (zendesk_user_id or external_id or email or name):
    raise Exception(
        "Zendesk error: missing_search_key — provide at least one of Zendesk User ID, External ID, Email, or Name to search by."
    )

# zendesk_subdomain Integration Secret — same name as the OAuth-templating secret, separate value registration.
subdomain_secret_key = next((key for key in secrets if key.endswith("zendesk_subdomain")), None)
if not subdomain_secret_key:
    raise Exception("zendesk_subdomain secret is not set for this business — set it before running this action.")
zendesk_subdomain = secrets[subdomain_secret_key]

full_domain = f"{zendesk_subdomain}.zendesk.com"

# Precedence when more than one is set: Zendesk User ID > External ID > Email > Name — each provided key is tried in turn until one matches.
user = None

if zendesk_user_id:
    # Direct record fetch by Zendesk's primary key; a 404 means no match, not a fatal error.
    resp = zendesk_request_with_retry(
        kizen.api.get,
        f"{BASE_URL}/api/v2/users/{zendesk_user_id}.json",
        params={"full_domain": full_domain},
    )
    if upstream_status_code(resp) != 404:
        check_response(resp, "searching for user by Zendesk User ID")
        user = resp.json().get("body", {}).get("user")

if not user and external_id:
    # Dedicated list-with-filter endpoint — a precise match, unlike the general search endpoint below.
    resp = zendesk_request_with_retry(
        kizen.api.get,
        f"{BASE_URL}/api/v2/users.json",
        params={"external_id": external_id, "full_domain": full_domain},
    )
    check_response(resp, "searching for user by external ID")
    users = resp.json().get("body", {}).get("users") or []
    user = users[0] if users else None

if not user and email:
    resp = zendesk_request_with_retry(
        kizen.api.get,
        f"{BASE_URL}/api/v2/users/search.json",
        params={"query": f"email:{email}", "full_domain": full_domain},
    )
    check_response(resp, "searching for user by email")
    users = resp.json().get("body", {}).get("users") or []
    user = users[0] if users else None

if not user and name:
    resp = zendesk_request_with_retry(
        kizen.api.get,
        f"{BASE_URL}/api/v2/users/search.json",
        params={"query": f"name:{name}", "full_domain": full_domain},
    )
    check_response(resp, "searching for user by name")
    users = resp.json().get("body", {}).get("users") or []
    user = users[0] if users else None

was_created = False

if not user and create_if_missing:
    if not name:  # Zendesk requires a name to create any new user.
        raise Exception("Zendesk error: invalid_name — Name is required to create a new user (no existing user matched).")

    new_user = {"name": name}
    if email:
        new_user["email"] = email
    if phone:
        new_user["phone"] = phone
    if external_id:
        new_user["external_id"] = external_id
    if organization_id:
        try:
            new_user["organization_id"] = int(organization_id)
        except ValueError:
            raise Exception(f"Zendesk error: invalid_organization_id — must be numeric, got {organization_id!r}.")

    create_resp = zendesk_request_with_retry(
        kizen.api.post,
        f"{BASE_URL}/api/v2/users.json",
        json={"user": new_user},
        params={"full_domain": full_domain},
    )
    check_response(create_resp, "creating user")

    user = create_resp.json().get("body", {}).get("user", {})
    was_created = True

user_organization_id = user.get("organization_id") if user else None

outputs.user_id = str(user.get("id")) if user and user.get("id") is not None else ""
outputs.user_name = (user.get("name") if user else "") or ""
outputs.user_email = (user.get("email") if user else "") or ""
outputs.organization_id = str(user_organization_id) if user_organization_id is not None else ""
outputs.role = (user.get("role") if user else "") or ""
outputs.was_created = was_created
