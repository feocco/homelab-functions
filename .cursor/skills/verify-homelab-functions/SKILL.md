---
name: verify-homelab-functions
description: >-
  Prove the local homelab-functions broker through its HTTP API and MCP stdio
  server. Use when checking GET /health, GET /docs, bearer auth, POST
  /v1/notify/joe, python -m homelab.mcp_server, or POST /v1/pebble against a
  real process from this checkout without real secrets or deployment. Doctor
  also requires GET /v1/catalog/services to return the disposable verify-stub
  service from this run.
---

# Verify homelab-functions

This skill drives the real `homelab-functions` process in this checkout. It
does not deploy, does not read `.env`, and does not use production tokens.

## Launch

From the repo root, with `.venv` already installed as in `AGENTS.md`:

```bash
./.venv/bin/python .cursor/skills/verify-homelab-functions/verify.py
```

That one command is the launch. The helper binds `127.0.0.1` ports, seeds a
scratch directory with disposable catalog JSON and SQLite paths, sets
`NOTIFICATION_ACTION_RECORDER_ENABLED=false`, and starts
`./.venv/bin/python -m homelab.server` with generated secrets. It also starts
an in-process Home Assistant WebSocket mock and an in-process Pebble webhook
mock on loopback.

Ready means stdout contains `doctor health status=ok`,
`service=homelab-functions`, and `catalog=verify-stub`. A process that
answers TCP is not enough. Health must report `recorder_enabled=false` and
`token_configured=true`. The helper then sends GET `/v1/catalog/services`
with the generated broker token and requires the disposable `verify-stub`
service.

Each run owns a new scratch directory and new ports. Parallel runs do not
share ledgers, catalogs, mock captures, or tokens. Do not point this helper
at a deployed host. Do not drive a server you did not start.

## Doctor

There is no long-lived verify instance to attach to. If anything looks off,
run the same command again. After launch, the helper probes `GET /health` on
the child it started, then GET `/v1/catalog/services` with the generated
broker token, and prints a `doctor` line with the pid, loopback URL, and
`catalog=verify-stub`. Refuse to drive when that line is missing, the pid is
dead, health names a different service, or the catalog is not `verify-stub`.

## Drive

The helper then runs the ordered `CHECKS` registry in
`.cursor/skills/verify-homelab-functions/verify.py`. Stdout prints
`check <name> PASS` or `check <name> FAIL` for each row.

| Check | Expected observable |
| --- | --- |
| `health` | `GET /health` returns 200 with `status=ok` and `service=homelab-functions` |
| `docs` | `GET /docs` returns 200 HTML that names `homelab-functions` and `/v1/notify/joe` |
| `bearer-unauth` | `GET /v1/catalog/services` returns 401 with no bearer, a wrong bearer, and the ring token |
| `bearer-ok` | `GET /v1/catalog/services` returns 200 with the ephemeral broker bearer |
| `notify-joe` | `POST /v1/notify/joe` returns 200 `sent` and the HA mock captured `call_service` |
| `mcp-stdio` | `python -m homelab.mcp_server` answers initialize, tools/list, and one tools/call |
| `pebble-relay` | `POST /v1/pebble` returns 401 with the broker token, then forwards JSON with the ring token and no audio field |

Feature recipes live in [`features/README.md`](features/README.md). Use the
feature file for the path you are proving. The helper still runs every check
in one process so cleanup stays with the same owner.

Stable handles are the route paths, MCP method names, and the `check <name>`
lines. Do not use coordinates or tab order.

## Evidence

The helper writes a transcript to a new file under
`/tmp/verify-homelab-functions/<run_id>/evidence.txt` and prints that path on
`lifecycle start` and `lifecycle evidence`. The transcript records launch,
doctor, each check, cleanup, and `result PASS` or `result FAIL`. It does not
print bearer values or webhook keys.

Proof standards:

- Exercise the real HTTP routes and the real `python -m homelab.mcp_server`
  stdio process. Do not call internal setters.
- Capture the action and the result. For notify, keep both the HTTP `sent`
  body and the HA mock `call_service`. For Pebble, keep both the `/v1/pebble`
  response and the webhook JSON.
- Mocks are only the production boundaries already isolated in this service.
  Home Assistant is the WebSocket mock. The Grok webhook is the loopback
  POST sink. The helper must not contact a non-loopback address.
- After cleanup, the evidence file must still exist at the printed path.

## Cleanup

Cleanup always runs after success or failure, including a SIGTERM of this
helper. It sends `SIGTERM` then `SIGKILL` only to process groups the helper
started, the broker and the MCP child. It does not kill by process name. It
stops the in-process mock runners, deletes the scratch directory, and leaves
the evidence file in place. Stdout must include
`lifecycle cleanup complete scratch_removed=true` and
`lifecycle evidence exists=true`.

If the helper itself is killed with SIGKILL, this process cannot run
cleanup. Read the evidence file for that `run_id`. The `scratch=` field is
the unique scratch directory for that run. The `lifecycle launch` lines
name child pids. The `cmd=` text is context for which child that pid was.
Do not treat `cmd=` as a full command line to match.

For each pid, inspect the current working directory with
`lsof -a -p <pid> -d cwd -Fn`. The cwd must equal that run's `scratch=`
path exactly. If `lsof` is unavailable, or the cwd is missing, or the cwd
does not match exactly, do not signal that pid. A reused pid whose command
resembles `python -m homelab.server` is not enough. When the cwd matches,
send SIGTERM to that process group. Then delete only that exact scratch
directory. Do not kill by process name. A later isolated run also starts
clean because it picks new ports and a new scratch directory.

## Helpers

`verify.py` is executable. Invoke it with the repo venv from the repo root:

```bash
./.venv/bin/python .cursor/skills/verify-homelab-functions/verify.py
```

It accepts no flags. Launch, doctor, drive, evidence, and cleanup are phases
of that process. They show up as `lifecycle`, `doctor`, `check`, and `result`
lines.

Each run is isolated. The helper never reads `.env`. It builds a child
environment with generated tokens, loopback URLs, disposable SQLite paths,
and seeded catalog files. `PYTHONPATH` is this checkout's repo root so a
shared `.venv` cannot import another tree. `SERVICE_HOST` is `127.0.0.1`.
The HTTP client sets `trust_env=False`. Child cwd stays in scratch so
`load_dotenv()` does not see the repo `.env`. Exit status is 0 only when
every check passed and evidence still exists after cleanup.

When the app changes, run `/maintain-verification-skill` so this map stays
honest.
