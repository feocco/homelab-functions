from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_FUNCTIONS_URL = "http://127.0.0.1:8091"


class HomelabFunctionsError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail


def notify_joe(
    title: str,
    message: str,
    *,
    tag: str | None = None,
    group: str | None = None,
    url: str | None = None,
    buttons: list[dict[str, Any]] | None = None,
    service_url: str | None = None,
    token: str | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": title,
        "message": message,
    }
    if tag is not None:
        payload["tag"] = tag
    if group is not None:
        payload["group"] = group
    if url is not None:
        payload["url"] = url
    if buttons is not None:
        payload["buttons"] = buttons

    return _post_json(
        "/v1/notify/joe",
        payload,
        service_url=service_url,
        token=token,
        timeout=timeout,
    )


def _post_json(
    path: str,
    payload: dict[str, Any],
    *,
    service_url: str | None,
    token: str | None,
    timeout: float,
) -> dict[str, Any]:
    base_url = (service_url or os.environ.get("HOMELAB_FUNCTIONS_URL") or DEFAULT_FUNCTIONS_URL).rstrip("/")
    auth_token = token if token is not None else os.environ.get("HOMELAB_FUNCTIONS_TOKEN")
    if not auth_token:
        raise HomelabFunctionsError(
            "HOMELAB_FUNCTIONS_TOKEN is required to call homelab-functions",
            code="missing_client_token",
        )

    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=body,
        headers={
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        error_payload = _parse_error_payload(body_text)
        raise HomelabFunctionsError(
            error_payload.get("message") or f"homelab-functions returned HTTP {exc.code}",
            status=exc.code,
            code=error_payload.get("code"),
            detail=error_payload.get("detail"),
        ) from exc
    except URLError as exc:
        raise HomelabFunctionsError(
            f"Could not reach homelab-functions at {base_url}: {exc.reason}",
            code="service_unreachable",
        ) from exc
    except TimeoutError as exc:
        raise HomelabFunctionsError(
            f"Timed out calling homelab-functions at {base_url}",
            code="service_timeout",
        ) from exc


def _parse_error_payload(body: str) -> dict[str, str]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {"message": body}

    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        error = payload["error"]
        return {
            "message": str(error.get("message") or ""),
            "code": str(error.get("code") or ""),
            "detail": str(error.get("detail") or ""),
        }
    if isinstance(payload, dict):
        return {"message": str(payload.get("message") or payload)}
    return {"message": str(payload)}
