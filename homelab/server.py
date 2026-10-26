from __future__ import annotations

import asyncio
import hmac
import html
import json
import logging
import os
import re
from functools import wraps
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from .access import ACCESS_VECTOR, AccessOperation, AccessParameter
from .action_recorder import NotificationActionRecorder
from .home_assistant import HomeAssistantConfig
from .notification_ledger import NotificationLedger


LOGGER = logging.getLogger("homelab-functions")
MAX_BUTTONS = 3
ACTION_RE = re.compile(r"[^A-Za-z0-9_]+")
ACTION_FIELDS = (
    "action",
    "title",
    "uri",
    "behavior",
    "textInputButtonTitle",
    "textInputPlaceholder",
)


class ConfigError(RuntimeError):
    pass


class ValidationError(ValueError):
    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class HomeAssistantError(RuntimeError):
    pass


class CatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    ha_url: str
    ha_long_lived_token: str
    ha_notify_joe_service: str
    ha_notify_jess_service: str
    homelab_functions_token: str
    service_host: str = "0.0.0.0"
    service_port: int = 8091
    request_timeout_seconds: float = 10
    log_level: str = "INFO"
    notification_ledger_path: str = "/app/data/notifications.sqlite3"
    notification_action_recorder_enabled: bool = True
    homelab_catalog_path: str = "/app/config/service-catalog.json"
    homelab_smoke_signal_targets_path: str = "/app/config/smoke-signal-targets.json"

    @classmethod
    def from_env(cls) -> "Config":
        required = {
            "HA_URL": os.environ.get("HA_URL"),
            "HA_LONG_LIVED_TOKEN": os.environ.get("HA_LONG_LIVED_TOKEN"),
            "HA_NOTIFY_JOE_SERVICE": os.environ.get("HA_NOTIFY_JOE_SERVICE"),
            "HA_NOTIFY_JESS_SERVICE": os.environ.get("HA_NOTIFY_JESS_SERVICE"),
            "HOMELAB_FUNCTIONS_TOKEN": os.environ.get("HOMELAB_FUNCTIONS_TOKEN"),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            ha_url=required["HA_URL"] or "",
            ha_long_lived_token=required["HA_LONG_LIVED_TOKEN"] or "",
            ha_notify_joe_service=required["HA_NOTIFY_JOE_SERVICE"] or "",
            ha_notify_jess_service=required["HA_NOTIFY_JESS_SERVICE"] or "",
            homelab_functions_token=required["HOMELAB_FUNCTIONS_TOKEN"] or "",
            service_host=os.environ.get("SERVICE_HOST", "0.0.0.0"),
            service_port=int(os.environ.get("SERVICE_PORT", "8091")),
            request_timeout_seconds=float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "10")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            notification_ledger_path=os.environ.get(
                "NOTIFICATION_LEDGER_PATH",
                "/app/data/notifications.sqlite3",
            ),
            notification_action_recorder_enabled=env_bool(
                "NOTIFICATION_ACTION_RECORDER_ENABLED",
                default=True,
            ),
            homelab_catalog_path=os.environ.get(
                "HOMELAB_CATALOG_PATH",
                "/app/config/service-catalog.json",
            ),
            homelab_smoke_signal_targets_path=os.environ.get(
                "HOMELAB_SMOKE_SIGNAL_TARGETS_PATH",
                "/app/config/smoke-signal-targets.json",
            ),
        )


class HomeAssistantClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    async def send_notification(self, service_data: dict[str, Any], *, notify_service: str) -> str | None:
        domain, service = split_ha_notify_service(notify_service)
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


