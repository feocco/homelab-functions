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
