import time
from urllib.parse import urlparse

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

ticket_id = inputs.ticket_id

resp = zendesk_request_with_retry(kizen.api.get, f"{BASE_URL}/tickets/{ticket_id}.json")
if is_upstream_error(resp):
    raise_zendesk_error(resp, "fetching ticket")

ticket = resp.json().get("body", {}).get("ticket", {})

# TEMPORARY diagnostic — testing the additional_service_urls/full_domain mechanism as a
# candidate for per-business dynamic hosts. Does NOT touch the working fetch above; only
# reports its own outcome. Remove this block once the experiment concludes either way.
# Step 1 (proven): full_domain="kizen-79102.zendesk.com" (our enumerated host) succeeded.
# Step 2 (this test): a host NOT listed in additional_service_urls at all — tells us whether
# that list is an enforced whitelist (multi-tenant would need every subdomain pre-enumerated,
# a non-starter) or full_domain accepts any value freely (which would solve the multi-tenant
# problem outright, no sub_domain_regex_validation needed).
try:
    full_domain_resp = zendesk_request_with_retry(
        kizen.api.get,
        f"{BASE_URL}/api/v2/tickets/{ticket_id}.json",
        params={"full_domain": "nonexistent-subdomain-test-12345.zendesk.com"},
    )
    if is_upstream_error(full_domain_resp):
        raise_zendesk_error(full_domain_resp, "full_domain diagnostic (unlisted host)")
    full_domain_ticket = full_domain_resp.json().get("body", {}).get("ticket", {})
    debug_full_domain_test = f"success: subject={full_domain_ticket.get('subject')!r}"
except Exception as exc:
    debug_full_domain_test = f"failed: {exc}"

# TEMPORARY diagnostic — checking whether setup_assistant Configuration values (e.g.
# zendesk_subdomain) are reachable from a Python Code Step at all. The dev toolkit's
# Configuration tab describes them as "exposed to plugin scripts at this.config" — that's
# JS-surface language, but never directly tested from Python. Remove once concluded.
config_debug_attempts = []
try:
    config_debug_attempts.append(f"global 'config' = {config!r}")
except NameError as exc:
    config_debug_attempts.append(f"global 'config' -> NameError: {exc}")
try:
    config_debug_attempts.append(f"kizen.config = {kizen.config!r}")
except AttributeError as exc:
    config_debug_attempts.append(f"kizen.config -> AttributeError: {exc}")
except NameError as exc:
    config_debug_attempts.append(f"kizen.config -> NameError: {exc}")
try:
    config_debug_attempts.append(f"kizen.business_config = {kizen.business_config!r}")
except AttributeError as exc:
    config_debug_attempts.append(f"kizen.business_config -> AttributeError: {exc}")
except NameError as exc:
    config_debug_attempts.append(f"kizen.business_config -> NameError: {exc}")
debug_config_test = " | ".join(config_debug_attempts)

# TEMPORARY diagnostic — a coworker (Eric Ravet) pointed to a DIFFERENT mechanism than the
# three attribute-access attempts above: a Kizen-internal platform API endpoint,
# external-integrations/business-plugin-apps/{plugin_api_name}, called like any other
# kizen.api request rather than read off a Python global. This is a genuinely separate
# candidate for reading setup_assistant Configuration values from a Python Code Step — the
# earlier debug_config_test failures don't rule this out, since they only tested attribute
# access, never an actual HTTP call to this route. Per developer.kizen.com's Service Accounts
# doc, kizen.api's root URL already bakes in "/api" — pass only the path after that, matching
# the doc's own "/custom-objects" example — so no leading "/api" segment here (a first attempt
# with one 404'd). That doc also separately confirms "a service account can export business
# config but cannot import it," consistent with this being a real, sanctioned read path.
# Remove this block once concluded.
business_config_payload = None
try:
    business_config_resp = kizen.api.get(
        "/external-integrations/business-plugin-apps/zendesk_preview_kzn_18120_spike_explore_zendesk_integration"
    )
    if business_config_resp.ok:
        business_config_payload = business_config_resp.json()
        top_level_keys = list(business_config_payload.keys()) if isinstance(business_config_payload, dict) else None
        non_plugin_app_view = (
            {k: v for k, v in business_config_payload.items() if k != "plugin_app"}
            if isinstance(business_config_payload, dict)
            else business_config_payload
        )
        debug_business_config_test = (
            f"success (HTTP {business_config_resp.status_code}); top-level keys: {top_level_keys}; "
            f"non-plugin_app content: {non_plugin_app_view}"
        )
    else:
        debug_business_config_test = f"failed: HTTP {business_config_resp.status_code} — {business_config_resp.text[:500]}"
except Exception as exc:
    debug_business_config_test = f"failed: {exc}"

# TEMPORARY diagnostic — chains the two mechanisms above: read this business's own configured
# zendesk_subdomain (via the business-plugin-apps call just above) and build a `full_domain`
# value FROM it dynamically, instead of the hardcoded "kizen-79102.zendesk.com" used in the
# earlier full_domain tests. This is the real candidate mechanism for per-business host
# routing — a per-call override built in script.py, not a change to base_service_url itself
# (which is resolved by the deployed proxy before script.py ever runs, the same static layer
# that already defeated {{fieldKey}} templating). kizen.json's zendesk_api service now also
# declares sub_domain_regex_validation ("wildcard" pattern matching any *.zendesk.com host) so
# this is a declared, intentional capability rather than relying on an undocumented gap in
# additional_service_urls enforcement. Remove this block once concluded.
try:
    clean_config = (
        (business_config_payload or {}).get("config", {}).get("__kizen_clean_config", {})
        if isinstance(business_config_payload, dict)
        else {}
    )
    dynamic_subdomain = clean_config.get("zendesk_subdomain")
    if not dynamic_subdomain:
        raise Exception(f"zendesk_subdomain not found in business config: {clean_config!r}")
    dynamic_full_domain = f"{dynamic_subdomain}.zendesk.com"
    dynamic_resp = zendesk_request_with_retry(
        kizen.api.get,
        f"{BASE_URL}/api/v2/tickets/{ticket_id}.json",
        params={"full_domain": dynamic_full_domain},
    )
    if is_upstream_error(dynamic_resp):
        raise_zendesk_error(dynamic_resp, "dynamic full_domain diagnostic")
    dynamic_ticket = dynamic_resp.json().get("body", {}).get("ticket", {})
    debug_dynamic_full_domain_test = (
        f"success: dynamic_full_domain={dynamic_full_domain!r}, subject={dynamic_ticket.get('subject')!r}"
    )
except Exception as exc:
    debug_dynamic_full_domain_test = f"failed: {exc}"

# The ticket object only carries requester_id, not the requester's email — a second lookup
# against the user record is required. If that lookup fails, leave requester_email blank
# rather than failing the whole Get Ticket call over a secondary piece of data.
requester_id = ticket.get("requester_id")
requester_email = ""
if requester_id:
    user_resp = zendesk_request_with_retry(kizen.api.get, f"{BASE_URL}/users/{requester_id}.json")
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
outputs.debug_full_domain_test = debug_full_domain_test  # TEMPORARY — remove with the block above
outputs.debug_config_test = debug_config_test  # TEMPORARY — remove with the block above
outputs.debug_business_config_test = debug_business_config_test  # TEMPORARY — remove with the block above
outputs.debug_dynamic_full_domain_test = debug_dynamic_full_domain_test  # TEMPORARY — remove with the block above
outputs.ticket_url = agent_url
