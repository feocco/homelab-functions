# homelab-functions

`homelab-functions` is a small broker for shared homelab actions, such as
notifying Joe or Jess, plus a Python helper package for talking to it and to
Home Assistant.

- **Reference:** `GET /docs` on a running server lists every action with its
  parameters, status codes, and copyable HTTP, Python, and MCP examples.
  `GET /openapi.json` is the same contract for machines.
- **Explanation:** [docs/architecture.md](docs/architecture.md) covers the
  access registry, the [caller matrix](docs/architecture.md#callers), the
  notification and report flows, and the security model.
- **How-to:** the sections below.

## Quickstart

Run the server locally, open the reference, and send one notification.

Put the server variables from [Configuration](#configuration) in `.env`, then:

```bash
python3 -m venv .venv
./.venv/bin/pip install -e '.[test]'
./.venv/bin/python -m homelab.server
```

Open <http://127.0.0.1:8091/docs>. In another shell:

```bash
export HOMELAB_FUNCTIONS_URL=http://127.0.0.1:8091
export HOMELAB_FUNCTIONS_TOKEN=replace_me
./.venv/bin/python -c 'import homelab; print(homelab.notify_joe("Hello", "homelab-functions is working"))'
```

The response includes `"status": "sent"` and a `notification_id`.

## How-to guides

Every protected action can be called three ways. Python helpers and the MCP
server read `HOMELAB_FUNCTIONS_URL` and `HOMELAB_FUNCTIONS_TOKEN`.

| Call shape | Entry point |
| --- | --- |
| HTTP | `$HOMELAB_FUNCTIONS_URL/v1/...` with `Authorization: Bearer $HOMELAB_FUNCTIONS_TOKEN` |
| Python | `homelab.HomelabFunctionsClient` or module helpers such as `homelab.notify_joe` |
| MCP | `python3 -m homelab.mcp_server`. Tool names match operation ids. |

### Notify Joe or Jess

```python
import homelab

homelab.notify_jess(
    "Dinner plan",
    "Please review and accept the weekly plan.",
    tag="mealie-planner-2026-w44",
    group="mealie-planner",
    buttons=[{"title": "Open plan", "uri": "/lovelace/meals"}],
)
```

`notify_joe` takes the same arguments. Use a tag that is unique per
conversation, and the group your caller owns in the
[caller matrix](docs/architecture.md#callers).

### Check for a reply

The server records every notification button press in the shared ledger.
Poll by tag:

```python
latest = homelab.list_notifications(tag="mealie-planner-2026-w44", limit=1)
record = latest["notifications"][0]
if record["status"] == "responded":
    print(record["actions"][0]["action"], record["actions"][0].get("reply_text"))
```

### Handle a button press in your own service

Long-running services that need to act on a button immediately listen for
`mobile_app_notification_action` themselves and route by prefix:

```python
import homelab

router = homelab.NotificationActionRouter()
router.register("SRE_APPROVE", handle_approval)

button = {
    "title": "Approve",
    "action": homelab.NotificationActionRouter.make_action("SRE_APPROVE", "417"),
}

async def on_event(event: dict) -> None:
    router.handle_event(event)

async with homelab.HomeAssistantWebSocketClient.from_env() as ha:
    ha.add_event_handler(on_event)
    await ha.subscribe_events("mobile_app_notification_action")
    await ha.wait_closed()
```

Store the decision in the shared ledger with
`homelab.record_notification_action("SRE_APPROVE::417", tag="sre-incident-417", group="homelab-sre")`.
For a typed reply, add `"behavior": "textInput"`, `"textInputButtonTitle"`, and
`"textInputPlaceholder"` to the button and read `reply_text` from the event.

### Add a Report button to a workflow

```python
homelab.notify_joe(
    "Cat food monitor",
    "Morning food check failed.",
    tag="cat-food-monitor-morning",
    group="cat-food-monitor",
    buttons=[homelab.workflow_report_button("cat-food-monitor")],
)
```

When the workflow's listener receives `WORKFLOW_REPORT::cat-food-monitor`,
record what Joe typed:

```python
data = event["data"]
homelab.record_workflow_report("cat-food-monitor", data["reply_text"], source="mobile-action", event=data)
```

Read reports back with `homelab.list_workflow_reports(workflow="cat-food-monitor")`
or `homelab.get_workflow_report(report_id)`.

### Read the service catalog

```python
services = homelab.get_service_catalog()["services"]
targets = homelab.get_smoke_signal_targets()["targets"]
```

`homelab-config` generates both files. The server only authenticates and
returns them. `GET /docs` is the page the catalog links as this service's
docs.

### Relay a Pebble Index recording

The phone posts to `POST /v1/pebble`. This route is HTTP-only. It is not a
Python helper and not an MCP tool. Set the Pebble app webhook URL to:

`https://homelab-functions.home.feocco.com/v1/pebble`

Send `Authorization: Bearer $PEBBLE_RING_TOKEN`. That token is not
`HOMELAB_FUNCTIONS_TOKEN`. A transcription-only JSON body looks like this:

```bash
curl -sS -X POST "$HOMELAB_FUNCTIONS_URL/v1/pebble" \
  -H "Authorization: Bearer $PEBBLE_RING_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"event_id\":\"ring-event-1\",\"event_type\":\"single-click-hold\",\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"transcription\":\"Turn the lights off\",\"client\":\"ring\"}"
```

The server forwards JSON, including `event_id`, `event_type`, `timestamp`,
`transcript`, `audio_duration_seconds`, and allowlisted Pebble source fields.
Events more than five minutes from the server clock are rejected. The server
does not forward the audio file, and the route rejects bodies over 9 MiB. See
[Pebble ring relay](docs/architecture.md#pebble-ring-relay) for the tailnet
fallback and the homelab-config secret follow-up.

### See what needs Joe

Open <https://waiting.home.feocco.com> and sign in once with
`HOMELAB_FUNCTIONS_TOKEN`. The page is an inbox of only the things that need
Joe, in seven categories: PR approve/merge, deploy approval, secret or key
needed, DNS or infra decision, question to answer, copy/paste value or
command, and evidence to review. Each item shows what is needed, why, one
primary action, and a link to its source on GitHub. Copy items copy their
value in one click. Items leave the page live once they are handled. The page
updates over server-sent events and falls back to polling every 30 seconds.

A pull request becomes an approve/merge item when it is open, not a draft,
its checks passed or it has none, nobody requested changes, and it has no
merge conflict. Joe merges on GitHub himself; the inbox never approves or
merges.

Agents put everything else in the inbox with one task-list line in a pull
request or issue body or comment:

```markdown
- [ ] **Joe: secret** Add `DASHBOARD_GH_APP_KEY` as one base64 line. Why: the inbox cannot read GitHub without it. Action: https://github.com/feocco/homelab-config/settings/secrets/actions
- [ ] **Joe: copy** Publish the inbox DNS record. Why: the route needs it. Copy: `./scripts/dispatch-route-dns --mode apply --watch`
```

Kinds are `deploy`, `secret` (or `key`), `infra` (or `dns`), `question`,
`copy`, and `evidence`. `Action:` takes one https URL; `copy` asks need
`Copy:`. Ticking the box, deleting the line, or closing the source removes
the item; Joe can also press **Done**. Lines the inbox cannot read show up in
the snapshot's `rejected` list with the reason.

**Send feedback** on an item starts one Cursor agent for it, and that agent
opens one pull request for Joe to review. More feedback on the same item goes
to the same agent. Feedback never approves, merges, or deploys anything.

Agents read the same snapshot and send the same feedback through the broker:

```python
import homelab

inbox = homelab.get_waiting_items()
first = inbox["items"][0]
homelab.send_waiting_feedback(first["id"], "Use the base64 form of the key.", request_id="joe-2026-11-01-a")
```

See [Waiting on me](docs/architecture.md#waiting-on-me) for auth, identity,
and failure handling.

### Use the MCP server or Cursor plugin

`.cursor-plugin/plugin.json` points at `mcp.json` and `skills/`. The plugin
requires `HOMELAB_FUNCTIONS_TOKEN` and defaults `HOMELAB_FUNCTIONS_URL` to
`http://127.0.0.1:8091`. To run the server by hand:

```bash
HOMELAB_FUNCTIONS_TOKEN=replace_me python3 -m homelab.mcp_server
```

The server uses only the standard library. `homelab-functions-mcp` is the
installed command for the same server.

### Read Home Assistant state, events, or call a service

Connect to Home Assistant directly. Do not add these operations to the broker.

```python
import homelab

async with homelab.HomeAssistantWebSocketClient.from_env() as ha:
    states = await ha.get_states()
    await ha.subscribe_events("state_changed")
    await ha.call_service("switch", "turn_on", {"entity_id": "switch.example"})
```

Event handlers run outside the WebSocket reader task, so a handler can call
`get_states()` or `call_service()` on the same client. `help(homelab.HomeAssistantWebSocketClient)`
shows the full API.

## Reference

Protected actions are declared in `homelab.access.ACCESS_VECTOR`. The routes,
OpenAPI document, client methods, MCP tools, and `/docs` page all come from it.
`POST /v1/pebble` is the HTTP-only exception in `HTTP_ONLY_OPERATIONS`.
The inbox's browser routes are in `DASHBOARD_ROUTES` and use a session
cookie instead of the bearer token. These routes are public:

- `GET /health`
- `GET /docs`
- `GET /openapi.json`

### Configuration

Server:

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
HOMELAB_CATALOG_PATH=/app/config/service-catalog.json
HOMELAB_SMOKE_SIGNAL_TARGETS_PATH=/app/config/smoke-signal-targets.json
PEBBLE_RING_TOKEN=replace_me
GROK_PEBBLE_WEBHOOK_KEY=replace_me
GROK_PEBBLE_WEBHOOK_URL=https://api2.cursor.sh/automations/webhook/e3f09362-8a64-5de6-989c-c5d4b75e6238
XAI_API_KEY=
XAI_STT_MODEL=grok-voice-transcribe-2.0
PEBBLE_LEDGER_PATH=/app/data/pebble-events.sqlite3
PEBBLE_AUDIO_DIR=/app/data/pebble-audio
PEBBLE_AUDIO_TTL_SECONDS=86400
DASHBOARD_PUBLIC_URL=https://waiting.home.feocco.com
DASHBOARD_GH_APP_ID=
DASHBOARD_GH_APP_INSTALLATION_ID=
DASHBOARD_GH_OWNER=feocco
DASHBOARD_GH_APP_KEY=
DASHBOARD_REFRESH_SECONDS=60
CURSOR_API_KEY=
WAITING_FEEDBACK_PATH=/app/data/waiting-feedback.sqlite3
```

The inbox reads GitHub only when all four `DASHBOARD_GH_*` values are set.
`DASHBOARD_GH_APP_KEY` is the App's RSA private key. A `.env` value is one
line, so store it base64-encoded (`base64 -w0 key.pem`) or with `\n` escapes.
Without `CURSOR_API_KEY`, the inbox still shows every item and says that
feedback is off.

`PEBBLE_RING_TOKEN` and `GROK_PEBBLE_WEBHOOK_KEY` are required before the
ring route will forward. `GROK_PEBBLE_WEBHOOK_URL` defaults to the Cursor
webhook above when unset. `XAI_API_KEY` is required only when audio arrives
without a transcription. The other pebble variables have the defaults shown.

Python client and MCP server:

```text
HOMELAB_FUNCTIONS_URL=http://homelab-functions:8091
HOMELAB_FUNCTIONS_TOKEN=replace_me
```

`HomeAssistantWebSocketClient.from_env()`:

```text
HA_URL=https://example.ui.nabu.casa
HA_LONG_LIVED_TOKEN=replace_me
HA_REQUEST_TIMEOUT_SECONDS=30
```

## Development

```bash
./.venv/bin/pytest -q
```

`tests/test_access_vector_parity.py` fails when the registry, routes, client,
MCP tools, `/docs` page, caller matrix, or skill drift apart. See
[What changes together](docs/architecture.md#what-changes-together) before
adding an action.

Run the inbox on fake data, then check it end to end:

```bash
./.venv/bin/python -m homelab.waiting_cli demo --port 8099
./.venv/bin/python -m homelab.waiting_cli verify --url http://127.0.0.1:8099 \
  --token demo-token-not-a-secret --exercise
```
