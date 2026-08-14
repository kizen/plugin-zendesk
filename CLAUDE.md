# Zendesk Plugin — Developer Context

Manages Zendesk support tickets, comments, and end users from Kizen agentic workflows via OAuth 2.0.

---

## What This Plugin Does

Seven agentic workflow steps, all running as Python 3.13 Code Steps through the Kizen proxy:

1. **Create Ticket** — opens a new ticket with a subject and initial comment.
2. **Update Ticket** — changes status/priority/type/assignee/group/tags/custom fields on an existing ticket.
3. **Add Ticket Comment** — posts a public reply or an internal note, optionally changing status at the same time.
4. **Get Ticket** — fetches a ticket's current state, including the requester's email.
5. **Get Ticket Comments** — fetches the full conversation thread, pre-flattened into one transcript string.
6. **Search Tickets** — finds tickets by requester, status, priority, type, tags, organization, date, or a raw query.
7. **Find or Create User** — resolves a Zendesk end user by email, optionally creating them if absent.

---

## Auth Method

### OAuth 2.0 — business-level, currently single-tenant

- One Zendesk OAuth connection per Kizen business, stored by the Kizen proxy under service name `zendesk_api`.
- **Unlike every other Kizen OAuth plugin (Slack, Google Drive, etc.), Zendesk does not have one Kizen-owned shared OAuth app.** Each Zendesk customer creates their *own* OAuth client inside their *own* Zendesk subdomain. That's a structural mismatch with how `kizen.json`'s `services[]` block works everywhere else on this platform — see **Known Constraints** below. This plugin is currently wired to a single dev/test Zendesk account (`kizen-79102.zendesk.com`), not a real per-business connection.
- Auth is handled transparently by the proxy when it works; scripts never touch tokens directly.

**OAuth scopes requested:**

| Scope | Purpose |
| --- | --- |
| `read` | Required specifically by `/api/v2/search.json` (Search Tickets) — the granular scopes below are not accepted as a substitute there. |
| `tickets:read` | Get Ticket, Get Ticket Comments, and other ticket-read paths. |
| `tickets:write` | Create Ticket, Update Ticket, Add Ticket Comment. |
| `users:read` | Find or Create User (lookup), Get Ticket's requester-email resolution. |
| `users:write` | Find or Create User (creation branch). |

Zendesk's scopes are a mix of coarse (`read`/`write`/`impersonate`) and resource-scoped (`tickets:read`, etc.) forms, and **they are not interchangeable per-endpoint** — confirmed the hard way when `/search.json` rejected a token that already had `tickets:read`/`users:read` with *"You are missing the following required scopes: read"*. If a future endpoint 403s despite having what looks like the right scope, check whether it wants the coarse form instead.

**Proxy URL pattern:**

```text
/external-integrations/proxy/<plugin_api_name>/zendesk_api/<zendesk_api_path>
```

While this plugin's PR preview is live (unmerged), `<plugin_api_name>` is preview-qualified as `zendesk_preview_<branch-slug>` instead of the plain `zendesk` — every script's `BASE_URL` constant currently has this preview name hardcoded and **must be updated to plain `zendesk` once this plugin is actually merged/published**, or every proxy call will 404.

---

## kizen.api and the Proxy Envelope

`kizen.api.{get,post,put,patch,delete}` is the injected proxy client. It wraps a successful upstream call as:

```json
{"status_code": <upstream status>, "response_headers": {...}, "body": <upstream response>}
```

**Critical gotcha, discovered the hard way:** the proxy's own HTTP status (`resp.ok`/`resp.status_code`) does **not** reliably reflect whether the upstream Zendesk call succeeded. A connection failure (e.g. an unreachable host) comes back as the proxy's own `200 OK` with an envelope like `{"status_code": 503, "response_headers": {}, "body": ""}` — `resp.ok` is `True`, but the real call failed. Every script in this plugin uses a shared `is_upstream_error(resp)` helper that checks *both* `resp.ok` and the embedded `status_code` before proceeding. If you add a new script, copy this helper — checking `resp.ok` alone will crash on this class of failure instead of raising a clean error.

`raise_zendesk_error(resp, context)` (also copied into every script) extracts Zendesk's real error, including the per-field `details` object Zendesk returns on `422 RecordInvalid` responses — without it, validation failures collapse into a useless generic message. Always let this surface Zendesk's real reason rather than writing a custom message.

