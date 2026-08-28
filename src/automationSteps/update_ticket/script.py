import json
import time

# Preview-qualified while this plugin's PR is open; switch to plain "zendesk" once merged.
PLUGIN_API_NAME = "zendesk_preview_kzn_18120_spike_explore_zendesk_integration"
BASE_URL = f"/external-integrations/proxy/{PLUGIN_API_NAME}/zendesk_api"

VALID_PRIORITIES = {"urgent", "high", "normal", "low"}
VALID_TYPES = {"problem", "incident", "question", "task"}
VALID_STATUSES = {"new", "open", "pending", "hold", "solved", "closed"}

# HELPERS


def is_upstream_error(resp):
    # Proxy can return its own 200 while wrapping a failed upstream call (e.g. status_code 503 in the body).
    if not resp.ok:
        return True
    try:
        payload = resp.json()
    except Exception:
        return False
    status_code = payload.get("status_code") if isinstance(payload, dict) else None
    return isinstance(status_code, int) and not (200 <= status_code < 300)


def raise_zendesk_error(resp, context):
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
status = getattr(inputs, "ticket_status", None)
priority = getattr(inputs, "priority", None)
ticket_type = getattr(inputs, "type", None)
assignee_id = getattr(inputs, "assignee_id", None)
group_id = getattr(inputs, "group_id", None)
add_tags_raw = getattr(inputs, "add_tags", None)
remove_tags_raw = getattr(inputs, "remove_tags", None)
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

ticket = {}

if status:
    ticket["status"] = status
if priority:
    ticket["priority"] = priority
if ticket_type:
    ticket["type"] = ticket_type

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

if custom_fields:
    try:
        parsed_custom_fields = json.loads(custom_fields)
    except json.JSONDecodeError as exc:
        raise Exception(f"Zendesk error: invalid_custom_fields — must be valid JSON: {exc}")
    if not isinstance(parsed_custom_fields, list):
        raise Exception("Zendesk error: invalid_custom_fields — must be a JSON array of {id, value} objects.")
    ticket["custom_fields"] = parsed_custom_fields

add_tag_list = [t.strip() for t in add_tags_raw.split(",") if t.strip()] if add_tags_raw else []
remove_tag_list = [t.strip() for t in remove_tags_raw.split(",") if t.strip()] if remove_tags_raw else []

if not ticket and not add_tag_list and not remove_tag_list:
    raise Exception("Zendesk error: no_fields_to_update — set at least one field besides Ticket ID.")

# zendesk_subdomain Integration Secret — same name as the OAuth-templating secret, separate value registration.
subdomain_secret_key = next((key for key in secrets if key.endswith("zendesk_subdomain")), None)
if not subdomain_secret_key:
    raise Exception("zendesk_subdomain secret is not set for this business — set it before running this action.")
zendesk_subdomain = secrets[subdomain_secret_key]

full_domain = f"{zendesk_subdomain}.zendesk.com"

result = {}

if ticket:
    resp = zendesk_request_with_retry(
        kizen.api.put,
        f"{BASE_URL}/api/v2/tickets/{ticket_id}.json",
        json={"ticket": ticket},
        params={"full_domain": full_domain},
    )
    if is_upstream_error(resp):
        raise_zendesk_error(resp, "updating ticket")
    result = resp.json().get("body", {}).get("ticket", {})

if add_tag_list:
    add_resp = zendesk_request_with_retry(
        kizen.api.put,
        f"{BASE_URL}/api/v2/tickets/{ticket_id}/tags.json",
        json={"tags": add_tag_list},
        params={"full_domain": full_domain},
    )
    if is_upstream_error(add_resp):
        raise_zendesk_error(add_resp, "adding tags")

if remove_tag_list:
    remove_resp = zendesk_request_with_retry(
        kizen.api.delete,
        f"{BASE_URL}/api/v2/tickets/{ticket_id}/tags.json",
        json={"tags": remove_tag_list},
        params={"full_domain": full_domain},
    )
    if is_upstream_error(remove_resp):
        raise_zendesk_error(remove_resp, "removing tags")

if not result:
    # Only tags changed — the tags endpoint doesn't return ticket_status/updated_at, so fetch them.
    get_resp = zendesk_request_with_retry(
        kizen.api.get,
        f"{BASE_URL}/api/v2/tickets/{ticket_id}.json",
        params={"full_domain": full_domain},
    )
    if not is_upstream_error(get_resp):
        result = get_resp.json().get("body", {}).get("ticket", {})

outputs.ticket_id = str(result.get("id", ticket_id))
outputs.ticket_status = result.get("status", "")
outputs.updated_at = result.get("updated_at", "")
