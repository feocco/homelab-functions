# homelab-functions

`homelab-functions` is a small local broker and helper package for common
homelab actions.

Use the deployed HTTP service for common stable functions, especially notifying
Joe or Jess. Use the client-side Home Assistant helper for service-specific
discovery, state reads, event listeners, and direct service calls.

## Notify Joe

```python
import homelab

homelab.notify_joe(
    "Test notification",
    "homelab-functions is working",
    tag="homelab-functions-test",
)
```

The helper calls the local `homelab-functions` HTTP service. Callers do not need
Home Assistant credentials or WebSocket code.

## Notify Jess

```python
import homelab

homelab.notify_jess(
    "Dinner plan",
    "Please review and accept the weekly plan.",
    tag="mealie-planner-plan-id",
    group="mealie-planner",
)
```

Jess notifications use the same broker and payload shape as Joe notifications,
but route to the configured `HA_NOTIFY_JESS_SERVICE`.

## Python client

`HomelabFunctionsClient` calls every protected action. The constructor takes
the service URL, bearer token, and timeout. The action list is
`homelab.access.ACCESS_VECTOR`.

```python
from homelab import HomelabFunctionsClient

client = HomelabFunctionsClient(
    "http://127.0.0.1:8091",
    token="replace_me",
    timeout=10,
)
client.notify_joe("Plant status", "Ficus needs water.")
catalog = client.get_service_catalog()
targets = client.get_smoke_signal_targets()
```

The module-level helpers, including `notify_joe`, `get_service_catalog`,
`get_smoke_signal_targets`, `list_workflow_reports`, and `get_workflow_report`,
use the same client. When you omit the URL or token, they read
`HOMELAB_FUNCTIONS_URL` and `HOMELAB_FUNCTIONS_TOKEN`.

## MCP and the Cursor plugin

This repo includes a Cursor plugin. `.cursor-plugin/plugin.json` points at
`mcp.json` and `skills/`. The plugin starts a stdio MCP server with
`python3 -m homelab.mcp_server`. The `homelab-functions-mcp` command starts
that same server.

`.cursor-plugin/plugin.json` declares `HOMELAB_FUNCTIONS_URL` with default
`http://127.0.0.1:8091` and requires `HOMELAB_FUNCTIONS_TOKEN`. `mcp.json`
passes those values as `${HOMELAB_FUNCTIONS_URL}` and
`${HOMELAB_FUNCTIONS_TOKEN}`, and sets the server working directory to
`${CURSOR_PLUGIN_ROOT}`. The server does not embed a token. Its tools are
the protected actions in `homelab.access.ACCESS_VECTOR`.

Use `homelab.HomeAssistantWebSocketClient` for Home Assistant state, events,
and service calls. Do not add those operations to the broker.

## Home Assistant Client Helper

For services that need Home Assistant state or events, connect directly to Home
Assistant with the shared helper instead of copying WebSocket boilerplate.

```python
import homelab

async with homelab.HomeAssistantWebSocketClient.from_env() as ha:
    states = await ha.get_states()
    await ha.subscribe_events("state_changed")
    await ha.call_service("switch", "turn_on", {"entity_id": "switch.example"})
```

Subscribed event handlers run outside the WebSocket reader task, so a handler can
call `get_states()` or `call_service()` on the same client without blocking the
response reader.

The helper reads:

```text
HA_URL=https://example.ui.nabu.casa
HA_LONG_LIVED_TOKEN=replace_me
HA_REQUEST_TIMEOUT_SECONDS=30
```

The public helper methods have docstrings. From an interactive Python shell:

```python
import homelab

help(homelab.HomeAssistantWebSocketClient)
```

## Actionable Notification Callbacks

For services that send Home Assistant mobile notification buttons and need to
handle the button press, use `NotificationActionRouter` with a direct
`HomeAssistantWebSocketClient` subscription.

```python
import homelab

router = homelab.NotificationActionRouter()
router.register("MY_SERVICE_APPROVE", handle_approval)

button = {
    "title": "Approve",
    "action": homelab.NotificationActionRouter.make_action("MY_SERVICE_APPROVE", token),
}

async def handle_ha_event(event: dict) -> None:
    router.handle_event(event)
```

The deployed `homelab-functions` server records
`mobile_app_notification_action` events in the shared notification ledger, but
it does not route or execute those actions. Each long-running service still owns
its own Home Assistant listener and business logic.

Buttons that request a typed response can include Home Assistant text-input
fields. The service that listens for the action reads `reply_text` from the
`mobile_app_notification_action` event.

```python
button = {
    "title": "Reply",
    "action": homelab.NotificationActionRouter.make_action("MY_SERVICE_REPLY", token),
    "behavior": "textInput",
    "textInputButtonTitle": "Send",
    "textInputPlaceholder": "Add a note",
}
```

