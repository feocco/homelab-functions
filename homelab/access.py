from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


_MISSING = _Missing()
_STRING: dict[str, Any] = {"type": "string"}
_INTEGER: dict[str, Any] = {"type": "integer"}
_OBJECT: dict[str, Any] = {"type": "object"}
_BUTTONS: dict[str, Any] = {
    "type": "array",
    "maxItems": 3,
    "items": {
        "type": "object",
        "required": ["title"],
        "properties": {
            "title": {"type": "string"},
            "action": {"type": "string"},
            "uri": {"type": "string"},
            "behavior": {"type": "string"},
            "textInputButtonTitle": {"type": "string"},
            "textInputPlaceholder": {"type": "string"},
        },
    },
}


def freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(freeze(item) for item in value)
    return value


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class AccessParameter:
    name: str
    location: str
    json_schema: Mapping[str, Any]
    required: bool = False
    default: Any = _MISSING

    def __post_init__(self) -> None:
        if self.location not in {"body", "query", "path"}:
            raise ValueError(f"Unsupported location for {self.name}: {self.location}")
        object.__setattr__(self, "json_schema", freeze(dict(self.json_schema)))
        if self.default is not _MISSING:
            object.__setattr__(self, "default", freeze(self.default))

    def has_default(self) -> bool:
        return self.default is not _MISSING

    def schema_with_default(self) -> dict[str, Any]:
        schema = to_jsonable(self.json_schema)
        if self.has_default():
            schema["default"] = to_jsonable(self.default)
        return schema


@dataclass(frozen=True)
class AccessError:
    status: str
    description: str

    def __post_init__(self) -> None:
        if len(self.status) != 3 or not self.status.isdigit():
            raise ValueError(f"HTTP status must be three digits, got {self.status}")
        if not self.description:
            raise ValueError("error description is required")


