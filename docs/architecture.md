# homelab-functions architecture

This document explains how `homelab-functions` is put together and why. For
task-oriented steps, see the [README](../README.md). For the full operation
reference, open `GET /docs` on a running server.

## Named actions, not a proxy

`homelab-functions` is a small broker for stable homelab actions. It exposes a
fixed list of named HTTP actions, such as `notify_joe`, and nothing else.
Callers that need Home Assistant state, events, or arbitrary service calls use
`homelab.HomeAssistantWebSocketClient` in their own process.

That split keeps the deployed server small and keeps the bearer token from
becoming a master key for Home Assistant. A new action belongs in the broker
only when several callers need the same stable behavior.

## Access registry

Every protected action is declared once, in `homelab.access.ACCESS_VECTOR`.
Each entry records an operation id, HTTP method, `/v1` path, domain, summary,
description, typed parameters, and documented error statuses. A parameter has
a body, query, or path location, a JSON schema, and required and default
metadata. Operations that share a request body name a component, such as
`NotificationRequest`.

These surfaces are generated from that registry:

- the aiohttp route behind the bearer check
- the OpenAPI operation at `/openapi.json`, tagged with its domain
- the `HomelabFunctionsClient` method and the module-level helper
- the MCP tool served by `python3 -m homelab.mcp_server`
- the operation card on `GET /docs`, with HTTP, Python, and MCP examples

The plugin skill in `skills/homelab-functions/SKILL.md` names every action.
`tests/test_access_vector_parity.py` checks all of these surfaces against an
independent copy of the contract. It also runs each documented example through
the real client to prove that the three call shapes send the same request.

`/health`, `/docs`, and `/openapi.json` stay public. They are listed in
`homelab.access.PUBLIC_ENDPOINTS`, not in the registry. The Waiting on me
browser routes are listed in `homelab.access.DASHBOARD_ROUTES`. The parity
tests check that every served route belongs to exactly one of these lists.

## Callers

`homelab.consumers.CONSUMERS` records who calls which action and which ledger
keys each caller owns. The same data renders the matrix on `GET /docs`. The
parity tests check that this table matches it exactly.

| Caller | Actions | Group | Tags | Action prefixes |
| --- | --- | --- | --- | --- |
| `@Rosie` (Grok Bot) | `notify_joe`, `notify_jess` (only after Joe confirmation), `list_notifications` (short-lived reply checks) | `rosie` | `rosie-date-night-*`, `rosie-reminder-*` | `ROSIE_` |
| `homelab-sre-agent` | `notify_joe`, `record_notification_action`, `list_notifications` | `homelab-sre` | `sre-incident-<id>` | `SRE_APPROVE`, `SRE_IGNORE` |
| Household/runtime workflows: `cat-food-monitor`, `laundry-monitor`, `plant-monitor`, `bedtime`, `mealie-planner` | `notify_joe`, `notify_jess`, `record_workflow_report`, `workflow_report_button("<workflow-slug>")` | — | — | `WORKFLOW_REPORT` |
| `smoke-signal` & runtime checkers | `get_service_catalog`, `get_smoke_signal_targets` | — | — | — |

Groups, tags, and action prefixes are conventions for the shared notification
ledger. The server does not enforce them. They matter because the ledger is
shared:

- `list_notifications` filters by `group` and `tag`. A caller that reuses
  another caller's group reads the wrong replies.
- The action recorder matches a button press to the latest notification with
  the same tag. Tags must be unique per conversation, such as one
  `sre-incident-<id>` per incident.
- `NotificationActionRouter` dispatches on the prefix before `::`. Two callers
  with the same prefix would handle each other's buttons.

`workflow_report_button` is a local helper. It builds a Report button with the
`WORKFLOW_REPORT` prefix and does not call the broker.

## Notification flow

1. A caller sends `notify_joe` or `notify_jess` with a title, message, and
   optional tag, group, URL, and up to three buttons.
2. The server validates the payload, calls the configured Home Assistant
   notify service over WebSocket, and records the notification in the shared
   SQLite ledger.
