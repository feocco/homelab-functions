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
`homelab.access.PUBLIC_ENDPOINTS`, not in the registry.

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
issues, or webhooks. A future investigator service can read reports with
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

## Pebble ring relay

`POST /v1/pebble` accepts the Pebble Index webhook and forwards JSON to one
Grok webhook chosen by press count. Both hold gestures post to this route.
`single-click-hold` goes to the personal webhook,
`GROK_PEBBLE_WEBHOOK_URL` and `GROK_PEBBLE_WEBHOOK_KEY`. That pair is the
existing `pebble-ring-intake` destination. `double-click-hold` goes to the
work webhook, `GROK_PEBBLE_WORK_WEBHOOK_URL` and
`GROK_PEBBLE_WORK_WEBHOOK_KEY`. Those work variables have no default. If
either is unset, the route returns 503 `work_webhook_not_configured`, logs
that code, and does not call the personal webhook. Any other trigger,
including `test-event`, is 400 `unknown_press_count` and is not forwarded.
The ring posts `multipart/form-data` for each hold gesture. When a recording
has no transcription, the server transcribes it with xAI and still forwards
only JSON. The audio file stays on disk until
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

homelab-config follow-up, not part of deploying this image: declare
`GROK_PEBBLE_WORK_WEBHOOK_URL` and `GROK_PEBBLE_WORK_WEBHOOK_KEY` on
`homelab-functions` `optional_env` in `service-secrets.yaml`. Do not add them
to the required `env` list. `render-service-env` does not read `optional_env`,
so also render `HOMELAB_FUNCTIONS__GROK_PEBBLE_WORK_WEBHOOK_URL` and
`HOMELAB_FUNCTIONS__GROK_PEBBLE_WORK_WEBHOOK_KEY` into container
`GROK_PEBBLE_WORK_WEBHOOK_URL` and `GROK_PEBBLE_WORK_WEBHOOK_KEY` when those
GitHub secrets are set, and skip them when they are unset. Leave the route,
DNS, and Tailscale Serve config as they are. The container
already mounts `./data` on `/app/data`, which holds the dedupe ledger and the
short-lived audio files. `GET /docs` on the existing service is the catalog
`docs_url` for this API, so the catalog generator does not need a new path.

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
test that proves the client and MCP still omit it. Press-count routing stays
on this route. It does not add a client method, an MCP tool, or a second path.
