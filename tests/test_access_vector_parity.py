import asyncio
import inspect
import json
import os
import re
import select
import subprocess
import sys
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from aiohttp.test_utils import TestClient, TestServer

import homelab
from homelab.access import ACCESS_VECTOR
from homelab.client import HomelabFunctionsClient, HomelabFunctionsError
from homelab.server import Config, HomeAssistantError, create_app


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "parity-token-do-not-leak"
PUBLIC_PATHS = ("/health", "/docs", "/openapi.json")
TIMESTAMP = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$"

STRING = {"type": "string"}
INTEGER = {"type": "integer"}
OBJECT = {"type": "object"}
BUTTONS = {
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

INVALID_REQUEST = {"status": "400", "description": "Invalid request"}
UNAUTHORIZED = {"status": "401", "description": "Missing or invalid token"}
NOTIFY_ERRORS = (
    INVALID_REQUEST,
    UNAUTHORIZED,
    {"status": "502", "description": "Home Assistant rejected the notification request"},
    {"status": "504", "description": "Timed out calling Home Assistant"},
)
LIMIT_ERRORS = (
    {"status": "400", "description": "Invalid limit"},
    UNAUTHORIZED,
)
REQUEST_ERRORS = (INVALID_REQUEST, UNAUTHORIZED)
CATALOG_ERRORS = (
    UNAUTHORIZED,
    {"status": "503", "description": "Catalog file unavailable"},
)
REPORT_ERRORS = (
    {"status": "400", "description": "Invalid report id"},
    UNAUTHORIZED,
    {"status": "404", "description": "Report not found"},
)

CONTRACTS = (
    {
        "operation_id": "notify_joe",
        "method": "POST",
        "path": "/v1/notify/joe",
        "summary": "Send a mobile notification to Joe",
        "description": (
            "Send a mobile notification through the configured Home Assistant service for Joe."
        ),
        "parameters": (
            {"name": "title", "location": "body", "required": True, "schema": STRING},
            {"name": "message", "location": "body", "required": True, "schema": STRING},
            {"name": "tag", "location": "body", "required": False, "schema": STRING},
            {"name": "group", "location": "body", "required": False, "schema": STRING},
            {"name": "url", "location": "body", "required": False, "schema": STRING},
            {"name": "buttons", "location": "body", "required": False, "schema": BUTTONS},
        ),
        "errors": NOTIFY_ERRORS,
        "body_component": "NotificationRequest",
    },
    {
        "operation_id": "notify_jess",
        "method": "POST",
        "path": "/v1/notify/jess",
        "summary": "Send a mobile notification to Jess",
        "description": (
            "Send a mobile notification through the configured Home Assistant service for Jess."
        ),
        "parameters": (
            {"name": "title", "location": "body", "required": True, "schema": STRING},
            {"name": "message", "location": "body", "required": True, "schema": STRING},
            {"name": "tag", "location": "body", "required": False, "schema": STRING},
            {"name": "group", "location": "body", "required": False, "schema": STRING},
            {"name": "url", "location": "body", "required": False, "schema": STRING},
            {"name": "buttons", "location": "body", "required": False, "schema": BUTTONS},
        ),
        "errors": NOTIFY_ERRORS,
        "body_component": "NotificationRequest",
    },
    {
        "operation_id": "list_notifications",
        "method": "GET",
        "path": "/v1/notifications",
        "summary": "List notification records",
        "description": "List notification records from the shared ledger.",
        "parameters": (
            {"name": "limit", "location": "query", "required": False, "schema": INTEGER, "default": 50},
            {"name": "group", "location": "query", "required": False, "schema": STRING},
            {"name": "tag", "location": "query", "required": False, "schema": STRING},
        ),
        "errors": LIMIT_ERRORS,
    },
    {
        "operation_id": "record_notification_action",
        "method": "POST",
        "path": "/v1/notifications/actions",
        "summary": "Record a mobile notification action",
        "description": "Record a mobile notification action against the latest matching notification.",
        "parameters": (
            {"name": "action", "location": "body", "required": True, "schema": STRING},
            {"name": "tag", "location": "body", "required": False, "schema": STRING},
            {"name": "group", "location": "body", "required": False, "schema": STRING},
            {"name": "reply_text", "location": "body", "required": False, "schema": STRING},
            {"name": "event", "location": "body", "required": False, "schema": OBJECT},
        ),
        "errors": REQUEST_ERRORS,
    },
    {
        "operation_id": "get_service_catalog",
        "method": "GET",
        "path": "/v1/catalog/services",
        "summary": "Return the generated homelab service catalog",
        "description": "Return the service catalog JSON generated by homelab-config.",
        "parameters": (),
        "errors": CATALOG_ERRORS,
    },
    {
        "operation_id": "get_smoke_signal_targets",
        "method": "GET",
        "path": "/v1/catalog/smoke-signal-targets",
        "summary": "Return the generated Smoke Signal target list",
        "description": "Return the Smoke Signal target list generated by homelab-config.",
        "parameters": (),
        "errors": CATALOG_ERRORS,
    },
    {
        "operation_id": "record_workflow_report",
        "method": "POST",
        "path": "/v1/workflow-reports",
        "summary": "Record a workflow report",
        "description": "Record a workflow report for later investigation.",
        "parameters": (
            {"name": "workflow_slug", "location": "body", "required": True, "schema": STRING},
            {"name": "summary", "location": "body", "required": True, "schema": STRING},
            {"name": "source", "location": "body", "required": False, "schema": STRING},
            {"name": "notification_id", "location": "body", "required": False, "schema": INTEGER},
            {"name": "event", "location": "body", "required": False, "schema": OBJECT},
        ),
        "errors": REQUEST_ERRORS,
        "body_component": "WorkflowReportRequest",
    },
    {
        "operation_id": "list_workflow_reports",
        "method": "GET",
        "path": "/v1/workflow-reports",
        "summary": "List workflow reports",
        "description": "List workflow reports from the shared ledger.",
        "parameters": (
            {"name": "limit", "location": "query", "required": False, "schema": INTEGER, "default": 50},
            {"name": "workflow", "location": "query", "required": False, "schema": STRING},
        ),
        "errors": LIMIT_ERRORS,
    },
    {
        "operation_id": "get_workflow_report",
        "method": "GET",
        "path": "/v1/workflow-reports/{report_id}",
        "summary": "Get one workflow report",
        "description": "Get one workflow report by id.",
        "parameters": (
            {"name": "report_id", "location": "path", "required": True, "schema": INTEGER},
        ),
        "errors": REPORT_ERRORS,
    },
)

EXPECTED_IDS = (
    "notify_joe",
    "notify_jess",
    "list_notifications",
    "record_notification_action",
    "get_service_catalog",
    "get_smoke_signal_targets",
    "record_workflow_report",
    "list_workflow_reports",
    "get_workflow_report",
)
EXPECTED_ROUTES = (
    ("POST", "/v1/notify/joe"),
    ("POST", "/v1/notify/jess"),
    ("GET", "/v1/notifications"),
    ("POST", "/v1/notifications/actions"),
    ("GET", "/v1/catalog/services"),
    ("GET", "/v1/catalog/smoke-signal-targets"),
    ("POST", "/v1/workflow-reports"),
    ("GET", "/v1/workflow-reports"),
    ("GET", "/v1/workflow-reports/{report_id}"),
)

PUBLIC_EXPORTS = [
    "HomeAssistantConfig",
    "HomeAssistantError",
    "HomeAssistantWebSocketClient",
    "HomelabFunctionsClient",
    "HomelabFunctionsError",
    "NotificationActionRouter",
    "get_service_catalog",
    "get_smoke_signal_targets",
    "get_workflow_report",
    "list_notifications",
    "list_workflow_reports",
    "notify_jess",
    "notify_joe",
    "record_notification_action",
    "record_workflow_report",
    "workflow_report_button",
    "websocket_url",
]

CATALOG = {
    "schema": "homelab-service-catalog.v1",
    "services": [{"service": "grafana", "host": "parity-host", "enabled": True}],
}
SMOKE_TARGETS = {
    "schema": "homelab-smoke-signal-targets.v1",
    "targets": [{"name": "Parity Grafana", "url": "http://grafana.parity/api/health"}],
}
JOE_SERVICE = "notify.mobile_app_pixel"
JESS_SERVICE = "notify.mobile_app_jwellz2"


def plain(value):
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def emitted_schema(parameter):
    schema = plain(parameter["schema"])
    if "default" in parameter:
        schema["default"] = parameter["default"]
    return schema


def expected_input_schema(parameters):
    properties = {}
    required = []
    for parameter in parameters:
        properties[parameter["name"]] = emitted_schema(parameter)
        if parameter["required"]:
            required.append(parameter["name"])
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def contract_by_id(operation_id):
    for item in CONTRACTS:
        if item["operation_id"] == operation_id:
            return item
    raise AssertionError(operation_id)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeHomeAssistant:
    def __init__(self):
        self.calls = []

    async def send_notification(self, service_data, *, notify_service):
        if service_data["title"] == "Leak probe":
            raise HomeAssistantError(f"failure included {TOKEN}")
        self.calls.append(
            {
                "notify_service": notify_service,
                "service_data": json.loads(json.dumps(service_data)),
            }
        )
        return "context-123"


def write_runtime(tmp: Path) -> Config:
    catalog_path = tmp / "service-catalog.json"
    smoke_path = tmp / "smoke-signal-targets.json"
    catalog_path.write_text(json.dumps(CATALOG), encoding="utf-8")
    smoke_path.write_text(json.dumps(SMOKE_TARGETS), encoding="utf-8")
    return Config(
        ha_url="https://example.ui.nabu.casa",
        ha_long_lived_token="ha-token",
        ha_notify_joe_service=JOE_SERVICE,
        ha_notify_jess_service=JESS_SERVICE,
        homelab_functions_token=TOKEN,
        homelab_catalog_path=str(catalog_path),
        homelab_smoke_signal_targets_path=str(smoke_path),
        notification_ledger_path=str(tmp / "notifications.sqlite3"),
        notification_action_recorder_enabled=False,
    )


def body_schema(parameters):
    return expected_input_schema(
        [parameter for parameter in parameters if parameter["location"] == "body"]
    )


def contract_default(operation_id, name):
    for parameter in contract_by_id(operation_id)["parameters"]:
        if parameter["name"] == name and "default" in parameter:
            return parameter["default"]
    raise AssertionError(f"{operation_id} has no default for {name}")


class ContractTests(unittest.TestCase):
    def test_registry_matches_contract(self):
        self.assertEqual(tuple(operation.operation_id for operation in ACCESS_VECTOR), EXPECTED_IDS)
        public = set(PUBLIC_PATHS)
        for operation, expected in zip(ACCESS_VECTOR, CONTRACTS, strict=True):
            self.assertEqual(operation.operation_id, expected["operation_id"])
            self.assertEqual(operation.method, expected["method"])
            self.assertEqual(operation.path, expected["path"])
            self.assertEqual(operation.summary, expected["summary"])
            self.assertEqual(operation.description, expected["description"])
            self.assertNotIn(operation.path, public)
            self.assertEqual(len(operation.parameters), len(expected["parameters"]))
            for parameter, spec in zip(operation.parameters, expected["parameters"], strict=True):
                self.assertEqual(parameter.name, spec["name"])
                self.assertEqual(parameter.location, spec["location"])
                self.assertEqual(parameter.required, spec["required"])
                self.assertEqual(plain(parameter.json_schema), spec["schema"])
                if "default" in spec:
                    self.assertTrue(parameter.has_default())
                    self.assertEqual(parameter.default, spec["default"])
                else:
                    self.assertFalse(parameter.has_default())
            self.assertEqual(operation.input_schema(), expected_input_schema(expected["parameters"]))
            self.assertNotIn("additionalProperties", json.dumps(operation.input_schema()))
            self.assertEqual(operation.body_component, expected.get("body_component"))
            self.assertEqual(
                [(error.status, error.description) for error in operation.errors],
                [(error["status"], error["description"]) for error in expected["errors"]],
            )

    def test_protected_routes_match_contract(self):
        with TemporaryDirectory() as tmp:
            app = create_app(write_runtime(Path(tmp)), ha_client=FakeHomeAssistant())
            protected = []
            available = set()
            for route in app.router.routes():
                path = route.resource.canonical
                available.add((route.method, path))
                if route.method == "HEAD" or not path.startswith("/v1"):
                    continue
                protected.append((route.method, path))
        self.assertEqual(tuple(protected), EXPECTED_ROUTES)
        for path in PUBLIC_PATHS:
            self.assertIn(("GET", path), available)
            self.assertNotIn(("POST", path), available)

    def test_client_surface(self):
        signature = inspect.signature(HomelabFunctionsClient.__init__)
        self.assertEqual(list(signature.parameters), ["self", "service_url", "token", "timeout"])
        self.assertEqual(list(homelab.__all__), PUBLIC_EXPORTS)
        for name in PUBLIC_EXPORTS:
            self.assertIsNotNone(getattr(homelab, name))
        for operation in ACCESS_VECTOR:
            method = getattr(HomelabFunctionsClient, operation.operation_id)
            self.assertTrue(callable(method))
            self.assertTrue(callable(getattr(homelab, operation.operation_id)))
            parameters = inspect.signature(method).parameters
            names = [name for name in parameters if name != "self"]
            self.assertEqual(names, [parameter.name for parameter in operation.parameters])
            for parameter in operation.parameters:
                declared = parameters[parameter.name]
                if parameter.required:
                    self.assertIs(declared.default, inspect.Parameter.empty, parameter.name)
                elif parameter.has_default():
                    self.assertEqual(declared.default, parameter.default)
                else:
                    self.assertIsNone(declared.default)
        self.assertIs(homelab.HomelabFunctionsClient, HomelabFunctionsClient)

    def test_client_reads_environment_for_url_and_token(self):
        with patch.dict(
            os.environ,
            {
                "HOMELAB_FUNCTIONS_URL": "http://broker.test:8091",
                "HOMELAB_FUNCTIONS_TOKEN": "env-token",
            },
        ):
            with patch("homelab.client.urlopen", return_value=FakeResponse(CATALOG)) as urlopen:
                result = HomelabFunctionsClient().get_service_catalog()
        self.assertEqual(result, CATALOG)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://broker.test:8091/v1/catalog/services")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.headers["Authorization"], "Bearer env-token")

    def test_client_requires_token(self):
        with patch.dict(os.environ, {}, clear=True):
            client = HomelabFunctionsClient("http://127.0.0.1:9", token=None, timeout=1)
            with self.assertRaises(HomelabFunctionsError) as caught:
                client.get_service_catalog()
        self.assertEqual(caught.exception.code, "missing_client_token")
        self.assertIn("HOMELAB_FUNCTIONS_TOKEN", str(caught.exception))

    def test_client_builds_notification_query(self):
        with patch(
            "homelab.client.urlopen",
            return_value=FakeResponse({"notifications": []}),
        ) as urlopen:
            homelab.list_notifications(
                group="parity-suite",
                tag="parity-joe-client",
                limit=10,
                service_url="http://broker.test:8091",
                token="env-token",
            )
        request = urlopen.call_args.args[0]
        parsed = urlsplit(request.full_url)
        self.assertEqual(parsed.path, "/v1/notifications")
        self.assertEqual(
            parse_qs(parsed.query),
            {"limit": ["10"], "group": ["parity-suite"], "tag": ["parity-joe-client"]},
        )

    def test_plugin_manifests_and_skill_agree(self):
        manifest = json.loads((ROOT / ".cursor-plugin" / "plugin.json").read_text(encoding="utf-8"))
        mcp_path = ROOT / "mcp.json"
        skill_path = ROOT / "skills" / "homelab-functions" / "SKILL.md"
        self.assertEqual(manifest["mcpServers"], "./mcp.json")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(manifest["name"], "homelab-functions")
        self.assertTrue(mcp_path.is_file())
        self.assertTrue(skill_path.is_file())

        mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
        self.assertEqual(list(mcp["mcpServers"]), ["homelab-functions"])
        server = mcp["mcpServers"]["homelab-functions"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["command"], "python3")
        self.assertEqual(server["args"], ["-m", "homelab.mcp_server"])
        self.assertEqual(server["cwd"], "${CURSOR_PLUGIN_ROOT}")
        self.assertEqual(
            server["env"],
            {
                "HOMELAB_FUNCTIONS_URL": "${HOMELAB_FUNCTIONS_URL}",
                "HOMELAB_FUNCTIONS_TOKEN": "${HOMELAB_FUNCTIONS_TOKEN}",
            },
        )
        variables = manifest["variables"]
        self.assertEqual(variables["type"], "object")
        self.assertEqual(variables["required"], ["HOMELAB_FUNCTIONS_TOKEN"])
        self.assertEqual(
            variables["properties"]["HOMELAB_FUNCTIONS_URL"]["default"],
            "http://127.0.0.1:8091",
        )
        self.assertNotIn("default", variables["properties"]["HOMELAB_FUNCTIONS_TOKEN"])
        raw_mcp = mcp_path.read_text(encoding="utf-8")
        self.assertNotIn("${env:", raw_mcp)
        self.assertNotIn(TOKEN, raw_mcp)
        self.assertNotIn("replace_me", raw_mcp)
        self.assertNotIn("Bearer ", raw_mcp)

        skill = skill_path.read_text(encoding="utf-8")
        _, frontmatter, body = skill.split("---", 2)
        self.assertIn("name: homelab-functions", frontmatter)
        for operation_id in EXPECTED_IDS:
            self.assertIn(operation_id, body)
        self.assertIn("HomeAssistantWebSocketClient", body)
        self.assertIn("state", body)
        self.assertIn("events", body)
        self.assertIn("service calls", body)
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('homelab-functions-mcp = "homelab.mcp_server:main"', pyproject)


_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_plugin(value, variables):
    if not isinstance(value, str):
        raise AssertionError(type(value).__name__)

    def replace(match):
        name = match.group(1)
        if name not in variables:
            raise AssertionError(name)
        return variables[name]

    expanded = _PLACEHOLDER.sub(replace, value)
    if "${" in expanded:
        raise AssertionError(expanded)
    return expanded


def system_python_env(extra):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONUTF8": "0",
        "PYTHONUNBUFFERED": "1",
    }
    env.update(extra)
    if "PYTHONPATH" in env:
        raise AssertionError("PYTHONPATH")
    return env


