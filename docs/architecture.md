# homelab-functions Architecture

`homelab-functions` is a small broker for stable homelab actions. It exposes
named HTTP endpoints for reusable actions and keeps generic Home Assistant
state, event, and service-call work in client-side helpers.

## Access registry

Protected actions are declared once, in `homelab.access.ACCESS_VECTOR`. The
registry is an immutable `AccessVector`. Each entry records an operation id,
HTTP method, path, summary, description, named input parameters, and the
error responses OpenAPI documents. A parameter has a body, query, or path
location, required and default metadata, and a JSON schema. An operation can
name a shared request-body component, such as `NotificationRequest`.

The HTTP routes, OpenAPI operation ids, `HomelabFunctionsClient` methods, and
MCP tools are built from that registry. `HomelabFunctionsClient` is the
synchronous Python client. Its constructor takes the service URL, bearer
token, and timeout.

The stdio MCP server is `python3 -m homelab.mcp_server`. The
`homelab-functions-mcp` command starts the same server. The Cursor plugin
manifest `.cursor-plugin/plugin.json` points at `mcp.json` and `skills/`.
`mcp.json` sets `cwd` to `${CURSOR_PLUGIN_ROOT}` and passes
`HOMELAB_FUNCTIONS_URL` and `HOMELAB_FUNCTIONS_TOKEN` from the plugin
variables. `HOMELAB_FUNCTIONS_URL` defaults to `http://127.0.0.1:8091`.
`HOMELAB_FUNCTIONS_TOKEN` is required.

`/health`, `/docs`, and `/openapi.json` stay public. They are not registry
entries.

Use `homelab.HomeAssistantWebSocketClient` for Home Assistant state, events,
and service calls. Do not add those operations to the broker.

## Notification Flow

Callers send notifications with `notify_joe` or `notify_jess`. The server
validates the payload, calls the configured Home Assistant notify service, and
records the sent notification in the shared SQLite ledger.

The server also runs a background Home Assistant listener for
`mobile_app_notification_action` and records those events in the ledger. It does
not execute action-specific behavior. Services that need button callbacks still
own their own action handling, either through their direct listener fast path or
by polling the ledger for durable callback records.

## Workflow Report Flow

Workflow reports are human-submitted incident records for services such as
`cat-food-monitor`.

1. A workflow adds `homelab.workflow_report_button("workflow-slug")` to a mobile
   notification.
2. Joe taps Report and submits text through the Home Assistant mobile app.
3. The workflow's own event listener receives the
   `mobile_app_notification_action` event.
4. The workflow calls `record_workflow_report` with the workflow slug, summary
   text, and optional source, notification id, and raw event data.
5. `homelab-functions` stores the report with status `reported`.

V1 records only. It does not start Codex, Cursor, GitHub issue creation, or an
outbound webhook. This keeps the deployed service reliable and makes the report
inbox useful before the investigator automation exists.

## Catalog Flow

`homelab-functions` exposes protected catalog reads for services that need a
runtime view of the homelab without cloning `homelab-config`.

`homelab-config` generates the JSON files during deploy and mounts them into
the container. `homelab-functions` only checks the bearer token and returns the
files. `get_service_catalog` and `get_smoke_signal_targets` are the registry
operations for those reads. Their paths live in `ACCESS_VECTOR`.

This keeps service truth in `homelab-config` while giving small runtime services
like Smoke Signal a simple API and cacheable payload.

## Future Investigator Boundary

The future investigator should be a separate service or agent workflow. It can
read reports with `list_workflow_reports` or `get_workflow_report`, or from a
future relay endpoint, and then decide how to start a Codex or Cursor
investigation.

A future relay event should include:

```json
{
  "report_id": 123,
  "workflow_slug": "cat-food-monitor",
  "summary": "The morning check did not run.",
  "created_at": "2026-06-14T12:00:00+00:00",
  "source": "mobile-action",
  "notification_id": 456,
  "links": {
    "report": "/v1/workflow-reports/123"
  }
}
```

The investigator owns repository selection, branch/session creation, runtime
logs, and any deployment or pull-request workflow. `homelab-functions` owns only
the stable intake record and authentication boundary.
