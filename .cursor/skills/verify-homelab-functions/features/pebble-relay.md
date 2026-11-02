# Pebble relay

`POST /v1/pebble` is the HTTP-only ring intake. The phone authenticates
with `PEBBLE_RING_TOKEN`. The broker forwards JSON to the configured
webhook and keeps audio on the host.

## Sub-features

- `pebble-auth` uses `PEBBLE_RING_TOKEN`. The broker bearer must return
  401 on this route.
- `pebble-fresh-timestamp` accepts a timestamp within five minutes of the
  server clock.
- `pebble-forward-json` returns `"status": "forwarded"`.
- `pebble-webhook-json` makes the loopback webhook observe JSON with no
  audio field.

## How to get to it (user POV)

- POST `http://127.0.0.1:<port>/v1/pebble` as JSON with
  `Authorization: Bearer $HOMELAB_FUNCTIONS_TOKEN` and expect 401.
- POST the same path as JSON with
  `Authorization: Bearer $PEBBLE_RING_TOKEN`.
- In production the Pebble app posts to
  `https://homelab-functions.home.feocco.com/v1/pebble`. Do not use that
  host in this skill.
- Run `./.venv/bin/python .cursor/skills/verify-homelab-functions/verify.py`
  and read `check pebble-relay`.

## Driving it with verify.py

Preconditions:

- `verify.py` printed `doctor health status=ok` and `catalog=verify-stub`.
- The loopback webhook is at `http://127.0.0.1:<webhook-port>/webhook`.
- `GROK_PEBBLE_WEBHOOK_URL` for the child is that loopback URL.
- The JSON body includes a fresh UTC timestamp.

- **Reject broker token.** POST `/v1/pebble` with the ephemeral broker
  token and the same JSON body. Status `401`. Error code is
  `unauthorized`. The webhook must not receive a POST.
- **Post JSON.** POST `/v1/pebble` with the ephemeral ring token and body
  `event_id`, `event_type=single-click-hold`, a fresh `timestamp`,
  `transcription=Turn the lights off`, and `client=ring`. The helper does
  this in `check pebble-relay`.
- **Read HTTP result.** Status `200`. JSON `"status"` is `forwarded`.
- **Read webhook.** The mock received one POST after the 401. Content type
  is `application/json`. Parsed JSON has `event_id` and `transcript` `Turn
  the lights off`. It has no `audio` field.
- **Proof.** Stdout contains `check pebble-relay PASS`, `broker=401`,
  `webhook_content_type=application/json`, and
  `webhook_has_audio_field=false`. Evidence must not contain the webhook
  key or ring token.

## Gotchas

- Events older than five minutes are rejected. Always send a fresh
  timestamp.
- The broker token must not authorize this route. `check pebble-relay`
  requires 401 for that token before the ring-token request.
- JSON without a transcription and without audio returns 400. This recipe
  sends `transcription`.
- The helper must not use the default Cursor webhook host. That address is
  not loopback.
- Cleanup may delete local audio files in scratch. The webhook capture in
  the evidence transcript is the proof that JSON left the host.