def write_rpc(proc, message):
    raw = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
    proc.stdin.write(raw)
    proc.stdin.flush()


def read_rpc_line(proc, timeout=10):
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise AssertionError(read_stderr_text(proc) or "timed out waiting for MCP stdout")
    line = proc.stdout.readline()
    if not line:
        raise AssertionError(read_stderr_text(proc) or "MCP stdout closed")
    return line


def read_stderr_text(proc):
    if proc.stderr is None:
        return ""
    fd = proc.stderr.fileno()
    os.set_blocking(fd, False)
    chunks = []
    try:
        while True:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.set_blocking(fd, True)
    return b"".join(chunks).decode("utf-8", errors="replace")


def close_stdio(proc):
    if proc.poll() is not None:
        return
    if proc.stdin and not proc.stdin.closed:
        proc.stdin.close()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def initialize_message(request_id, version):
    params = {"capabilities": {}, "clientInfo": {"name": "parity", "version": "0"}}
    if version is not None:
        params["protocolVersion"] = version
    return {"jsonrpc": "2.0", "id": request_id, "method": "initialize", "params": params}


def assert_omits_additional_properties(test, value):
    if isinstance(value, Mapping):
        test.assertNotIn("additionalProperties", value)
        for item in value.values():
            assert_omits_additional_properties(test, item)
    elif isinstance(value, list):
        for item in value:
            assert_omits_additional_properties(test, item)


