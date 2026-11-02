"""Browser routes for the Waiting on me inbox.

The browser signs in once with `HOMELAB_FUNCTIONS_TOKEN` and keeps only an
HMAC-signed session cookie. That cookie opens the dashboard routes in
`DASHBOARD_ROUTES` and nothing under `/v1`. Rotating the token ends every
session.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from functools import cache
from html import escape
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from .access import DASHBOARD_ROUTES
from .waiting import FeedbackError, FeedbackService, WaitingBoard, validate_feedback, validate_item_id


SESSION_COOKIE = "waiting_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600
HEARTBEAT_SECONDS = 15.0
SIGN_IN_FAILURE_DELAY_SECONDS = 1.0


@dataclass(frozen=True)
class DashboardSettings:
    token: str
    public_url: str

    @property
    def hostname(self) -> str:
        return (urlsplit(self.public_url).hostname or "").lower()

    @property
    def secure_cookie(self) -> bool:
        return self.public_url.startswith("https://")


BOARD_KEY = web.AppKey("waiting_board", WaitingBoard)
FEEDBACK_KEY = web.AppKey("waiting_feedback", FeedbackService)
SETTINGS_KEY = web.AppKey("waiting_settings", DashboardSettings)


def session_cookie_value(token: str, expires_at: int) -> str:
    return f"{expires_at}.{_session_signature(token, expires_at)}"


def session_is_valid(token: str, value: str, now: float) -> bool:
    expires, _, signature = value.partition(".")
    if not token or not expires.isdigit() or int(expires) < now:
        return False
    return hmac.compare_digest(signature, _session_signature(token, int(expires)))


def _session_signature(token: str, expires_at: int) -> str:
    key = hmac.new(token.encode(), b"waiting-dashboard-session-v1", hashlib.sha256).digest()
    digest = hmac.new(key, str(expires_at).encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _signed_in(request: web.Request) -> bool:
    settings = request.app[SETTINGS_KEY]
    return session_is_valid(settings.token, request.cookies.get(SESSION_COOKIE, ""), time.time())


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    return bool(origin) and (urlsplit(origin).netloc or "").lower() == request.host.lower()


def _json_error(status: int, code: str, message: str, detail: str | None = None) -> web.Response:
    error = {"code": code, "message": message}
    if detail:
        error["detail"] = detail
    return web.json_response({"error": error}, status=status, headers={"Cache-Control": "no-store"})


def _unauthorized() -> web.Response:
    return _json_error(401, "unauthorized", "Sign in at /waiting first")


async def root(request: web.Request) -> web.StreamResponse:
    host = (urlsplit(f"//{request.host}").hostname or "").lower()
    if not host or host != request.app[SETTINGS_KEY].hostname:
        raise web.HTTPNotFound()
    raise web.HTTPFound("/waiting")


async def page(request: web.Request) -> web.Response:
    if not _signed_in(request):
        return _html(sign_in_html(error=""))
    return _html(dashboard_html(request.app[BOARD_KEY].payload()))


async def sign_in(request: web.Request) -> web.Response:
    if not _same_origin(request):
        return _json_error(403, "cross_origin", "Sign-in must come from the dashboard page")
    form = await request.post()
    presented = form.get("token")
    settings = request.app[SETTINGS_KEY]
    presented_digest = hashlib.sha256(str(presented or "").encode()).digest()
    expected_digest = hashlib.sha256(settings.token.encode()).digest()
    if not settings.token or not hmac.compare_digest(presented_digest, expected_digest):
        await asyncio.sleep(SIGN_IN_FAILURE_DELAY_SECONDS)
        return _html(sign_in_html(error="That token did not match."), status=401)
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    response = web.HTTPSeeOther("/waiting")
    response.set_cookie(
        SESSION_COOKIE,
        session_cookie_value(settings.token, expires_at),
        max_age=SESSION_TTL_SECONDS,
        path="/waiting",
        httponly=True,
        secure=settings.secure_cookie,
        samesite="Strict",
    )
    raise response


async def sign_out(request: web.Request) -> web.Response:
    if not _same_origin(request):
        return _json_error(403, "cross_origin", "Sign-out must come from the dashboard page")
    response = web.HTTPSeeOther("/waiting")
    response.del_cookie(SESSION_COOKIE, path="/waiting")
    raise response


async def snapshot(request: web.Request) -> web.Response:
    if not _signed_in(request):
        return _unauthorized()
    return web.json_response(request.app[BOARD_KEY].payload(), headers={"Cache-Control": "no-store"})


async def events(request: web.Request) -> web.StreamResponse:
    """Stream `snapshot` events, with a `ping` event when nothing changed for a while."""

    if not _signed_in(request):
        return _unauthorized()
    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-store, no-transform",
            "X-Accel-Buffering": "no",
        }
    )
    await response.prepare(request)
    board = request.app[BOARD_KEY]
    try:
        await response.write(b"retry: 5000\n\n")
        with board.subscription() as queue:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    await response.write(b"event: ping\ndata: {}\n\n")
                    continue
                if payload is None:
                    break
                await response.write(sse_event(payload))
    except ConnectionResetError:
        pass
    return response


def sse_event(payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {payload['version']}\nevent: snapshot\ndata: {data}\n\n".encode("utf-8")


async def feedback(request: web.Request) -> web.Response:
    if not _signed_in(request):
        return _unauthorized()
    if not _same_origin(request):
        return _json_error(403, "cross_origin", "Feedback must come from the dashboard page")
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _json_error(400, "invalid_request", "Request body must be JSON")
    return await submit_feedback(request.app[FEEDBACK_KEY], payload)


async def handled(request: web.Request) -> web.Response:
    """Joe marks an item done. Session only: agents cannot clear Joe's inbox."""

    if not _signed_in(request):
        return _unauthorized()
    if not _same_origin(request):
        return _json_error(403, "cross_origin", "Marking an item done must come from the inbox page")
    try:
        payload = await request.json()
        result = request.app[BOARD_KEY].mark_handled(validate_item_id(payload))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _json_error(400, "invalid_request", "Request body must be JSON")
    except FeedbackError as exc:
        return _json_error(exc.status.value, exc.code, str(exc), exc.detail)
    return web.json_response(result)


