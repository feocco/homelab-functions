# homelab-functions

`homelab-functions` is a small local broker for common homelab actions.

V1 exposes one function:

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