class ClientHelperTests(unittest.TestCase):
    def test_helpers_send_every_registry_input(self):
        base = "http://broker.test:8091"
        token = "helper-token"
        buttons_joe = [{"title": "Open", "action": "OPEN", "uri": "https://example.test/open"}]
        buttons_jess = [{"title": "Accept", "action": "ACCEPT"}]
        notification_limit = contract_default("list_notifications", "limit")
        report_limit = contract_default("list_workflow_reports", "limit")
        specs = [
            (
                lambda: homelab.notify_joe(
                    "Joe title",
                    "café",
                    tag="joe-tag",
                    group="joe-group",
                    url="https://example.test/joe",
                    buttons=buttons_joe,
                    service_url=base,
                    token=token,
                ),
                {"marker": "notify_joe"},
                "POST",
                "/v1/notify/joe",
                {},
                {
                    "title": "Joe title",
                    "message": "café",
                    "tag": "joe-tag",
                    "group": "joe-group",
                    "url": "https://example.test/joe",
                    "buttons": buttons_joe,
                },
            ),
            (
                lambda: homelab.notify_jess(
                    "Jess title",
                    "Jess message",
                    tag="jess-tag",
                    group="jess-group",
                    url="https://example.test/jess",
                    buttons=buttons_jess,
                    service_url=base,
                    token=token,
                ),
                {"marker": "notify_jess"},
                "POST",
                "/v1/notify/jess",
                {},
                {
                    "title": "Jess title",
                    "message": "Jess message",
                    "tag": "jess-tag",
                    "group": "jess-group",
                    "url": "https://example.test/jess",
                    "buttons": buttons_jess,
                },
            ),
            (
                lambda: homelab.list_notifications(
                    limit=7,
                    group="suite",
                    tag="joe-tag",
                    service_url=base,
                    token=token,
                ),
                {"marker": "list_notifications"},
                "GET",
                "/v1/notifications",
                {"limit": ["7"], "group": ["suite"], "tag": ["joe-tag"]},
                None,
            ),
            (
                lambda: homelab.list_notifications(
                    group="plants",
                    service_url=base,
                    token=token,
                ),
                {"marker": "list_notifications_default"},
                "GET",
                "/v1/notifications",
                {"limit": [str(notification_limit)], "group": ["plants"]},
                None,
            ),
            (
                lambda: homelab.record_notification_action(
                    "ACK",
                    tag="action-tag",
                    group="action-group",
                    reply_text="done",
                    event={"sourceDeviceName": "Pixel"},
                    service_url=base,
                    token=token,
                ),
                {"marker": "record_notification_action"},
                "POST",
                "/v1/notifications/actions",
                {},
                {
                    "action": "ACK",
                    "tag": "action-tag",
                    "group": "action-group",
                    "reply_text": "done",
                    "event": {"sourceDeviceName": "Pixel"},
                },
            ),
            (
                lambda: homelab.get_service_catalog(service_url=base, token=token),
                {"marker": "get_service_catalog"},
                "GET",
                "/v1/catalog/services",
                {},
                None,
            ),
            (
                lambda: homelab.get_smoke_signal_targets(service_url=base, token=token),
                {"marker": "get_smoke_signal_targets"},
                "GET",
                "/v1/catalog/smoke-signal-targets",
                {},
                None,
            ),
            (
                lambda: homelab.record_workflow_report(
                    "cat-food",
                    "Bowl empty",
                    source="monitor",
                    notification_id=42,
                    event={"origin": "helper"},
                    service_url=base,
                    token=token,
                ),
                {"marker": "record_workflow_report"},
                "POST",
                "/v1/workflow-reports",
                {},
                {
                    "workflow_slug": "cat-food",
                    "summary": "Bowl empty",
                    "source": "monitor",
                    "notification_id": 42,
                    "event": {"origin": "helper"},
                },
            ),
            (
                lambda: homelab.list_workflow_reports(
                    limit=3,
                    workflow="cat-food",
                    service_url=base,
                    token=token,
                ),
                {"marker": "list_workflow_reports"},
                "GET",
                "/v1/workflow-reports",
                {"limit": ["3"], "workflow": ["cat-food"]},
                None,
            ),
            (
                lambda: homelab.list_workflow_reports(
                    workflow="plants",
                    service_url=base,
                    token=token,
                ),
                {"marker": "list_workflow_reports_default"},
                "GET",
                "/v1/workflow-reports",
                {"limit": [str(report_limit)], "workflow": ["plants"]},
                None,
            ),
            (
                lambda: homelab.get_workflow_report(15, service_url=base, token=token),
                {"marker": "get_workflow_report"},
                "GET",
                "/v1/workflow-reports/15",
                {},
                None,
            ),
        ]
        seen = set()
        for invoke, payload, method, path, query, body in specs:
            with patch("homelab.client.urlopen", return_value=FakeResponse(payload)) as urlopen:
                self.assertEqual(invoke(), payload)
            request = urlopen.call_args.args[0]
            parsed = urlsplit(request.full_url)
            self.assertEqual(request.get_method(), method)
            self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", base + path)
            self.assertEqual(parse_qs(parsed.query), query)
            self.assertEqual(request.headers["Authorization"], f"Bearer {token}")
            if body is None:
                self.assertIsNone(request.data)
            else:
                self.assertEqual(json.loads(request.data.decode("utf-8")), body)
                self.assertEqual(request.headers["Content-type"], "application/json")
            seen.add(payload["marker"])
        self.assertTrue(set(EXPECTED_IDS) <= seen)


