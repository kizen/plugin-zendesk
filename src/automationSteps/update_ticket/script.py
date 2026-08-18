import json
import time

# NOTE: while this plugin's PR preview is live, api_name is preview-qualified
# (zendesk_preview_<branch-slug>) instead of the plain "zendesk" — see the PR's
# plugin-wizard bot comment for the current value. Update once merged/published.
PLUGIN_API_NAME = "zendesk_preview_kzn_18120_spike_explore_zendesk_integration"
BASE_URL = f"/external-integrations/proxy/{PLUGIN_API_NAME}/zendesk_api"

VALID_PRIORITIES = {"urgent", "high", "normal", "low"}
VALID_TYPES = {"problem", "incident", "question", "task"}
VALID_STATUSES = {"new", "open", "pending", "hold", "solved", "closed"}
VALID_TAG_MODES = {"set", "add", "remove"}

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
status = getattr(inputs, "ticket_status", None)
priority = getattr(inputs, "priority", None)
ticket_type = getattr(inputs, "type", None)
assignee_id = getattr(inputs, "assignee_id", None)
group_id = getattr(inputs, "group_id", None)
tags = getattr(inputs, "ticket_tags", None)
tag_mode = getattr(inputs, "tag_mode", None) or "set"
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
if tag_mode not in VALID_TAG_MODES:
    raise Exception(
        f"Zendesk error: invalid_tag_mode — must be one of {', '.join(sorted(VALID_TAG_MODES))}, got {tag_mode!r}."
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

if tags:
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    # Zendesk's ticket-update payload has three distinct tag fields depending on desired
    # behavior: "tags" replaces the full set, "additional_tags"/"remove_tags" are update-only
    # convenience fields that add/remove without touching the rest.
    if tag_mode == "set":
        ticket["tags"] = tag_list
    elif tag_mode == "add":
        ticket["additional_tags"] = tag_list
    else:
        ticket["remove_tags"] = tag_list

if custom_fields:
    try:
        parsed_custom_fields = json.loads(custom_fields)
    except json.JSONDecodeError as exc:
        raise Exception(f"Zendesk error: invalid_custom_fields — must be valid JSON: {exc}")
    if not isinstance(parsed_custom_fields, list):
        raise Exception("Zendesk error: invalid_custom_fields — must be a JSON array of {id, value} objects.")
    ticket["custom_fields"] = parsed_custom_fields

if not ticket:
    raise Exception("Zendesk error: no_fields_to_update — set at least one field besides Ticket ID.")

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
    raise_zendesk_error(resp, "updating ticket")

result = resp.json().get("body", {}).get("ticket", {})

outputs.ticket_id = str(result.get("id", ticket_id))
outputs.ticket_status = result.get("status", "")
outputs.updated_at = result.get("updated_at", "")
