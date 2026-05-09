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

Do not use the deployed `homelab-functions` server as a generic Home Assistant
proxy. Add named server endpoints only for stable reusable actions.

## Service API

- `GET /health`
- `POST /v1/notify/joe`

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
    {"title": "Open plants", "uri": "/lovelace/plants"}
  ]
}
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
