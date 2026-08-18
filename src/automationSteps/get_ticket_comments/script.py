import time
from datetime import datetime

# Preview-qualified while this plugin's PR is open; switch to plain "zendesk" once merged.
PLUGIN_API_NAME = "zendesk_preview_kzn_18120_spike_explore_zendesk_integration"
BASE_URL = f"/external-integrations/proxy/{PLUGIN_API_NAME}/zendesk_api"

MAX_PER_PAGE = 100
DEFAULT_LIMIT = 100
MAX_PAGES = 10  # Safety cap on execution time, not a Zendesk limit — 10 * 100/page = 1000 comments.

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


def format_timestamp(raw):
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw


# MAIN LOGIC

ticket_id = inputs.ticket_id
public_only = bool(getattr(inputs, "public_only", False))
limit = getattr(inputs, "limit", None)

if limit is None or limit == "":
    target_count = DEFAULT_LIMIT
else:
    try:
        target_count = int(limit)
    except (TypeError, ValueError):
        raise Exception(f"Zendesk error: invalid_limit — must be a number, got {limit!r}.")
    if target_count < 1:
        raise Exception("Zendesk error: invalid_limit — must be at least 1.")

# zendesk_subdomain Integration Secret — same name as the OAuth-templating secret, separate value registration.
subdomain_secret_key = next((key for key in secrets if key.endswith("zendesk_subdomain")), None)
if not subdomain_secret_key:
    raise Exception("zendesk_subdomain secret is not set for this business — set it before running this action.")
zendesk_subdomain = secrets[subdomain_secret_key]

full_domain = f"{zendesk_subdomain}.zendesk.com"

# Pages fetched newest-first (reversed below for the transcript) until target_count is satisfied,
# a page comes back short, or MAX_PAGES is hit. include=users side-loads author names in one pass.
comments = []
users_by_id = {}
page = 1
while True:
    resp = zendesk_request_with_retry(
        kizen.api.get,
        f"{BASE_URL}/api/v2/tickets/{ticket_id}/comments.json",
        params={
            "include": "users",
            "sort_order": "desc",
            "per_page": MAX_PER_PAGE,
            "page": page,
            "full_domain": full_domain,
        },
    )
    if is_upstream_error(resp):
        raise_zendesk_error(resp, "fetching ticket comments")

    payload = resp.json().get("body", {})
    page_comments = payload.get("comments") or []
    for user in payload.get("users") or []:
        users_by_id[user.get("id")] = user

    comments.extend(page_comments if not public_only else [c for c in page_comments if c.get("public")])

    reached_last_page = len(page_comments) < MAX_PER_PAGE
    if len(comments) >= target_count or reached_last_page or page >= MAX_PAGES:
        break
    page += 1

comments = comments[:target_count]


def author_label(comment):
    author = users_by_id.get(comment.get("author_id"))
    if author:
        return author.get("name") or author.get("email") or str(comment.get("author_id", ""))
    return str(comment.get("author_id", ""))


lines = []
for comment in reversed(comments):  # oldest to newest for the transcript
    visibility = "public" if comment.get("public") else "internal"
    lines.append(
        f"[{format_timestamp(comment.get('created_at'))}] {author_label(comment)} ({visibility}): {comment.get('body', '')}"
    )

last_comment = comments[0] if comments else None  # comments is still newest-first here

outputs.thread_text = "\n".join(lines)
outputs.comment_count = len(comments)
outputs.last_comment_body = last_comment.get("body", "") if last_comment else ""
outputs.last_comment_is_public = bool(last_comment.get("public")) if last_comment else False
outputs.last_comment_author_id = str(last_comment.get("author_id", "")) if last_comment else ""
