# MCP stdio

The MCP server is a stdio JSON-RPC process. Callers start
`python -m homelab.mcp_server`, then send initialize, tools/list, and
tools/call. Tool names match AccessVector operation ids.

## Sub-features

- `mcp-initialize` answers protocol version `2024-11-05` with server name
  `homelab-functions`.
- `mcp-tools-list` includes `notify_joe` and omits `relay_pebble`.
- `mcp-tools-call` runs `notify_joe` against the live loopback broker.
- `mcp-ha-proof` makes the HA mock observe title `verify-mcp`.

## How to get to it (user POV)

- Run `./.venv/bin/python -m homelab.mcp_server` with
  `HOMELAB_FUNCTIONS_URL` and `HOMELAB_FUNCTIONS_TOKEN` for this isolated
  broker.
- Use the Cursor plugin MCP entry in `mcp.json`, which starts
  `python3 -m homelab.mcp_server`.
- Run `./.venv/bin/python .cursor/skills/verify-homelab-functions/verify.py`
  and read `check mcp-stdio`.

## Driving it with verify.py

Preconditions:

- `verify.py` printed `doctor health status=ok` and `catalog=verify-stub`.
- `check notify-joe` may already have run. The MCP call uses a distinct
  title.
- The MCP child environment points at this run's loopback broker URL.

- **Start MCP.** The helper starts `sys.executable -m homelab.mcp_server`
  with stdin and stdout pipes. Stdout contains `lifecycle launch mcp pid=`.
- **Initialize.** Write a JSON-RPC `initialize` request with
  `protocolVersion` `2024-11-05`. Result `serverInfo.name` is
  `homelab-functions`. Result protocol version is `2024-11-05`.
- **List tools.** Write `tools/list`. The tool names include `notify_joe`
  and do not include `relay_pebble`.
- **Call notify_joe.** Write `tools/call` with name `notify_joe` and
  arguments `title=verify-mcp` and `message=mcp tools/call`. Result
  `isError` is false. Content JSON `"status"` is `sent`.
- **Confirm HA.** The HA mock captured `call_service` with title
  `verify-mcp`.
- **Proof.** Stdout contains `check mcp-stdio PASS` and
  `tools_call=notify_joe ha_title=verify-mcp`. The helper then stops that
  MCP pid.

## Gotchas

- MCP logs go to stderr. Do not parse stderr for success.
- `relay_pebble` must stay off `tools/list`. Pebble is HTTP-only.
- The MCP process must talk to this run's broker. A default
  `http://127.0.0.1:8091` target is the wrong instance.
- Close stdin and stop the captured MCP pid. Do not kill other
  `homelab.mcp_server` processes by name.
