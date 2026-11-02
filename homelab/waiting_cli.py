"""Run the Waiting on me inbox on fake data, or verify a running server.

```bash
python -m homelab.waiting_cli demo --port 8099
python -m homelab.waiting_cli verify --url http://127.0.0.1:8099 --token demo-token-not-a-secret --exercise
```

`demo` serves the real app on fake GitHub search results that go through the
real inbox classifier, plus a fake Cursor API. It needs no secrets and makes
no network calls. Over a few refreshes a new pull request arrives, then an
agent's question is ticked off on "GitHub" and leaves the inbox.

`verify` checks a running server and prints one JSON report. It exits 1 when
any check fails. `--exercise` sends feedback and marks an item done, so it
refuses to run unless `/health` reports `waiting_demo: true`.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .github_waiting import inbox_from_node
from .waiting import CATEGORY_LABELS, CATEGORY_ORDER, SCHEMA, Inbox, InboxItem


DEMO_TOKEN = "demo-token-not-a-secret"
DEMO_OWNER = "feocco"


def _ago(minutes: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return moment.isoformat(timespec="seconds")


def _pull(
    repo: str,
    number: int,
    title: str,
    *,
    minutes: int,
    checks: str | None = "SUCCESS",
    state: str = "OPEN",
    review: str | None = None,
    draft: bool = False,
    body: str = "",
    comments: tuple[str, ...] = (),
) -> dict[str, Any]:
    url = f"https://github.com/{DEMO_OWNER}/{repo}/pull/{number}"
    return {
        "__typename": "PullRequest",
        "number": number,
        "title": title,
        "url": url,
        "body": body,
        "updatedAt": _ago(minutes),
        "state": state,
        "isDraft": draft,
        "headRefName": f"cursor/demo-{number}",
        "isCrossRepository": False,
        "reviewDecision": review,
        "mergeable": "MERGEABLE",
        "repository": {"nameWithOwner": f"{DEMO_OWNER}/{repo}"},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": checks} if checks else None}}]},
        "comments": {
            "nodes": [
                {"body": text, "url": f"{url}#issuecomment-{number}{index}", "updatedAt": _ago(minutes)}
                for index, text in enumerate(comments)
            ]
        },
    }


def _issue(repo: str, number: int, title: str, *, minutes: int, body: str) -> dict[str, Any]:
    return {
        "__typename": "Issue",
        "number": number,
        "title": title,
        "url": f"https://github.com/{DEMO_OWNER}/{repo}/issues/{number}",
        "body": body,
        "updatedAt": _ago(minutes),
        "state": "OPEN",
        "repository": {"nameWithOwner": f"{DEMO_OWNER}/{repo}"},
        "comments": {"nodes": []},
    }


def demo_nodes(*, question_answered: bool = False, arrival: bool = False) -> list[dict[str, Any]]:
    """Fake GitHub search results. Several are deliberately not actionable."""

    question_box = "x" if question_answered else " "
    nodes = [
        _pull(
            "homelab-functions",
            914,
            "Add the Waiting on me inbox",
            minutes=8,
            review="APPROVED",
            body=(
                "- [ ] **Joe: evidence** Check the inbox screenshots on phone and desktop. "
                "Why: they are the proof that the layout works before you merge. "
                "Action: https://github.com/feocco/homelab-functions/pull/914#evidence"
            ),
        ),
        _pull(
            "pirate-radio",
            925,
            "Serialize approve and skip decisions per article",
            minutes=180,
            state="MERGED",
            body=(
                "- [ ] **Joe: deploy** Roll out pirate-radio 21afae6 on macmini. "
                "Why: the image is published, and macmini still runs 8ac3d4f. "
                "Action: https://github.com/feocco/homelab-config/actions/workflows/deploy.yml"
            ),
        ),
        _pull(
            "homelab-config",
            941,
            "Route waiting.home.feocco.com",
            minutes=35,
            draft=True,
            body="\n".join(
                (
                    "- [ ] **Joe: secret** Add HOMELAB_FUNCTIONS__DASHBOARD_GH_APP_KEY as one base64 line. "
                    "Why: the inbox cannot read GitHub until the App key is set. "
                    "Action: https://github.com/feocco/homelab-config/settings/secrets/actions",
                    "- [ ] **Joe: dns** Keep waiting.home.feocco.com as a DNS-only record that points at the tailnet. "
                    "Why: the name becomes public, but only tailnet devices can reach it.",
                    "- [ ] **Joe: copy** Run the DNS apply on macmini. "
                    "Why: it writes the UniFi and Cloudflare records for the inbox. "
                    "Copy: `./scripts/dispatch-route-dns --mode apply --watch`",
                    "- [x] **Joe: copy** Render the env file. Why: done last week. Copy: `./scripts/render-service-env homelab-functions`",
                )
            ),
        ),
        _issue(
            "bedtime",
            37,
            "Bedtime reminder rules",
            minutes=90,
            body=(
                f"- [{question_box}] **Joe: question** Should the bedtime reminder skip Fridays? "
                "Why: the routine needs one rule before it ships."
            ),
        ),
        _pull("pirate-radio", 926, "Add extractor golden files", minutes=20, checks="FAILURE"),
        _pull("homelab-sre-agent", 58, "Summarize disk alerts before paging", minutes=5, checks="PENDING"),
        _pull(
            "laundry-monitor",
            22,
            "Debounce washer finish events",
            minutes=300,
            review="CHANGES_REQUESTED",
            comments=("- [ ] **Joe: approve** Merge this now. Why: agents cannot ask for merges.",),
        ),
    ]
    if arrival:
        nodes.insert(0, _pull("homelab-config", 943, "Pin the Caddy image digest", minutes=0))
    return nodes


class DemoWaitingSource:
    """Fake GitHub. The cycle: base, a pull request arrives, the question is answered, repeat."""

    owner = DEMO_OWNER

    def __init__(self, phase_every: int = 3) -> None:
        self.phase_every = phase_every
        self.fetches = 0

    async def fetch(self) -> Inbox:
        self.fetches += 1
        phase = (self.fetches // self.phase_every) % 3
        items: list[InboxItem] = []
        rejected = []
        for node in demo_nodes(arrival=phase >= 1, question_answered=phase == 2):
            found, refused = inbox_from_node(node, self.owner)
            items.extend(found)
            rejected.extend(refused)
        return Inbox(items, rejected)


class DemoCursorAgents:
    """Fake Cursor API that records calls."""

    def __init__(self) -> None:
        self.launches: list[dict[str, Any]] = []
        self.follow_ups: list[tuple[str, str]] = []

    async def launch(self, *, agent_id: str, name: str, prompt: str, item: InboxItem) -> str:
        self.launches.append({"agent_id": agent_id, "name": name, "prompt": prompt, "item": item.id})
        return f"https://cursor.com/agents/{agent_id}"

    async def follow_up(self, agent_id: str, prompt: str) -> str:
        self.follow_ups.append((agent_id, prompt))
        return "followup"


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

    def post_json(self, path: str, headers: dict[str, str], payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        status, _, body = self.call(
            "POST",
            path,
            headers={**headers, "Content-Type": "application/json"},
            body=json.dumps(payload).encode(),
        )
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            return status, {}

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


def item_problems(item: dict[str, Any]) -> list[str]:
    """Return what is wrong with one inbox item, or an empty list."""

    problems = []
    if item.get("category") not in {category.value for category in CATEGORY_ORDER}:
        problems.append("unknown category")
    for key in ("need", "why"):
        if not str(item.get(key) or "").strip():
            problems.append(f"missing {key}")
    source = item.get("source") or {}
    if not str(source.get("url") or "").startswith("https://github.com/") or not source.get("label"):
        problems.append("source must be a labeled GitHub link")
    action = item.get("action") or {}
    if not action.get("label"):
        problems.append("action needs a label")
    if action.get("type") == "copy":
        if item.get("category") != "copy" or not action.get("value"):
            problems.append("only copy items copy, and they need a value")
    elif action.get("type") == "open":
        if not str(action.get("url") or "").startswith("https://"):
            problems.append("open action needs an https URL")
    else:
        problems.append("action type must be open or copy")
    if item.get("category") == "approve_merge" and action.get("url") != source.get("url"):
        problems.append("approve/merge must only open the pull request on GitHub")
    return problems


def verify(base_url: str, token: str, *, exercise: bool = False, timeout: float = 10) -> dict[str, Any]:
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
    if not check("inbox sign-in sets a session cookie", status == 303 and cookie.startswith("waiting_session="), status):
        return {"ok": False, "url": base_url, "checks": checks}
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
    first = views["v1_json"]
    check(
        "HTML, SSE, snapshot JSON, and /v1/waiting are one document",
        all(view == first for view in views.values()),
        {name: view.get("version") for name, view in views.items()},
    )
    check("snapshot schema", first.get("schema") == SCHEMA, first.get("schema"))
    check(
        "exactly the seven inbox categories, in order",
        [(entry["id"], entry["label"]) for entry in first.get("categories", [])]
        == [(category.value, CATEGORY_LABELS[category]) for category in CATEGORY_ORDER],
        [entry.get("label") for entry in first.get("categories", [])],
    )
    items = first.get("items", [])
    problems = {item.get("id"): item_problems(item) for item in items if item_problems(item)}
    check("every item has a need, a why, one primary action, and a source link", not problems, problems or len(items))
    check("item ids are unique", len({item["id"] for item in items}) == len(items), len(items))

    status, _, _ = http.call("GET", "/waiting/snapshot", headers=bearer)
    check("bearer token does not open inbox routes", status == 401, status)
    status, _, _ = http.call("GET", "/v1/waiting", headers=session)
    check("session cookie does not open /v1", status == 401, status)
    target = next((item for item in items if item["category"] != "approve_merge"), None)
    if target is not None:
        status, _ = http.post_json("/waiting/handled", bearer, {"item_id": target["id"]})
        check("agents cannot mark Joe's items done", status == 401, status)

    if exercise:
        _exercise(http, bearer, {**session, "Origin": http.origin}, health, items, check)

    return {"ok": all(entry["ok"] for entry in checks), "url": base_url, "checks": checks}


def _exercise(http: _Http, bearer, browser, health, items, check) -> None:
    if not check("exercise only runs against the demo", health.get("waiting_demo") is True, health.get("waiting_demo")):
        return
    merge = next((item for item in items if item["category"] == "approve_merge"), None)
    target = next((item for item in items if item["category"] not in ("approve_merge", "question")), None)
    if not check("demo has an approve/merge item and another item", merge is not None and target is not None):
        return

    request_id = f"verify-{secrets.token_hex(6)}"
    first_payload = {"item_id": target["id"], "feedback": "Verify: first note.", "request_id": request_id}
    status, first = http.post_json("/v1/waiting/feedback", bearer, first_payload)
    check("first feedback launches one agent", status == 200 and first.get("delivery") == "launch", first)
    status, again = http.post_json("/v1/waiting/feedback", bearer, first_payload)
    check("resending the same request_id is a no-op", status == 200 and again.get("duplicate") is True, again)
    status, second = http.post_json(
        "/waiting/feedback",
        browser,
        {"item_id": target["id"], "feedback": "Verify: second note.", "request_id": f"{request_id}-2"},
    )
    agent_id = (first.get("agent") or {}).get("id")
    check(
        "later feedback goes to the same agent",
        status == 200 and second.get("delivery") in ("followup", "steer") and (second.get("agent") or {}).get("id") == agent_id,
        second,
    )

    status, refused = http.post_json("/waiting/handled", browser, {"item_id": merge["id"]})
    check("approve/merge cannot be cleared from the inbox", status == 409, refused.get("error"))
    status, done = http.post_json("/waiting/handled", browser, {"item_id": target["id"]})
    after = json.loads(http.call("GET", "/v1/waiting", headers=bearer)[2])
    check(
        "Joe marking an item done removes it from every view",
        status == 200 and target["id"] not in {item["id"] for item in after["items"]},
        done,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m homelab.waiting_cli", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Serve the inbox on fake data.")
    demo.add_argument("--port", type=int, default=8099)
    demo.add_argument("--token", default=DEMO_TOKEN)
    demo.add_argument("--refresh-seconds", type=int, default=5)
    demo.add_argument("--no-cursor", action="store_true", help="Run with feedback off, as without CURSOR_API_KEY.")
    check = commands.add_parser("verify", help="Check a running server and print a JSON report.")
    check.add_argument("--url", required=True)
    check.add_argument("--token", required=True, help="HOMELAB_FUNCTIONS_TOKEN of that server.")
    check.add_argument("--exercise", action="store_true", help="Send feedback and mark an item done. Demo only.")
    args = parser.parse_args(argv)

    if args.command == "demo":
        from aiohttp import web

        with tempfile.TemporaryDirectory(prefix="waiting-demo-") as data_dir:
            print(f"Waiting on me demo: http://127.0.0.1:{args.port}/waiting token={args.token}", file=sys.stderr)
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

    started = time.monotonic()
    report = verify(args.url, args.token, exercise=args.exercise)
    report["seconds"] = round(time.monotonic() - started, 2)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