3. Android Companion notifications go out with `ttl: 0` and
   `priority: high` so Doze does not delay them.
4. A background listener records every `mobile_app_notification_action` event
   against the latest notification with a matching tag. It does not run
   action-specific behavior.
5. Callers read replies with `list_notifications`, or handle the event in
   their own listener and store it with `record_notification_action`.

## Workflow report flow

Workflow reports are human-submitted "please investigate" records.

1. A workflow adds `homelab.workflow_report_button("<workflow-slug>")` to a
   notification.
2. Joe taps Report and types what went wrong.
3. The workflow's own listener receives the
   `mobile_app_notification_action` event.
4. The workflow calls `record_workflow_report` with the slug, the text, and
   optional source, notification id, and raw event.
5. The server stores the report with status `reported`.

Reports are records only. The server does not start Codex, Cursor, GitHub
issues, or webhooks from a report. Only `send_waiting_feedback` starts Cursor
work. A future investigator service can read reports with
`list_workflow_reports` or `get_workflow_report` and own repository selection,
sessions, logs, and pull requests.

## Catalog flow

`homelab-config` generates the service catalog and the Smoke Signal target
list during deploy and mounts them into the container. `get_service_catalog`
and `get_smoke_signal_targets` check the bearer token and return the files
unchanged. Runtime checkers get a cacheable view of the homelab without
cloning `homelab-config`.

## Security model

- Every `/v1` action requires `Authorization: Bearer $HOMELAB_FUNCTIONS_TOKEN`,
  compared in constant time. Missing or wrong tokens get `401`.
- The token lives in the server and caller environments. The client, MCP
  server, plugin manifest, and docs page never embed it. MCP error output
  redacts it.
- `GET /docs` sends a Content-Security-Policy with `connect-src 'none'` and
  hash-pinned inline script and style. The page cannot send requests, so it
  cannot run authenticated calls or leak a pasted token.
- The broker has no generic state, event, or service-call route. Adding one
  would turn the token into Home Assistant admin access.
- The inbox page sends a Content-Security-Policy with hash-pinned script
  and style and `connect-src 'self'`. It renders agent-written text with
  `textContent`, and the embedded snapshot escapes `<`, `>`, and `&`.
- `DASHBOARD_GH_APP_KEY` and `CURSOR_API_KEY` are redacted from logs and never
  appear in responses or error messages.

## Pebble ring relay

`POST /v1/pebble` accepts the Pebble Index webhook and forwards JSON to the
configured Grok webhook. The ring posts `multipart/form-data` in every Hold
and Talk mode. When a recording has no transcription, the server transcribes
it with xAI and still forwards only JSON. The audio file stays on disk until
`PEBBLE_AUDIO_TTL_SECONDS` (default 24 hours). The event id is stored in
SQLite and still dedupes after the audio file is gone, including across a
restart. Only allowlisted Pebble fields leave the host, timestamps must be
within five minutes of the server clock, and duplicate detection happens
before an attached audio file is persisted. The 9 MiB request-body limit is
scoped to this route; other routes keep aiohttp's default 1 MiB limit.

The route is declared in `HTTP_ONLY_OPERATIONS` in `homelab/access.py`, not in
`ACCESS_VECTOR`. The ring token is `PEBBLE_RING_TOKEN`. Putting this route in
the shared registry would authorize it with `HOMELAB_FUNCTIONS_TOKEN` and
would publish a JSON client method and an MCP tool. The client and MCP speak
JSON with that shared token, and a tool that accepted the recording would put
raw audio on the agent path. OpenAPI and `GET /docs` still document the
route. `tests/test_access_vector_parity.py` asserts it is absent from the
client and from `tools/list`.

Joe sets this URL in the Pebble app:

`https://homelab-functions.home.feocco.com/v1/pebble`

