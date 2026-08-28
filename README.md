# Zendesk Plugin for Kizen

Create, update, and search Zendesk support tickets, post replies and internal notes, and look up or create Zendesk end users, directly from Kizen agentic workflows.

## Overview

This plugin connects a Zendesk account to Kizen and exposes seven actions as Agentic Workflow Code Steps. All actions run as Python 3.13 Code Steps and communicate with Zendesk through Kizen's external-integrations proxy.

## Actions

| Action | Description |
| --- | --- |
| **Create Ticket** | Opens a new ticket with a title and description; optionally sets requester, priority, type, status, tags, assignee, group, external ID, and custom fields. |
| **Update Ticket** | Changes status, priority, type, assignee, group, tags, or custom fields on an existing ticket. Only the fields set are changed. |
| **Add Ticket Comment** | Posts a public reply or an internal note on an existing ticket, optionally changing status at the same time. |
| **Get Ticket** | Fetches a ticket's current state, including the requester's email. |
| **Get Ticket Comments** | Fetches a ticket's full conversation thread, pre-flattened into one formatted transcript, plus the most recent comment on its own. |
| **Search Tickets** | Finds tickets by requester, status, priority, type, tags, organization, external ID, or date; or a raw Zendesk search query for full control. |
| **Find or Create User** | Looks up a Zendesk end user by Zendesk User ID, External ID, email, or name (in that priority order), optionally creating one if no match exists. |

## Authentication

This plugin uses a **business-level** OAuth 2.0 connection — one shared Zendesk account connects on behalf of the whole business, rather than each user connecting individually. Every action runs as a Kizen Code Step, which always executes as a fixed service account and cannot complete a per-user OAuth consent flow.

**Unlike Kizen's other OAuth integrations, Zendesk has no Kizen-owned shared app.** Each customer must create their own OAuth client inside their own Zendesk subdomain and connect it to this plugin.

### Setup