def env_bool(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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

        for field in ("behavior", "textInputButtonTitle", "textInputPlaceholder"):
            value = button.get(field)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValidationError(f"buttons[{index}].{field} must be a non-empty string", field="buttons")
                normalized[field] = value.strip()

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
    # Android Companion delivers via FCM. Default (normal) priority can be
    # deferred under Doze — e.g. no buzz until the screen turns on / unlock.
    # ttl=0 + priority=high forces immediate wake/delivery for every notify.
    # Docs: https://companion.home-assistant.io/docs/notifications/critical-notifications/
    data: dict[str, Any] = {
        "ttl": 0,
        "priority": "high",
    }

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
            {key: value for key, value in button.items() if key in ACTION_FIELDS}
            for button in buttons
        ]

    service_data["data"] = data
    return service_data


def _error_content(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
        },
    }


def _openapi_parameter(parameter: AccessParameter) -> dict[str, Any]:
    documented: dict[str, Any] = {
        "name": parameter.name,
        "in": parameter.location,
    }
    if parameter.required:
        documented["required"] = True
    documented["schema"] = parameter.schema_with_default()
    return documented


def _component_schemas() -> dict[str, Any]:
    schemas: dict[str, Any] = {
        "ErrorResponse": {
            "type": "object",
            "required": ["error"],
            "properties": {
                "error": {
                    "type": "object",
                    "required": ["code", "message"],
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "detail": {"type": "string"},
                    },
                }
            },
        }
    }
    schemas.update(ACCESS_VECTOR.component_schemas())
    return schemas


def _openapi_responses(operation: AccessOperation) -> dict[str, Any]:
    responses: dict[str, Any] = {"200": {"description": operation.summary}}
    for error in operation.errors:
        responses[error.status] = _error_content(error.description)
    return responses


def _openapi_operation(operation: AccessOperation) -> dict[str, Any]:
    located = [parameter for parameter in operation.parameters if parameter.location != "body"]
    documented: dict[str, Any] = {
        "operationId": operation.operation_id,
        "summary": operation.summary,
        "description": operation.description,
        "security": [{"bearerAuth": []}],
    }
    if located:
        documented["parameters"] = [_openapi_parameter(parameter) for parameter in located]
    if operation.body_parameters():
        if operation.body_component:
            schema: dict[str, Any] = {"$ref": f"#/components/schemas/{operation.body_component}"}
        else:
            schema = operation.body_object_schema()
        documented["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": schema}},
        }
    documented["responses"] = _openapi_responses(operation)
    return documented


def _protected_doc_rows() -> str:
    rows: list[str] = []
    for operation in ACCESS_VECTOR:
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(operation.method)}</code></td>"
            f"<td><code>{html.escape(operation.path)}</code></td>"
            f"<td>{html.escape(operation.summary)}</td>"
            "<td>Bearer token</td>"
            "</tr>"
        )
    return "\n".join(rows)


def service_openapi() -> dict[str, Any]:
    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": "homelab-functions",
            "version": "0.1.0",
            "description": "Notification and homelab API service for stable reusable actions.",
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "Use HOMELAB_FUNCTIONS_TOKEN. Browser docs intentionally do not store tokens.",
                }
            },
            "schemas": _component_schemas(),
        },
        "paths": {
            "/health": {
                "get": {
                    "summary": "Report service health",
                    "responses": {
                        "200": {
                            "description": "Service health payload",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    },
                }
            },
        },
    }
    for operation in ACCESS_VECTOR:
        path_item = document["paths"].setdefault(operation.path, {})
        path_item[operation.method.lower()] = _openapi_operation(operation)
    return document


