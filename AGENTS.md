# AGENTS.md

## Repo-specific decisions

- The deployed server should expose only specific reusable actions, such as
  `notify_joe`.
- Do not turn the deployed server into a generic Home Assistant proxy.
- Use client-side helpers for direct Home Assistant state, event, and
  service-call work.

## Setup and run

From the repo root:

```bash
python3 -m venv .venv
./.venv/bin/pip install -e '.[test]'
```

Put the server variables from the README configuration section in `.env`,
then start the broker:

```bash
./.venv/bin/python -m homelab.server
```

The process listens on `SERVICE_HOST` and `SERVICE_PORT`. The defaults are
`0.0.0.0` and `8091`. Open `http://127.0.0.1:8091/docs` for the live
reference. `GET /health` and `GET /openapi.json` are also public.

The stdio MCP server is:

```bash
./.venv/bin/python -m homelab.mcp_server
```

It reads `HOMELAB_FUNCTIONS_URL` and `HOMELAB_FUNCTIONS_TOKEN`.

## Tests

```bash
./.venv/bin/pytest -q
```

`tests/test_access_vector_parity.py` fails when the registry, routes,
client, MCP tools, `/docs` page, caller matrix, or plugin skill drift
apart.

## Local verification

To prove the real local process through HTTP and MCP without `.env`, real
secrets, or deployment:

```bash
./.venv/bin/python .cursor/skills/verify-homelab-functions/verify.py
```

The helper binds loopback ports, seeds disposable catalog files and SQLite
paths, disables the notification action recorder, launches this checkout's
server, drives the mapped checks, and then stops only the processes it
started. See `.cursor/skills/verify-homelab-functions/SKILL.md`.

## Auth

Never build custom auth. If auth is needed, reuse the homelab IdP (Authentik via Caddy forward_auth). Bots use bearer tokens.

Human or browser sign-in to homelab services uses Authentik through Caddy
`forward_auth`. Machine callers use scoped bearer tokens. Scripts, MCP
clients, and bots send `Authorization: Bearer $HOMELAB_FUNCTIONS_TOKEN` for
every protected AccessVector route. The Pebble ring sends
`Authorization: Bearer $PEBBLE_RING_TOKEN` to `POST /v1/pebble` only. That
ring token is not `HOMELAB_FUNCTIONS_TOKEN` and does not authorize any
other route.

## Access parity

- Any protected `/v1` action must be declared in the `AccessVector`
  registry in `homelab/access.py`.
- That action must have a matching HTTP route and OpenAPI operation, a
  `HomelabFunctionsClient` method, an MCP tool, a plugin skill entry, and
  behavior-level coverage in `tests/test_access_vector_parity.py`.
- `/health`, `/docs`, and `/openapi.json` stay public and outside the
  registry.
- `POST /v1/pebble` is the one HTTP-only exception. It lives in
  `HTTP_ONLY_OPERATIONS`, not `ACCESS_VECTOR`, because the ring posts
  multipart audio and authenticates with `PEBBLE_RING_TOKEN`. Do not give
  it a `HomelabFunctionsClient` method or an MCP tool. OpenAPI, `GET
  /docs`, and `tests/test_access_vector_parity.py` still have to name it.
- Do not add generic Home Assistant state, event, or service-call
  operations to the registry. Use `homelab.HomeAssistantWebSocketClient`
  for that work.

## Caller registry

Callers and their ledger conventions live in `CONSUMERS` in
`homelab/consumers.py`. `GET /docs` renders them, and the caller matrix in
`docs/architecture.md` must match `consumer_matrix_markdown()` exactly.

## Coding and docs

- Public helpers intended for reuse across repos should have concise
  docstrings.
- Add class-level usage examples when lifecycle matters, such as async
  context managers.
- Keep README examples for end-to-end workflows. Keep docstrings for
  call-site usage.
- Use `homelab.HomeAssistantWebSocketClient` for new Python Home Assistant
  integrations.
- Keep comments only when they record a non-obvious reason the code cannot
  show.

## pstack

One implementer and one reviewer. Do not start an arena, a swarm, or a
multi-judge fan-out unless Joe asks.
