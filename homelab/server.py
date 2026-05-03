from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web


LOGGER = logging.getLogger("homelab-functions")
MAX_BUTTONS = 3
ACTION_RE = re.compile(r"[^A-Za-z0-9_]+")


class ConfigError(RuntimeError):
    pass


class ValidationError(ValueError):
    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class HomeAssistantError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    ha_url: str
    ha_long_lived_token: str
    ha_notify_joe_service: str
    homelab_functions_token: str
    service_host: str = "0.0.0.0"
    service_port: int = 8091
    request_timeout_seconds: float = 10
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        required = {
            "HA_URL": os.environ.get("HA_URL"),
            "HA_LONG_LIVED_TOKEN": os.environ.get("HA_LONG_LIVED_TOKEN"),
            "HA_NOTIFY_JOE_SERVICE": os.environ.get("HA_NOTIFY_JOE_SERVICE"),
            "HOMELAB_FUNCTIONS_TOKEN": os.environ.get("HOMELAB_FUNCTIONS_TOKEN"),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            ha_url=required["HA_URL"] or "",
            ha_long_lived_token=required["HA_LONG_LIVED_TOKEN"] or "",
            ha_notify_joe_service=required["HA_NOTIFY_JOE_SERVICE"] or "",
            homelab_functions_token=required["HOMELAB_FUNCTIONS_TOKEN"] or "",
            service_host=os.environ.get("SERVICE_HOST", "0.0.0.0"),
            service_port=int(os.environ.get("SERVICE_PORT", "8091")),
            request_timeout_seconds=float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "10")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )


class HomeAssistantClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    async def send_notification(self, service_data: dict[str, Any]) -> str | None:
        domain, service = split_ha_notify_service(self.config.ha_notify_joe_service)
        result = await self.call_service(domain, service, service_data)
        context = result.get("context") if isinstance(result, dict) else None
        context_id = context.get("id") if isinstance(context, dict) else None
        return context_id if isinstance(context_id, str) else None

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any],
    ) -> dict[str, Any]:
        ws_url = websocket_url(self.config.ha_url)
        timeout = ClientTimeout(total=self.config.request_timeout_seconds)
        async with ClientSession(timeout=timeout) as session:
            async with session.ws_connect(ws_url) as ws:
                auth_required = await ws.receive_json()
                if auth_required.get("type") != "auth_required":
                    raise HomeAssistantError("Home Assistant did not request WebSocket auth")

                await ws.send_json(
                    {
                        "type": "auth",
                        "access_token": self.config.ha_long_lived_token,
                    }
                )
                auth_response = await ws.receive_json()
                if auth_response.get("type") != "auth_ok":
                    message = auth_response.get("message") or auth_response.get("type") or "auth failed"
                    raise HomeAssistantError(f"Home Assistant auth failed: {message}")

                await ws.send_json(
                    {
                        "id": 1,
                        "type": "call_service",
                        "domain": domain,
                        "service": service,
                        "service_data": service_data,
                    }
                )

                while True:
                    message = await ws.receive()
                    if message.type == WSMsgType.ERROR:
                        raise HomeAssistantError("Home Assistant WebSocket failed")
                    if message.type in (WSMsgType.CLOSED, WSMsgType.CLOSE):
                        raise HomeAssistantError("Home Assistant closed the WebSocket")
                    if message.type != WSMsgType.TEXT:
                        continue

                    payload = message.json()
                    if payload.get("id") != 1:
                        continue
                    if not payload.get("success"):
                        error = payload.get("error")
                        if isinstance(error, dict):
                            error_message = error.get("message") or error.get("code") or "call_service failed"
                        else:
                            error_message = "call_service failed"
                        raise HomeAssistantError(f"Home Assistant call_service failed: {error_message}")

                    result = payload.get("result")
                    return result if isinstance(result, dict) else {}


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def split_ha_notify_service(value: str) -> tuple[str, str]:
    parts = value.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValidationError("HA notify service must look like notify.mobile_app_phone")
    return parts[0], parts[1]


def websocket_url(ha_url: str) -> str:
    parsed = urlsplit(ha_url.rstrip("/"))
    if parsed.scheme == "https":
        scheme = "wss"
    elif parsed.scheme == "http":
        scheme = "ws"
    elif parsed.scheme in ("ws", "wss"):
        scheme = parsed.scheme
    else:
        raise ConfigError("HA_URL must start with http://, https://, ws://, or wss://")

    path = parsed.path.rstrip("/")
    if not path.endswith("/api/websocket"):
        path = f"{path}/api/websocket"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def validate_notification_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("Request body must be a JSON object")

    title = required_string(payload, "title")
    message = required_string(payload, "message")
    validated: dict[str, Any] = {
        "title": title,
        "message": message,
    }

    for field in ("tag", "group", "url"):
        value = payload.get(field)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"{field} must be a non-empty string", field=field)
            validated[field] = value.strip()

    buttons = payload.get("buttons")
    if buttons is not None:
        validated["buttons"] = validate_buttons(buttons)

    return validated


