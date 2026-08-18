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

### OAuth 2.0 — business-level, per-business host routing solved; client identity still shared

- One Zendesk OAuth connection per Kizen business, stored by the Kizen proxy under service name `zendesk_api`.
- **Unlike every other Kizen OAuth plugin (Slack, Google Drive, etc.), Zendesk does not have one Kizen-owned shared OAuth app.** Each Zendesk customer creates their *own* OAuth client inside their *own* Zendesk subdomain — a structural mismatch with how `kizen.json`'s `services[]` block works everywhere else on this platform. See **Multi-tenant design** below for what this plugin now does about it, and **Known Constraints** for what's still unsolved.
- Auth is handled transparently by the proxy when it works; scripts never touch tokens directly.

**Multi-tenant design:** every action reads a `zendesk_subdomain` secret and builds a `full_domain` query param on every real Zendesk call (see `get_ticket/script.py` for the reference implementation, replicated identically across all 7 actions) — this is what actually routes each call to the right account. `base_service_url` is a generic placeholder (`"https://zendesk.com/api/v2"`) never actually exercised; `additional_service_urls` was removed entirely (confirmed unnecessary once `sub_domain_regex_validation` is correctly set). Separately, `services[].auth_credentials.authorize_url`/`token_url` are templated with `{{secret.zendesk_subdomain}}` so the OAuth connect flow itself redirects to and authenticates against the business's own account. **These are two genuinely separate secret registrations under the identical name** — the value that makes `{{secret.<key>}}` manifest templating work is entered somewhere different than the value automation-step `secrets: [...]` (Integration Secrets) read from; see Known Constraints.

**Still open: `client_id`/`client_secret` are static, shared across every business.** This plugin currently only actually works end-to-end for the one Zendesk account whose OAuth client (`kizen_integration_dev`, registered in `kizen-79102.zendesk.com`) these credentials belong to — a genuinely different business's Zendesk account would reject this `client_id` outright (Zendesk OAuth clients are registered per-account), even though `authorize_url`/`token_url` now correctly point at that business's own subdomain. The same `{{secret.<key>}}` pattern that solved `authorize_url`/`token_url` should extend to `client_id`/`client_secret`, but this is untested — don't consider multi-tenancy complete until it is.

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

**Inputs:** `user_email`\*, `user_name`, `phone`, `external_id`, `organization_id`, `create_if_missing` (boolean, default `false`).

**Outputs:** `user_id`, `user_name`, `user_email`, `organization_id`, `role`, `was_created`.

