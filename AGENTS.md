# AGENTS.md

## Repo-Specific Decisions

- The deployed server should expose only specific reusable actions, such as
  `notify_joe`.
- Do not turn the deployed server into a generic Home Assistant proxy.
- Use client-side helpers for direct Home Assistant state, event, and
  service-call work.

## Agent Workflows

- Run tests with `./.venv/bin/pytest -q`.
- Use `homelab.HomeAssistantWebSocketClient` for new Python Home Assistant
  integrations.

## Access Parity

- Any protected `/v1` action must be declared in the `AccessVector` registry in
  `homelab/access.py`.
- That action must have a matching HTTP route and OpenAPI operation, a
  `HomelabFunctionsClient` method, an MCP tool, a plugin skill entry, and
  behavior-level coverage in `tests/test_access_vector_parity.py`.
- `/health`, `/docs`, and `/openapi.json` stay public and outside the registry.
- `POST /v1/pebble` is the one HTTP-only exception. It lives in
  `HTTP_ONLY_OPERATIONS`, not `ACCESS_VECTOR`, because the ring posts
  multipart audio and authenticates with `PEBBLE_RING_TOKEN`. Do not give it
  a `HomelabFunctionsClient` method or an MCP tool. OpenAPI, `GET /docs`, and
  `tests/test_access_vector_parity.py` still have to name it.
- Press count stays inside that relay. `single-click-hold` keeps
  `GROK_PEBBLE_WEBHOOK_URL` and `GROK_PEBBLE_WEBHOOK_KEY`. `double-click-hold`
  uses `GROK_PEBBLE_WORK_WEBHOOK_URL` and `GROK_PEBBLE_WORK_WEBHOOK_KEY`, which
  have no default. Any other gesture is rejected. Missing work configuration
  returns 503 `work_webhook_not_configured` and must not forward to the
  personal webhook. Do not add a second route for this split.
- Callers and their ledger conventions live in `CONSUMERS` in
 `homelab/consumers.py`. `GET /docs` renders them, and the caller matrix in
 `docs/architecture.md` must match `consumer_matrix_markdown()` exactly.
- Do not add generic Home Assistant state, event, or service-call operations
  to the registry. Use `homelab.HomeAssistantWebSocketClient` for that work.

## Public API Documentation

- Public helpers intended for reuse across repos should have concise docstrings.
- Add class-level usage examples when lifecycle matters, such as async context
  managers.
- Keep README examples for end-to-end workflows; keep docstrings for call-site
  usage.
