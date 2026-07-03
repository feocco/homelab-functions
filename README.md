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

- `GET /health`
- `GET /docs`
- `GET /openapi.json`
- `POST /v1/notify/joe`
- `POST /v1/notify/jess`
- `GET /v1/notifications`
- `POST /v1/notifications/actions`
- `GET /v1/catalog/services`
- `GET /v1/catalog/smoke-signal-targets`
- `POST /v1/workflow-reports`
- `GET /v1/workflow-reports`
- `GET /v1/workflow-reports/{id}`

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

Recent reports can be inspected through the service API:

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