No shared/common code library exists across plugins on this platform — these helpers are intentionally copy-pasted into every script, not imported, matching the established convention (see `plugin-slack`'s own CLAUDE.md).

---

## Agentic Workflow Steps

### `create_ticket`

**Inputs:** `subject`\*, `comment_body`\*, `requester_email`, `requester_name`, `priority`, `type`, `ticket_status`, `ticket_tags` (comma-separated), `assignee_id`, `group_id`, `external_id`, `custom_fields` (JSON array string).

**Outputs:** `ticket_id`, `ticket_url` (agent-facing, derived from the API response's own `url` field — never hardcode the subdomain), `ticket_status`, `created_at`.

Notable behavior, all confirmed by direct testing against a real account, not assumption:
- `requester_email` for a brand-new requester **requires `requester_name`** — Zendesk rejects user creation without one (`"Name: is too short"`). Not proactively validated (would require an extra lookup to know if the requester already exists), but documented in the hint.
- `ticket_status = "pending"` **requires `assignee_id`** — proactively validated in script before calling Zendesk.
- `ticket_status = "new"` is accepted but **not guaranteed to stick** — Zendesk may silently report back `open` instead (likely because the ticket was created by an authenticated agent connection with a public comment, not a genuine untouched end-user submission).
- `ticket_status = "hold"` may fail with `custom_status_id: Custom status is invalid` on accounts with Zendesk's **Custom Ticket Statuses** feature enabled — an account-configuration issue, not an assignee issue, out of scope to fix without adding a `custom_status_id` input.
- `ticket_status = "closed"` succeeds directly on creation on this account, despite Zendesk's general API docs stating `closed` isn't valid for new-ticket creation.
- Omitting `ticket_status` defaults to `open`, not `new`.

### `update_ticket`

**Inputs:** `ticket_id`\*, everything else optional (`ticket_status`, `priority`, `type`, `assignee_id`, `group_id`, `ticket_tags` + `tag_mode`, `custom_fields`). Raises `no_fields_to_update` if nothing besides `ticket_id` is set.

**Outputs:** `ticket_id`, `ticket_status`, `updated_at`.

`tag_mode` (`set`/`add`/`remove`) maps to Zendesk's three distinct tag fields on ticket update — `tags` (full replace), `additional_tags`, `remove_tags` — rather than diffing tag lists client-side. Reuse this mapping if a dedicated Add/Remove Tag action is ever built.

### `add_ticket_comment`

**Inputs:** `ticket_id`\*, `body`\*, `is_public`\* (boolean), `ticket_status` (optional).

**Outputs:** `comment_id`, `created_at`, `is_public`.

Zendesk's ticket-update response (same `PUT /tickets/{id}.json` as Update Ticket) does not echo back the comment that was just added — only the ticket. This action makes a second call, `GET /tickets/{id}/comments.json?sort_order=desc&per_page=1`, and takes the first row as the new comment.

### `get_ticket`

**Inputs:** `ticket_id`\* only.

**Outputs:** `subject`, `description`, `ticket_status`, `priority`, `type`, `ticket_tags`, `requester_email`, `requester_id`, `assignee_id`, `group_id`, `organization_id`, `external_id`, `created_at`, `updated_at`, `ticket_url`.

The ticket object only carries `requester_id`, not email — a second call, `GET /users/{requester_id}.json`, resolves it. If that secondary lookup fails, `requester_email` is left blank rather than failing the whole action.

### `get_ticket_comments`

**Inputs:** `ticket_id`\*, `public_only` (boolean), `limit` (**string**, not number — see Known Constraints).

**Outputs:** `thread_text` (formatted transcript, oldest to newest, e.g. `[2026-07-30 14:02] Jane Agent (public): ...`), `comment_count`, `last_comment_body`, `last_comment_is_public`, `last_comment_author_id`.

Paginates for real (`page=1,2,3...`, Zendesk's classic offset pagination) up to a `MAX_PAGES=10` safety cap (1000 comments — matches Zendesk's own documented cap on other list endpoints, not an arbitrary number), fetching newest-first so `limit` means "N most recent," then reversing that slice into chronological order for the transcript. Uses Zendesk's `include=users` side-loading to resolve every commenter's display name in one extra response field instead of one lookup per commenter.

### `search_tickets`

**Inputs:** all optional — `requester_email`, `ticket_status`, `priority`, `type`, `ticket_tags`, `organization_id`, `created_after`, `updated_after`, `external_id`, `raw_query`, `limit` (string).

**Outputs:** `tickets` (JSON array string, each with `id`/`subject`/`status`/`priority`/`requester_id`/`updated_at`/`ticket_url`), `count`.

Builds a Zendesk search query string, always prefixed `type:ticket`. `raw_query`, when set, **replaces** every structured filter above (still combined with the fixed `type:ticket` prefix) rather than trying to merge both — avoids duplicate/conflicting clauses. `created_after`/`updated_after` accept a bare date or an ISO datetime but only use the date portion — Zendesk's search date filters (`created>`, `updated>`) don't support time-of-day granularity. Paginates the same way as Get Ticket Comments, up to Zendesk's hard 1000-result cap (100/page × 10 pages — a real Zendesk limit, not a Kizen one; requests beyond it 422).

### `find_or_create_user`

**Inputs:** `email`\*, `name`, `phone`, `external_id`, `organization_id`, `create_if_missing` (boolean, default `false`).

**Outputs:** `user_id`, `name`, `email`, `organization_id`, `role`, `was_created`.

Searches `GET /users/search.json?query=email:{email}` (exact field-scoped match, not fuzzy) and takes the first result. If nothing matches and `create_if_missing` is `true`, creates via `POST /users.json` — proactively validates `name` is set first (same lesson as Create Ticket's requester creation: Zendesk requires a name for any new user). If nothing matches and `create_if_missing` is `false` (the default), every output comes back blank/`false` rather than raising — "not found" is a valid, expected result here, not an error.

Deliberately does **not** use Zendesk's single-call `users/create_or_update.json` convenience endpoint, despite it existing — that endpoint always creates-or-updates unconditionally, which would violate `create_if_missing`'s default-`false` semantics (never create unless explicitly asked).

---

## Rate Limits

Per Zendesk's documented, plan-dependent limits: account-wide throughput ranges from 200/min (Team) to 2,500/min (Enterprise Plus); Update Ticket specifically is capped at 100/min account-wide and 30 updates per 10 min per user per ticket. `/api/v2/search` returns at most 1,000 results (100/page, 10 pages) and 422s beyond that — Search Tickets and Get Ticket Comments both respect this via their pagination caps. Every script includes a single-retry-on-429 helper (`zendesk_request_with_retry`) that respects `Retry-After` up to 20 seconds before giving up, matching the automation step's execution time budget.

---

## Known Constraints

- **Multi-tenant / per-business connections are unsolved — this is the single biggest open question in this plugin.** `base_service_url` is currently hardcoded to one dev Zendesk account (`kizen-79102.zendesk.com`); every business that installs this plugin as-is would see *that* account's tickets, not their own. Two candidate mechanisms were tested for making `services[]` fields vary per business:
  - `{{secret.<key>}}` (what the original spike ticket assumed exists) — **never actually tested**; zero evidence it exists anywhere on this platform after a ~20-repo survey.
  - `{{fieldKey}}` templating sourced from a `setup_assistant` field (the same syntax already used elsewhere on the platform for `when`/`object_id` conditions) — **tested directly against a real account and disproven.** A controlled 3-way comparison (wrong subdomain value / correct subdomain value / reverted to a literal string) showed the templated version failing identically regardless of the Configuration value, while only reverting to a literal string fixed it — the substitution never actually happened.
  - `src/setupAssistant/assistant.json`'s `zendesk_subdomain` field is a **deliberately kept artifact** of this failed experiment — it's currently unused (nothing reads its value), left in place as a placeholder for whoever picks up the real multi-tenant design rather than deleted.
  - No existing Kizen plugin combines an interactive OAuth consent flow with per-business dynamic credentials — Slack/Google Drive/etc. all use one Kizen-owned shared app; the plugins with genuine per-installation credentials (`plugin-mysql`, `plugin-postgres`, `plugin-snowflake`) have no OAuth/`services[]` at all. This needs direct input from whoever owns the Kizen plugin platform backend, not more local experimentation.
  - **Promising unexplored lead, found in `plugin-google-sheets`'s spike doc, not yet investigated here:** a newer platform capability, `additional_service_urls` (alternate hosts a service can reach, selected per proxy call via a `full_domain` query parameter) with a companion `sub_domain_regex_validation` field. That plugin used it to route to a second *fixed* host, not a per-business one, so it doesn't directly prove per-business subdomain support — but `sub_domain_regex_validation`'s name alone suggests it may be built for exactly this kind of subdomain-varies-by-caller case. Worth testing directly before assuming the multi-tenant question needs a platform-team redesign from scratch.

- **OAuth refresh tokens die roughly every 60–90 minutes, and this is a platform bug, not something fixable from this repo.** Zendesk's refresh tokens are single-use/rotating — Zendesk's own docs confirm a successful refresh deletes both the previous access *and* refresh token. Kizen refreshes via a scheduled cron job (confirmed via a `"refreshed_via": "cron"` business event log), and the evidence strongly suggests that cron job doesn't persist the newly-rotated refresh token after a successful refresh, so the *next* refresh cycle fails using an already-invalidated token. Filed as a bug with the platform team. If a 503 `token_expired` shows up during development, just reconnect — don't re-diagnose it as a new issue.

- **Reserved automation-step field names, undocumented anywhere accessible:** `status` and `tags` are rejected at publish time with `"API Name is reserved"` — not caught by any client-side validation. Worked around by prefixing to `ticket_status`/`ticket_tags` throughout. There may be other reserved words not yet discovered; if a future field name causes an inexplicable publish failure, check for this class of error first.

- **`data_type: "number"` is unsafe for any optional input, full stop.** Leaving an optional `number`-typed field blank crashes the platform's own input-construction step (`KizenConversionError: Failed to convert value '' to float`) *before* your script ever runs — confirmed this happens regardless of whether a `default` is set (the `default`+`required: true` pairing used elsewhere on this platform does prevent it, but `default`+`required: false` does not). Every numeric-ish optional input in this plugin (`limit` on Search Tickets and Get Ticket Comments, `assignee_id`, `group_id`, `organization_id`) uses `data_type: "string"` and parses the int in script.py instead.

- **The Zendesk logo cannot be used without a license.** Zendesk's brand guidelines explicitly require third-party logo use to go through Zendesk Legal (`IP@ZENDESK.COM`). `src/thumbnail.png` is a generic, non-Zendesk-branded placeholder (a neutral slate-blue rounded square with a message-bubble glyph) — deliberate, not a TODO. Don't substitute the real Zendesk logo without confirming Kizen has a license.

- **Local dev toolkit changes are not live for `services[]` resolution until pushed and redeployed.** The Code Steps runner's Remote mode sends your *current local* script.py to Kizen's real backend, so script logic changes are genuinely live without a push — but `services[]` fields (`base_service_url`, `authorize_url`, etc.) are resolved by the already-deployed backend proxy, which only knows about the last *pushed* state of `kizen.json`. Testing a `services[]` change without pushing first will silently test the old value, not the new one — this cost real time twice during this plugin's development.

- **Local dev's `Local` mode never provides a `kizen` global at all** — only `inputs`/`secrets`/`outputs`. It's useful for validating pure Python logic (input parsing, enum checks, payload building) but cannot exercise the real Zendesk API call. Use **`Remote` mode** in the Code Steps sandbox to actually test a real round-trip — it sends the script to Kizen's real `/coderunner/run` backend, which does inject `kizen.api`. Note this is not a dry run: clicking Run in Remote mode performs the real side effect.

---

## How to Extend

### Add a new agentic workflow step

1. Create a new folder under `src/automationSteps/`.
2. Add `config.json` (`name`, `plugin_description`, `action_description`, `action_type`, `runtime: "python-3-13"`, `script`, `inputs[]`, `outputs[]`). Avoid the field names `status`/`tags`; use `data_type: "string"` for any optional numeric-ish field.
3. Add `script.py`. Copy `is_upstream_error`, `raise_zendesk_error`, and `zendesk_request_with_retry` from any existing script — don't try to import them, this platform has no shared code mechanism between plugins.
4. Validate locally: `npx @kizenapps/cli build` (packager validation), then test pure logic in the dev toolkit's Code Steps sandbox in **Local** mode.
5. Push, wait for the plugin-wizard bot's redeploy confirmation on the PR, then test the real round-trip in **Remote** mode.

### Add a new OAuth scope

Add it to `auth_credentials.scopes` in `kizen.json`, push, then **reconnect/reauthorize** — an already-connected token doesn't retroactively gain a new scope.

---

## File Structure

```text
plugin-zendesk/
├── kizen.json                                   # Manifest, OAuth config, scopes
├── CLAUDE.md                                     # This file
├── README.md
├── src/
│   ├── thumbnail.png                             # Generic placeholder — see Known Constraints
│   ├── setupAssistant/
│   │   └── assistant.json                        # zendesk_subdomain field — currently unused, kept intentionally
│   └── automationSteps/
│       ├── create_ticket/
│       ├── update_ticket/
│       ├── add_ticket_comment/
│       ├── get_ticket/
│       ├── get_ticket_comments/
│       ├── search_tickets/
│       └── find_or_create_user/
│           └── {config.json, script.py}          # Same shape in every folder
```
