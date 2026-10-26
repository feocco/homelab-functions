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
- Do not add generic Home Assistant state, event, or service-call operations
  to the registry. Use `homelab.HomeAssistantWebSocketClient` for that work.

## Public API Documentation

- Public helpers intended for reuse across repos should have concise docstrings.
- Add class-level usage examples when lifecycle matters, such as async context
  managers.
- Keep README examples for end-to-end workflows; keep docstrings for call-site
  usage.
