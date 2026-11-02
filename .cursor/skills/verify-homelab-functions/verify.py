#!/usr/bin/env python3
"""Launch an isolated local homelab-functions process and prove HTTP plus MCP."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import (
    ClientError,
    ClientSession,
    ClientTimeout,
    TCPConnector,
    WSMsgType,
    web,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
HELPER = Path(__file__).resolve().relative_to(REPO_ROOT)
EVIDENCE_ROOT = Path("/tmp/verify-homelab-functions")
NOTIFY_JOE_SERVICE = "notify.mobile_app_verify_joe"
NOTIFY_JESS_SERVICE = "notify.mobile_app_verify_jess"
HEALTH_TIMEOUT_SECONDS = 10.0
HTTP_TIMEOUT_SECONDS = 5.0
MCP_TIMEOUT_SECONDS = 10.0
BROKER_SPAWN_ATTEMPTS = 5
STDERR_TAIL_LINES = 8
STDERR_TAIL_BYTES = 2048
IMPORT_PROBE = (
    "import homelab, os, pathlib, sys;"
    "root = pathlib.Path(os.environ['PYTHONPATH']).resolve();"
    "here = pathlib.Path(homelab.__file__).resolve();"
    "sys.exit(0 if root in here.parents else 1)"
)


class CheckFailure(Exception):
    pass


@dataclass(frozen=True)
class Check:
    name: str
    expected: str
    drive: Callable[[Session], Awaitable[None]]


@dataclass
class Session:
    run_id: str
    repo_root: Path
    scratch: Path
    evidence_path: Path
    secrets: list[str]
    lines: list[str] = field(default_factory=list)
    processes: list[asyncio.subprocess.Process] = field(default_factory=list)
    log_files: list[Any] = field(default_factory=list)
    runners: list[web.AppRunner] = field(default_factory=list)
    http: ClientSession | None = None
    broker: asyncio.subprocess.Process | None = None
    broker_port: int | None = None
    ha_port: int | None = None
    webhook_port: int | None = None
    broker_token: str = ""
    pebble_token: str = ""
    webhook_key: str = ""
    ha_token: str = ""
    ha_calls: list[dict[str, Any]] = field(default_factory=list)
    webhook_posts: list[dict[str, Any]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    broker_stderr_path: Path | None = None
    mcp_stderr_path: Path | None = None

    @property
    def broker_url(self) -> str:
        if self.broker_port is None:
            raise CheckFailure("broker port is not bound")
        return f"http://127.0.0.1:{self.broker_port}"

    def emit(self, line: str) -> None:
        safe = redact(line, self.secrets)
        sys.stdout.write(safe + "\n")
        sys.stdout.flush()
        self.lines.append(safe)
        self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json_body: Any | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, str, bytes, Any]:
        if self.http is None:
            raise CheckFailure("HTTP client is not running")
        headers = dict(extra_headers or {})
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        url = require_loopback(self.broker_url + path)
        async with self.http.request(method, url, json=json_body, headers=headers) as response:
            body = await response.read()
            content_type = response.content_type
            parsed: Any = None
            if content_type == "application/json":
                parsed = json.loads(body.decode("utf-8"))
            return response.status, content_type, body, parsed


def redact(text: str, secrets_to_hide: Sequence[str]) -> str:
    redacted = text
    for secret in secrets_to_hide:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    redacted = re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1[redacted]", redacted)
    redacted = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.=]+", r"\1[redacted]", redacted)
    return redacted


def require_loopback(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "ws"}:
        raise CheckFailure("refusing a URL that is not http or ws on loopback")
    if parsed.hostname != "127.0.0.1":
        raise CheckFailure("refusing a non-loopback URL")
    return url


def require_unauthorized(status: int, payload: Any, *, what: str) -> None:
    if status != 401:
        raise CheckFailure(f"{what} returned {status}")
    if not isinstance(payload, dict) or payload.get("error", {}).get("code") != "unauthorized":
        raise CheckFailure(f"{what} did not use error code unauthorized")


def require_verify_stub(payload: Any, *, what: str) -> None:
    if not isinstance(payload, dict) or payload.get("schema") != "homelab-service-catalog.v1":
        raise CheckFailure(f"{what} catalog payload mismatch")
    services = payload.get("services")
    if not isinstance(services, list) or not services or services[0].get("service") != "verify-stub":
        raise CheckFailure(f"{what} did not return the disposable verify-stub service")


def new_secret() -> str:
    value = secrets.token_urlsafe(24)
    if len(value) < 16:
        raise RuntimeError("generated secret was too short")
    return value


def allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def bound_port(runner: web.AppRunner) -> int:
    addresses = runner.addresses
    if not addresses:
        raise CheckFailure("loopback mock bound no address")
    host, port = addresses[0][0], addresses[0][1]
    if host not in {"127.0.0.1", "::ffff:127.0.0.1"}:
        raise CheckFailure("loopback mock bound a non-loopback host")
    return int(port)


def child_env(session: Session, extra: dict[str, str] | None = None) -> dict[str, str]:
    if session.ha_port is None or session.webhook_port is None or session.broker_port is None:
        raise CheckFailure("ports are not bound")
    pythonpath = str(session.repo_root)
    venv_bin = str(session.repo_root / ".venv" / "bin")
    env = {
        "PATH": venv_bin,
        "HOME": str(session.scratch),
        "TMPDIR": str(session.scratch / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": pythonpath,
        "VIRTUAL_ENV": str(session.repo_root / ".venv"),
        "HA_URL": require_loopback(f"http://127.0.0.1:{session.ha_port}"),
        "HA_LONG_LIVED_TOKEN": session.ha_token,
        "HA_NOTIFY_JOE_SERVICE": NOTIFY_JOE_SERVICE,
        "HA_NOTIFY_JESS_SERVICE": NOTIFY_JESS_SERVICE,
        "HOMELAB_FUNCTIONS_TOKEN": session.broker_token,
        "HOMELAB_FUNCTIONS_URL": require_loopback(session.broker_url),
        "SERVICE_HOST": "127.0.0.1",
        "SERVICE_PORT": str(session.broker_port),
        "REQUEST_TIMEOUT_SECONDS": "5",
        "LOG_LEVEL": "WARNING",
        "NOTIFICATION_LEDGER_PATH": str(session.scratch / "notifications.sqlite3"),
        "NOTIFICATION_ACTION_RECORDER_ENABLED": "false",
        "HOMELAB_CATALOG_PATH": str(session.scratch / "service-catalog.json"),
        "HOMELAB_SMOKE_SIGNAL_TARGETS_PATH": str(session.scratch / "smoke-signal-targets.json"),
        "PEBBLE_RING_TOKEN": session.pebble_token,
        "GROK_PEBBLE_WEBHOOK_URL": require_loopback(
            f"http://127.0.0.1:{session.webhook_port}/webhook"
        ),
        "GROK_PEBBLE_WEBHOOK_KEY": session.webhook_key,
        "PEBBLE_LEDGER_PATH": str(session.scratch / "pebble-events.sqlite3"),
        "PEBBLE_AUDIO_DIR": str(session.scratch / "pebble-audio"),
    }
    if extra:
        env.update(extra)
    if env.get("PYTHONPATH") != str(session.repo_root):
        raise CheckFailure("child PYTHONPATH is not this checkout")
    return env


def seed_scratch(session: Session) -> None:
    (session.scratch / "tmp").mkdir(parents=True, exist_ok=True)
    (session.scratch / "pebble-audio").mkdir(parents=True, exist_ok=True)
    (session.scratch / "service-catalog.json").write_text(
        json.dumps(
            {
                "schema": "homelab-service-catalog.v1",
                "services": [{"service": "verify-stub", "host": "127.0.0.1", "enabled": True}],
            }
        ),
        encoding="utf-8",
    )
    (session.scratch / "smoke-signal-targets.json").write_text(
        json.dumps(
            {
                "schema": "homelab-smoke-signal-targets.v1",
                "targets": [{"name": "Verify stub", "url": "http://127.0.0.1/health"}],
            }
        ),
        encoding="utf-8",
    )


def emit_stderr_tail(session: Session, label: str, path: Path | None) -> None:
    if path is None:
        return
    for handle in session.log_files:
        try:
            handle.flush()
        except OSError:
            pass
    try:
        raw = path.read_bytes()
    except OSError:
        return
    if not raw:
        return
    chunk = raw[-STDERR_TAIL_BYTES:]
    text = redact(chunk.decode("utf-8", errors="replace"), session.secrets)
    lines = [line for line in text.splitlines() if line.strip()][-STDERR_TAIL_LINES:]
    for line in lines:
        session.emit(f"log {label}-stderr {line}")


async def start_site(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app, handle_signals=False, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, bound_port(runner)


def ha_app(session: Session) -> web.Application:
    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2024.1.0"})
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            payload = json.loads(message.data)
            if payload.get("type") == "auth":
                token = payload.get("access_token")
                if not isinstance(token, str) or not secrets.compare_digest(token, session.ha_token):
                    await ws.send_json({"type": "auth_invalid", "message": "invalid"})
                    break
                await ws.send_json({"type": "auth_ok", "ha_version": "2024.1.0"})
                continue
            if payload.get("type") == "call_service":
                captured = {
                    "id": payload.get("id"),
                    "domain": payload.get("domain"),
                    "service": payload.get("service"),
                    "service_data": payload.get("service_data"),
                }
                session.ha_calls.append(captured)
                await ws.send_json(
                    {
                        "id": payload.get("id"),
                        "type": "result",
                        "success": True,
                        "result": {"context": {"id": f"verify-ha-{session.run_id}"}},
                    }
                )
        return ws

    app = web.Application()
    app.router.add_get("/api/websocket", websocket_handler)
    return app


def webhook_app(session: Session) -> web.Application:
    async def handle_webhook(request: web.Request) -> web.Response:
        body = await request.read()
        content_type = request.content_type
        parsed: Any = None
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        has_audio_field = isinstance(parsed, dict) and "audio" in parsed
        session.webhook_posts.append(
            {
                "content_type": content_type,
                "parsed": parsed,
                "byte_length": len(body),
                "has_audio_field": has_audio_field,
            }
        )
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/webhook", handle_webhook)
    return app


async def wait_for_health(session: Session) -> dict[str, Any]:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    last_error = "broker did not answer GET /health"
    while time.monotonic() < deadline:
        if session.broker is not None and session.broker.returncode is not None:
            raise CheckFailure("broker process exited before becoming healthy")
        try:
            status, _, _, parsed = await session.request("GET", "/health")
            if status == 200 and isinstance(parsed, dict) and parsed.get("status") == "ok":
                return parsed
            last_error = f"GET /health status {status}"
        except (CheckFailure, ClientError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = redact(str(exc), session.secrets)
        await asyncio.sleep(0.05)
    raise CheckFailure(last_error)


async def assert_import_from_checkout(session: Session, env: dict[str, str]) -> None:
    if env.get("PYTHONPATH") != str(session.repo_root):
        raise CheckFailure("child PYTHONPATH is not this checkout")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        IMPORT_PROBE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(session.scratch),
        env=env,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        detail = redact(err.decode("utf-8", errors="replace")[-512:], session.secrets).strip()
        raise CheckFailure(f"child would import homelab from outside this checkout {detail}")


async def spawn_broker(session: Session) -> None:
    python = sys.executable
    last_error = "broker failed to start"
    for _ in range(BROKER_SPAWN_ATTEMPTS):
        session.broker_port = allocate_loopback_port()
        env = child_env(session)
        await assert_import_from_checkout(session, env)
        stdout_path = session.scratch / f"broker-{session.broker_port}.stdout"
        stderr_path = session.scratch / f"broker-{session.broker_port}.stderr"
        session.broker_stderr_path = stderr_path
        stdout_fh = stdout_path.open("wb")
        stderr_fh = stderr_path.open("wb")
        session.log_files.extend((stdout_fh, stderr_fh))
        proc = await asyncio.create_subprocess_exec(
            python,
            "-m",
            "homelab.server",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=stdout_fh,
            stderr=stderr_fh,
            cwd=str(session.scratch),
            env=env,
            start_new_session=True,
        )
        session.processes.append(proc)
        session.broker = proc
        session.emit(
            f"lifecycle launch broker pid={proc.pid} cmd=python -m homelab.server "
            f"url={session.broker_url}"
        )
        try:
            await wait_for_health(session)
            return
        except CheckFailure as exc:
            last_error = str(exc)
            emit_stderr_tail(session, "broker", stderr_path)
            await terminate_process(proc)
    raise CheckFailure(last_error)


async def terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    await proc.wait()


async def launch(session: Session) -> None:
    seed_scratch(session)
    session.http = ClientSession(
        timeout=ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
        connector=TCPConnector(family=socket.AF_INET, force_close=True),
        trust_env=False,
    )
    ha_runner, session.ha_port = await start_site(ha_app(session))
    session.runners.append(ha_runner)
    session.emit(f"lifecycle launch ha-mock url=http://127.0.0.1:{session.ha_port}/api/websocket")
    webhook_runner, session.webhook_port = await start_site(webhook_app(session))
    session.runners.append(webhook_runner)
    session.emit(
        f"lifecycle launch pebble-webhook url=http://127.0.0.1:{session.webhook_port}/webhook"
    )
    await spawn_broker(session)


async def doctor(session: Session) -> None:
    if session.broker is None or session.broker.returncode is not None:
        raise CheckFailure("broker process is not running")
    try:
        os.kill(session.broker.pid, 0)
    except OSError as exc:
        raise CheckFailure("broker pid is not alive") from exc
    payload = await wait_for_health(session)
    if payload.get("service") != "homelab-functions":
        raise CheckFailure("doctor saw a health payload for the wrong service")
    if payload.get("notification_action_recorder_enabled") is not False:
        raise CheckFailure("doctor expected the notification action recorder to be disabled")
    if payload.get("token_configured") is not True:
        raise CheckFailure("doctor expected token_configured true")
    catalog_status, _, _, catalog = await session.request(
        "GET",
        "/v1/catalog/services",
        token=session.broker_token,
    )
    if catalog_status != 200:
        raise CheckFailure(f"doctor catalog status {catalog_status}")
    require_verify_stub(catalog, what="doctor")
    session.emit(
        "doctor health status=ok service=homelab-functions "
        "recorder_enabled=false token_configured=true catalog=verify-stub "
        f"pid={session.broker.pid} url={session.broker_url}"
    )


async def drive_health(session: Session) -> None:
    status, _, _, parsed = await session.request("GET", "/health")
    if status != 200:
        raise CheckFailure(f"GET /health status {status}")
    if not isinstance(parsed, dict):
        raise CheckFailure("GET /health body is not JSON")
    if parsed.get("status") != "ok" or parsed.get("service") != "homelab-functions":
        raise CheckFailure("GET /health payload mismatch")
    if parsed.get("notification_action_recorder_enabled") is not False:
        raise CheckFailure("GET /health recorder was enabled")
    if parsed.get("ha_url_configured") is not True:
        raise CheckFailure("GET /health ha_url_configured was not true")
    session.emit("check health detail status=ok recorder_enabled=false")


async def drive_docs(session: Session) -> None:
    status, content_type, body, _ = await session.request("GET", "/docs")
    if status != 200:
        raise CheckFailure(f"GET /docs status {status}")
    if "html" not in content_type:
        raise CheckFailure(f"GET /docs content type {content_type}")
    text = body.decode("utf-8", errors="replace")
    required = ("homelab-functions", "/v1/notify/joe", "/health", "notify_joe")
    missing = [item for item in required if item not in text]
    if missing:
        raise CheckFailure(f"GET /docs missing {', '.join(missing)}")
    session.emit(f"check docs detail content_type={content_type} bytes={len(body)}")


async def drive_bearer_unauth(session: Session) -> None:
    missing_status, _, _, missing_body = await session.request("GET", "/v1/catalog/services")
    wrong_status, _, _, wrong_body = await session.request(
        "GET",
        "/v1/catalog/services",
        token="wrong-token",
    )
    ring_status, _, _, ring_body = await session.request(
        "GET",
        "/v1/catalog/services",
        token=session.pebble_token,
    )
    require_unauthorized(missing_status, missing_body, what="missing bearer")
    require_unauthorized(wrong_status, wrong_body, what="wrong bearer")
    require_unauthorized(ring_status, ring_body, what="ring token on catalog")
    session.emit("check bearer-unauth detail missing=401 wrong=401 ring=401")


async def drive_bearer_ok(session: Session) -> None:
    status, _, _, payload = await session.request(
        "GET",
        "/v1/catalog/services",
        token=session.broker_token,
    )
    if status != 200:
        raise CheckFailure(f"valid bearer returned {status}")
    require_verify_stub(payload, what="bearer-ok")
    session.emit("check bearer-ok detail status=200 catalog=verify-stub")


async def drive_notify_joe(session: Session) -> None:
    before = len(session.ha_calls)
    status, _, _, payload = await session.request(
        "POST",
        "/v1/notify/joe",
        token=session.broker_token,
        json_body={"title": "verify-http", "message": "isolated notify check"},
    )
    if status != 200:
        raise CheckFailure(f"POST /v1/notify/joe status {status}")
    if not isinstance(payload, dict) or payload.get("status") != "sent":
        raise CheckFailure("notify_joe payload was not sent")
    if not payload.get("notification_id"):
        raise CheckFailure("notify_joe did not return a notification_id")
    captured = session.ha_calls[before:]
    if not captured:
        raise CheckFailure("HA mock captured no call_service")
    call = captured[-1]
    if call.get("domain") != "notify" or call.get("service") != "mobile_app_verify_joe":
        raise CheckFailure("HA mock captured the wrong service")
    service_data = call.get("service_data")
    if not isinstance(service_data, dict) or service_data.get("title") != "verify-http":
        raise CheckFailure("HA mock captured the wrong notification title")
    session.emit(
        "check notify-joe detail status=sent "
        "ha_domain=notify ha_service=mobile_app_verify_joe title=verify-http"
    )


async def mcp_rpc(
    proc: asyncio.subprocess.Process,
    message: dict[str, Any],
    *,
    secrets_to_hide: Sequence[str],
) -> dict[str, Any]:
    if proc.stdin is None or proc.stdout is None:
        raise CheckFailure("MCP stdio pipes are missing")
    raw = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
    proc.stdin.write(raw)
    await proc.stdin.drain()
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=MCP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise CheckFailure("timed out waiting for MCP stdout") from exc
    if not line:
        raise CheckFailure("MCP stdout closed")
    payload = json.loads(line.decode("utf-8"))
    if not isinstance(payload, dict):
        raise CheckFailure("MCP returned a non-object JSON line")
    return json.loads(redact(json.dumps(payload), secrets_to_hide))


async def drive_mcp_stdio(session: Session) -> None:
    env = child_env(session)
    await assert_import_from_checkout(session, env)
    stderr_path = session.scratch / "mcp.stderr"
    session.mcp_stderr_path = stderr_path
    stderr_fh = stderr_path.open("wb")
    session.log_files.append(stderr_fh)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "homelab.mcp_server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr_fh,
        cwd=str(session.scratch),
        env=env,
        start_new_session=True,
    )
    session.processes.append(proc)
    session.emit(f"lifecycle launch mcp pid={proc.pid} cmd=python -m homelab.mcp_server")
    try:
        initialized = await mcp_rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "verify-homelab-functions", "version": "0"},
                },
            },
            secrets_to_hide=session.secrets,
        )
        result = initialized.get("result")
        if not isinstance(result, dict):
            raise CheckFailure("initialize returned no result")
        if result.get("protocolVersion") != "2024-11-05":
            raise CheckFailure("initialize protocol version mismatch")
        info = result.get("serverInfo")
        if not isinstance(info, dict) or info.get("name") != "homelab-functions":
            raise CheckFailure("initialize serverInfo mismatch")
        listed = await mcp_rpc(
            proc,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            secrets_to_hide=session.secrets,
        )
        tools = listed.get("result", {}).get("tools")
        if not isinstance(tools, list):
            raise CheckFailure("tools/list returned no tools")
        names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
        if "notify_joe" not in names:
            raise CheckFailure("tools/list omitted notify_joe")
        if "relay_pebble" in names:
            raise CheckFailure("tools/list included HTTP-only relay_pebble")
        before = len(session.ha_calls)
        called = await mcp_rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "notify_joe",
                    "arguments": {
                        "title": "verify-mcp",
                        "message": "mcp tools/call",
                    },
                },
            },
            secrets_to_hide=session.secrets,
        )
        call_result = called.get("result")
        if not isinstance(call_result, dict) or call_result.get("isError") is not False:
            raise CheckFailure("tools/call notify_joe returned an error")
        content = call_result.get("content")
        if not isinstance(content, list) or not content:
            raise CheckFailure("tools/call notify_joe returned no content")
        text = content[0].get("text") if isinstance(content[0], dict) else None
        if not isinstance(text, str):
            raise CheckFailure("tools/call notify_joe content was not text")
        payload = json.loads(text)
        if payload.get("status") != "sent":
            raise CheckFailure("tools/call notify_joe did not send")
        captured = [
            call
            for call in session.ha_calls[before:]
            if call.get("service_data", {}).get("title") == "verify-mcp"
        ]
        if not captured:
            raise CheckFailure("HA mock did not capture the MCP notify_joe call_service")
        session.emit(
            "check mcp-stdio detail initialize=ok tools_list=ok "
            "tools_call=notify_joe ha_title=verify-mcp"
        )
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
            try:
                await proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        await terminate_process(proc)


async def drive_pebble_relay(session: Session) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    event_id = f"verify-{session.run_id}"
    body = {
        "event_id": event_id,
        "event_type": "single-click-hold",
        "timestamp": timestamp,
        "transcription": "Turn the lights off",
        "client": "ring",
    }
    broker_status, _, _, broker_payload = await session.request(
        "POST",
        "/v1/pebble",
        token=session.broker_token,
        json_body=body,
    )
    require_unauthorized(broker_status, broker_payload, what="broker token on /v1/pebble")
    before = len(session.webhook_posts)
    status, _, _, payload = await session.request(
        "POST",
        "/v1/pebble",
        token=session.pebble_token,
        json_body=body,
    )
    if status != 200:
        raise CheckFailure(f"POST /v1/pebble status {status}")
    if not isinstance(payload, dict) or payload.get("status") != "forwarded":
        raise CheckFailure("pebble relay payload was not forwarded")
    posts = session.webhook_posts[before:]
    if len(posts) != 1:
        raise CheckFailure(f"webhook captured {len(posts)} posts")
    post = posts[0]
    if post.get("content_type") != "application/json":
        raise CheckFailure("webhook content type was not application/json")
    parsed = post.get("parsed")
    if not isinstance(parsed, dict):
        raise CheckFailure("webhook body was not JSON")
    if post.get("has_audio_field") or "audio" in parsed:
        raise CheckFailure("webhook JSON included an audio field")
    if parsed.get("event_id") != event_id:
        raise CheckFailure("webhook JSON event_id mismatch")
    if parsed.get("transcript") != "Turn the lights off":
        raise CheckFailure("webhook JSON transcript mismatch")
    keys = ",".join(sorted(parsed))
    session.emit(
        "check pebble-relay detail broker=401 status=forwarded "
        f"webhook_content_type=application/json webhook_has_audio_field=false keys={keys}"
    )


CHECKS: tuple[Check, ...] = (
    Check(
        "health",
        "GET /health returns 200 with status ok and service homelab-functions",
        drive_health,
    ),
    Check(
        "docs",
        "GET /docs returns 200 HTML that names homelab-functions and /v1/notify/joe",
        drive_docs,
    ),
    Check(
        "bearer-unauth",
        "GET /v1/catalog/services returns 401 without a bearer, with a wrong bearer, and with the ring token",
        drive_bearer_unauth,
    ),
    Check(
        "bearer-ok",
        "protected GET /v1/catalog/services returns 200 with the ephemeral bearer",
        drive_bearer_ok,
    ),
    Check(
        "notify-joe",
        "POST /v1/notify/joe returns 200 sent and the HA mock captured call_service",
        drive_notify_joe,
    ),
    Check(
        "mcp-stdio",
        "python -m homelab.mcp_server initialize, tools/list, and tools/call succeed",
        drive_mcp_stdio,
    ),
    Check(
        "pebble-relay",
        "POST /v1/pebble returns 401 with the broker token, then forwards JSON with the ring token and no audio",
        drive_pebble_relay,
    ),
)


async def cleanup(session: Session) -> None:
    session.emit("lifecycle cleanup start")
    for proc in reversed(session.processes):
        await terminate_process(proc)
    alive = [proc.pid for proc in session.processes if proc.returncode is None]
    if alive:
        session.emit(f"lifecycle cleanup still_running pids={','.join(str(pid) for pid in alive)}")
        raise CheckFailure("cleanup left captured processes running")
    for handle in session.log_files:
        try:
            handle.close()
        except OSError:
            pass
    session.log_files.clear()
    for runner in reversed(session.runners):
        await runner.cleanup()
    session.runners.clear()
    if session.http is not None:
        await session.http.close()
        session.http = None
    if session.scratch.exists():
        shutil.rmtree(session.scratch)
    session.emit("lifecycle cleanup complete scratch_removed=true")


def evidence_exists(session: Session) -> None:
    if not session.evidence_path.is_file():
        raise CheckFailure("evidence file is missing after cleanup")
    body = session.evidence_path.read_text(encoding="utf-8")
    if "lifecycle cleanup complete" not in body:
        raise CheckFailure("evidence file lost the cleanup record")
    session.emit(f"lifecycle evidence exists=true path={session.evidence_path}")


async def async_main(session: Session) -> int:
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    stopping = False

    def on_sigterm() -> None:
        if stopping:
            return
        if main_task is not None and not main_task.done():
            main_task.cancel()

    loop.add_signal_handler(signal.SIGTERM, on_sigterm)
    try:
        try:
            await launch(session)
            await doctor(session)
            for check in CHECKS:
                try:
                    await check.drive(session)
                    session.emit(f"check {check.name} PASS")
                except CheckFailure as exc:
                    session.failed.append(check.name)
                    session.emit(
                        f"check {check.name} FAIL {exc} expected={check.expected}"
                    )
                    if check.name == "mcp-stdio":
                        emit_stderr_tail(session, "mcp", session.mcp_stderr_path)
        except asyncio.CancelledError:
            session.failed.append("interrupted")
            session.emit("lifecycle FAIL interrupted by SIGTERM")
        except CheckFailure as exc:
            session.failed.append("lifecycle")
            session.emit(f"lifecycle FAIL {exc}")
    finally:
        stopping = True
        try:
            await cleanup(session)
        except CheckFailure as exc:
            session.failed.append("cleanup")
            session.emit(f"lifecycle cleanup FAIL {exc}")
        try:
            evidence_exists(session)
        except CheckFailure as exc:
            session.failed.append("cleanup")
            session.emit(f"lifecycle cleanup FAIL {exc}")
        loop.remove_signal_handler(signal.SIGTERM)
    if session.failed:
        session.emit(f"result FAIL checks={','.join(session.failed)}")
        return 1
    session.emit("result PASS")
    return 0


def main() -> int:
    if sys.argv[1:]:
        sys.stderr.write(f"usage: {sys.executable} {HELPER}\n")
        return 2
    if not (REPO_ROOT / ".venv" / "bin" / "python").exists():
        sys.stderr.write("missing .venv. Run python3 -m venv .venv and install .[test].\n")
        return 2
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
    scratch = Path(tempfile.mkdtemp(prefix=f"verify-homelab-functions-{run_id}-"))
    session = Session(
        run_id=run_id,
        repo_root=REPO_ROOT,
        scratch=scratch,
        evidence_path=EVIDENCE_ROOT / run_id / "evidence.txt",
        secrets=[],
    )
    session.broker_token = new_secret()
    session.pebble_token = new_secret()
    session.webhook_key = new_secret()
    session.ha_token = new_secret()
    session.secrets.extend(
        [session.broker_token, session.pebble_token, session.webhook_key, session.ha_token]
    )
    session.emit(
        f"lifecycle start run_id={run_id} evidence={session.evidence_path} "
        f"scratch={session.scratch}"
    )
    try:
        return asyncio.run(async_main(session))
    except Exception as exc:
        session.emit(f"lifecycle crash {type(exc).__name__}: {exc}")
        session.emit("result FAIL checks=crash")
        return 1


if __name__ == "__main__":
    sys.exit(main())
