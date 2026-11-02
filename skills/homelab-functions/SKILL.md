---
name: homelab-functions
description: >-
  Use the homelab-functions broker for named actions such as notify_joe,
  notify_jess, list_notifications, record_notification_action,
  get_service_catalog, get_smoke_signal_targets, record_workflow_report,
  list_workflow_reports, get_workflow_report, get_waiting_items, and
  send_waiting_feedback. For Home Assistant state, events, and service calls,
  use homelab.HomeAssistantWebSocketClient.
---

# Homelab functions

`homelab-functions` is a local broker for a fixed set of reusable actions. The action list is the `AccessVector` registry in `homelab/access.py`. Call an action with `HomelabFunctionsClient`, the HTTP API, or the homelab-functions MCP server. MCP tool names match the operation ids in that registry.

`/health`, `/docs`, and `/openapi.json` are public. Every `/v1` action requires `HOMELAB_FUNCTIONS_TOKEN`.

Use the notification group, tag pattern, and action prefix your caller owns in the caller matrix in `docs/architecture.md` or on `GET /docs#callers`. For example, `@Rosie` uses group `rosie` and the `ROSIE_` prefix, and notifies Jess only after Joe confirms.

Do not send Home Assistant state, events, or service calls through this broker. Use `homelab.HomeAssistantWebSocketClient` for `get_states`, event subscriptions, and `call_service`.

## Notify Joe

Use `notify_joe` to send a mobile notification to Joe through the configured Home Assistant notify service. Pass a title and a message. Add a tag, group, URL, or buttons when the phone notification needs them.

## Notify Jess

Use `notify_jess` for the same kind of notification, routed to Jess. Do not call `notify_joe` when the recipient is Jess.

## List notifications

Use `list_notifications` to read recent rows from the shared notification ledger. Filter with `group`, `tag`, and `limit` when you need a slice of that history.

## Record a notification action

Use `record_notification_action` when a service has already handled a mobile button press and needs the shared ledger to store that action. The call stores the action. It does not run the action.

## Service catalog

Use `get_service_catalog` to read the generated homelab service catalog. `homelab-config` owns the file. This action only returns it.

## Smoke Signal targets

Use `get_smoke_signal_targets` to read the generated Smoke Signal target list. Use it when a checker needs that file and should not clone `homelab-config`.

## Record a workflow report

Use `record_workflow_report` to store a human report that a workflow needs investigation. Pass the workflow slug and the summary text. Source, notification id, and the raw event are optional.

## List workflow reports

Use `list_workflow_reports` to read recent workflow reports. Filter with `workflow` and `limit`.

## Get one workflow report

Use `get_workflow_report` when you already have a report id and need that one record.

## Waiting on me

Use `get_waiting_items` to read Joe's action inbox. It returns the same `waiting-on-me.v2` snapshot that `https://waiting.home.feocco.com` shows: only items that need Joe, in seven categories (PR approve/merge, deploy approval, secret or key needed, DNS or infra decision, question to answer, copy/paste value or command, evidence to review). Each item has an id like `feocco/homelab-config#41:secret:3f9a1c2b7d`, what is needed, why, one primary action, and a source link.

To ask Joe for something, add an unchecked task-list line to your pull request or issue body or a comment: `- [ ] **Joe: secret** What is needed. Why: the reason. Action: https://…`. Kinds are `deploy`, `secret`, `infra`, `question`, `copy` (ends with ``Copy: `value` ``), and `evidence`. Do not ask for approve/merge; ready pull requests appear on their own. Check the snapshot's `rejected` list if your line does not show up.

Use `send_waiting_feedback` to pass Joe's feedback on one item to Cursor. The first feedback starts one Cursor agent that opens one pull request. Later feedback goes to the same agent. Send a `request_id` and reuse it on retry, so a retry does not send the feedback twice. Feedback never approves, merges, deploys, or clears an item. It returns `503` when `CURSOR_API_KEY` is not set on the server.

## Pebble ring relay

`POST /v1/pebble` is HTTP-only. It is not an MCP tool and not a `HomelabFunctionsClient` method. The Pebble app authenticates with `PEBBLE_RING_TOKEN`. That token is not `HOMELAB_FUNCTIONS_TOKEN`. Do not send ring audio through any other action.

## Direct Home Assistant access

Use `homelab.HomeAssistantWebSocketClient` when you need live Home Assistant state, events, or service calls. Connect with `HomeAssistantWebSocketClient.from_env()`. Do not add a generic state, event, or service-call route to the homelab-functions server.
