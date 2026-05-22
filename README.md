# homelab-functions

`homelab-functions` is a small local broker and helper package for common
homelab actions.

Use the deployed HTTP service for common stable functions, especially notifying
Joe. Use the client-side Home Assistant helper for service-specific discovery,
state reads, event listeners, and direct service calls.

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

The deployed `homelab-functions` server should not become a generic action
router. Each long-running service owns its own Home Assistant listener and
business logic.

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
- `POST /v1/notify/joe`
- `GET /v1/notifications`
- `POST /v1/notifications/actions`

`POST /v1/notify/joe` requires:

```text
Authorization: Bearer $HOMELAB_FUNCTIONS_TOKEN
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

## Configuration

Server runtime environment:

```text
HA_URL=https://example.ui.nabu.casa
HA_LONG_LIVED_TOKEN=replace_me
HA_NOTIFY_JOE_SERVICE=notify.mobile_app_your_phone
HOMELAB_FUNCTIONS_TOKEN=replace_me
SERVICE_HOST=0.0.0.0
SERVICE_PORT=8091
TZ=America/New_York
LOG_LEVEL=INFO
REQUEST_TIMEOUT_SECONDS=10
NOTIFICATION_LEDGER_PATH=/app/data/notifications.sqlite3
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