@dataclass(frozen=True)
class AccessOperation:
    operation_id: str
    method: str
    path: str
    summary: str
    description: str
    domain: str
    parameters: tuple[AccessParameter, ...] = ()
    errors: tuple[AccessError, ...] = ()
    body_component: str | None = None

    def __post_init__(self) -> None:
        if not self.operation_id:
            raise ValueError("operation_id is required")
        if not self.domain:
            raise ValueError(f"{self.operation_id} must name a domain")
        object.__setattr__(self, "method", self.method.upper())
        object.__setattr__(self, "parameters", tuple(self.parameters))
        names = [parameter.name for parameter in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate parameters on {self.operation_id}")
        object.__setattr__(self, "errors", tuple(self.errors))
        statuses = [error.status for error in self.errors]
        if len(statuses) != len(set(statuses)):
            raise ValueError(f"Duplicate error status on {self.operation_id}")
        if "401" not in statuses:
            raise ValueError(f"{self.operation_id} must document 401")
        if self.body_component is not None and not self.body_parameters():
            raise ValueError(f"{self.operation_id} body component requires body parameters")

    def parameter(self, name: str) -> AccessParameter:
        for parameter in self.parameters:
            if parameter.name == name:
                return parameter
        raise KeyError(name)

    def input_schema(self) -> dict[str, Any]:
        return self._object_schema(self.parameters)

    def body_parameters(self) -> tuple[AccessParameter, ...]:
        return tuple(parameter for parameter in self.parameters if parameter.location == "body")

    def body_object_schema(self) -> dict[str, Any]:
        return self._object_schema(self.body_parameters())

    def _object_schema(self, parameters: tuple[AccessParameter, ...]) -> dict[str, Any]:
        properties = {parameter.name: parameter.schema_with_default() for parameter in parameters}
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        required = [parameter.name for parameter in parameters if parameter.required]
        if required:
            schema["required"] = required
        return schema


@dataclass(frozen=True)
class AccessVector:
    operations: tuple[AccessOperation, ...]

    def __post_init__(self) -> None:
        operations = tuple(self.operations)
        object.__setattr__(self, "operations", operations)
        ids = [operation.operation_id for operation in operations]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate operation_id in AccessVector")
        seen_routes: set[tuple[str, str]] = set()
        for operation in operations:
            if not operation.path.startswith("/v1/"):
                raise ValueError(
                    f"{operation.operation_id} must be a protected /v1 path, got {operation.path}"
                )
            route = (operation.method, operation.path)
            if route in seen_routes:
                raise ValueError(f"Duplicate route {operation.method} {operation.path}")
            seen_routes.add(route)
        self.component_schemas()
        object.__setattr__(
            self,
            "_by_id",
            MappingProxyType({operation.operation_id: operation for operation in operations}),
        )

    def component_schemas(self) -> dict[str, dict[str, Any]]:
        schemas: dict[str, dict[str, Any]] = {}
        for operation in self.operations:
            if operation.body_component is None:
                continue
            schema = operation.body_object_schema()
            previous = schemas.get(operation.body_component)
            if previous is None:
                schemas[operation.body_component] = schema
            elif previous != schema:
                raise ValueError(
                    f"{operation.body_component} must match on every operation that uses it"
                )
        return schemas

    def domains(self) -> tuple[str, ...]:
        """Return operation domains in first-declared order."""

        return tuple(dict.fromkeys(operation.domain for operation in self.operations))

    def __iter__(self):
        return iter(self.operations)

    def __len__(self) -> int:
        return len(self.operations)

    def __contains__(self, operation_id: object) -> bool:
        return isinstance(operation_id, str) and operation_id in self._by_id

    def get(self, operation_id: str) -> AccessOperation:
        try:
            return self._by_id[operation_id]
        except KeyError:
            raise KeyError(operation_id) from None


def _param(
    name: str,
    location: str,
    json_schema: dict[str, Any],
    *,
    required: bool = False,
    default: Any = _MISSING,
) -> AccessParameter:
    return AccessParameter(name, location, json_schema, required, default)


@dataclass(frozen=True)
class PublicEndpoint:
    """An unauthenticated route. These stay outside `ACCESS_VECTOR`."""

    method: str
    path: str
    summary: str


PUBLIC_ENDPOINTS: tuple[PublicEndpoint, ...] = (
    PublicEndpoint("GET", "/health", "Report service health and notification recorder status."),
    PublicEndpoint("GET", "/docs", "This reference page."),
    PublicEndpoint("GET", "/openapi.json", "OpenAPI 3.1 document for every protected action."),
)

ERROR_RESPONSE_SCHEMA: Mapping[str, Any] = freeze(
    {
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
)

_UNAUTHORIZED = AccessError("401", "Missing or invalid token")


def _documented_errors(*errors: AccessError) -> tuple[AccessError, ...]:
    return tuple(sorted((*errors, _UNAUTHORIZED), key=lambda error: int(error.status)))


ACCESS_VECTOR = AccessVector(
    (
        AccessOperation(
            operation_id="notify_joe",
            method="POST",
            path="/v1/notify/joe",
            summary="Send a mobile notification to Joe",
            description=(
                "Send a mobile notification through the configured Home Assistant service for Joe."
            ),
            domain="Notifications",
            parameters=(
                _param("title", "body", _STRING, required=True),
                _param("message", "body", _STRING, required=True),
                _param("tag", "body", _STRING),
                _param("group", "body", _STRING),
                _param("url", "body", _STRING),
                _param("buttons", "body", _BUTTONS),
            ),
            errors=_documented_errors(
                AccessError("400", "Invalid request"),
                AccessError("502", "Home Assistant rejected the notification request"),
                AccessError("504", "Timed out calling Home Assistant"),
            ),
            body_component="NotificationRequest",
        ),
        AccessOperation(
            operation_id="notify_jess",
            method="POST",
            path="/v1/notify/jess",
            summary="Send a mobile notification to Jess",
            description=(
                "Send a mobile notification through the configured Home Assistant service for Jess."
            ),
            domain="Notifications",
            parameters=(
                _param("title", "body", _STRING, required=True),
                _param("message", "body", _STRING, required=True),
                _param("tag", "body", _STRING),
                _param("group", "body", _STRING),
                _param("url", "body", _STRING),
                _param("buttons", "body", _BUTTONS),
            ),
            errors=_documented_errors(
                AccessError("400", "Invalid request"),
                AccessError("502", "Home Assistant rejected the notification request"),
                AccessError("504", "Timed out calling Home Assistant"),
            ),
            body_component="NotificationRequest",
        ),
        AccessOperation(
            operation_id="list_notifications",
            method="GET",
            path="/v1/notifications",
            summary="List notification records",
            description="List notification records from the shared ledger.",
            domain="Notifications",
            parameters=(
                _param("limit", "query", _INTEGER, default=50),
                _param("group", "query", _STRING),
                _param("tag", "query", _STRING),
            ),
            errors=_documented_errors(AccessError("400", "Invalid limit")),
        ),
        AccessOperation(
            operation_id="record_notification_action",
            method="POST",
            path="/v1/notifications/actions",
            summary="Record a mobile notification action",
            description=(
                "Record a mobile notification action against the latest matching notification."
            ),
            domain="Notifications",
            parameters=(
                _param("action", "body", _STRING, required=True),
                _param("tag", "body", _STRING),
                _param("group", "body", _STRING),
                _param("reply_text", "body", _STRING),
                _param("event", "body", _OBJECT),
            ),
            errors=_documented_errors(AccessError("400", "Invalid request")),
        ),
        AccessOperation(
            operation_id="get_service_catalog",
            method="GET",
            path="/v1/catalog/services",
            summary="Return the generated homelab service catalog",
            description="Return the service catalog JSON generated by homelab-config.",
            domain="Catalog",
            parameters=(),
            errors=_documented_errors(AccessError("503", "Catalog file unavailable")),
        ),
        AccessOperation(
            operation_id="get_smoke_signal_targets",
            method="GET",
            path="/v1/catalog/smoke-signal-targets",
            summary="Return the generated Smoke Signal target list",
            description="Return the Smoke Signal target list generated by homelab-config.",
            domain="Catalog",
            parameters=(),
            errors=_documented_errors(AccessError("503", "Catalog file unavailable")),
        ),
        AccessOperation(
            operation_id="record_workflow_report",
            method="POST",
            path="/v1/workflow-reports",
            summary="Record a workflow report",
            description="Record a workflow report for later investigation.",
            domain="Workflow reports",
            parameters=(
                _param("workflow_slug", "body", _STRING, required=True),
                _param("summary", "body", _STRING, required=True),
                _param("source", "body", _STRING),
                _param("notification_id", "body", _INTEGER),
                _param("event", "body", _OBJECT),
            ),
            errors=_documented_errors(AccessError("400", "Invalid request")),
            body_component="WorkflowReportRequest",
        ),
        AccessOperation(
            operation_id="list_workflow_reports",
            method="GET",
            path="/v1/workflow-reports",
            summary="List workflow reports",
            description="List workflow reports from the shared ledger.",
            domain="Workflow reports",
            parameters=(
                _param("limit", "query", _INTEGER, default=50),
                _param("workflow", "query", _STRING),
            ),
            errors=_documented_errors(AccessError("400", "Invalid limit")),
        ),
        AccessOperation(
            operation_id="get_workflow_report",
            method="GET",
            path="/v1/workflow-reports/{report_id}",
            summary="Get one workflow report",
            description="Get one workflow report by id.",
            domain="Workflow reports",
            parameters=(_param("report_id", "path", _INTEGER, required=True),),
            errors=_documented_errors(
                AccessError("400", "Invalid report id"),
                AccessError("404", "Report not found"),
            ),
        ),
    )
)


# Phone-reachable URL for the Pebble app. homelab-config already publishes this
# hostname: UniFi DNS on the LAN, DNS-only tailnet address off the LAN, Caddy
# reverse proxy with no Authentik gate. Do not invent a new public name.
PEBBLE_PHONE_URL = "https://homelab-functions.home.feocco.com/v1/pebble"
# Existing Tailscale Serve mapping (hosts/macmini/tailscale-serve.yaml).
PEBBLE_TAILNET_HTTP_URL = "http://100.81.237.11:8091/v1/pebble"


@dataclass(frozen=True)
class HttpOnlyOperation:
    """A protected `/v1` route that must not join `ACCESS_VECTOR`.

    Membership in `ACCESS_VECTOR` means the shared `HOMELAB_FUNCTIONS_TOKEN`
    plus a JSON client method and an MCP tool. A route belongs here only when
    those three projections would be the wrong contract.
    """

    operation_id: str
    method: str
    path: str
    summary: str
    description: str
    domain: str
    auth_scheme: str
    auth_env: str
    exclusion_reason: str
    errors: tuple[AccessError, ...] = ()

    def __post_init__(self) -> None:
        if not self.operation_id:
            raise ValueError("operation_id is required")
        if self.auth_scheme == "bearerAuth":
            raise ValueError(f"{self.operation_id} must not use the shared bearer scheme")
        if self.auth_env == "HOMELAB_FUNCTIONS_TOKEN":
            raise ValueError(f"{self.operation_id} must not use the shared broker token")
        if not self.exclusion_reason:
            raise ValueError(f"{self.operation_id} must record why it is HTTP-only")
        if not self.path.startswith("/v1/"):
            raise ValueError(f"{self.operation_id} must be a protected /v1 path")
        object.__setattr__(self, "method", self.method.upper())
        object.__setattr__(self, "errors", tuple(self.errors))
        statuses = [error.status for error in self.errors]
        if len(statuses) != len(set(statuses)):
            raise ValueError(f"Duplicate error status on {self.operation_id}")
        if "401" not in statuses:
            raise ValueError(f"{self.operation_id} must document 401")

    def openapi_operation(self) -> dict[str, Any]:
        responses: dict[str, Any] = {"200": {"description": self.summary}}
        for error in self.errors:
            responses[error.status] = {
                "description": error.description,
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
                },
            }
        return {
            "operationId": self.operation_id,
            "summary": self.summary,
            "description": self.description,
            "tags": [self.domain],
            "security": [{self.auth_scheme: []}],
            "x-homelab-http-only": True,
            "x-homelab-auth-env": self.auth_env,
            "x-homelab-exclusion": self.exclusion_reason,
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data": {"schema": _PEBBLE_MULTIPART_SCHEMA},
                    "application/json": {"schema": _PEBBLE_JSON_SCHEMA},
                },
            },
            "responses": responses,
        }