That hostname is the existing private route in homelab-config
(`services/homelab-functions/ops.yaml`). Caddy reverse-proxies it to port
8091 with no Authentik gate. On the LAN, UniFi DNS resolves the name to the
Mac mini. Off the LAN, the existing DNS-only record points at the Mac mini
tailnet address `100.81.237.11`. No DNS, Cloudflare, or Tailscale change is
required. The phone sends `Authorization: Bearer $PEBBLE_RING_TOKEN`.

The same host already serves the app over Tailscale HTTP on port 8091
(`hosts/macmini/tailscale-serve.yaml`). If the ring app cannot use the HTTPS
name, the phone can use `http://100.81.237.11:8091/v1/pebble` or
`http://maclabs-mac-mini.taildf3445.ts.net:8091/v1/pebble` while it is on the
tailnet.

homelab-config follow-up, not part of deploying this image: add
`PEBBLE_RING_TOKEN` and `GROK_PEBBLE_WEBHOOK_KEY` to the service secret list,
and set `XAI_API_KEY` when the ring will send audio without a transcription.
Leave the route, DNS, and Tailscale Serve config as they are. The container
already mounts `./data` on `/app/data`, which holds the dedupe ledger and the
short-lived audio files. `GET /docs` on the existing service is the catalog
`docs_url` for this API, so the catalog generator does not need a new path.

## Waiting on me

The inbox at `https://waiting.home.feocco.com` lists only what needs Joe.
homelab-config routes that hostname to this service on port 8091, and `/`
redirects to `/waiting` there. It has no status board: running checks,
failing checks, drafts, and agent progress are not shown.

### Items

Every item has one of seven categories, what is needed, why, one primary
action, and a source link. `InboxItem` in `homelab/waiting.py` refuses to
exist otherwise: a copy item must carry a value, only copy items copy, every
other action opens an https URL, and an approve/merge action can only open its
own pull request on GitHub.

| Category | Comes from | Primary action | Leaves when |
| --- | --- | --- | --- |
| PR approve/merge | An open, non-draft pull request with passing or no checks, no requested changes, no conflict | Review and merge on GitHub | Merged or closed on GitHub |
| Deploy approval | `**Joe: deploy**` ask line | Review the deploy | Box ticked, line removed, source closed, or Done |
| Secret or key needed | `**Joe: secret**` or `**Joe: key**` | Add the secret | Same |
| DNS or infra decision | `**Joe: infra**` or `**Joe: dns**` | Make the decision | Same |
| Question to answer | `**Joe: question**` | Answer on GitHub | Same |
| Copy/paste value or command | `**Joe: copy**` with `Copy:` | Copy (one click) | Same |
| Evidence to review | `**Joe: evidence**` | Review the evidence | Same |

Ask lines are unchecked task-list items in a pull request or issue body or
comment: `- [ ] **Joe: kind** What is needed. Why: the reason. Action: https://…`
(or `Copy: `value`` for copy asks). Open pull requests and issues count, and
so do pull requests merged in the last 14 days, because a deploy usually
waits on a merged change. Agents cannot ask for an approve/merge item; an
ask with an unknown kind, no reason, a non-https action, or a copy ask with
no value goes to the snapshot's `rejected` list with the reason, so the agent
can fix it.

An item's id is `owner/repo#number:category`, plus a hash of the need for ask
lines. It stays stable when the rest of the body changes.

### One snapshot

`homelab.waiting.WaitingBoard` builds one `waiting-on-me.v2` document each
time something changes. The page embeds it, `GET /waiting/events` streams it,
`GET /waiting/snapshot` returns it for the polling fallback, and
`get_waiting_items` returns it to agents. None of them builds its own copy, so
a browser and an agent never disagree. `version` increases with each publish.

The board refreshes every `DASHBOARD_REFRESH_SECONDS` (default 60) with one
GraphQL request. When GitHub fails, the board keeps the last good items, sets
`github.status` to `error`, and the page says how old its data is. Without the
`DASHBOARD_GH_*` settings, `github.status` is `unconfigured` and the message
names the missing variables.

