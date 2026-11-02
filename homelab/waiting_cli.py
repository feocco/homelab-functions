"""Run the Waiting on me dashboard on fake data, or verify a running server.

```bash
python -m homelab.waiting_cli demo --port 8099
python -m homelab.waiting_cli verify --url http://127.0.0.1:8099 --token demo-token --exercise-feedback
```

`demo` serves the real app with an in-memory GitHub source and a fake Cursor
API. It needs no secrets and makes no network calls. A new pull request
appears every few refreshes so the live update path has something to show.

`verify` checks a running server and prints one JSON report. It exits 1 when
any check fails. `--exercise-feedback` sends feedback, so it refuses to run
unless `/health` reports `waiting_demo: true`.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .waiting import FailingCheck, ItemState, WaitingItem


DEMO_TOKEN = "demo-token-not-a-secret"


def _ago(minutes: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return moment.isoformat(timespec="seconds")


def _item(
    repo: str,
    number: int,
    title: str,
    *,
    state: ItemState,
    author: str = "cursor",
    minutes: int = 30,
    failing: tuple[FailingCheck, ...] = (),
) -> WaitingItem:
    checks = {
        ItemState.READY: "passed",
        ItemState.FAILING: "failing",
        ItemState.RUNNING: "running",
        ItemState.CHANGES_REQUESTED: "passed",
    }[state]
    return WaitingItem(
        repo=f"feocco/{repo}",
        number=number,
        title=title,
        url=f"https://github.com/feocco/{repo}/pull/{number}",
        author=author,
        state=state,
        checks=checks,
        failing_checks=failing,
        created_at=_ago(minutes + 90),
        updated_at=_ago(minutes),
        head_ref=f"cursor/demo-{number}",
        cross_repository=False,
    )


def demo_items() -> list[WaitingItem]:
    return [
        _item("homelab-config", 901, "Route grafana.home.feocco.com through the default convention", state=ItemState.READY, minutes=12),
        _item("bedtime", 904, "Drop the morning follow-up prompt", state=ItemState.READY, author="feocco", minutes=95),
        _item(
            "pirate-radio",
            902,
            "Serialize approve and skip decisions per article",
            state=ItemState.FAILING,
            minutes=40,
            failing=(
                FailingCheck("test (vitest)", "https://github.com/feocco/pirate-radio/actions"),
                FailingCheck("typecheck", "https://github.com/feocco/pirate-radio/actions"),
            ),
        ),
        _item("homelab-functions", 903, "Add a retry budget to the Pebble relay", state=ItemState.RUNNING, minutes=3),
        _item("homelab-sre-agent", 905, "Summarize disk alerts before paging", state=ItemState.CHANGES_REQUESTED, minutes=300),
    ]


def demo_arrivals() -> list[WaitingItem]:
    return [
        _item("homelab-config", 906, "Pin the Caddy image digest", state=ItemState.READY, minutes=0),
        _item("laundry-monitor", 907, "Debounce washer finish events", state=ItemState.RUNNING, minutes=0),
    ]


class DemoWaitingSource:
    """Fake GitHub data. One arrival joins every `reveal_every` fetches, then the cycle restarts."""

    owner = "feocco"

    def __init__(self, reveal_every: int = 3) -> None:
        self.reveal_every = reveal_every
        self.fetches = 0

    async def fetch(self) -> list[WaitingItem]:
        self.fetches += 1
        arrivals = demo_arrivals()
        cycle = len(arrivals) + 1
        revealed = (self.fetches // self.reveal_every) % cycle
        fresh = [replace(item, updated_at=_ago(0), created_at=_ago(0)) for item in arrivals[:revealed]]
        return demo_items() + fresh


class DemoCursorAgents:
    """Fake Cursor API: records calls and reports a fake PR after launch."""

    def __init__(self) -> None:
        self.launches: list[dict[str, Any]] = []
        self.follow_ups: list[tuple[str, str]] = []
        self._items: dict[str, WaitingItem] = {}

    async def launch(self, *, agent_id: str, name: str, prompt: str, item: WaitingItem) -> str:
        self.launches.append({"agent_id": agent_id, "name": name, "prompt": prompt, "item": item.id})
        self._items[agent_id] = item
        return f"https://cursor.com/agents/{agent_id}"

    async def follow_up(self, agent_id: str, prompt: str) -> str:
        self.follow_ups.append((agent_id, prompt))
        return "followup"

    async def pull_request_url(self, agent_id: str) -> str | None:
        item = self._items.get(agent_id)
        return f"https://github.com/{item.repo}/pull/{item.number + 50}" if item else None


def demo_app(port: int, token: str, data_dir: str, *, refresh_seconds: int = 5, feedback: bool = True):
    from .server import Config, create_app

    config = Config(
        ha_url="http://127.0.0.1:9",
        ha_long_lived_token="demo-ha-token",
        ha_notify_joe_service="notify.demo_joe",
        ha_notify_jess_service="notify.demo_jess",
        homelab_functions_token=token,
        notification_ledger_path=str(Path(data_dir) / "notifications.sqlite3"),
        notification_action_recorder_enabled=False,
        pebble_ledger_path=str(Path(data_dir) / "pebble.sqlite3"),
        pebble_audio_dir=str(Path(data_dir) / "pebble-audio"),
        dashboard_public_url=f"http://127.0.0.1:{port}",
        dashboard_refresh_seconds=refresh_seconds,
        waiting_feedback_path=str(Path(data_dir) / "waiting-feedback.sqlite3"),
    )
    return create_app(
        config,
        waiting_source=DemoWaitingSource(),
        cursor_agents=DemoCursorAgents() if feedback else None,
        waiting_demo=True,
    )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class _Http:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.origin = self.base_url
        self._opener = build_opener(_NoRedirect)

    def call(self, method: str, path: str, *, headers: dict[str, str] | None = None, body: bytes | None = None):
        request = Request(f"{self.base_url}{path}", data=body, method=method, headers=headers or {})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers), response.read().decode("utf-8")
        except HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode("utf-8", errors="replace")

    def first_sse_snapshot(self, cookie: str) -> dict[str, Any]:
        request = Request(f"{self.base_url}/waiting/events", headers={"Cookie": cookie})
        with self._opener.open(request, timeout=self.timeout) as response:
            event = ""
            for raw in response:
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith("event: "):
                    event = line[len("event: ") :]
                elif line.startswith("data: ") and event == "snapshot":
                    return json.loads(line[len("data: ") :])
        raise RuntimeError("SSE stream ended before a snapshot event")


def _embedded_snapshot(html: str) -> dict[str, Any]:
    marker = '<script type="application/json" id="snapshot">'
    start = html.index(marker) + len(marker)
    return json.loads(html[start : html.index("</script>", start)])


def verify(base_url: str, token: str, *, exercise_feedback: bool = False, timeout: float = 10) -> dict[str, Any]:
    """Check a running server end to end and return a JSON-ready report."""

    http = _Http(base_url, timeout)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    bearer = {"Authorization": f"Bearer {token}"}
    status, _, body = http.call("GET", "/health")
    health = json.loads(body) if status == 200 else {}
    check("health", status == 200 and health.get("status") == "ok", {
        key: health.get(key) for key in ("waiting_github_status", "waiting_feedback_enabled", "waiting_demo")
    })

    status, headers, _ = http.call(
        "POST",
        "/waiting/session",
        headers={"Origin": http.origin, "Content-Type": "application/x-www-form-urlencoded"},
        body=urlencode({"token": token}).encode(),
    )
    cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
    if not check("dashboard sign-in sets a session cookie", status == 303 and cookie.startswith("waiting_session="), status):
        return {"ok": False, "checks": checks}
    session = {"Cookie": cookie}

    views: dict[str, dict[str, Any]] = {}
    for _attempt in range(5):
        views = {
            "v1_json": json.loads(http.call("GET", "/v1/waiting", headers=bearer)[2]),
            "snapshot_json": json.loads(http.call("GET", "/waiting/snapshot", headers=session)[2]),
            "sse": http.first_sse_snapshot(cookie),
            "html": _embedded_snapshot(http.call("GET", "/waiting", headers=session)[2]),
        }
        if len({view.get("version") for view in views.values()}) == 1:
            break
    versions = {name: view.get("version") for name, view in views.items()}
    first = views["v1_json"]
    check("HTML, SSE, snapshot JSON, and /v1/waiting are one document", all(view == first for view in views.values()), versions)
    check("snapshot schema", first.get("schema") == "waiting-on-me.v1", first.get("schema"))
    items = first.get("items", [])
    check(
        "every item links to its GitHub pull request",
        all(item["url"] == f"https://github.com/{item['repo']}/pull/{item['number']}" for item in items),
        len(items),
    )
    check("item ids are unique", len({item["id"] for item in items}) == len(items), [item["id"] for item in items])

    status, _, _ = http.call("GET", "/waiting/snapshot", headers=bearer)
    check("bearer token does not open dashboard routes", status == 401, status)
    status, _, _ = http.call("GET", "/v1/waiting", headers=session)
    check("session cookie does not open /v1", status == 401, status)

    if exercise_feedback:
        _verify_feedback(http, bearer, session, health, items, check)

    return {"ok": all(entry["ok"] for entry in checks), "url": base_url, "checks": checks}


def _verify_feedback(http: _Http, bearer, session, health, items, check) -> None:
    if not check("feedback exercise only runs against the demo", health.get("waiting_demo") is True, health.get("waiting_demo")):
        return
    target = next((item for item in items if not item.get("feedback")), None)
    if not check("an item without feedback exists", target is not None):
        return
    request_id = f"verify-{secrets.token_hex(6)}"
    send = {"Content-Type": "application/json"}

    def post(path: str, headers: dict[str, str], payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        status, _, body = http.call("POST", path, headers={**headers, **send}, body=json.dumps(payload).encode())
        return status, json.loads(body)

    first_payload = {"item_id": target["id"], "feedback": "Verify: first note.", "request_id": request_id}
    status, first = post("/v1/waiting/feedback", bearer, first_payload)
    check("first feedback launches one agent", status == 200 and first.get("delivery") == "launch", first)
    status, again = post("/v1/waiting/feedback", bearer, first_payload)
    check("resending the same request_id is a no-op", status == 200 and again.get("duplicate") is True, again)
    status, second = post(
        "/waiting/feedback",
        {**session, "Origin": http.origin},
        {"item_id": target["id"], "feedback": "Verify: second note.", "request_id": f"{request_id}-2"},
    )
    agent_id = first.get("agent", {}).get("id")
    check(
        "later feedback goes to the same agent",
        status == 200 and second.get("delivery") in ("followup", "steer") and second.get("agent", {}).get("id") == agent_id,
        second,
    )
    snapshot = json.loads(http.call("GET", "/v1/waiting", headers=bearer)[2])
    linked = next((item for item in snapshot["items"] if item["id"] == target["id"]), {})
    check("snapshot shows the item's agent", (linked.get("feedback") or {}).get("agent_id") == agent_id, linked.get("feedback"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m homelab.waiting_cli", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Serve the dashboard on fake data.")
    demo.add_argument("--port", type=int, default=8099)
    demo.add_argument("--token", default=DEMO_TOKEN)
    demo.add_argument("--refresh-seconds", type=int, default=5)
    demo.add_argument("--no-cursor", action="store_true", help="Run with feedback off, as without CURSOR_API_KEY.")
    check = commands.add_parser("verify", help="Check a running server and print a JSON report.")
    check.add_argument("--url", required=True)
    check.add_argument("--token", required=True, help="HOMELAB_FUNCTIONS_TOKEN of that server.")
    check.add_argument("--exercise-feedback", action="store_true", help="Send feedback. Demo servers only.")
    args = parser.parse_args(argv)

    if args.command == "demo":
        from aiohttp import web

        with tempfile.TemporaryDirectory(prefix="waiting-demo-") as data_dir:
            print(f"Waiting dashboard demo: http://127.0.0.1:{args.port}/waiting token={args.token}", file=sys.stderr)
            web.run_app(
                demo_app(
                    args.port,
                    args.token,
                    data_dir,
                    refresh_seconds=args.refresh_seconds,
                    feedback=not args.no_cursor,
                ),
                host="127.0.0.1",
                port=args.port,
                print=None,
            )
        return 0

    report = verify(args.url, args.token, exercise_feedback=args.exercise_feedback)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