_PEBBLE_TEXT_PROPERTIES: dict[str, Any] = {
    "transcription": {"type": "string"},
    "recordedAt": {
        "type": "string",
        "description": (
            "Unix milliseconds within five minutes of the server clock. "
            "One of recordedAt or timestamp is required."
        ),
    },
    "client": {"type": "string"},
    "test": {"type": "string"},
    "event_id": {"type": "string"},
    "event_type": {"type": "string"},
    "trigger": {"type": "string"},
    "timestamp": {
        "type": "string",
        "description": (
            "ISO-8601 time within five minutes of the server clock. "
            "One of recordedAt or timestamp is required."
        ),
    },
    "transcript": {"type": "string"},
    "audio_duration_seconds": {"type": "number"},
}
_PEBBLE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": _PEBBLE_TEXT_PROPERTIES,
}
_PEBBLE_MULTIPART_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "audio": {
            "type": "string",
            "format": "binary",
            "description": "AAC audio in an M4A container. Stored locally until TTL. Never forwarded.",
        },
        **_PEBBLE_TEXT_PROPERTIES,
    },
}

_PEBBLE_EXCLUSION = (
    "The Pebble ring posts multipart audio and authenticates with PEBBLE_RING_TOKEN. "
    "ACCESS_VECTOR membership would accept the call with HOMELAB_FUNCTIONS_TOKEN and "
    "would publish a JSON client method and an MCP tool. The client and MCP speak JSON "
    "with the shared token, and an MCP tool cannot carry this audio without putting the "
    "raw recording on the agent path."
)