1. **Create a Zendesk OAuth client.** In your Zendesk account: **Admin Center → Apps and integrations → APIs → Zendesk API → OAuth Clients → Add OAuth client.**
   - Grant type: **Authorization Code** (not Client Credentials — that grant type doesn't issue refresh tokens)
   - Client kind: **Confidential**
   - Register Kizen's OAuth redirect URI
   - Note the Client ID and Client Secret
2. **Grant the required scopes** on the OAuth client (see Scopes below).
3. **Connect in Kizen.** Navigate to **App Marketplace → Zendesk**, install the plugin, and complete the OAuth "Connect" flow, authorizing the connection for your business.

### Scopes

| Scope | Purpose |
| --- | --- |
| `read` | Required specifically by Search Tickets (`/api/v2/search.json`) — the granular scopes below aren't accepted as a substitute there. |
| `tickets:read` | Get Ticket, Get Ticket Comments, and other ticket-read paths. |
| `tickets:write` | Create Ticket, Update Ticket, Add Ticket Comment. |
| `users:read` | Find or Create User (lookup), Get Ticket's requester-email resolution. |
| `users:write` | Find or Create User (creation branch). |

Zendesk's scopes mix coarse (`read`/`write`) and resource-scoped (`tickets:read`, etc.) forms that aren't interchangeable per-endpoint — if an endpoint 403s despite having what looks like the right scope, it may want the coarse form instead. Adding a new scope requires reconnecting the OAuth connection afterward; an already-connected token doesn't retroactively gain it.

### Token refresh

Zendesk's refresh tokens are single-use and rotate on every refresh. If a step fails with a `token_expired` error, reconnect the plugin and retry.

## Action Reference

### Create Ticket

**Inputs:** `subject` (Ticket Title)\*, `comment_body` (Description)\*, `requester_email`, `requester_name`, `priority`, `type` (Ticket Type), `ticket_status`, `ticket_tags` (comma-separated), `assignee_id`, `group_id`, `external_id`, `custom_fields` (JSON array string).

**Outputs:** `ticket_id`, `ticket_url`, `ticket_status`, `created_at`.

- `requester_email` for a brand-new requester requires `requester_name` — Zendesk rejects user creation without one.
- `ticket_status = "pending"` requires `assignee_id`.
- `ticket_status = "new"` isn't guaranteed to stick — Zendesk may report back `open` instead.
- `ticket_status = "hold"` may fail on accounts with Zendesk's Custom Ticket Statuses feature enabled.
- Omitting `ticket_status` defaults to `open`, not `new`.

### Update Ticket

**Inputs:** `ticket_id`\*, everything else optional (`ticket_status`, `priority`, `type`, `assignee_id`, `group_id`, `add_tags`, `remove_tags`, `custom_fields`). Raises `no_fields_to_update` if nothing besides `ticket_id` is set.

**Outputs:** `ticket_id`, `ticket_status`, `updated_at`.

`add_tags` and `remove_tags` are independent comma-separated lists — set either or both in the same call to add some tags and remove others at once. Both are applied via Zendesk's dedicated tags endpoints (`PUT`/`DELETE /api/v2/tickets/{id}/tags.json`), not the main ticket-update body — Zendesk doesn't accept tag changes there. Tags not listed in either field are always left untouched. Zendesk does not allow adding or removing tags on a closed ticket.

### Add Ticket Comment

**Inputs:** `ticket_id`\*, `body`\*, `is_public`\* (boolean), `ticket_status` (optional).

**Outputs:** `comment_id`, `created_at`, `is_public`.

Zendesk's ticket-update response doesn't echo back the comment that was just added — only the ticket. This action makes a second call, `GET /tickets/{id}/comments.json?sort_order=desc&per_page=1`, and takes the first row as the new comment.

### Get Ticket

**Inputs:** `ticket_id`\* only.

**Outputs:** `subject` (Ticket Title), `description`, `ticket_status`, `priority`, `type` (Ticket Type), `ticket_tags`, `requester_email`, `requester_id`, `assignee_id`, `group_id`, `organization_id`, `external_id`, `created_at`, `updated_at`, `ticket_url`.

The ticket object only carries `requester_id`, not email — a second call, `GET /users/{requester_id}.json`, resolves it. If that lookup fails, `requester_email` is left blank rather than failing the whole action.

### Get Ticket Comments

**Inputs:** `ticket_id`\*, `public_only` (boolean), `limit` (string, default `100`).

**Outputs:** `thread_text` (formatted transcript, oldest to newest), `comment_count`, `last_comment_body`, `last_comment_is_public`, `last_comment_author_id`.

Paginates for real, up to a 1000-comment safety cap, fetching newest-first so `limit` means "N most recent," then reversing that slice into chronological order. Uses Zendesk's `include=users` side-loading to resolve commenter names in one pass.

### Search Tickets

**Inputs:** all optional — `requester_email`, `ticket_status`, `priority`, `type` (Ticket Type), `ticket_tags`, `organization_id`, `created_after`, `updated_after`, `external_id`, `raw_query`, `limit` (string, default `100`).

**Outputs:** `tickets` (JSON array string, each with `id`/`subject`/`status`/`priority`/`requester_id`/`updated_at`/`ticket_url`), `count`.

Always prefixed `type:ticket`. `ticket_tags` matches tickets having **any** of the listed tags — Zendesk treats repeated `tags:` clauses as OR, not AND, unlike combining different filter types (which does AND together). `raw_query`, when set, replaces every structured filter above rather than merging both. `created_after`/`updated_after` only use the date portion — Zendesk's search date filters don't support time-of-day granularity. Paginates up to Zendesk's hard 1,000-result cap (100/page × 10 pages).

### Find or Create User

**Inputs:** `zendesk_user_id`, `external_id`, `user_email`, `user_name`, `phone`, `organization_id`, `create_if_missing` (boolean, default `false`).

**Outputs:** `user_id`, `user_name`, `user_email`, `organization_id`, `role`, `was_created`.

At least one of `zendesk_user_id`, `external_id`, `user_email`, or `user_name` must be set. Search precedence when more than one is provided: **Zendesk User ID → External ID → Email → Name** — exactly one is used per call, never combined into a single query. Zendesk User ID is a direct record fetch by primary key (a nonexistent ID is treated as "no match," not an error). External ID uses Zendesk's dedicated, guaranteed-unique lookup. Email is an exact match. Name matching is fuzzy, not exact — a partial name can match, so prefer Zendesk User ID or External ID whenever available. If nothing matches and `create_if_missing` is `true`, a user is created (requires `user_name`); if `false` (the default), every output comes back blank/`false` rather than raising.

## Known Limitations

- **Multi-tenant OAuth client identity is not yet solved.** Per-business host routing and OAuth-URL redirection work correctly (every action routes to the connected business's own Zendesk subdomain), but `client_id`/`client_secret` are currently static — a single OAuth client, shared across every business that connects this plugin. A genuinely different Zendesk account would reject that client outright, since Zendesk OAuth clients are registered per-account. **The confirmed path forward is Zendesk's [Global OAuth Client](https://developer.zendesk.com/documentation/marketplace/building-a-marketplace-app/set-up-a-global-oauth-client/) program**, which lets a single registered client authenticate across many Zendesk accounts instead of being locked to one. It requires a sponsored developer account (subdomain prefixed `d3v-`) and a request through the Zendesk Marketplace portal.
- **OAuth refresh tokens can expire faster than expected.** Zendesk's refresh tokens are single-use and rotate on every refresh; if the rotated token isn't persisted correctly by the refresh cycle, the connection can die and need reconnecting more often than the token's nominal lifetime would suggest.
- **Rate limits** are plan-dependent (roughly 200–2,500 requests/min account-wide); Update Ticket is additionally capped at 100/min account-wide and 30 per 10 minutes per user per ticket. `/api/v2/search` caps at 1,000 results. Search Tickets and Get Ticket Comments respect these via pagination; a single-retry-on-429 helper respects `Retry-After` up to 20 seconds.
- **No attachment support, no side conversations, no bulk/batch operations, no change-notification trigger.**
- Zendesk's brand guidelines require third-party logo use to go through Zendesk Legal — this plugin ships a generic, non-branded placeholder icon.

## Architecture Notes

**Proxy envelope.** `kizen.api.{get,post,put,patch,delete}` wraps a successful upstream call as `{"status_code", "response_headers", "body"}`. The proxy's own HTTP status doesn't reliably reflect whether the upstream Zendesk call succeeded — a connection failure can come back as the proxy's own `200 OK` with a failure embedded in the body. Every script uses a shared `is_upstream_error(resp)` helper checking both `resp.ok` and the embedded `status_code`.

**Per-business host routing.** Every action reads a `zendesk_subdomain` Integration Secret and builds a `full_domain` query parameter on every real Zendesk call, so each business's actions route to that business's own account. The same secret name also templates `authorize_url`/`token_url` in `kizen.json` via `{{secret.zendesk_subdomain}}`, so the OAuth connect flow itself authenticates against the business's own account — but this is a genuinely separate value registration from the Integration Secret, despite the identical name; setting one does not set the other.

**Reserved field names.** `status`, `tags`, `email`, and `name` are rejected as automation-step input/output names at publish time — not caught by client-side validation. This plugin works around it by prefixing (`ticket_status`, `ticket_tags`, `user_email`, `user_name`).

**Optional numeric and boolean inputs need care.** A `data_type: "number"` input crashes the platform's input-construction step if left blank, regardless of `required`/`default` — every numeric-ish input here uses `data_type: "string"` instead. A blank optional boolean input arrives at the script as the literal value `false`, not unset/`null` — inputs are named so `false` is also the intended default, rather than relying on a `getattr(..., default)` pattern to recover an "unset" state that doesn't actually exist.

## Contributing

### Add a new action

1. Create a new folder under `src/automationSteps/`.
2. Add `config.json` with `name`, `plugin_description`, `action_description`, `action_type`, `runtime: "python-3-13"`, `script`, `"secrets": ["zendesk_subdomain"]`, `inputs[]`, `outputs[]`. Avoid the field names `status`/`tags`/`email`/`name`; use `data_type: "string"` for numeric-ish optional fields.
3. Add `script.py`. Copy the `is_upstream_error`, `raise_zendesk_error`, and `zendesk_request_with_retry` helpers from any existing action, along with the `zendesk_subdomain` secret-read and `full_domain` construction block — pass `full_domain` (plus an explicit `/api/v2` path segment) on every `kizen.api` call.
4. Validate locally with `npx @kizenapps/cli build`, then test in the dev toolkit's Code Steps sandbox.
5. Push and test the real round-trip in Remote mode before merging.

### Add a new OAuth scope

Add it to `auth_credentials.scopes` in `kizen.json`, deploy, then reconnect — an already-connected token doesn't retroactively gain a new scope.

## File Structure

```text
plugin-zendesk/
├── kizen.json                    # Manifest, OAuth config, scopes
├── README.md                     # This file
├── src/
│   ├── thumbnail.png             # Generic placeholder icon
│   └── automationSteps/
│       ├── create_ticket/
│       ├── update_ticket/
│       ├── add_ticket_comment/
│       ├── get_ticket/
│       ├── get_ticket_comments/
│       ├── search_tickets/
│       └── find_or_create_user/
│           └── {config.json, script.py}
```