Searches `GET /users/search.json?query=email:{email}` (exact field-scoped match, not fuzzy) and takes the first result. If nothing matches and `create_if_missing` is `true`, creates via `POST /users.json` — proactively validates `name` is set first (same lesson as Create Ticket's requester creation: Zendesk requires a name for any new user). If nothing matches and `create_if_missing` is `false` (the default), every output comes back blank/`false` rather than raising — "not found" is a valid, expected result here, not an error.

Deliberately does **not** use Zendesk's single-call `users/create_or_update.json` convenience endpoint, despite it existing — that endpoint always creates-or-updates unconditionally, which would violate `create_if_missing`'s default-`false` semantics (never create unless explicitly asked).

---

## Rate Limits

Per Zendesk's documented, plan-dependent limits: account-wide throughput ranges from 200/min (Team) to 2,500/min (Enterprise Plus); Update Ticket specifically is capped at 100/min account-wide and 30 updates per 10 min per user per ticket. `/api/v2/search` returns at most 1,000 results (100/page, 10 pages) and 422s beyond that — Search Tickets and Get Ticket Comments both respect this via their pagination caps. Every script includes a single-retry-on-429 helper (`zendesk_request_with_retry`) that respects `Retry-After` up to 20 seconds before giving up, matching the automation step's execution time budget.

---

## Known Constraints

- **Multi-tenant / per-business connections — host routing and OAuth-URL redirection are solved; client identity is not.** The path here went through several disproven mechanisms before landing on what actually works:
  - `{{secret.<key>}}` templating in `authorize_url`/`token_url` (what the original spike ticket assumed) — **confirmed real and working**, but only once the secret's value is registered as a real Integration Secret; the syntax alone isn't enough (see below).
  - `{{fieldKey}}` templating (a `setup_assistant` field referenced directly in `base_service_url`) — **tested and disproven.** A controlled 3-way comparison (wrong value / correct value / reverted to literal) showed the templated version failing identically regardless of the Configuration value — the substitution never happened. The `setup_assistant` surface this used has since been removed entirely from this plugin (nothing reads it).
  - `additional_service_urls`/`full_domain`/`sub_domain_regex_validation` — confirmed real for per-call host routing (a script can override the destination host via a `full_domain` query param, validated by `sub_domain_regex_validation` against the bare subdomain *label*, not the full host string). Now wired into all 7 actions.
  - `base_service_url` itself **cannot be made dynamic on its own** — neither a literal `*` wildcard nor a bare root domain (with or without scheme) produces a working default host; each either gets rejected outright, crashes on a missing URL scheme, or (once syntactically valid) hits Zendesk's real marketing site and 301-redirects, since a bare root domain isn't an account-specific endpoint. It's now just a generic, never-exercised placeholder — every action overrides it via `full_domain`.
  - **Two genuinely separate secret stores share the same secret name — this cost real debugging time.** `zendesk_subdomain` is registered TWICE: once as an **Integration Secret** (via each step's flat `config.json` `"secrets": [...]` array, backed by `kizen.json`'s `base_config.secrets`, read from Python's injected `secrets` dict) for `full_domain` construction, and once via whatever surface feeds `{{secret.<key>}}` manifest templating for `authorize_url`/`token_url`. Setting a value for one does NOT set it for the other, despite the identical name and both nominally being declared under `base_config.secrets`. The declaration *shape* (flat array vs. nested `base_config`) was a red herring — the actual fix was registering an Integration Secret value specifically, which the flat-array declaration (matching `plugin-mysql`/`plugin-kitchen-sink`'s convention) has always used correctly. If a future secret hits `"Secret '<name>' not found or invalid"` at pre-execution (before script.py even runs) despite `{{secret.<key>}}` templating already working for the same name elsewhere, check for this exact two-store gap first.
  - When reading an Integration Secret in script.py, don't assume the injected key is `<plain_api_name>__<secret_name>` — match by suffix instead (`next((k for k in secrets if k.endswith("zendesk_subdomain")), None)`), the same defensive approach `plugin-mysql` uses. The prefix isn't reliably the plain `kizen.json` `api_name` (this plugin's own proxy paths are preview-qualified while a PR is open, and secrets injection may follow a similar but not identical pattern).
  - **Still open: per-business OAuth client identity (`client_id`/`client_secret`).** Both are still static, shared across every business — this plugin only actually works end-to-end for the one Zendesk account (`kizen-79102.zendesk.com`) whose OAuth client these credentials belong to. A genuinely different business's account would reject this `client_id` outright. Extending the same `{{secret.<key>}}` pattern to `client_id`/`client_secret` is the natural next step but is untested — no existing Kizen plugin combines an interactive OAuth consent flow with per-business dynamic client credentials (Slack/Google Drive/etc. all use one Kizen-owned shared app; `plugin-mysql`/`plugin-postgres`/`plugin-snowflake` have genuine per-installation credentials but no OAuth/`services[]` at all).

- **OAuth refresh tokens die roughly every 60–90 minutes, and this is a platform bug, not something fixable from this repo.** Zendesk's refresh tokens are single-use/rotating — Zendesk's own docs confirm a successful refresh deletes both the previous access *and* refresh token. Kizen refreshes via a scheduled cron job (confirmed via a `"refreshed_via": "cron"` business event log), and the evidence strongly suggests that cron job doesn't persist the newly-rotated refresh token after a successful refresh, so the *next* refresh cycle fails using an already-invalidated token. Filed as a bug with the platform team. If a 503 `token_expired` shows up during development, just reconnect — don't re-diagnose it as a new issue.

- **Reserved automation-step field names, undocumented anywhere accessible:** `status`, `tags`, `email`, and `name` are all rejected at publish time with `"API Name is reserved"` — not caught by any client-side validation, and only surfaced on a real deploy (the plugin-wizard bot's staging publish step), not on `npx @kizenapps/cli build`. Worked around by prefixing: `ticket_status`/`ticket_tags` throughout, and `user_email`/`user_name` in Find or Create User. There may be other reserved words not yet discovered; if a future field name causes an inexplicable publish failure, check for this class of error first — the error response's `automation_action_configs` array is positional (alphabetical by automation step folder name) with per-input/per-output error objects, so match array indices back to that step's `inputs`/`outputs` order in `config.json` to find the offending field.

- **`data_type: "number"` is unsafe for any optional input, full stop.** Leaving an optional `number`-typed field blank crashes the platform's own input-construction step (`KizenConversionError: Failed to convert value '' to float`) *before* your script ever runs — confirmed this happens regardless of whether a `default` is set (the `default`+`required: true` pairing used elsewhere on this platform does prevent it, but `default`+`required: false` does not). Every numeric-ish optional input in this plugin (`limit` on Search Tickets and Get Ticket Comments, `assignee_id`, `group_id`, `organization_id`) uses `data_type: "string"` and parses the int in script.py instead.

- **The Zendesk logo cannot be used without a license.** Zendesk's brand guidelines explicitly require third-party logo use to go through Zendesk Legal (`IP@ZENDESK.COM`). `src/thumbnail.png` is a generic, non-Zendesk-branded placeholder (a neutral slate-blue rounded square with a message-bubble glyph) — deliberate, not a TODO. Don't substitute the real Zendesk logo without confirming Kizen has a license.

- **Local dev toolkit changes are not live for `services[]` resolution until pushed and redeployed.** The Code Steps runner's Remote mode sends your *current local* script.py to Kizen's real backend, so script logic changes are genuinely live without a push — but `services[]` fields (`base_service_url`, `authorize_url`, etc.) are resolved by the already-deployed backend proxy, which only knows about the last *pushed* state of `kizen.json`. Testing a `services[]` change without pushing first will silently test the old value, not the new one — this cost real time twice during this plugin's development.

- **Local dev's `Local` mode never provides a `kizen` global at all** — only `inputs`/`secrets`/`outputs`. It's useful for validating pure Python logic (input parsing, enum checks, payload building) but cannot exercise the real Zendesk API call. Use **`Remote` mode** in the Code Steps sandbox to actually test a real round-trip — it sends the script to Kizen's real `/coderunner/run` backend, which does inject `kizen.api`. Note this is not a dry run: clicking Run in Remote mode performs the real side effect.

---

## How to Extend

### Add a new agentic workflow step

1. Create a new folder under `src/automationSteps/`.
2. Add `config.json` (`name`, `plugin_description`, `action_description`, `action_type`, `runtime: "python-3-13"`, `script`, `"secrets": ["zendesk_subdomain"]`, `inputs[]`, `outputs[]`). Avoid the field names `status`/`tags`/`email`/`name`; use `data_type: "string"` for any optional numeric-ish field.
3. Add `script.py`. Copy `is_upstream_error`, `raise_zendesk_error`, and `zendesk_request_with_retry` from any existing script — don't try to import them, this platform has no shared code mechanism between plugins. Also copy the `zendesk_subdomain` secret-read + `full_domain` construction block from `get_ticket/script.py`, and pass `full_domain` (plus an explicit `/api/v2` path segment) on every `kizen.api` call — see Multi-tenant design above.
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
