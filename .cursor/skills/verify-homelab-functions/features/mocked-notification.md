# Mocked notification

`POST /v1/notify/joe` sends Joe a mobile notification through Home
Assistant. In verification, Home Assistant is a loopback WebSocket mock, so
the proof is the captured `call_service` rather than a phone buzz.

## Sub-features

- `notify-auth` requires the ephemeral broker bearer.
- `notify-send` returns JSON with `"status": "sent"` and a
  `notification_id`.
- `notify-ha-service` makes the mock observe `notify.mobile_app_verify_joe`.
- `notify-payload` makes the mock observe title `verify-http`.

## How to get to it (user POV)

- POST `http://127.0.0.1:<port>/v1/notify/joe` with JSON `title` and
  `message` and the broker bearer.
- Call `homelab.notify_joe(...)` against `HOMELAB_FUNCTIONS_URL` for this
  run.
- Run `./.venv/bin/python .cursor/skills/verify-homelab-functions/verify.py`
  and read `check notify-joe`.

## Driving it with verify.py

Preconditions:

- `verify.py` printed `doctor health status=ok` and `catalog=verify-stub`.
- The HA mock is at `http://127.0.0.1:<ha-port>/api/websocket`.
- `HA_NOTIFY_JOE_SERVICE` for this run is `notify.mobile_app_verify_joe`.
- The action recorder is disabled.

- **Send notification.** POST `/v1/notify/joe` with
  `{"title": "verify-http", "message": "isolated notify check"}` and the
  ephemeral broker bearer. The helper does this in `check notify-joe`.
- **Read HTTP result.** Status `200`. JSON `"status"` is `sent`.
  `notification_id` is present.
- **Read HA mock.** The mock captured `call_service` with domain `notify`,
  service `mobile_app_verify_joe`, and `service_data.title` `verify-http`.
- **Proof.** Stdout contains `check notify-joe PASS` and
  `ha_domain=notify ha_service=mobile_app_verify_joe title=verify-http`.

## Gotchas

- A 200 with `"status": "sent"` is incomplete without the mock
  `call_service`. The phone path is replaced by that capture.
- If the mock is not loopback, the helper must refuse to start. Do not fall
  back to a real `HA_URL`.
- Re-enable the action recorder only in production. This run must keep it
  off so the broker does not open a second HA socket.
- `notify_jess` is a different AccessVector route. This feature proves
  `notify_joe` only.
