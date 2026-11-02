"""Small JSON-over-HTTP transport shared by the GitHub and Cursor clients.

Tests replace `AiohttpJsonTransport` with a fake that records requests, so the
clients never need a live network to prove their behavior.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol

from aiohttp import ClientConnectorError, ClientError, ClientSession, ClientTimeout


@dataclass(frozen=True)
class JsonResponse:
    status: int
    body: Any


class TransportError(RuntimeError):
    """The request failed before a response arrived.

    `maybe_delivered` is False only when the connection was never opened, so a
    caller knows whether the remote side could have acted on the request.
    """

    def __init__(self, message: str, *, maybe_delivered: bool) -> None:
        super().__init__(message)
        self.maybe_delivered = maybe_delivered


class JsonTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        payload: Any | None = None,
    ) -> JsonResponse: ...


class AiohttpJsonTransport:
    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        payload: Any | None = None,
    ) -> JsonResponse:
        timeout = ClientTimeout(total=self.timeout_seconds)
        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.request(method, url, headers=headers, json=payload) as response:
                    text = await response.text()
                    return JsonResponse(response.status, _decode(text))
        except ClientConnectorError as exc:
            raise TransportError(f"could not connect: {type(exc).__name__}", maybe_delivered=False) from exc
        except asyncio.TimeoutError as exc:
            raise TransportError("timed out", maybe_delivered=True) from exc
        except ClientError as exc:
            raise TransportError(f"connection failed: {type(exc).__name__}", maybe_delivered=True) from exc


def _decode(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