class PluginLaunchTests(unittest.TestCase):
    def test_manifest_command_runs_with_system_python(self):
        manifest = json.loads((ROOT / ".cursor-plugin" / "plugin.json").read_text(encoding="utf-8"))
        mcp = json.loads((ROOT / "mcp.json").read_text(encoding="utf-8"))
        server = mcp["mcpServers"]["homelab-functions"]
        properties = manifest["variables"]["properties"]
        values = {
            "CURSOR_PLUGIN_ROOT": str(ROOT),
            "HOMELAB_FUNCTIONS_URL": properties["HOMELAB_FUNCTIONS_URL"]["default"],
            "HOMELAB_FUNCTIONS_TOKEN": "plugin-smoke-token",
        }
        command = expand_plugin(server["command"], values)
        args = [expand_plugin(item, values) for item in server["args"]]
        cwd = expand_plugin(server["cwd"], values)
        env = system_python_env(
            {key: expand_plugin(value, values) for key, value in server["env"].items()}
        )
        self.assertEqual(command, "python3")
        self.assertEqual(args, ["-m", "homelab.mcp_server"])
        self.assertEqual(cwd, str(ROOT))
        previous = os.getcwd()
        with TemporaryDirectory() as unrelated:
            os.chdir(unrelated)
            try:
                self.assertNotEqual(os.getcwd(), cwd)
                proc = subprocess.Popen(
                    [command, *args],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
                try:
                    write_rpc(
                        proc,
                        initialize_message(1, "2024-11-05"),
                    )
                    opened = json.loads(read_rpc_line(proc).decode("utf-8"))
                    self.assertEqual(opened["id"], 1)
                    self.assertEqual(opened["result"]["protocolVersion"], "2024-11-05")
                    self.assertEqual(opened["result"]["serverInfo"]["name"], "homelab-functions")
                    write_rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                    listed = json.loads(read_rpc_line(proc).decode("utf-8"))
                    self.assertEqual(
                        [tool["name"] for tool in listed["result"]["tools"]],
                        list(EXPECTED_IDS),
                    )
                    err = read_stderr_text(proc)
                    self.assertNotIn("plugin-smoke-token", err)
                    self.assertNotIn("aiohttp", err)
                finally:
                    close_stdio(proc)
            finally:
                os.chdir(previous)


class McpProtocolTests(unittest.TestCase):
    def spawn(self):
        return subprocess.Popen(
            ["python3", "-m", "homelab.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT),
            env=system_python_env({}),
        )

    def test_initialize_echoes_supported_versions_and_falls_back(self):
        proc = self.spawn()
        try:
            request_id = 0
            for version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
                request_id += 1
                write_rpc(proc, initialize_message(request_id, version))
                payload = json.loads(read_rpc_line(proc).decode("utf-8"))
                self.assertEqual(payload["id"], request_id)
                self.assertEqual(payload["result"]["protocolVersion"], version)
                self.assertEqual(
                    payload["result"]["capabilities"],
                    {"tools": {"listChanged": False}},
                )
            request_id += 1
            write_rpc(proc, initialize_message(request_id, "1999-01-01"))
            fallback = json.loads(read_rpc_line(proc).decode("utf-8"))
            self.assertEqual(fallback["result"]["protocolVersion"], "2025-11-25")
            request_id += 1
            write_rpc(proc, initialize_message(request_id, None))
            missing = json.loads(read_rpc_line(proc).decode("utf-8"))
            self.assertEqual(missing["result"]["protocolVersion"], "2025-11-25")
        finally:
            close_stdio(proc)

    def test_ignores_inbound_jsonrpc_responses(self):
        proc = self.spawn()
        try:
            write_rpc(proc, {"jsonrpc": "2.0", "id": "resp-温度", "result": {"note": "café"}})
            write_rpc(
                proc,
                {"jsonrpc": "2.0", "id": 4, "error": {"code": -32600, "message": "nope"}},
            )
            write_rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
            payload = json.loads(read_rpc_line(proc).decode("utf-8"))
            self.assertEqual(payload["id"], 2)
            self.assertEqual(payload["result"], {})
            ready, _, _ = select.select([proc.stdout], [], [], 0.3)
            self.assertEqual(ready, [])
        finally:
            close_stdio(proc)


class OpenAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = TemporaryDirectory()
        self.http = TestClient(TestServer(create_app(write_runtime(Path(self.tmpdir.name)))))
        await self.http.start_server()

    async def asyncTearDown(self):
        await self.http.close()
        self.tmpdir.cleanup()

    async def test_openapi_matches_contract(self):
        response = await self.http.get("/openapi.json")
        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["openapi"], "3.1.0")
        self.assertEqual(payload["info"]["title"], "homelab-functions")
        self.assertIn("bearerAuth", payload["components"]["securitySchemes"])
        self.assertNotIn("operationId", payload["paths"]["/health"]["get"])
        self.assertNotIn("/docs", payload["paths"])

        found = []
        for path, path_item in payload["paths"].items():
            for method, operation in path_item.items():
                if "operationId" in operation:
                    found.append((operation["operationId"], method.upper(), path))
        self.assertEqual(
            found,
            [(item["operation_id"], item["method"], item["path"]) for item in CONTRACTS],
        )

        for expected in CONTRACTS:
            operation = payload["paths"][expected["path"]][expected["method"].lower()]
            self.assertEqual(operation["operationId"], expected["operation_id"])
            self.assertEqual(operation["summary"], expected["summary"])
            self.assertEqual(operation["description"], expected["description"])
            self.assertEqual(operation["security"], [{"bearerAuth": []}])
            self.assert_openapi_inputs(operation, expected)
            self.assert_openapi_responses(operation, expected)

        schemas = payload["components"]["schemas"]
        self.assertEqual(
            schemas["NotificationRequest"],
            body_schema(contract_by_id("notify_joe")["parameters"]),
        )
        self.assertEqual(
            schemas["NotificationRequest"],
            body_schema(contract_by_id("notify_jess")["parameters"]),
        )
        self.assertEqual(
            schemas["WorkflowReportRequest"],
            body_schema(contract_by_id("record_workflow_report")["parameters"]),
        )
        self.assertIn("ErrorResponse", schemas)
        assert_omits_additional_properties(self, payload)

        docs = await self.http.get("/docs")
        body = await docs.text()
        self.assertEqual(docs.status, 200)
        self.assertIn("does not store tokens", body)
        for _method, path in EXPECTED_ROUTES:
            self.assertIn(path, body)

        health = await self.http.get("/health")
        self.assertEqual(health.status, 200)
        catalog = await self.http.get("/v1/catalog/services")
        self.assertEqual(catalog.status, 401)

    async def test_every_registry_route_rejects_missing_bearer(self):
        self.assertEqual(len(ACCESS_VECTOR), 9)
        for operation in ACCESS_VECTOR:
            path = operation.path
            for parameter in operation.parameters:
                if parameter.location == "path":
                    path = path.replace("{" + parameter.name + "}", "15")
            self.assertNotIn("{", path)
            response = await self.http.request(operation.method, path)
            self.assertEqual(response.status, 401, f"{operation.method} {path}")
            payload = await response.json()
            self.assertEqual(payload["error"]["code"], "unauthorized")

    def assert_openapi_responses(self, operation, expected):
        responses = operation["responses"]
        self.assertEqual(set(responses), {"200", *(item["status"] for item in expected["errors"])})
        self.assertEqual(responses["200"]["description"], expected["summary"])
        for item in expected["errors"]:
            documented = responses[item["status"]]
            self.assertEqual(documented["description"], item["description"])
            self.assertEqual(
                documented["content"]["application/json"]["schema"],
                {"$ref": "#/components/schemas/ErrorResponse"},
            )

    def assert_openapi_inputs(self, operation, expected):
        body_params = [item for item in expected["parameters"] if item["location"] == "body"]
        other_params = [item for item in expected["parameters"] if item["location"] != "body"]
        if body_params:
            schema = operation["requestBody"]["content"]["application/json"]["schema"]
            self.assertTrue(operation["requestBody"]["required"])
            component = expected.get("body_component")
            if component:
                self.assertEqual(schema, {"$ref": f"#/components/schemas/{component}"})
            else:
                self.assertEqual(schema, body_schema(expected["parameters"]))
        else:
            self.assertNotIn("requestBody", operation)

        documented = operation.get("parameters", [])
        self.assertEqual(len(documented), len(other_params))
        for actual, item in zip(documented, other_params, strict=True):
            self.assertEqual(actual["name"], item["name"])
            self.assertEqual(actual["in"], item["location"])
            self.assertEqual(actual.get("required", False), item["required"])
            self.assertEqual(actual["schema"], emitted_schema(item))


class BackgroundApp:
    def __init__(self, app):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="parity-aiohttp", daemon=True)
        self.ready = threading.Event()
        self.server = None
        self.url = ""
        self.error = None
        self.app = app

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._start())
        except Exception as exc:
            self.error = exc
            self.ready.set()
            return
        self.ready.set()
        try:
            self.loop.run_forever()
        finally:
            if self.server is not None:
                self.loop.run_until_complete(self.server.close())
            self.loop.close()

    async def _start(self):
        self.server = TestServer(self.app)
        await self.server.start_server()
        self.url = str(self.server.make_url("/")).rstrip("/")

    def start(self) -> str:
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError("test server did not start")
        if self.error is not None:
            raise self.error
        return self.url

    def close(self) -> None:
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = TemporaryDirectory()
        self.fake = FakeHomeAssistant()
        self.background = BackgroundApp(
            create_app(write_runtime(Path(self.tmpdir.name)), ha_client=self.fake)
        )
        self.url = self.background.start()
        self.api = HomelabFunctionsClient(self.url, TOKEN, timeout=5)
        self.stderr_parts = []
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "homelab.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT),
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "LANG": "C",
                "LC_ALL": "C",
                "PYTHONUTF8": "0",
                "PYTHONPATH": str(ROOT),
                "PYTHONUNBUFFERED": "1",
                "HOMELAB_FUNCTIONS_URL": self.url,
                "HOMELAB_FUNCTIONS_TOKEN": TOKEN,
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            },
        )
        self.rpc_id = 0

    def tearDown(self):
        proc = getattr(self, "proc", None)
        err = ""
        if proc is not None:
            if proc.poll() is None:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            err = self.read_stderr()
        background = getattr(self, "background", None)
        if background is not None:
            background.close()
        tmpdir = getattr(self, "tmpdir", None)
        if tmpdir is not None:
            tmpdir.cleanup()
        leaked = TOKEN in err or any(TOKEN in part for part in getattr(self, "stderr_parts", []))
        if leaked:
            raise AssertionError("MCP stderr leaked the bearer token")

    def read_stderr(self):
        if self.proc.stderr is None:
            return ""
        fd = self.proc.stderr.fileno()
        os.set_blocking(fd, False)
        chunks = []
        try:
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.set_blocking(fd, True)
        text = b"".join(chunks).decode("utf-8", errors="replace")
        if text:
            self.stderr_parts.append(text)
        return text

    def rpc(self, method, params=None, *, notification=False):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notification:
            self.rpc_id += 1
            message["id"] = self.rpc_id
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        if notification:
            return None
        assert self.proc.stdout is not None
        ready, _, _ = select.select([self.proc.stdout], [], [], 10)
        if not ready:
            self.fail(f"timed out waiting for MCP response: {self.read_stderr()}")
        line = self.proc.stdout.readline()
        if not line:
            self.fail(f"MCP stdout closed: {self.read_stderr()}")
        self.last_stdout = line
        text = line.decode("utf-8")
        self.assertNotIn(TOKEN, text)
        return json.loads(text)

    def call_tool(self, name, arguments=None):
        response = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        self.assertNotIn("error", response)
        result = response["result"]
        self.assertEqual(len(result["content"]), 1)
        content = result["content"][0]
        self.assertEqual(content["type"], "text")
        self.assertNotIn(TOKEN, content["text"])
        return result["isError"], json.loads(content["text"])

    def test_client_and_mcp_cover_every_action(self):
        initialized = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "parity-test", "version": "0"},
            },
        )
        self.assertEqual(
            initialized["result"],
            {
                "capabilities": {"tools": {"listChanged": False}},
                "protocolVersion": "2025-11-25",
                "serverInfo": {"name": "homelab-functions", "version": "0.1.0"},
            },
        )
        self.rpc("notifications/initialized", notification=True)
        ping = self.rpc("ping")
        self.assertEqual(ping["result"], {})

        listed = self.rpc("tools/list")
        tools = listed["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], list(EXPECTED_IDS))
        for tool, expected in zip(tools, CONTRACTS, strict=True):
            self.assertEqual(tool["name"], expected["operation_id"])
            self.assertEqual(tool["description"], expected["description"])
            self.assertEqual(tool["inputSchema"], expected_input_schema(expected["parameters"]))
            self.assertNotIn("additionalProperties", json.dumps(tool["inputSchema"]))
        self.assertNotIn("get_states", [tool["name"] for tool in tools])
        self.assertNotIn("call_service", [tool["name"] for tool in tools])

        unknown = self.rpc("tools/call", {"name": "call_service", "arguments": {}})
        self.assertEqual(unknown["error"]["code"], -32602)
        self.assertNotIn(TOKEN, json.dumps(unknown))

        leak_error, leak_body = self.call_tool(
            "notify_joe",
            {"title": "Leak probe", "message": "Check MCP error redaction"},
        )
        self.assertTrue(leak_error)
        self.assertEqual(leak_body["status"], 502)
        self.assertEqual(leak_body["detail"], "failure included [redacted]")
        self.assertNotIn(TOKEN, json.dumps(leak_body))

        joe = self.api.notify_joe(
            "Parity Joe client",
            "Client path",
            tag="parity-joe-client",
            group="parity-suite",
            buttons=[{"title": "Open", "action": "PARITY_OPEN"}],
        )
        self.assertEqual(joe["status"], "sent")
        self.assertEqual(joe["ha_context_id"], "context-123")
        self.assertIsInstance(joe["notification_id"], int)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(
            self.fake.calls[0],
            {
                "notify_service": JOE_SERVICE,
                "service_data": {
                    "title": "Parity Joe client",
                    "message": "Client path",
                    "data": {
                        "ttl": 0,
                        "priority": "high",
                        "tag": "parity-joe-client",
                        "group": "parity-suite",
                        "actions": [{"title": "Open", "action": "PARITY_OPEN"}],
                    },
                },
            },
        )

        joe_error, joe_mcp = self.call_tool(
            "notify_joe",
            {
                "title": "Parity Joe mcp",
                "message": "MCP path",
                "tag": "parity-joe-mcp",
                "group": "parity-suite",
                "url": "https://example.parity/joe",
            },
        )
        self.assertFalse(joe_error)
        self.assertEqual(joe_mcp["status"], "sent")
        self.assertEqual(joe_mcp["ha_context_id"], "context-123")
        self.assertIsInstance(joe_mcp["notification_id"], int)
        self.assertNotEqual(joe_mcp["notification_id"], joe["notification_id"])
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(
            self.fake.calls[1],
            {
                "notify_service": JOE_SERVICE,
                "service_data": {
                    "title": "Parity Joe mcp",
                    "message": "MCP path",
                    "data": {
                        "ttl": 0,
                        "priority": "high",
                        "tag": "parity-joe-mcp",
                        "group": "parity-suite",
                        "url": "https://example.parity/joe",
                        "clickAction": "https://example.parity/joe",
                    },
                },
            },
        )

        jess = self.api.notify_jess(
            "Parity Jess client",
            "Client path",
            tag="parity-jess-client",
            group="parity-suite",
        )
        self.assertEqual(jess["status"], "sent")
        self.assertEqual(self.fake.calls[2]["notify_service"], JESS_SERVICE)
        self.assertEqual(self.fake.calls[2]["service_data"]["title"], "Parity Jess client")
        self.assertEqual(
            self.fake.calls[2]["service_data"]["data"]["tag"],
            "parity-jess-client",
        )

        jess_error, jess_mcp = self.call_tool(
            "notify_jess",
            {
                "title": "Parity Jess mcp",
                "message": "MCP path",
                "tag": "parity-jess-mcp",
                "group": "parity-suite",
            },
        )
        self.assertFalse(jess_error)
        self.assertEqual(jess_mcp["status"], "sent")
        self.assertEqual(len(self.fake.calls), 4)
        self.assertEqual(self.fake.calls[3]["notify_service"], JESS_SERVICE)
        self.assertEqual(self.fake.calls[3]["service_data"]["message"], "MCP path")

        history = self.api.list_notifications(group="parity-suite", limit=10)
        self.assertEqual(
            [item["title"] for item in history["notifications"]],
            [
                "Parity Jess mcp",
                "Parity Jess client",
                "Parity Joe mcp",
                "Parity Joe client",
            ],
        )
        self.assertEqual(
            [item["tag"] for item in history["notifications"]],
            [
                "parity-jess-mcp",
                "parity-jess-client",
                "parity-joe-mcp",
                "parity-joe-client",
            ],
        )
        self.assertTrue(all(item["status"] == "sent" for item in history["notifications"]))
        self.assertTrue(all(item["ha_context_id"] == "context-123" for item in history["notifications"]))
        self.assertTrue(all(item["actions"] == [] for item in history["notifications"]))
        self.assertRegex(history["notifications"][0]["created_at"], TIMESTAMP)

        limited = self.api.list_notifications(group="parity-suite", limit=1)
        self.assertEqual([item["title"] for item in limited["notifications"]], ["Parity Jess mcp"])
        list_error, listed_mcp = self.call_tool(
            "list_notifications",
            {"group": "parity-suite", "limit": 1},
        )
        self.assertFalse(list_error)
        self.assertEqual(listed_mcp, limited)

        action = self.api.record_notification_action(
            "PARITY_ACK",
            tag="parity-joe-client",
            group="parity-suite",
            reply_text="done",
            event={"sourceDeviceName": "Pixel"},
        )
        self.assertEqual(action["status"], "recorded")
        self.assertEqual(action["notification_id"], joe["notification_id"])
        self.assertIsInstance(action["action_id"], int)
        recorded = self.api.list_notifications(tag="parity-joe-client", limit=5)
        self.assertEqual(recorded["notifications"][0]["status"], "responded")
        self.assertEqual(recorded["notifications"][0]["actions"][0]["action"], "PARITY_ACK")
        self.assertEqual(recorded["notifications"][0]["actions"][0]["reply_text"], "done")
        self.assertEqual(
            recorded["notifications"][0]["actions"][0]["event"],
            {"sourceDeviceName": "Pixel"},
        )

        mcp_action_error, mcp_action = self.call_tool(
            "record_notification_action",
            {
                "action": "PARITY_ACK_MCP",
                "tag": "parity-joe-mcp",
                "group": "parity-suite",
                "reply_text": "mcp-done",
                "event": {"sourceDeviceName": "Pixel"},
            },
        )
        self.assertFalse(mcp_action_error)
        self.assertEqual(mcp_action["status"], "recorded")
        self.assertEqual(mcp_action["notification_id"], joe_mcp["notification_id"])
        mcp_recorded_error, mcp_recorded = self.call_tool(
            "list_notifications",
            {"tag": "parity-joe-mcp", "limit": 5},
        )
        self.assertFalse(mcp_recorded_error)
        self.assertEqual(mcp_recorded["notifications"][0]["status"], "responded")
        self.assertEqual(mcp_recorded["notifications"][0]["actions"][0]["action"], "PARITY_ACK_MCP")
        self.assertEqual(mcp_recorded["notifications"][0]["actions"][0]["reply_text"], "mcp-done")

        self.assertEqual(self.api.get_service_catalog(), CATALOG)
        catalog_error, catalog = self.call_tool("get_service_catalog")
        self.assertFalse(catalog_error)
        self.assertEqual(catalog, CATALOG)
        self.assertEqual(self.api.get_smoke_signal_targets(), SMOKE_TARGETS)
        smoke_error, smoke = self.call_tool("get_smoke_signal_targets")
        self.assertFalse(smoke_error)
        self.assertEqual(smoke, SMOKE_TARGETS)
        self.assertEqual(len(self.fake.calls), 4)

        unicode_error, unicode_sent = self.call_tool(
            "notify_joe",
            {"title": "温度", "message": "café", "tag": "parity-unicode"},
        )
        self.assertFalse(unicode_error)
        self.assertEqual(unicode_sent["status"], "sent")
        self.assertEqual(self.fake.calls[-1]["service_data"]["title"], "温度")
        self.assertEqual(self.fake.calls[-1]["service_data"]["message"], "café")
        listed_unicode_error, listed_unicode = self.call_tool(
            "list_notifications",
            {"tag": "parity-unicode", "limit": 5},
        )
        self.assertFalse(listed_unicode_error)
        self.assertEqual(listed_unicode["notifications"][0]["title"], "温度")
        self.assertEqual(listed_unicode["notifications"][0]["message"], "café")
        self.assertIn("温度".encode("utf-8"), self.last_stdout)
        self.assertIn("café".encode("utf-8"), self.last_stdout)

        with self.assertRaises(HomelabFunctionsError) as missing_summary:
            self.api.record_workflow_report("parity-client-flow", "")
        self.assertEqual(missing_summary.exception.status, 400)
        self.assertEqual(missing_summary.exception.code, "invalid_request")
        self.assertEqual(missing_summary.exception.detail, "summary")
        bad_report_error, bad_report = self.call_tool(
            "record_workflow_report",
            {"workflow_slug": "parity-mcp-flow", "summary": ""},
        )
        self.assertTrue(bad_report_error)
        self.assertEqual(bad_report["status"], 400)
        self.assertEqual(bad_report["code"], "invalid_request")
        self.assertEqual(bad_report["detail"], "summary")
        self.assertNotIn(TOKEN, bad_report["message"])

        created = self.api.record_workflow_report(
            "parity-client-flow",
            "Client bowl sensor stayed empty.",
            source="parity-client",
            notification_id=42,
            event={"sourceDeviceName": "Pixel", "origin": "client"},
        )
        self.assertEqual(created["status"], "reported")
        self.assertEqual(created["report_id"], created["report"]["id"])
        self.assertEqual(created["report"]["workflow_slug"], "parity-client-flow")
        self.assertEqual(created["report"]["summary"], "Client bowl sensor stayed empty.")
        self.assertEqual(created["report"]["source"], "parity-client")
        self.assertEqual(created["report"]["notification_id"], 42)
        self.assertEqual(created["report"]["status"], "reported")
        self.assertEqual(
            created["report"]["event"],
            {"sourceDeviceName": "Pixel", "origin": "client"},
        )
        self.assertRegex(created["report"]["created_at"], TIMESTAMP)

        mcp_created_error, mcp_created = self.call_tool(
            "record_workflow_report",
            {
                "workflow_slug": "parity-mcp-flow",
                "summary": "MCP bowl sensor stayed empty.",
                "source": "parity-mcp",
                "notification_id": 43,
                "event": {"sourceDeviceName": "Pixel", "origin": "mcp"},
            },
        )
        self.assertFalse(mcp_created_error)
        self.assertEqual(mcp_created["status"], "reported")
        self.assertEqual(mcp_created["report"]["summary"], "MCP bowl sensor stayed empty.")
        self.assertEqual(mcp_created["report"]["notification_id"], 43)
        self.assertEqual(mcp_created["report"]["event"]["origin"], "mcp")

        client_list = self.api.list_workflow_reports(workflow="parity-client-flow", limit=20)
        self.assertEqual(len(client_list["reports"]), 1)
        self.assertEqual(client_list["reports"][0]["id"], created["report_id"])
        self.assertEqual(client_list["reports"][0]["summary"], "Client bowl sensor stayed empty.")
        mcp_list_error, mcp_list = self.call_tool(
            "list_workflow_reports",
            {"workflow": "parity-mcp-flow", "limit": 20},
        )
        self.assertFalse(mcp_list_error)
        self.assertEqual(len(mcp_list["reports"]), 1)
        self.assertEqual(mcp_list["reports"][0]["summary"], "MCP bowl sensor stayed empty.")

        fetched = self.api.get_workflow_report(created["report_id"])
        self.assertEqual(fetched["report"]["id"], created["report_id"])
        self.assertEqual(fetched["report"]["summary"], "Client bowl sensor stayed empty.")
        mcp_get_error, mcp_fetched = self.call_tool(
            "get_workflow_report",
            {"report_id": mcp_created["report_id"]},
        )
        self.assertFalse(mcp_get_error)
        self.assertEqual(mcp_fetched["report"]["id"], mcp_created["report_id"])
        self.assertEqual(mcp_fetched["report"]["workflow_slug"], "parity-mcp-flow")

        with self.assertRaises(HomelabFunctionsError) as missing:
            self.api.get_workflow_report(999999)
        self.assertEqual(missing.exception.status, 404)
        self.assertEqual(missing.exception.code, "not_found")
        self.assertEqual(str(missing.exception), "Workflow report not found")
        missing_error, missing_body = self.call_tool("get_workflow_report", {"report_id": 999999})
        self.assertTrue(missing_error)
        self.assertEqual(missing_body["status"], 404)
        self.assertEqual(missing_body["code"], "not_found")
        self.assertEqual(missing_body["detail"], "report_id")
        self.assertEqual(missing_body["message"], "Workflow report not found")
        self.assertNotIn(TOKEN, json.dumps(missing_body))
        self.assertNotIn(TOKEN, self.read_stderr())
