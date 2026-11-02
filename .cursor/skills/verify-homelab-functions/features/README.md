# homelab-functions verification map

This directory is the maintained source for verifying the caller-facing
behavior of `homelab-functions`. Read the index before driving the service,
then use the matching feature file as the recipe.

## Baseline preconditions

- Work from the repo root with `.venv` installed as in `AGENTS.md`.
- Run `./.venv/bin/python .cursor/skills/verify-homelab-functions/verify.py`.
- Require a `doctor` line for `http://127.0.0.1:<port>`, service
  `homelab-functions`, and `catalog=verify-stub`.
- Let the helper pick loopback ports and a scratch directory. Do not export
  production tokens. Do not read `.env`.
- Never drive an instance that this verification run did not start.

## Driving conventions

- Start every recipe from the helper's isolated state.
- Treat route paths, MCP method names, and `check <name>` lines as literal.
- Run the helper from the repo root with `./.venv/bin/python`.
- Leave proof artifacts in `/tmp/verify-homelab-functions/<run_id>/evidence.txt`.

## Proof and skip reporting

- Capture the HTTP or MCP action and the resulting body, not only the final
  status code.
- Mutation proof includes a second observer. Notify uses the HA mock
  `call_service`. Pebble uses the loopback webhook body.
- Record the feature file and check name with every artifact.
- Report an unreachable path with the attempted route and the unmet
  precondition.
- Do not report a skipped entry point as verified through a different path.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the
caller-visible behavior. It then uses exactly four H2 sections in this order.

1. `Sub-features` lists short IDs with one line for each behavior.
2. `How to get to it (user POV)` lists every caller entry point.
3. `Driving it with verify.py` starts with `Preconditions:` and uses labeled
   bullets that pair each action with an exact command and observable result.
4. `Gotchas` lists traps that can waste or invalidate a verification run.

Keep implementation details out of the map. Name only caller paths, stable
handles, required state, commands, and observable proof.

## Features

- [Health and docs](./health-and-docs.md) covers public `GET /health` and
  `GET /docs`.
- [Bearer auth](./bearer-auth.md) covers 401 and 200 on a protected
  AccessVector route.
- [Mocked notification](./mocked-notification.md) covers
  `POST /v1/notify/joe` through the loopback Home Assistant WebSocket.
- [MCP stdio](./mcp-stdio.md) covers `python -m homelab.mcp_server`
  initialize, tools/list, and tools/call.
- [Pebble relay](./pebble-relay.md) covers `POST /v1/pebble` forwarding JSON
  and not audio.