Do not use the deployed `homelab-functions` server as a generic Home Assistant
proxy. Add named server endpoints only for stable reusable actions.

## Service API

`homelab.access.ACCESS_VECTOR` is the registry of protected `/v1` actions.
HTTP routes, OpenAPI operation ids, `HomelabFunctionsClient`, and the MCP
tools all come from that registry.

These routes stay public and are not in the registry.

- `GET /health`
- `GET /docs`
- `GET /openapi.json`

Protected endpoints require:

```text
Authorization: Bearer $HOMELAB_FUNCTIONS_TOKEN
```

The browser docs page documents protected endpoints but does not store tokens
or execute authenticated calls.

The catalog endpoints serve generated JSON files mounted into the container.
`homelab-config` owns the catalog generation; this service only authenticates
and returns the files:

```text
HOMELAB_CATALOG_PATH=/app/config/service-catalog.json
HOMELAB_SMOKE_SIGNAL_TARGETS_PATH=/app/config/smoke-signal-targets.json
```

Example request:

```json
{
  "title": "Plant status",
  "message": "Ficus needs water.",
  "tag": "plant-monitor-ficus",
  "group": "plant-monitor",
  "url": "/lovelace/plants",
  "buttons": [
    {"title": "Open plants", "uri": "/lovelace/plants"},
    {
      "title": "Reply",
      "action": "PLANT_REPLY::token",
      "behavior": "textInput",
      "textInputButtonTitle": "Send",
      "textInputPlaceholder": "Add a note"
    }
  ]
}
```

Successful notification sends are written to the shared notification ledger.
Services that listen for their own `mobile_app_notification_action` events can
record the response without turning `homelab-functions` into a generic action
router:

```python
homelab.record_notification_action(
    "PLANT_REPLY::token",
    tag="plant-monitor-ficus",
    group="plant-monitor",
    reply_text="Watered today",
    event=home_assistant_event_data,
)
```

The recent ledger can be inspected with:

```python
homelab.list_notifications(group="plant-monitor", limit=20)
```

## Workflow Reports

Workflow reports are a standard way to turn a phone notification into a durable
"please investigate this workflow" record. A workflow can add the shared Report
button to any notification:

```python
import homelab

homelab.notify_joe(
    "Cat food monitor",
    "Morning food check failed.",
    tag="cat-food-monitor-morning",
    group="cat-food-monitor",
    buttons=[homelab.workflow_report_button("cat-food-monitor")],
)
```

The Report button uses Home Assistant's mobile text-input action. The workflow
that listens for `mobile_app_notification_action` events records Joe's submitted
text:

```python
import homelab

router = homelab.NotificationActionRouter()


def handle_report(workflow_slug: str, event: dict) -> None:
    data = event.get("data", {})
    homelab.record_workflow_report(
        workflow_slug,
        data["reply_text"],
        source="mobile-action",
        event=data,
    )


router.register("WORKFLOW_REPORT", handle_report)
```

Workflow slugs are caller-owned strings. Prefer stable kebab-case names such as
`cat-food-monitor`, but `homelab-functions` does not keep a workflow registry.

Recent reports can be inspected with the client or the service API:

```python
reports = homelab.list_workflow_reports(workflow="cat-food-monitor", limit=20)
report = homelab.get_workflow_report(reports["reports"][0]["id"])
```

```bash
curl -H "Authorization: Bearer $HOMELAB_FUNCTIONS_TOKEN" \
  "$HOMELAB_FUNCTIONS_URL/v1/workflow-reports?workflow=cat-food-monitor&limit=20"
```

V1 only records reports. It does not launch Codex, Cursor, GitHub issues, or a
webhook. Future investigator services should consume these report records by id
or through a dedicated relay.

## Configuration

Server runtime environment:

```text
HA_URL=https://example.ui.nabu.casa
HA_LONG_LIVED_TOKEN=replace_me
HA_NOTIFY_JOE_SERVICE=notify.mobile_app_your_phone
HA_NOTIFY_JESS_SERVICE=notify.mobile_app_jess_phone
HOMELAB_FUNCTIONS_TOKEN=replace_me
SERVICE_HOST=0.0.0.0
SERVICE_PORT=8091
TZ=America/New_York
LOG_LEVEL=INFO
REQUEST_TIMEOUT_SECONDS=10
NOTIFICATION_LEDGER_PATH=/app/data/notifications.sqlite3
NOTIFICATION_ACTION_RECORDER_ENABLED=true
```

Client helper environment:

```text
HOMELAB_FUNCTIONS_URL=http://homelab-functions:8091
HOMELAB_FUNCTIONS_TOKEN=replace_me
```

## Local

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
pytest
SERVICE_PORT=8091 python -m homelab.server
```