async def submit_feedback(service: FeedbackService, payload: Any) -> web.Response:
    """Shared by the dashboard route and `send_waiting_feedback`."""

    try:
        item_id, text, request_id = validate_feedback(payload)
        result = await service.submit(item_id, text, request_id)
    except FeedbackError as exc:
        return _json_error(exc.status.value, exc.code, str(exc), exc.detail)
    return web.json_response(result)


def register_dashboard_routes(app: web.Application) -> None:
    handlers = {
        ("GET", "/"): root,
        ("GET", "/waiting"): page,
        ("POST", "/waiting/session"): sign_in,
        ("POST", "/waiting/sign-out"): sign_out,
        ("GET", "/waiting/events"): events,
        ("GET", "/waiting/snapshot"): snapshot,
        ("POST", "/waiting/feedback"): feedback,
        ("POST", "/waiting/handled"): handled,
    }
    declared = {(route.method, route.path) for route in DASHBOARD_ROUTES}
    if set(handlers) != declared:
        raise RuntimeError("Dashboard handlers do not match DASHBOARD_ROUTES")
    for route in DASHBOARD_ROUTES:
        app.router.add_route(route.method, route.path, handlers[(route.method, route.path)])


@cache
def _asset(name: str) -> str:
    return resources.files("homelab").joinpath("static", name).read_text(encoding="utf-8")


def _sha256(source: str) -> str:
    return base64.b64encode(hashlib.sha256(source.encode("utf-8")).digest()).decode()


@cache
def page_headers() -> dict[str, str]:
    policy = "; ".join(
        (
            "default-src 'none'",
            f"script-src 'sha256-{_sha256(_asset('waiting.js'))}'",
            f"style-src 'sha256-{_sha256(_asset('waiting.css'))}'",
            "connect-src 'self'",
            "img-src 'self' data:",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
        )
    )
    return {
        "Content-Security-Policy": policy,
        "Referrer-Policy": "same-origin",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }


def _html(body: str, status: int = 200) -> web.Response:
    return web.Response(text=body, status=status, content_type="text/html", headers=page_headers())


def embedded_json(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _document(title: str, body: str, *, script: bool) -> str:
    script_tag = f"<script>{_asset('waiting.js')}</script>" if script else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<link rel="icon" href="data:,">'
        f"<title>{escape(title)}</title><style>{_asset('waiting.css')}</style></head>"
        f"<body>{body}{script_tag}</body></html>"
    )


def sign_in_html(*, error: str) -> str:
    message = f'<p class="form-error" role="alert">{escape(error)}</p>' if error else ""
    body = f"""
<main class="sign-in">
  <h1>Waiting on me</h1>
  <p>Paste <code>HOMELAB_FUNCTIONS_TOKEN</code> to sign in. The browser keeps a 30-day session cookie, not the token.</p>
  {message}
  <form method="post" action="/waiting/session">
    <label for="token">Token</label>
    <input id="token" name="token" type="password" autocomplete="current-password" required autofocus>
    <button class="button primary" type="submit">Sign in</button>
  </form>
</main>"""
    return _document("Sign in · Waiting on me", body, script=False)


def dashboard_html(payload: dict[str, Any]) -> str:
    body = f"""
<header class="top">
  <div class="top-inner">
    <div class="brand">
      <h1>Waiting on me</h1>
      <span class="chip demo" id="demo" hidden>Demo data</span>
    </div>
    <div class="status">
      <span class="live" id="live" data-mode="connecting" role="status" aria-live="polite">
        <span class="dot" aria-hidden="true"></span><span class="live-label">Connecting</span>
      </span>
      <time class="updated" id="updated"></time>
    </div>
    <form class="sign-out" method="post" action="/waiting/sign-out">
      <button class="link-button" type="submit">Sign out</button>
    </form>
  </div>
</header>
<main class="board">
  <p class="summary" id="summary" aria-live="polite"></p>
  <div class="notices" id="notices"></div>
  <div id="sections"></div>
  <p class="empty" id="empty" hidden>Nothing needs you right now.</p>
  <p class="policy" id="policy"></p>
</main>
<template id="section-template">
  <section class="section">
    <h2><span class="section-label"></span> <span class="count"></span></h2>
    <ol class="items"></ol>
  </section>
</template>
<template id="item-template">
  <li class="item">
    <div class="item-body">
      <p class="need"></p>
      <p class="why"></p>
      <div class="copy-box" hidden><code class="copy-value"></code></div>
      <p class="meta">
        <a class="source" target="_blank" rel="noopener noreferrer"></a>
        <span class="clears" hidden>Leaves the inbox when merged or closed on GitHub</span>
        <span class="chip new-chip">New</span>
      </p>
    </div>
    <div class="item-actions">
      <a class="button primary action-open" target="_blank" rel="noopener noreferrer" hidden></a>
      <button class="button primary action-copy" type="button" hidden></button>
      <div class="secondary">
        <button class="link-button done" type="button">Done</button>
        <button class="link-button feedback-toggle" type="button" aria-expanded="false">Send feedback</button>
      </div>
    </div>
    <form class="feedback-form" hidden>
      <label class="feedback-label">Feedback for a Cursor agent
        <textarea name="feedback" rows="3" maxlength="4000" required></textarea>
      </label>
      <p class="hint">The first note starts one Cursor agent for this item. It opens one pull request for you to review. Later notes go to the same agent.</p>
      <div class="form-row">
        <button class="button primary send" type="submit">Send</button>
        <button class="button ghost cancel" type="button">Cancel</button>
        <span class="form-status" role="status" aria-live="polite"></span>
      </div>
    </form>
  </li>
</template>
<script type="application/json" id="snapshot">{embedded_json(payload)}</script>"""
    return _document("Waiting on me", body, script=True)