def service_docs_html() -> str:
    document = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>homelab-functions API</title>
    <style>
      :root {
        color-scheme: light dark;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #f6f7f9;
        color: #18202f;
      }
      body { margin: 0; }
      main { max-width: 980px; margin: 0 auto; padding: 48px 20px 64px; }
      h1 { margin: 0 0 8px; font-size: 2.25rem; letter-spacing: 0; }
      h2 { margin-top: 32px; font-size: 1.15rem; }
      p { line-height: 1.55; color: #4b5563; }
      table { width: 100%; border-collapse: collapse; margin-top: 12px; background: #ffffff; }
      th, td { padding: 12px; border-bottom: 1px solid #d8dee8; text-align: left; vertical-align: top; }
      th { color: #1f2937; font-size: 0.85rem; text-transform: uppercase; }
      code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
      a { color: #075985; }
      .status { display: inline-flex; gap: 8px; align-items: center; font-weight: 600; }
      .dot { width: 10px; height: 10px; border-radius: 50%; background: #16a34a; }
      @media (prefers-color-scheme: dark) {
        :root { background: #101622; color: #f8fafc; }
        p { color: #cbd5e1; }
        table { background: #172033; }
        th, td { border-bottom-color: #334155; }
        th { color: #e2e8f0; }
        a { color: #7dd3fc; }
      }
    </style>
  </head>
  <body>
    <main>
      <h1>homelab-functions</h1>
      <p>Notification and homelab API service for stable reusable actions.</p>
      <p class="status"><span class="dot"></span> Healthy when <a href="/health">/health</a> returns <code>status: ok</code>.</p>
      <h2>Auth</h2>
      <p>Protected endpoints require <code>Authorization: Bearer $HOMELAB_FUNCTIONS_TOKEN</code>. This docs page does not store tokens or run authenticated calls.</p>
      <h2>API Endpoints</h2>
      <table>
        <thead><tr><th>Method</th><th>Path</th><th>Purpose</th><th>Auth</th></tr></thead>
        <tbody>
          <tr><td><code>GET</code></td><td><a href="/health"><code>/health</code></a></td><td>Machine-readable health check.</td><td>None</td></tr>
          <!--protected-routes-->
        </tbody>
      </table>
      <h2>Machine Schema</h2>
      <p><a href="/openapi.json">OpenAPI JSON</a></p>
    </main>
  </body>
</html>
"""
    return document.replace("<!--protected-routes-->", _protected_doc_rows())


CONFIG_KEY = web.AppKey("config", Config)
HA_CLIENT_KEY = web.AppKey("ha_client", HomeAssistantClient)
LEDGER_KEY = web.AppKey("notification_ledger", NotificationLedger)
ACTION_RECORDER_KEY = web.AppKey("notification_action_recorder", NotificationActionRecorder)
ACTION_RECORDER_TASK_KEY = web.AppKey("notification_action_recorder_task", asyncio.Task[None])


def _bearer_protected(handler):
    @wraps(handler)
    async def protected(request: web.Request) -> web.Response:
        config = request.app[CONFIG_KEY]
        if not authorized(request, config.homelab_functions_token):
            return error_response(
                HTTPStatus.UNAUTHORIZED,
                "unauthorized",
                "Invalid or missing bearer token",
            )
        return await handler(request)

    return protected


def _register_protected_routes(app: web.Application) -> None:
    handlers = {
        "notify_joe": notify_joe,
        "notify_jess": notify_jess,
        "list_notifications": list_notifications,
        "record_notification_action": record_notification_action,
        "get_service_catalog": catalog_services,
        "get_smoke_signal_targets": smoke_signal_targets,
        "record_workflow_report": record_workflow_report,
        "list_workflow_reports": list_workflow_reports,
        "get_workflow_report": get_workflow_report,
    }
    declared = {operation.operation_id for operation in ACCESS_VECTOR}
    registered = set(handlers)
    if registered != declared:
        missing = ", ".join(sorted(declared - registered))
        extra = ", ".join(sorted(registered - declared))
        raise RuntimeError(
            f"Protected handlers do not match AccessVector. missing=[{missing}] extra=[{extra}]"
        )
    for operation in ACCESS_VECTOR:
        handler = _bearer_protected(handlers[operation.operation_id])
        if operation.method == "GET":
            app.router.add_get(operation.path, handler)
        elif operation.method == "POST":
            app.router.add_post(operation.path, handler)
        else:
            app.router.add_route(operation.method, operation.path, handler)


def create_app(
    config: Config,
    ha_client: HomeAssistantClient | None = None,
    ledger: NotificationLedger | None = None,
) -> web.Application:
    app = web.Application()
    app[CONFIG_KEY] = config
    app[HA_CLIENT_KEY] = ha_client or HomeAssistantClient(config)
    app[LEDGER_KEY] = ledger or NotificationLedger(config.notification_ledger_path)
    if config.notification_action_recorder_enabled:
        app[ACTION_RECORDER_KEY] = NotificationActionRecorder(
            app[LEDGER_KEY],
            HomeAssistantConfig(
                ha_url=config.ha_url,
                ha_long_lived_token=config.ha_long_lived_token,
                request_timeout_seconds=config.request_timeout_seconds,
            ),
        )
        app.on_startup.append(start_action_recorder)
        app.on_cleanup.append(stop_action_recorder)
    app.router.add_get("/docs", docs)
    app.router.add_get("/openapi.json", openapi)
    app.router.add_get("/health", health)
    _register_protected_routes(app)
    return app


async def start_action_recorder(app: web.Application) -> None:
    recorder = app[ACTION_RECORDER_KEY]
    app[ACTION_RECORDER_TASK_KEY] = asyncio.create_task(
        recorder.run_forever(),
        name="homelab-notification-action-recorder",
    )


async def stop_action_recorder(app: web.Application) -> None:
    task = app.get(ACTION_RECORDER_TASK_KEY)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def docs(_request: web.Request) -> web.Response:
    return web.Response(text=service_docs_html(), content_type="text/html")


async def openapi(_request: web.Request) -> web.Response:
    return web.json_response(service_openapi())


async def health(request: web.Request) -> web.Response:
    config = request.app[CONFIG_KEY]
    recorder = request.app.get(ACTION_RECORDER_KEY)
    return web.json_response(
        {
            "status": "ok",
            "service": "homelab-functions",
            "ha_url_configured": bool(config.ha_url),
            "ha_notify_joe_service_configured": bool(config.ha_notify_joe_service),
            "ha_notify_jess_service_configured": bool(config.ha_notify_jess_service),
            "token_configured": bool(config.homelab_functions_token),
            "notification_ledger_configured": bool(config.notification_ledger_path),
            "notification_action_recorder_enabled": config.notification_action_recorder_enabled,
            "notification_action_recorder_connected": bool(recorder and recorder.connected),
            "notification_action_recorder_last_action_id": (
                recorder.last_action_id if recorder is not None else None
            ),
            "notification_action_recorder_last_error": (
                recorder.last_error if recorder is not None else ""
            ),
        }
    )


async def notify_joe(request: web.Request) -> web.Response:
    config = request.app[CONFIG_KEY]
    return await notify_recipient(request, config.ha_notify_joe_service)


async def notify_jess(request: web.Request) -> web.Response:
    config = request.app[CONFIG_KEY]
    return await notify_recipient(request, config.ha_notify_jess_service)


async def notify_recipient(request: web.Request, notify_service: str) -> web.Response:
    try:
        payload = await request.json()
        notification = validate_notification_payload(payload)
        service_data = build_service_data(notification)
        context_id = await request.app[HA_CLIENT_KEY].send_notification(
            service_data,
            notify_service=notify_service,
        )
        notification_record = request.app[LEDGER_KEY].record_sent(
            notification,
            service_data,
            ha_context_id=context_id,
        )
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
            "notification_id": notification_record["id"],
        }
    )


async def list_notifications(request: web.Request) -> web.Response:
    limit = parse_limit(
        request.query.get("limit"),
        default=ACCESS_VECTOR.get("list_notifications").parameter("limit").default,
    )
    notifications = request.app[LEDGER_KEY].list_notifications(
        limit=limit,
        group=optional_query_string(request.query.get("group")),
        tag=optional_query_string(request.query.get("tag")),
    )
    return web.json_response({"notifications": notifications})


async def record_notification_action(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        action_event = validate_notification_action_payload(payload)
    except ValidationError as exc:
        return error_response(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            str(exc),
            detail=exc.field,
        )

    result = request.app[LEDGER_KEY].record_action(action_event)
    return web.json_response(result)


async def catalog_services(request: web.Request) -> web.Response:
    config = request.app[CONFIG_KEY]
    return catalog_response(config.homelab_catalog_path, "service catalog")


async def smoke_signal_targets(request: web.Request) -> web.Response:
    config = request.app[CONFIG_KEY]
    return catalog_response(config.homelab_smoke_signal_targets_path, "Smoke Signal targets")


def catalog_response(path: str, label: str) -> web.Response:
    try:
        return web.json_response(load_json_file(path))
    except CatalogError as exc:
        LOGGER.warning("%s unavailable at %s: %s", label, path, exc)
        return error_response(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "catalog_unavailable",
            f"{label} is unavailable",
            detail=str(exc),
        )


def load_json_file(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as catalog_file:
            return json.load(catalog_file)
    except FileNotFoundError as exc:
        raise CatalogError("file not found") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError("file is not valid JSON") from exc
    except OSError as exc:
        raise CatalogError(str(exc)) from exc


async def record_workflow_report(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        report = validate_workflow_report_payload(payload)
    except ValidationError as exc:
        return error_response(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            str(exc),
            detail=exc.field,
        )

    recorded = request.app[LEDGER_KEY].record_workflow_report(report)
    return web.json_response(
        {
            "status": "reported",
            "report_id": recorded["id"],
            "report": recorded,
        }
    )


async def list_workflow_reports(request: web.Request) -> web.Response:
    limit = parse_limit(
        request.query.get("limit"),
        default=ACCESS_VECTOR.get("list_workflow_reports").parameter("limit").default,
    )
    reports = request.app[LEDGER_KEY].list_workflow_reports(
        limit=limit,
        workflow=optional_query_string(request.query.get("workflow")),
    )
    return web.json_response({"reports": reports})


async def get_workflow_report(request: web.Request) -> web.Response:
    try:
        report_id = int(request.match_info["report_id"])
    except ValueError:
        return error_response(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "report_id must be an integer",
            detail="report_id",
        )

    report = request.app[LEDGER_KEY].get_workflow_report(report_id)
    if report is None:
        return error_response(
            HTTPStatus.NOT_FOUND,
            "not_found",
            "Workflow report not found",
            detail="report_id",
        )
    return web.json_response({"report": report})


def parse_limit(raw_limit: str | None, *, default: int) -> int:
    if raw_limit is None or not raw_limit.strip():
        return default
    try:
        return int(raw_limit)
    except ValueError:
        raise web.HTTPBadRequest(
            text='{"error":{"code":"invalid_request","message":"limit must be an integer","detail":"limit"}}',
            content_type="application/json",
        )


def optional_query_string(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def validate_notification_action_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("Request body must be a JSON object")

    action = required_string(payload, "action")
    validated: dict[str, Any] = {"action": action}
    for field in ("tag", "group", "reply_text"):
        value = payload.get(field)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"{field} must be a non-empty string", field=field)
            validated[field] = value.strip()

    event = payload.get("event")
    if event is not None:
        if not isinstance(event, dict):
            raise ValidationError("event must be an object", field="event")
        validated["event"] = event

    return validated


def validate_workflow_report_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("Request body must be a JSON object")

    validated: dict[str, Any] = {
        "workflow_slug": required_string(payload, "workflow_slug"),
        "summary": required_string(payload, "summary"),
    }

    source = payload.get("source")
    if source is not None:
        if not isinstance(source, str) or not source.strip():
            raise ValidationError("source must be a non-empty string", field="source")
        validated["source"] = source.strip()

    notification_id = payload.get("notification_id")
    if notification_id is not None:
        if not isinstance(notification_id, int):
            raise ValidationError("notification_id must be an integer", field="notification_id")
        validated["notification_id"] = notification_id

    event = payload.get("event")
    if event is not None:
        if not isinstance(event, dict):
            raise ValidationError("event must be an object", field="event")
        validated["event"] = event

    return validated


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
