# Health and docs

Health and docs are the public pages a caller can open without a bearer
token. They show that this checkout's server is the homelab-functions
broker and that the live reference names the protected actions.

## Sub-features

- `health-ok` returns JSON with `status=ok` and `service=homelab-functions`.
- `health-recorder` reports the notification action recorder disabled in
  this isolated run.
- `docs-html` returns the HTML reference for the broker.
- `docs-routes` names `/v1/notify/joe` and `/health` on that page.

## How to get to it (user POV)

- Open `http://127.0.0.1:<port>/health` with a GET client.
- Open `http://127.0.0.1:<port>/docs` in a browser or GET client.
- Run `./.venv/bin/python .cursor/skills/verify-homelab-functions/verify.py`
  and read the `check health` and `check docs` lines.

## Driving it with verify.py

Preconditions:

- `.venv` is installed from the repo root.
- `verify.py` printed `doctor health status=ok` and `catalog=verify-stub`.
- The doctor URL is on `127.0.0.1`.

- **Read health.** GET `/health`. The helper does this in `check health`.
  Status `200`. JSON has `"status": "ok"` and `"service": "homelab-functions"`.
- **Confirm recorder off.** Use the same health body. The helper requires
  `"notification_action_recorder_enabled": false`.
- **Open docs.** GET `/docs`. The helper does this in `check docs`. Status
  `200`. Content type includes `html`.
- **Confirm action names.** The HTML includes `homelab-functions`,
  `/v1/notify/joe`, `/health`, and `notify_joe`.
- **Proof.** Stdout contains `check health PASS` and `check docs PASS`. The
  evidence file under `/tmp/verify-homelab-functions/` still exists after
  cleanup.

## Gotchas

- `GET /health` can return 200 while the action recorder is still enabled.
  This run must see `recorder_enabled=false`.
- `GET /docs` is public. A 200 on `/docs` does not prove bearer auth.
- Do not use a deployed `homelab-functions` URL. The helper binds a new
  loopback port each run.
- Cleanup deletes scratch SQLite files. It must not delete the evidence
  transcript.