The page shows **Live** while events or 15-second heartbeats arrive. If the
stream does not reconnect within 8 seconds, or goes 45 seconds without a
message, the page polls `/waiting/snapshot` every 30 seconds and shows
**Polling** until the stream returns. New items animate in; handled items
animate out.

### Human-only actions

- Approve and merge happen on GitHub, by Joe. The inbox links there and has no
  merge route, the GitHub token is read-only, and **Done** refuses
  approve/merge items with `409 merge_on_github`.
- **Done** (`POST /waiting/handled`) needs Joe's browser session and a
  same-host `Origin`. The bearer token cannot call it, so agents cannot clear
  Joe's inbox. Done is stored in SQLite and survives restarts.

### GitHub access

`homelab.github_waiting` signs a nine-minute RS256 JWT with
`DASHBOARD_GH_APP_KEY` and exchanges it for an installation token. A token with
any permission other than `read` is refused before any query, and only
GraphQL queries are sent. The inbox cannot change GitHub, even if someone
later grants the App write access.

### Feedback

Feedback on an item goes to exactly one Cursor Cloud Agent:

1. The first feedback stores a new `bc-…` agent id for the item in SQLite
   (`WAITING_FEEDBACK_PATH`), then calls `POST /v1/agents` with that id,
   `autoCreatePR: true`, and `workOnCurrentBranch: false`. The agent starts
   from the open pull request's branch, from the pull request itself when it
   comes from a fork, or else from the default branch, and opens its own pull
   request. That pull request then shows up as an approve/merge item.
2. Later feedback calls `POST /v1/agents/{id}/runs` on the same agent. When the
   agent is busy, the feedback steers the running turn instead.

| Failure | What happens |
| --- | --- |
| Two submits for one item at once | A per-item lock runs them in order. The item's primary key keeps one agent id even across processes. |
| Launch times out or Cursor returns 5xx | The item stays `launching`. Any retry re-sends the same agent id, and Cursor answers `409 agent_id_conflict` if the first launch landed. |
| Follow-up times out | The request is stored as `unconfirmed` and is not re-sent. The caller is told to check the agent. |
| Same `request_id` sent again | The stored result comes back with `duplicate: true`. Cursor is not called. |
| Agent busy and steering unavailable | `409 agent_busy`. Retrying later is safe. |
| `CURSOR_API_KEY` unset | The page shows that feedback is off, and the action returns `503`. |

Feedback never approves, merges, deploys, or clears an item. The launch body
is built from a fixed field list, the prompt tells the agent not to approve,
merge, or deploy and to treat the item text as data, and the GitHub token is
read-only.

### Browser auth

The browser signs in once with `HOMELAB_FUNCTIONS_TOKEN` and keeps a 30-day
session cookie: an expiry plus an HMAC keyed by the token. The cookie is
`HttpOnly`, `SameSite=Strict`, scoped to `/waiting`, and `Secure` when
`DASHBOARD_PUBLIC_URL` is HTTPS. Rotating the token ends every session. POSTs
must carry a same-host `Origin`. The cookie opens only `DASHBOARD_ROUTES`, and
the bearer token does not open them. That keeps `/v1` free of cookie auth.

## What changes together

Adding or changing a protected action touches all of these in one change:

1. The `AccessOperation` in `homelab/access.py`, including its domain.
2. The handler mapping in `homelab/server.py`. The server refuses to start if
   the mapping and the registry differ.
3. The `HomelabFunctionsClient` method and module helper in
   `homelab/client.py`.
4. The skill entry in `skills/homelab-functions/SKILL.md`.
5. The contract and behavior tests in `tests/test_access_vector_parity.py`.
6. A `CONSUMERS` row in `homelab/consumers.py` if a new caller uses it, plus
   this document's caller matrix.

The MCP tool, OpenAPI operation, and `/docs` card follow from the registry
with no extra code.

`POST /v1/pebble` does not follow that list. A change to it updates
`HTTP_ONLY_OPERATIONS`, the handler, OpenAPI, `GET /docs`, and the parity
test that proves the client and MCP still omit it.