def required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required", field=field)
    return value.strip()


def validate_buttons(buttons: Any) -> list[dict[str, str]]:
    if not isinstance(buttons, list):
        raise ValidationError("buttons must be an array", field="buttons")
    if len(buttons) > MAX_BUTTONS:
        raise ValidationError(f"buttons must contain at most {MAX_BUTTONS} items", field="buttons")

    validated: list[dict[str, str]] = []
    for index, button in enumerate(buttons):
        if not isinstance(button, dict):
            raise ValidationError(f"buttons[{index}] must be an object", field="buttons")

        title = button.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValidationError(f"buttons[{index}].title is required", field="buttons")

        normalized = {"title": title.strip()}
        action = button.get("action")
        if action is not None:
            if not isinstance(action, str) or not action.strip():
                raise ValidationError(f"buttons[{index}].action must be a non-empty string", field="buttons")
            normalized["action"] = action.strip()
        else:
            normalized["action"] = default_action(title)

        uri = button.get("uri")
        if uri is not None:
            if not isinstance(uri, str) or not uri.strip():
                raise ValidationError(f"buttons[{index}].uri must be a non-empty string", field="buttons")
            normalized["uri"] = uri.strip()

        validated.append(normalized)

    return validated


def default_action(title: str) -> str:
    slug = ACTION_RE.sub("_", title.strip().upper()).strip("_")
    return f"NOTIFY_JOE_{slug or 'ACTION'}"


def build_service_data(notification: dict[str, Any]) -> dict[str, Any]:
    service_data: dict[str, Any] = {
        "title": notification["title"],
        "message": notification["message"],
    }
    data: dict[str, Any] = {}

    for field in ("tag", "group"):
        value = notification.get(field)
        if value:
            data[field] = value

    url = notification.get("url")
    if url:
        data["url"] = url
        data["clickAction"] = url

    buttons = notification.get("buttons")
    if buttons:
        data["actions"] = [
            {key: value for key, value in button.items() if key in ("action", "title", "uri")}
            for button in buttons
        ]

    if data:
        service_data["data"] = data

    return service_data


CONFIG_KEY = web.AppKey("config", Config)
HA_CLIENT_KEY = web.AppKey("ha_client", HomeAssistantClient)


def create_app(config: Config, ha_client: HomeAssistantClient | None = None) -> web.Application:
    app = web.Application()
    app[CONFIG_KEY] = config
    app[HA_CLIENT_KEY] = ha_client or HomeAssistantClient(config)
    app.router.add_get("/health", health)
    app.router.add_post("/v1/notify/joe", notify_joe)
    return app


async def health(request: web.Request) -> web.Response:
    config = request.app[CONFIG_KEY]
    return web.json_response(
        {
            "status": "ok",
            "service": "homelab-functions",
            "ha_url_configured": bool(config.ha_url),
            "ha_notify_joe_service_configured": bool(config.ha_notify_joe_service),
            "token_configured": bool(config.homelab_functions_token),
        }
    )


async def notify_joe(request: web.Request) -> web.Response:
    config = request.app[CONFIG_KEY]
    if not authorized(request, config.homelab_functions_token):
        return error_response(HTTPStatus.UNAUTHORIZED, "unauthorized", "Invalid or missing bearer token")

    try:
        payload = await request.json()
        notification = validate_notification_payload(payload)
        service_data = build_service_data(notification)
        context_id = await request.app[HA_CLIENT_KEY].send_notification(service_data)
    except ValidationError as exc:
        return error_response(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            str(exc),
            detail=exc.field,
        )
    except asyncio.TimeoutError:
        return error_response(
            HTTPStatus.GATEWAY_TIMEOUT,
            "ha_timeout",
            "Timed out calling Home Assistant",
        )
    except HomeAssistantError as exc:
        LOGGER.warning("Home Assistant notification failed: %s", exc)
        return error_response(
            HTTPStatus.BAD_GATEWAY,
            "ha_error",
            "Home Assistant rejected the notification request",
            detail=str(exc),
        )

    return web.json_response(
        {
            "status": "sent",
            "ha_context_id": context_id,
        }
    )


def authorized(request: web.Request, expected_token: str) -> bool:
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False
    return hmac.compare_digest(header[len(prefix) :], expected_token)


def error_response(
    status: HTTPStatus,
    code: str,
    message: str,
    *,
    detail: str | None = None,
) -> web.Response:
    error: dict[str, str] = {
        "code": code,
        "message": message,
    }
    if detail:
        error["detail"] = detail
    return web.json_response({"error": error}, status=status.value)


def main() -> None:
    load_dotenv()
    config = Config.from_env()
    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO))
    web.run_app(create_app(config), host=config.service_host, port=config.service_port)


if __name__ == "__main__":
    main()