HTTP_ONLY_OPERATIONS: tuple[HttpOnlyOperation, ...] = (
    HttpOnlyOperation(
        operation_id="relay_pebble",
        method="POST",
        path="/v1/pebble",
        summary="Relay a Pebble Index ring recording",
        description=(
            "Accept a Pebble Index webhook (multipart audio or JSON text) and forward JSON "
            "to the configured Grok webhook. Only allowlisted fields are forwarded. Raw audio "
            "stays on this host until its TTL. Event timestamps must be within five minutes "
            "of the server clock, and the request body is limited to 9 MiB. "
            f"The phone posts to {PEBBLE_PHONE_URL} with Authorization: Bearer $PEBBLE_RING_TOKEN. "
            "This route is HTTP-only. It is not a HomelabFunctionsClient method or an MCP tool."
        ),
        domain="Pebble",
        auth_scheme="pebbleRingAuth",
        auth_env="PEBBLE_RING_TOKEN",
        exclusion_reason=_PEBBLE_EXCLUSION,
        errors=(
            AccessError("400", "Invalid request"),
            AccessError("401", "Missing or invalid pebble token"),
            AccessError("409", "Another request is processing this event id"),
            AccessError("413", "Pebble request or audio payload is too large"),
            AccessError("415", "Unsupported media type"),
            AccessError("502", "Transcription or webhook forwarding failed"),
            AccessError("503", "Pebble relay is not configured"),
            AccessError("504", "Timed out during transcription or webhook forwarding"),
        ),
    ),
)


def _assert_http_only_disjoint() -> None:
    registry_ids = {operation.operation_id for operation in ACCESS_VECTOR}
    registry_routes = {(operation.method, operation.path) for operation in ACCESS_VECTOR}
    seen: set[str] = set()
    for operation in HTTP_ONLY_OPERATIONS:
        if operation.operation_id in seen:
            raise ValueError(f"Duplicate HTTP-only operation_id {operation.operation_id}")
        seen.add(operation.operation_id)
        if operation.operation_id in registry_ids:
            raise ValueError(f"{operation.operation_id} cannot be both registry and HTTP-only")
        if (operation.method, operation.path) in registry_routes:
            raise ValueError(f"HTTP-only route collides with ACCESS_VECTOR: {operation.path}")


_assert_http_only_disjoint()
