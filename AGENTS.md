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
- The Waiting on me inbox's browser routes live in `DASHBOARD_ROUTES`, outside `/v1`,
 behind a session cookie. `tests/test_access_vector_parity.py` checks that
 every served route belongs to exactly one of `PUBLIC_ENDPOINTS`,
 `ACCESS_VECTOR`, `HTTP_ONLY_OPERATIONS`, or `DASHBOARD_ROUTES`.
- `POST /v1/pebble` is the one HTTP-only exception. It lives in
  `HTTP_ONLY_OPERATIONS`, not `ACCESS_VECTOR`, because the ring posts
  multipart audio and authenticates with `PEBBLE_RING_TOKEN`. Do not give it
  a `HomelabFunctionsClient` method or an MCP tool. OpenAPI, `GET /docs`, and
  `tests/test_access_vector_parity.py` still have to name it.
- Callers and their ledger conventions live in `CONSUMERS` in
 `homelab/consumers.py`. `GET /docs` renders them, and the caller matrix in
 `docs/architecture.md` must match `consumer_matrix_markdown()` exactly.
- Do not add generic Home Assistant state, event, or service-call operations
  to the registry. Use `homelab.HomeAssistantWebSocketClient` for that work.

## Waiting on Me Inbox

- It is an action inbox, not a status board. List only items that need Joe,
 in exactly seven categories: PR approve/merge, deploy approval, secret or
 key needed, DNS or infra decision, question to answer, copy/paste value or
 command, evidence to review. Do not add progress lanes, check-state views,
 or agent and task lists.
- To put something in Joe's inbox, add an unchecked task-list line to the pull
 request or issue body or a comment: `- [ ] **Joe: secret** What is needed.
 Why: the reason. Action: https://…`. Kinds: `deploy`, `secret`/`key`,
 `infra`/`dns`, `question`, `copy` (needs `Copy: `value``), `evidence`. Never
 ask for approve/merge; it comes from GitHub state only. After writing a line,
 check `get_waiting_items()["rejected"]` for your source URL.
- Browser: `https://waiting.home.feocco.com` (`/waiting`). Agents:
 `get_waiting_items` and `send_waiting_feedback`, which are normal registry
 actions with a client method, MCP tool, and skill entry.
- `WaitingBoard.payload()` in `homelab/waiting.py` is the only snapshot
 builder. The page, SSE stream, polling fallback, and `GET /v1/waiting` all
 serve it. Do not add a second serializer.
- `InboxItem` enforces need, why, one primary action, and a GitHub source.
 Approve/merge only links to GitHub. Only Joe's browser session can mark an
 item done (`POST /waiting/handled`), never for approve/merge items.
- Browser routes live in `DASHBOARD_ROUTES`, outside `/v1`. They use the
 session cookie from `homelab/waiting_web.py`. Never let the cookie open `/v1`
 or the bearer token open `/waiting/*`.
- Feedback keeps one Cursor agent per item. Store the agent id before calling
 Cursor, reuse it on retry, and never add launch fields that approve, merge,
 or deploy. The GitHub App token must stay read-only.
- UI copy: plain sentences, no internal jargon. Render untrusted text with
 `textContent`. Script and style stay inline and hash-pinned by the CSP.
- Verify a change end to end:
 `./.venv/bin/python -m homelab.waiting_cli demo --port 8099` in one shell,
 then `./.venv/bin/python -m homelab.waiting_cli verify --url
 http://127.0.0.1:8099 --token demo-token-not-a-secret --exercise`.
 It prints a JSON report and exits 1 on any failed check. Without
 `--exercise` it is read-only and safe to run against production with the
 real token.
- Visual changes: with the demo running, `./.venv/bin/python
 scripts/waiting_browser_check.py --out /tmp/waiting-shots` (needs
 `playwright` and Chrome) screenshots desktop and mobile in light and dark
 mode, checks categories, primary actions, copy, live arrival and removal,
 the polling fallback, and feedback, and prints a JSON report.

## Public API Documentation

- Public helpers intended for reuse across repos should have concise docstrings.
- Add class-level usage examples when lifecycle matters, such as async context
  managers.
- Keep README examples for end-to-end workflows; keep docstrings for call-site
  usage.
