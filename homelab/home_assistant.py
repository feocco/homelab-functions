from __future__ import annotations

"""Small Home Assistant WebSocket helper for homelab services.

This module is intentionally client-side only. Services that need Home Assistant
state or events should connect directly with this helper instead of routing live
event streams through the deployed homelab-functions HTTP server.
"""

import asyncio
import itertools
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientSession, ClientTimeout, ClientWebSocketResponse, WSMsgType


EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class HomeAssistantError(RuntimeError):
    """Raised when Home Assistant configuration, auth, or WebSocket calls fail."""

    pass


@dataclass(frozen=True)
class HomeAssistantConfig:
    """Configuration for direct Home Assistant WebSocket access."""

    ha_url: str
    ha_long_lived_token: str
    request_timeout_seconds: float = 30

    @classmethod
    def from_env(cls) -> "HomeAssistantConfig":
        """Build config from `HA_URL`, `HA_LONG_LIVED_TOKEN`, and optional timeout."""

        ha_url = os.environ.get("HA_URL")
        token = os.environ.get("HA_LONG_LIVED_TOKEN")
        missing = [
            name
            for name, value in (
                ("HA_URL", ha_url),
                ("HA_LONG_LIVED_TOKEN", token),
            )
            if not value
        ]
        if missing:
            raise HomeAssistantError(f"Missing required environment variables: {', '.join(missing)}")
        return cls(
            ha_url=ha_url or "",
            ha_long_lived_token=token or "",
            request_timeout_seconds=float(os.environ.get("HA_REQUEST_TIMEOUT_SECONDS", "30")),
        )


class HomeAssistantWebSocketClient:
    """Direct Home Assistant WebSocket client for state, events, and service calls.

    Typical usage:

    ```python
    import homelab

    async with homelab.HomeAssistantWebSocketClient.from_env() as ha:
        states = await ha.get_states()
        await ha.subscribe_events("state_changed")
        await ha.call_service("switch", "turn_on", {"entity_id": "switch.example"})
    ```

    Use this for app-specific discovery and long-running listeners. Use
    `homelab.notify_joe(...)` instead when a service only needs to send Joe a
    phone notification.
    """

    def __init__(self, config: HomeAssistantConfig) -> None:
        self.config = config
        self._session: ClientSession | None = None
        self._ws: ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._event_handlers: list[EventHandler] = []
        self._reader_task: asyncio.Task[None] | None = None

    @classmethod
    def from_env(cls) -> "HomeAssistantWebSocketClient":
        """Create a client from standard Home Assistant environment variables."""

        return cls(HomeAssistantConfig.from_env())

    async def __aenter__(self) -> "HomeAssistantWebSocketClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the WebSocket, authenticate, and start the background reader."""

        await self.close()
        timeout = ClientTimeout(total=self.config.request_timeout_seconds)
        self._session = ClientSession(timeout=timeout)
        self._ws = await self._session.ws_connect(websocket_url(self.config.ha_url))

        auth_required = await self._ws.receive_json()
        if auth_required.get("type") != "auth_required":
            raise HomeAssistantError(f"Unexpected Home Assistant auth handshake: {auth_required}")

        await self._ws.send_json({"type": "auth", "access_token": self.config.ha_long_lived_token})
        auth_response = await self._ws.receive_json()
        if auth_response.get("type") != "auth_ok":
            message = auth_response.get("message") or auth_response.get("type") or "auth failed"
            raise HomeAssistantError(f"Home Assistant auth failed: {message}")

        self._reader_task = asyncio.create_task(self._reader(), name="homelab-ha-websocket-reader")

    async def close(self) -> None:
        """Close the WebSocket session and cancel the background reader."""

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()
        self._ws = None
        self._session = None

    def add_event_handler(self, handler: EventHandler) -> None:
        """Register an async callback for subscribed Home Assistant events."""

        self._event_handlers.append(handler)

    async def get_states(self) -> list[dict[str, Any]]:
        """Return Home Assistant's current state list from `get_states`."""

        result = await self.request({"type": "get_states"})
        states = result.get("result")
        return states if isinstance(states, list) else []

    async def subscribe_events(self, event_type: str | None = None) -> None:
        """Subscribe to Home Assistant events, optionally limited by event type."""

        payload: dict[str, Any] = {"type": "subscribe_events"}
        if event_type:
            payload["event_type"] = event_type
        await self.request(payload)

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call a Home Assistant service and return the raw result object."""

        result = await self.request(
            {
                "type": "call_service",
                "domain": domain,
                "service": service,
                "service_data": service_data or {},
            }
        )
        service_result = result.get("result")
        return service_result if isinstance(service_result, dict) else {}

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a raw Home Assistant WebSocket request and wait for its result."""

        if not self._ws or self._ws.closed:
            raise HomeAssistantError("Home Assistant WebSocket is not connected")
        message_id = next(self._ids)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[message_id] = future
        await self._ws.send_json({"id": message_id, **payload})
        try:
            return await asyncio.wait_for(future, timeout=self.config.request_timeout_seconds)
        finally:
            self._pending.pop(message_id, None)

    async def _reader(self) -> None:
        if self._ws is None:
            raise HomeAssistantError("Home Assistant WebSocket reader started without a connection")
        async for message in self._ws:
            if message.type == WSMsgType.ERROR:
                raise HomeAssistantError("Home Assistant WebSocket failed")
            if message.type != WSMsgType.TEXT:
                continue

            payload = message.json()
            if payload.get("type") == "result":
                self._finish_pending(payload)
            elif payload.get("type") == "event":
                await self._dispatch_event(payload.get("event") or {})

    def _finish_pending(self, payload: dict[str, Any]) -> None:
        message_id = payload.get("id")
        future = self._pending.pop(message_id, None)
        if not future:
            return
        if payload.get("success", False):
            future.set_result(payload)
            return

        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or "request failed"
        else:
            message = "request failed"
        future.set_exception(HomeAssistantError(f"Home Assistant request failed: {message}"))

    async def _dispatch_event(self, event: dict[str, Any]) -> None:
        for handler in list(self._event_handlers):
            await handler(event)


def websocket_url(ha_url: str) -> str:
    """Convert a Home Assistant base URL into its `/api/websocket` URL."""

    parsed = urlsplit(ha_url.rstrip("/"))
    if parsed.scheme == "https":
        scheme = "wss"
    elif parsed.scheme == "http":
        scheme = "ws"
    elif parsed.scheme in {"ws", "wss"}:
        scheme = parsed.scheme
    else:
        raise HomeAssistantError("HA_URL must start with http://, https://, ws://, or wss://")

    path = parsed.path.rstrip("/")
    if not path.endswith("/api/websocket"):
        path = f"{path}/api/websocket"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))
