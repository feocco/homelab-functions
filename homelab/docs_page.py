"""HTML reference for `GET /docs`, rendered from the access registry.

Operation cards come from `ACCESS_VECTOR`; the caller matrix and recipes come
from `CONSUMERS`. The page carries a Content-Security-Policy that blocks all
network calls from the browser, so it cannot run authenticated requests.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator, Mapping
from functools import cache
from html import escape
from typing import Any

from .access import ACCESS_VECTOR, ERROR_RESPONSE_SCHEMA, PUBLIC_ENDPOINTS, AccessOperation
from .client import DEFAULT_FUNCTIONS_URL
from .consumers import CONSUMERS, Consumer


TITLE = "homelab-functions"
VERSION = "0.1.0"
DESCRIPTION = "Notification and homelab API service for stable reusable actions."
CALL_SHAPES = ("http", "python", "mcp")
_SHAPE_LABELS = {"http": "HTTP", "python": "Python", "mcp": "MCP"}
_SHAPE_LANGUAGES = {"http": "shell", "python": "python", "mcp": "json"}

DOMAIN_SUMMARIES = {
    "Notifications": (
        "Send phone notifications through Home Assistant and read the shared ledger of what "
        "was sent and how Joe or Jess responded."
    ),
    "Catalog": (
        "Read JSON files that homelab-config generates at deploy time. The broker checks the "
        "token and returns the file unchanged."
    ),
    "Workflow reports": (
        "Durable “please investigate” records submitted from a notification’s Report button."
    ),
}

EXAMPLE_VALUES: Mapping[str, Any] = {
    "title": "Plant status",
    "message": "Ficus needs water.",
    "tag": "plant-monitor-ficus",
    "group": "plant-monitor",
    "url": "/lovelace/plants",
    "buttons": [{"title": "Open plants", "uri": "/lovelace/plants"}],
    "limit": 20,
    "action": "PLANT_REPLY::ficus",
    "reply_text": "Watered today",
    "event": {"sourceDeviceName": "Pixel"},
    "workflow_slug": "cat-food-monitor",
    "summary": "Morning food check failed.",
    "source": "mobile-action",
    "notification_id": 42,
    "workflow": "cat-food-monitor",
    "report_id": 123,
}
_FALLBACK_BY_TYPE: Mapping[str, Any] = {
    "string": "example",
    "integer": 1,
    "object": {},
    "array": [],
}


def example_arguments(operation: AccessOperation) -> dict[str, Any]:
    """Return an example value for every parameter of one operation."""

    arguments: dict[str, Any] = {}
    for parameter in operation.parameters:
        if parameter.name in EXAMPLE_VALUES:
            arguments[parameter.name] = EXAMPLE_VALUES[parameter.name]
        else:
            arguments[parameter.name] = _FALLBACK_BY_TYPE[parameter.json_schema["type"]]
    return arguments


def http_example(operation: AccessOperation) -> str:
    arguments = example_arguments(operation)
    path = operation.path
    query: list[tuple[str, str]] = []
    body: dict[str, Any] = {}
    for parameter in operation.parameters:
        value = arguments[parameter.name]
        if parameter.location == "path":
            path = path.replace("{" + parameter.name + "}", str(value))
        elif parameter.location == "query":
            query.append((parameter.name, str(value)))
        else:
            body[parameter.name] = value
    lines = [
        f'curl -sS -X {operation.method} "$HOMELAB_FUNCTIONS_URL{path}"',
        '  -H "Authorization: Bearer $HOMELAB_FUNCTIONS_TOKEN"',
    ]
    if query:
        lines.append("  -G")
        lines.extend(f'  --data-urlencode "{name}={value}"' for name, value in query)
    if body:
        payload = json.dumps(body, indent=2, ensure_ascii=False)
        if "'" in payload:
            raise ValueError(f"{operation.operation_id} example body cannot contain single quotes")
        lines.append('  -H "Content-Type: application/json"')
        lines.append(f"  -d '{payload}'")
    return " \\\n".join(lines)


def python_example(operation: AccessOperation) -> str:
    arguments = example_arguments(operation)
    rendered = [
        _python_literal(arguments[parameter.name])
        if parameter.required
        else f"{parameter.name}={_python_literal(arguments[parameter.name])}"
        for parameter in operation.parameters
    ]
    call = f"client.{operation.operation_id}("
    if rendered:
        call += "\n" + "".join(f"    {item},\n" for item in rendered)
    call += ")"
    return (
        "from homelab import HomelabFunctionsClient\n\n"
        "# Reads HOMELAB_FUNCTIONS_URL and HOMELAB_FUNCTIONS_TOKEN.\n"
        "client = HomelabFunctionsClient()\n"
        f"result = {call}"
    )


def mcp_example(operation: AccessOperation) -> str:
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": operation.operation_id, "arguments": example_arguments(operation)},
    }
    return json.dumps(message, indent=2, ensure_ascii=False)


_EXAMPLE_RENDERERS = {"http": http_example, "python": python_example, "mcp": mcp_example}


def _python_literal(value: Any) -> str:
    _reject_non_json_literals(value)
    return json.dumps(value, ensure_ascii=False)


def _reject_non_json_literals(value: Any) -> None:
    if value is None or isinstance(value, bool):
        raise ValueError("Example values must not use null or booleans")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_non_json_literals(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_non_json_literals(item)


def schema_rows(
    name: str,
    schema: Mapping[str, Any],
    *,
    required: bool,
    default: Any = None,
    has_default: bool = False,
) -> Iterator[dict[str, Any]]:
    """Flatten a JSON schema into one row per field, nested fields included."""

    yield {
        "name": name,
        "type": _type_label(schema),
        "required": required,
        "constraints": _constraints(schema, default if has_default else None, has_default),
    }
    if schema.get("type") == "array" and isinstance(schema.get("items"), Mapping):
        yield from _object_rows(f"{name}[]", schema["items"])
    elif schema.get("type") == "object":
        yield from _object_rows(name, schema)


def _object_rows(prefix: str, schema: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    required = set(schema.get("required", ()))
    for child, child_schema in schema.get("properties", {}).items():
        name = f"{prefix}.{child}" if prefix else child
        yield from schema_rows(name, child_schema, required=child in required)


def _type_label(schema: Mapping[str, Any]) -> str:
    kind = schema.get("type", "any")
    if kind == "array" and isinstance(schema.get("items"), Mapping):
        return f"array<{schema['items'].get('type', 'any')}>"
    return str(kind)


def _constraints(schema: Mapping[str, Any], default: Any, has_default: bool) -> list[str]:
    notes: list[str] = []
    if has_default:
        notes.append(f"default {json.dumps(default)}")
    if "maxItems" in schema:
        notes.append(f"at most {schema['maxItems']} items")
    if "minItems" in schema:
        notes.append(f"at least {schema['minItems']} items")
    if "enum" in schema:
        notes.append("one of " + ", ".join(json.dumps(item) for item in schema["enum"]))
    if schema.get("type") == "object" and not schema.get("properties"):
        notes.append("any JSON object")
    return notes


def _code(text: str) -> str:
    return f"<code>{escape(text)}</code>"


def _op_link(operation_id: str) -> str:
    ACCESS_VECTOR.get(operation_id)
    return f'<a class="chip" href="#op-{escape(operation_id)}"><code>{escape(operation_id)}</code></a>'


def _domain_id(domain: str) -> str:
    return "domain-" + "-".join(domain.lower().split())


def _code_block(text: str, language: str, label: str) -> str:
    return (
        '<div class="code">'
        f'<div class="code-head"><span>{escape(label)}</span>'
        '<button type="button" class="copy" data-copy>Copy</button></div>'
        f'<pre><code class="language-{escape(language)}">{escape(text)}</code></pre>'
        "</div>"
    )


def _path_html(path: str) -> str:
    parts: list[str] = []
    for segment in path.split("/")[1:]:
        if segment.startswith("{") and segment.endswith("}"):
            parts.append(f'/<span class="path-param">{escape(segment)}</span>')
        else:
            parts.append(f"/{escape(segment)}")
    return "".join(parts)


def _method_badge(method: str) -> str:
    kind = method.lower() if method in {"GET", "POST"} else "other"
    return f'<span class="method method-{kind}">{escape(method)}</span>'


def _cell(label: str, content: str, extra_class: str = "") -> str:
    class_attr = f' class="{extra_class}"' if extra_class else ""
    return f'<td data-label="{escape(label)}"{class_attr}><div class="cell">{content}</div></td>'


def _parameter_table(operation: AccessOperation) -> str:
    if not operation.parameters:
        return '<p class="muted">No parameters.</p>'
    rows: list[str] = []
    for parameter in operation.parameters:
        for row in schema_rows(
            parameter.name,
            parameter.json_schema,
            required=parameter.required,
            default=parameter.default,
            has_default=parameter.has_default(),
        ):
            nested = row["name"] != parameter.name
            name = row["name"]
            if nested:
                head, _, leaf = name.rpartition(".")
                name_html = f'<code><span class="faint">{escape(head)}.</span>{escape(leaf)}</code>'
            else:
                name_html = f"<code><strong>{escape(name)}</strong></code>"
            required = (
                '<span class="pill pill-required">required</span>'
                if row["required"]
                else '<span class="pill">optional</span>'
            )
            constraints = "; ".join(escape(note) for note in row["constraints"])
            rows.append(
                f'<tr data-param="{escape(row["name"])}"{' class="nested"' if nested else ""}>'
                + _cell("Name", name_html)
                + _cell("In", f'<span class="in">in </span>{escape(parameter.location)}')
                + _cell("Type", _code(row["type"]))
                + _cell("Required", required)
                + _cell(
                    "Constraints",
                    constraints or '<span class="faint">—</span>',
                    "" if constraints else "empty",
                )
                + "</tr>"
            )
    return (
        '<div class="table-wrap"><table class="stack params">'
        "<thead><tr><th>Name</th><th>In</th><th>Type</th><th>Required</th><th>Constraints</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _response_table(operation: AccessOperation) -> str:
    rows = [
        '<tr data-status="200">'
        + _cell("Status", '<span class="status status-2xx">200</span>')
        + _cell("Meaning", escape(operation.summary))
        + _cell("Body", "JSON")
        + "</tr>"
    ]
    for error in operation.errors:
        family = f"status-{error.status[0]}xx"
        rows.append(
            f'<tr data-status="{escape(error.status)}">'
            + _cell("Status", f'<span class="status {family}">{escape(error.status)}</span>')
            + _cell("Meaning", escape(error.description))
            + _cell("Body", '<a href="#errors"><code>ErrorResponse</code></a>')
            + "</tr>"
        )
    return (
        '<div class="table-wrap"><table class="stack responses">'
        "<thead><tr><th>Status</th><th>Meaning</th><th>Body</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _examples(operation: AccessOperation) -> str:
    op_id = escape(operation.operation_id)
    tabs: list[str] = []
    panels: list[str] = []
    for index, shape in enumerate(CALL_SHAPES):
        label = _SHAPE_LABELS[shape]
        tab_id = f"tab-{op_id}-{shape}"
        panel_id = f"panel-{op_id}-{shape}"
        selected = "true" if index == 0 else "false"
        tabs.append(
            f'<button type="button" role="tab" id="{tab_id}" aria-controls="{panel_id}" '
            f'aria-selected="{selected}" data-shape="{shape}">{label}</button>'
        )
        caption = {
            "http": "curl",
            "python": "HomelabFunctionsClient",
            "mcp": "tools/call over stdio",
        }[shape]
        panels.append(
            f'<div class="panel" role="tabpanel" id="{panel_id}" aria-labelledby="{tab_id}" '
            f'data-shape="{shape}">'
            f'<p class="panel-label">{label}</p>'
            + _code_block(_EXAMPLE_RENDERERS[shape](operation), _SHAPE_LANGUAGES[shape], caption)
            + "</div>"
        )
    return (
        '<div class="tabs" data-tabs>'
        f'<div class="tablist" role="tablist" aria-label="Call shapes for {op_id}">{"".join(tabs)}</div>'
        f"{''.join(panels)}</div>"
    )


def _operation_card(operation: AccessOperation) -> str:
    op_id = escape(operation.operation_id)
    meta = [
        ("Operation", _code(operation.operation_id)),
        ("Auth", "Bearer token"),
        ("Python", _code(f"client.{operation.operation_id}()")),
        ("MCP tool", _code(operation.operation_id)),
    ]
    if operation.body_component:
        meta.append(("Body schema", _code(operation.body_component)))
    elif operation.body_parameters():
        meta.append(("Body", "JSON object"))
    meta_html = "".join(f"<div><dt>{label}</dt><dd>{value}</dd></div>" for label, value in meta)
    return (
        f'<article class="op" id="op-{op_id}" data-operation="{op_id}" '
        f'data-method="{escape(operation.method)}" data-path="{escape(operation.path)}">'
        '<div class="op-head">'
        f"{_method_badge(operation.method)}"
        f'<code class="path">{_path_html(operation.path)}</code>'
        f'<a class="anchor" href="#op-{op_id}" aria-label="Link to {op_id}">#</a>'
        "</div>"
        f"<h3>{escape(operation.summary)}</h3>"
        f"<p>{escape(operation.description)}</p>"
        f'<dl class="meta">{meta_html}</dl>'
        "<h4>Parameters</h4>"
        f"{_parameter_table(operation)}"
        "<h4>Responses</h4>"
        f"{_response_table(operation)}"
        "<h4>Examples</h4>"
        f"{_examples(operation)}"
        "</article>"
    )


def _domain_sections() -> str:
    domains = ACCESS_VECTOR.domains()
    if set(DOMAIN_SUMMARIES) != set(domains):
        raise RuntimeError("DOMAIN_SUMMARIES must describe every ACCESS_VECTOR domain")
    sections: list[str] = []
    for domain in domains:
        cards = "".join(
            _operation_card(operation) for operation in ACCESS_VECTOR if operation.domain == domain
        )
        sections.append(
            f'<section id="{_domain_id(domain)}" data-domain="{escape(domain)}">'
            '<p class="eyebrow">Reference</p>'
            f"<h2>{escape(domain)}</h2>"
            f'<p class="lead">{escape(DOMAIN_SUMMARIES[domain])}</p>'
            f"{cards}</section>"
        )
    return "".join(sections)


def _consumer_label(consumer: Consumer) -> str:
    label = _code(consumer.name) if consumer.name_is_identifier else escape(consumer.name)
    if consumer.note:
        label += f' <span class="muted">{escape(consumer.note)}</span>'
    if consumer.members:
        label += '<div class="members">' + " ".join(_code(member) for member in consumer.members) + "</div>"
    return label


def _consumer_actions(consumer: Consumer) -> str:
    items: list[str] = []
    for use in consumer.uses:
        condition = f' <span class="muted">{escape(use.condition)}</span>' if use.condition else ""
        items.append(f"<li>{_op_link(use.operation_id)}{condition}</li>")
    for helper in consumer.helpers:
        call = f'{escape(helper.name)}(<wbr>{escape(helper.arguments)})'
        items.append(f'<li><code class="call">{call}</code> <span class="muted">local helper</span></li>')
    return f'<ul class="uses">{"".join(items)}</ul>'


def _code_list(values: tuple[str, ...]) -> str:
    return " ".join(_code(value) for value in values) or '<span class="faint">—</span>'


def _consumer_matrix() -> str:
    rows = "".join(
        f'<tr data-consumer="{escape(consumer.slug)}">'
        + _cell("Caller", _consumer_label(consumer))
        + _cell("Actions", _consumer_actions(consumer))
        + _cell("Group", _code_list(consumer.groups))
        + _cell("Tags", _code_list(consumer.tags))
        + _cell("Action prefixes", _code_list(consumer.action_prefixes))
        + "</tr>"
        for consumer in CONSUMERS
    )
    return (
        '<div class="table-wrap"><table class="stack matrix">'
        "<thead><tr><th>Caller</th><th>Actions</th><th>Group</th><th>Tags</th>"
        "<th>Action prefixes</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


def _recipes() -> str:
    cards: list[str] = []
    for consumer in CONSUMERS:
        uses = " ".join(_op_link(operation_id) for operation_id in consumer.operation_ids())
        cards.append(
            f'<article class="recipe" id="recipe-{escape(consumer.slug)}" '
            f'data-consumer="{escape(consumer.slug)}">'
            f"<h3>{escape(consumer.recipe_title)}</h3>"
            f'<p class="muted">For {_consumer_label(consumer)}. Uses {uses}.</p>'
            + _code_block(consumer.recipe.rstrip("\n"), "python", "python")
            + "</article>"
        )
    return "".join(cards)


def _schema_table(schema: Mapping[str, Any], root: str) -> str:
    rows: list[str] = []
    for row in _object_rows(root, schema):
        required = (
            '<span class="pill pill-required">required</span>'
            if row["required"]
            else '<span class="pill">optional</span>'
        )
        rows.append(
            "<tr>"
            + _cell("Field", _code(row["name"]))
            + _cell("Type", _code(row["type"]))
            + _cell("Required", required)
            + "</tr>"
        )
    return (
        '<div class="table-wrap"><table class="stack">'
        "<thead><tr><th>Field</th><th>Type</th><th>Required</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _public_table() -> str:
    rows = "".join(
        f'<tr data-public="{escape(endpoint.path)}">'
        + _cell("Method", _method_badge(endpoint.method))
        + _cell("Path", f'<a href="{escape(endpoint.path)}"><code>{escape(endpoint.path)}</code></a>')
        + _cell("Purpose", escape(endpoint.summary))
        + _cell("Auth", "None")
        + "</tr>"
        for endpoint in PUBLIC_ENDPOINTS
    )
    return (
        '<div class="table-wrap"><table class="stack">'
        "<thead><tr><th>Method</th><th>Path</th><th>Purpose</th><th>Auth</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


def _nav() -> str:
    reference = "".join(
        f'<li><a href="#{_domain_id(domain)}">{escape(domain)}</a>'
        '<ul class="nav-ops">'
        + "".join(
            f'<li><a href="#op-{escape(op.operation_id)}"><span class="nav-method nav-{op.method.lower()}">'
            f"{escape(op.method)}</span>{escape(op.operation_id)}</a></li>"
            for op in ACCESS_VECTOR
            if op.domain == domain
        )
        + "</ul></li>"
        for domain in ACCESS_VECTOR.domains()
    )
    return (
        '<nav class="sidebar" aria-label="Sections"><div class="nav-inner">'
        '<p class="nav-heading">Start</p><ul>'
        '<li><a href="#overview">Overview</a></li>'
        '<li><a href="#auth">Authentication</a></li>'
        '<li><a href="#urls">URLs &amp; environment</a></li>'
        "</ul>"
        '<p class="nav-heading">Use</p><ul>'
        '<li><a href="#callers">Callers</a></li>'
        '<li><a href="#recipes">Recipes</a></li>'
        "</ul>"
        f'<p class="nav-heading">Reference</p><ul>{reference}'
        '<li><a href="#public">Public endpoints</a></li>'
        '<li><a href="#errors">Errors</a></li>'
        "</ul>"
        '<p class="nav-heading">Understand</p><ul>'
        '<li><a href="#boundaries">Boundaries</a></li>'
        "</ul></div></nav>"
    )


_HA_SNIPPET = """import homelab

async with homelab.HomeAssistantWebSocketClient.from_env() as ha:
    states = await ha.get_states()
    await ha.subscribe_events("state_changed")
    await ha.call_service("switch", "turn_on", {"entity_id": "switch.example"})"""

_MCP_CONFIG = """{
  "mcpServers": {
    "homelab-functions": {
      "type": "stdio",
      "command": "python3",
      "args": ["-m", "homelab.mcp_server"],
      "env": {
        "HOMELAB_FUNCTIONS_URL": "${HOMELAB_FUNCTIONS_URL}",
        "HOMELAB_FUNCTIONS_TOKEN": "${HOMELAB_FUNCTIONS_TOKEN}"
      }
    }
  }
}"""

_UNAUTHORIZED_EXAMPLE = json.dumps(
    {"error": {"code": "unauthorized", "message": "Invalid or missing bearer token"}},
    indent=2,
)


def _main() -> str:
    protected = len(ACCESS_VECTOR)
    return f"""
<section id="overview">
  <p class="eyebrow">Start</p>
  <h1>{escape(TITLE)} API reference</h1>
  <p class="lead">{escape(DESCRIPTION)} The broker exposes a fixed set of named actions.
  Every action has the same three call shapes: HTTP, the Python client, and an MCP tool with the same name.</p>
  <div class="stats">
    <div class="stat"><span class="stat-value">{protected}</span><span class="stat-label">protected actions</span></div>
    <div class="stat"><span class="stat-value">{len(PUBLIC_ENDPOINTS)}</span><span class="stat-label">public endpoints</span></div>
    <div class="stat"><span class="stat-value">{len(CALL_SHAPES)}</span><span class="stat-label">call shapes per action</span></div>
  </div>
  <div class="table-wrap"><table class="stack">
    <thead><tr><th>Call shape</th><th>Entry point</th><th>Name of the action</th></tr></thead>
    <tbody>
      <tr>{_cell("Call shape", "HTTP")}{_cell("Entry point", _code("$HOMELAB_FUNCTIONS_URL/v1/…"))}{_cell("Name of the action", "Method and path")}</tr>
      <tr>{_cell("Call shape", "Python")}{_cell("Entry point", _code("homelab.HomelabFunctionsClient"))}{_cell("Name of the action", "Method named after the operation id")}</tr>
      <tr>{_cell("Call shape", "MCP")}{_cell("Entry point", _code("python3 -m homelab.mcp_server"))}{_cell("Name of the action", "Tool named after the operation id")}</tr>
    </tbody>
  </table></div>
  <p>Machine-readable schema: <a href="/openapi.json"><code>/openapi.json</code></a>. Liveness:
  <a href="/health"><code>/health</code></a> returns <code>status: ok</code>.</p>
</section>

<section id="auth">
  <p class="eyebrow">Start</p>
  <h2>Authentication</h2>
  <p>Every <code>/v1</code> action requires the shared bearer token:</p>
  {_code_block("Authorization: Bearer $HOMELAB_FUNCTIONS_TOKEN", "http", "header")}
  <div class="callout"><strong>This page does not store tokens or run authenticated calls.</strong>
  Its Content-Security-Policy sets <code>connect-src 'none'</code>, so the browser cannot send requests from it.
  Copy an example and run it from a shell, Python, or an MCP client.</div>
  <p>A missing or wrong token returns <code>401</code>:</p>
  {_code_block(_UNAUTHORIZED_EXAMPLE, "json", "401 response")}
</section>

<section id="urls">
  <p class="eyebrow">Start</p>
  <h2>URLs &amp; environment</h2>
  <p>The Python client and the MCP server read the same two variables.</p>
  <div class="table-wrap"><table class="stack">
    <thead><tr><th>Variable</th><th>Default</th><th>Notes</th></tr></thead>
    <tbody>
      <tr>{_cell("Variable", _code("HOMELAB_FUNCTIONS_URL"))}{_cell("Default", _code(DEFAULT_FUNCTIONS_URL))}{_cell("Notes", "Containers on the homelab network use " + _code("http://homelab-functions:8091") + ".")}</tr>
      <tr>{_cell("Variable", _code("HOMELAB_FUNCTIONS_TOKEN"))}{_cell("Default", "None. Required.")}{_cell("Notes", "Must match the server’s " + _code("HOMELAB_FUNCTIONS_TOKEN") + ".")}</tr>
    </tbody>
  </table></div>
  <p>The Cursor plugin starts the MCP server with this <code>mcp.json</code> entry:</p>
  {_code_block(_MCP_CONFIG, "json", "mcp.json")}
</section>

<section id="callers">
  <p class="eyebrow">Use</p>
  <h2>Callers</h2>
  <p class="lead">Who calls which action, and the ledger keys each caller owns. Groups, tags, and
  action prefixes are conventions for the shared notification ledger. The server does not enforce them.</p>
  {_consumer_matrix()}
</section>

<section id="recipes">
  <p class="eyebrow">Use</p>
  <h2>Recipes</h2>
  <p class="lead">One end-to-end example per caller. Module helpers such as
  <code>homelab.notify_joe</code> wrap <code>HomelabFunctionsClient</code> and read the same environment variables.</p>
  {_recipes()}
</section>

{_domain_sections()}

<section id="public">
  <p class="eyebrow">Reference</p>
  <h2>Public endpoints</h2>
  <p class="lead">These routes need no token. They are not part of the access registry.</p>
  {_public_table()}
</section>

<section id="errors">
  <p class="eyebrow">Reference</p>
  <h2>Errors</h2>
  <p>Every documented error status returns the <code>ErrorResponse</code> envelope.
  <code>detail</code> names the offending field when there is one.</p>
  {_schema_table(ERROR_RESPONSE_SCHEMA, "")}
</section>

<section id="boundaries">
  <p class="eyebrow">Understand</p>
  <h2>Boundaries</h2>
  <p class="lead">The broker is a short list of named actions. It is not a Home Assistant proxy.</p>
  <div class="table-wrap"><table class="stack">
    <thead><tr><th>Need</th><th>Use</th><th>Runs in</th></tr></thead>
    <tbody>
      <tr>{_cell("Need", "Send a phone notification")}{_cell("Use", _op_link("notify_joe") + " " + _op_link("notify_jess"))}{_cell("Runs in", "Broker")}</tr>
      <tr>{_cell("Need", "Read Home Assistant state")}{_cell("Use", _code("HomeAssistantWebSocketClient.get_states()"))}{_cell("Runs in", "Caller")}</tr>
      <tr>{_cell("Need", "Subscribe to Home Assistant events")}{_cell("Use", _code("HomeAssistantWebSocketClient.subscribe_events()"))}{_cell("Runs in", "Caller")}</tr>
      <tr>{_cell("Need", "Call a Home Assistant service")}{_cell("Use", _code("HomeAssistantWebSocketClient.call_service()"))}{_cell("Runs in", "Caller")}</tr>
      <tr>{_cell("Need", "Handle a notification button")}{_cell("Use", _code("NotificationActionRouter") + " in the caller’s listener, then " + _op_link("record_notification_action"))}{_cell("Runs in", "Caller, then broker ledger")}</tr>
    </tbody>
  </table></div>
  <p>The broker’s background recorder stores every <code>mobile_app_notification_action</code> event in the
  ledger. It does not run button handlers. Each service owns its own listener and business logic.</p>
  <p>For state, events, and service calls, connect to Home Assistant directly:</p>
  {_code_block(_HA_SNIPPET, "python", "python")}
</section>
"""


_STYLE = """
:root {
  color-scheme: light;
  --bg: #f6f7f9; --surface: #ffffff; --surface-2: #f1f3f6; --border: #e1e5eb; --border-strong: #cbd2dc;
  --text: #151a22; --muted: #535d6e; --faint: #626c7d;
  --accent: #1f5fd6; --accent-soft: #eaf0fc;
  --code-bg: #f4f6f9; --code-text: #1b2230;
  --get: #0a7347; --get-bg: #e1f3e9; --post: #1d4ccc; --post-bg: #e6ecfd; --other: #8a4b00; --other-bg: #fbefdc;
  --ok: #0a7347; --ok-bg: #e1f3e9; --warn: #8f5200; --warn-bg: #fdf0db; --bad: #b42318; --bad-bg: #fde8e7;
  --header-bg: rgba(246, 247, 249, 0.86);
  --shadow: 0 1px 2px rgba(16, 24, 40, 0.04), 0 1px 3px rgba(16, 24, 40, 0.06);
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --topbar: 56px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #0c1016; --surface: #131920; --surface-2: #19212b; --border: #252e3a; --border-strong: #354253;
    --text: #e6eaf0; --muted: #a8b2c0; --faint: #8b97a8;
    --accent: #7eabff; --accent-soft: #172440;
    --code-bg: #0f141b; --code-text: #d9e1ec;
    --get: #63d39c; --get-bg: #0f2c1e; --post: #93b2ff; --post-bg: #17244a; --other: #f2b766; --other-bg: #382812;
    --ok: #63d39c; --ok-bg: #0f2c1e; --warn: #f2b766; --warn-bg: #382812; --bad: #ff8a80; --bad-bg: #3a1614;
    --header-bg: rgba(12, 16, 22, 0.86);
    --shadow: none;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #0c1016; --surface: #131920; --surface-2: #19212b; --border: #252e3a; --border-strong: #354253;
  --text: #e6eaf0; --muted: #a8b2c0; --faint: #8b97a8;
  --accent: #7eabff; --accent-soft: #172440;
  --code-bg: #0f141b; --code-text: #d9e1ec;
  --get: #63d39c; --get-bg: #0f2c1e; --post: #93b2ff; --post-bg: #17244a; --other: #f2b766; --other-bg: #382812;
  --ok: #63d39c; --ok-bg: #0f2c1e; --warn: #f2b766; --warn-bg: #382812; --bad: #ff8a80; --bad-bg: #3a1614;
  --header-bg: rgba(12, 16, 22, 0.86);
  --shadow: none;
}
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; scroll-padding-top: calc(var(--topbar) + 16px); }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.6 var(--sans); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
code, pre { font-family: var(--mono); font-variant-ligatures: none; }
code { font-size: 0.88em; overflow-wrap: break-word; }
p code, li code, td code, dd code, .callout code {
  background: var(--surface-2); border: 1px solid var(--border); border-radius: 5px; padding: 0.05em 0.35em;
}
h1, h2, h3, h4 { line-height: 1.25; letter-spacing: -0.01em; }
h1 { font-size: 2.1rem; margin: 0.2rem 0 0.75rem; }
h2 { font-size: 1.55rem; margin: 0.2rem 0 0.6rem; }
h3 { font-size: 1.12rem; margin: 0.9rem 0 0.35rem; }
h4 { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin: 1.4rem 0 0.5rem; }
.muted { color: var(--muted); }
.faint { color: var(--faint); }
.skip { position: absolute; left: -9999px; top: 8px; z-index: 50; background: var(--surface); padding: 8px 12px; border-radius: 6px; }
.skip:focus { left: 8px; }

.topbar {
  position: sticky; top: 0; z-index: 30; height: var(--topbar);
  background: var(--header-bg); backdrop-filter: saturate(1.4) blur(10px); -webkit-backdrop-filter: saturate(1.4) blur(10px);
  border-bottom: 1px solid var(--border);
}
.topbar-inner { max-width: 1320px; margin: 0 auto; height: 100%; padding: 0 24px; display: flex; align-items: center; gap: 16px; }
.brand { display: flex; align-items: center; gap: 10px; min-width: 0; font-weight: 650; color: var(--text); white-space: nowrap; overflow: hidden; }
.brand:hover { text-decoration: none; }
.logo { flex-shrink: 0; width: 26px; height: 26px; border-radius: 7px; background: linear-gradient(135deg, var(--accent), #7c4dff); display: grid; place-items: center; color: #fff; font: 700 13px/1 var(--mono); }
.brand-name { overflow: hidden; text-overflow: ellipsis; }
.version { font: 500 12px/1 var(--mono); color: var(--muted); border: 1px solid var(--border); border-radius: 999px; padding: 4px 8px; }
.topbar-links { margin-left: auto; display: flex; flex-shrink: 0; align-items: center; gap: 6px; }
.topbar-links a { color: var(--muted); font-size: 14px; padding: 6px 10px; border-radius: 8px; white-space: nowrap; }
.topbar-links a:hover { color: var(--text); background: var(--surface-2); text-decoration: none; }
.theme-toggle {
  display: inline-grid; place-items: center; width: 36px; height: 36px; border-radius: 9px; cursor: pointer;
  border: 1px solid var(--border); background: var(--surface); color: var(--text);
}
.theme-toggle:hover { border-color: var(--border-strong); }
.theme-toggle svg { width: 18px; height: 18px; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

.layout { max-width: 1320px; margin: 0 auto; padding: 0 24px; display: grid; grid-template-columns: 276px minmax(0, 1fr); gap: 44px; }
.sidebar { position: sticky; top: var(--topbar); align-self: start; max-height: calc(100vh - var(--topbar)); overflow-y: auto; padding: 28px 0 40px; }
.sidebar ul { list-style: none; margin: 0; padding: 0; }
.nav-heading { margin: 20px 0 6px; font-size: 11px; font-weight: 650; text-transform: uppercase; letter-spacing: 0.08em; color: var(--faint); }
.nav-heading:first-child { margin-top: 0; }
.sidebar a { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 14px; padding: 5px 10px; border-radius: 7px; }
.sidebar a:hover { color: var(--text); background: var(--surface-2); text-decoration: none; }
.sidebar a[aria-current="true"] { color: var(--accent); background: var(--accent-soft); font-weight: 600; }
.nav-ops { margin: 2px 0 6px 10px !important; border-left: 1px solid var(--border); padding-left: 6px !important; }
.nav-ops a { font-family: var(--mono); font-size: 12.5px; padding: 3px 8px; }
.nav-method { font: 700 9.5px/1 var(--mono); padding: 3px 4px; border-radius: 4px; min-width: 34px; text-align: center; }
.nav-get { color: var(--get); background: var(--get-bg); }
.nav-post { color: var(--post); background: var(--post-bg); }

main { min-width: 0; max-width: 900px; padding: 36px 0 120px; }
section { margin-bottom: 64px; }
.eyebrow { margin: 0; font-size: 12px; font-weight: 650; text-transform: uppercase; letter-spacing: 0.08em; color: var(--accent); }
.lead { font-size: 1.04rem; color: var(--muted); margin-top: 0; }
.stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 24px 0; }
.stat { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; box-shadow: var(--shadow); }
.stat-value { display: block; font-size: 1.7rem; font-weight: 700; line-height: 1.1; }
.stat-label { color: var(--muted); font-size: 13px; }
.callout { background: var(--accent-soft); border: 1px solid var(--border); border-left: 3px solid var(--accent); border-radius: 8px; padding: 12px 16px; margin: 16px 0; }

.table-wrap { border: 1px solid var(--border); border-radius: 10px; overflow-x: auto; background: var(--surface); margin: 12px 0 16px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { text-align: left; font-size: 11.5px; font-weight: 650; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); background: var(--surface-2); padding: 9px 12px; }
td { padding: 10px 12px; border-top: 1px solid var(--border); vertical-align: top; }
tr.nested td:first-child { padding-left: 28px; }
.in { display: none; }
.params td:first-child, .responses td:first-child { white-space: nowrap; }
.matrix code { white-space: nowrap; overflow-wrap: normal; }
.matrix code.call { white-space: normal; }
.members { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.uses { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
.chip { display: inline-block; }
.chip:hover { text-decoration: none; }
.chip code { color: var(--accent); }
.pill { display: inline-block; font-size: 11.5px; font-weight: 600; padding: 1px 8px; border-radius: 999px; color: var(--muted); background: var(--surface-2); border: 1px solid var(--border); }
.pill-required { color: var(--bad); background: var(--bad-bg); border-color: transparent; }
.status { display: inline-block; font: 700 12px/1.6 var(--mono); padding: 0 8px; border-radius: 6px; }
.status-2xx { color: var(--ok); background: var(--ok-bg); }
.status-4xx { color: var(--warn); background: var(--warn-bg); }
.status-5xx { color: var(--bad); background: var(--bad-bg); }

.op, .recipe { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 20px 24px 22px; margin: 20px 0; box-shadow: var(--shadow); }
.recipe h3 { margin-top: 0; }
.op-head { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.method { font: 700 12px/1 var(--mono); padding: 6px 9px; border-radius: 7px; letter-spacing: 0.03em; }
.method-get { color: var(--get); background: var(--get-bg); }
.method-post { color: var(--post); background: var(--post-bg); }
.method-other { color: var(--other); background: var(--other-bg); }
.path { font-size: 15px; font-weight: 600; overflow-wrap: anywhere; min-width: 0; }
.path-param { color: var(--accent); }
.anchor { margin-left: auto; color: var(--faint); font-weight: 600; padding: 0 4px; }
.op h3 { margin-top: 12px; }
.op > p { margin: 0.2rem 0 0.8rem; color: var(--muted); }
.meta { display: flex; flex-wrap: wrap; gap: 8px 22px; margin: 0; font-size: 13px; }
.meta div { display: flex; gap: 6px; align-items: baseline; min-width: 0; }
.meta dd { min-width: 0; overflow-wrap: anywhere; }
.meta dt { color: var(--faint); }
.meta dd { margin: 0; }

.tablist { display: flex; gap: 2px; border-bottom: 1px solid var(--border); margin-bottom: 10px; }
.tablist [role="tab"] {
  appearance: none; background: none; border: 0; border-bottom: 2px solid transparent; margin-bottom: -1px;
  padding: 7px 12px; font: 600 13px/1.2 var(--sans); color: var(--muted); cursor: pointer;
}
.tablist [role="tab"]:hover { color: var(--text); }
.tablist [role="tab"][aria-selected="true"] { color: var(--text); border-bottom-color: var(--accent); }
.panel-label { margin: 10px 0 4px; font-size: 12px; font-weight: 650; color: var(--muted); }
.js .panel-label { display: none; }
.code { border: 1px solid var(--border); border-radius: 10px; background: var(--code-bg); overflow: hidden; margin: 10px 0; }
.code-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 5px 6px 5px 12px; border-bottom: 1px solid var(--border); font: 500 12px/1.4 var(--mono); color: var(--muted); }
.copy {
  font: 600 12px/1 var(--sans); color: var(--muted); background: var(--surface); border: 1px solid var(--border);
  border-radius: 6px; padding: 5px 9px; cursor: pointer;
}
.copy:hover { color: var(--text); border-color: var(--border-strong); }
.copy[data-copied] { color: var(--ok); }
pre { margin: 0; padding: 14px 16px; overflow-x: auto; font-size: 13px; line-height: 1.55; color: var(--code-text); tab-size: 4; }
pre code { font-size: inherit; overflow-wrap: normal; }

footer { color: var(--faint); font-size: 13px; border-top: 1px solid var(--border); padding-top: 20px; }

@media (max-width: 1000px) {
  .layout { display: block; padding: 0; }
  .sidebar {
    position: sticky; top: var(--topbar); z-index: 20; max-height: none; overflow-x: auto; overflow-y: hidden;
    padding: 8px 16px; background: var(--header-bg); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--border); scrollbar-width: none;
  }
  .sidebar::-webkit-scrollbar { display: none; }
  .nav-inner { display: flex; align-items: center; gap: 4px; width: max-content; }
  .sidebar ul { display: contents; }
  .nav-heading, .sidebar .nav-ops { display: none; }
  .sidebar a { white-space: nowrap; padding: 5px 10px; font-size: 13.5px; }
  html { scroll-padding-top: calc(var(--topbar) + 64px); }
  main { padding: 24px 16px 96px; max-width: none; }
  table.matrix thead { display: none; }
  table.matrix, table.matrix tbody, table.matrix tr, table.matrix td { display: block; width: 100%; }
  table.matrix tr { padding: 10px 12px; border-top: 1px solid var(--border); }
  table.matrix tr:first-child { border-top: 0; }
  table.matrix td { border: 0; padding: 3px 0; display: grid; grid-template-columns: 8rem minmax(0, 1fr); gap: 10px; }
  table.matrix td::before { content: attr(data-label); color: var(--faint); font-size: 11px; font-weight: 650; text-transform: uppercase; letter-spacing: 0.04em; padding-top: 3px; }
  table.matrix code { display: inline-block; max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
}
@media (max-width: 720px) {
  body { font-size: 15px; }
  h1 { font-size: 1.65rem; }
  h2 { font-size: 1.35rem; }
  .topbar-inner { padding: 0 14px; gap: 10px; }
  .version, .hide-sm { display: none; }
  .stats { grid-template-columns: 1fr; gap: 8px; }
  .stat { display: flex; align-items: baseline; gap: 10px; padding: 10px 14px; }
  .op, .recipe { padding: 16px 14px 18px; border-radius: 12px; }
  table.stack thead { display: none; }
  table.stack, table.stack tbody, table.stack tr, table.stack td { display: block; width: 100%; }
  table.stack tr { padding: 10px 12px; border-top: 1px solid var(--border); }
  table.stack tr:first-child { border-top: 0; }
  table.stack td { border: 0; padding: 3px 0; display: grid; grid-template-columns: 6.6rem minmax(0, 1fr); gap: 10px; white-space: normal !important; min-width: 0 !important; }
  table.stack td::before { content: attr(data-label); color: var(--faint); font-size: 11px; font-weight: 650; text-transform: uppercase; letter-spacing: 0.04em; padding-top: 3px; }
  table.stack .cell { min-width: 0; }
  table.stack .cell code { display: inline-block; max-width: 100%; overflow-wrap: anywhere; }
  table.params tr, table.responses tr { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; }
  table.params td, table.responses td { display: block; width: auto; padding: 0; }
  table.params td::before, table.responses td::before { content: none; }
  table.params td[data-label="Name"] { flex-basis: 100%; }
  table.params td[data-label="In"] { order: 3; color: var(--muted); font-size: 13px; }
  table.params td[data-label="Constraints"] { order: 4; flex-basis: 100%; color: var(--muted); font-size: 13px; }
  table.params td.empty { display: none; }
  table.params tr.nested { padding-left: 28px; }
  table.params tr.nested td:first-child { padding-left: 0; }
  .in { display: inline; }
  table.responses td[data-label="Meaning"] { flex: 1 1 10rem; }
  table.responses td[data-label="Body"] { flex-basis: 100%; font-size: 13px; color: var(--muted); }
  table.responses td[data-label="Body"]::before { content: "Body"; margin-right: 8px; color: var(--faint); font-size: 11px; font-weight: 650; text-transform: uppercase; letter-spacing: 0.04em; }
  table.responses td[data-label="Body"] .cell { display: inline; }
  pre { font-size: 12.5px; padding: 12px; }
}
@media (max-width: 360px) {
  .hide-xs { display: none; }
}
"""

_SCRIPT = """
(function () {
  var root = document.documentElement;
  var THEME_KEY = "hf-docs-theme";
  var SHAPE_KEY = "hf-docs-shape";
  function read(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function write(key, value) { try { localStorage.setItem(key, value); } catch (e) {} }
  var theme = read(THEME_KEY);
  if (theme === "light" || theme === "dark") root.setAttribute("data-theme", theme);
  root.classList.add("js");

  function selectShape(shape) {
    document.querySelectorAll("[data-tabs]").forEach(function (group) {
      var tabs = group.querySelectorAll('[role="tab"]');
      tabs.forEach(function (tab) {
        var on = tab.getAttribute("data-shape") === shape;
        tab.setAttribute("aria-selected", on ? "true" : "false");
        tab.tabIndex = on ? 0 : -1;
        document.getElementById(tab.getAttribute("aria-controls")).hidden = !on;
      });
    });
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.className = "sr-only";
      document.body.appendChild(area);
      area.select();
      var ok = document.execCommand("copy");
      document.body.removeChild(area);
      ok ? resolve() : reject();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.getElementById("theme-toggle").addEventListener("click", function () {
      var current = root.getAttribute("data-theme") ||
        (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      var next = current === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      write(THEME_KEY, next);
    });

    var shapes = ["http", "python", "mcp"];
    var saved = read(SHAPE_KEY);
    selectShape(shapes.indexOf(saved) >= 0 ? saved : "http");
    document.querySelectorAll('[data-tabs] [role="tab"]').forEach(function (tab) {
      tab.addEventListener("click", function () {
        var shape = tab.getAttribute("data-shape");
        selectShape(shape);
        write(SHAPE_KEY, shape);
      });
      tab.addEventListener("keydown", function (event) {
        if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
        var step = event.key === "ArrowRight" ? 1 : -1;
        var index = shapes.indexOf(tab.getAttribute("data-shape"));
        var next = shapes[(index + step + shapes.length) % shapes.length];
        selectShape(next);
        write(SHAPE_KEY, next);
        tab.parentNode.querySelector('[data-shape="' + next + '"]').focus();
      });
    });

    document.querySelectorAll("[data-copy]").forEach(function (button) {
      button.addEventListener("click", function () {
        var code = button.closest(".code").querySelector("code");
        copyText(code.textContent).then(function () {
          button.textContent = "Copied";
          button.setAttribute("data-copied", "");
          setTimeout(function () { button.textContent = "Copy"; button.removeAttribute("data-copied"); }, 1600);
        }, function () { button.textContent = "Select and copy"; });
      });
    });

    var nav = document.querySelector(".sidebar");
    var links = {};
    nav.querySelectorAll('a[href^="#"]').forEach(function (link) { links[link.getAttribute("href").slice(1)] = link; });
    if (!("IntersectionObserver" in window)) return;
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        var link = links[entry.target.id];
        if (!link) return;
        nav.querySelectorAll('[aria-current="true"]').forEach(function (item) { item.removeAttribute("aria-current"); });
        link.setAttribute("aria-current", "true");
        if (nav.scrollWidth > nav.clientWidth) nav.scrollTo({ left: Math.max(0, link.offsetLeft - 16) });
      });
    }, { rootMargin: "-90px 0px -70% 0px" });
    Object.keys(links).forEach(function (id) {
      var target = document.getElementById(id);
      if (target) observer.observe(target);
    });
  });
})();
"""


def _sha256(source: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(source.encode("utf-8")).digest()).decode() + "'"


DOCS_HEADERS: Mapping[str, str] = {
    "Content-Security-Policy": "; ".join(
        (
            "default-src 'none'",
            f"style-src {_sha256(_STYLE)}",
            f"script-src {_sha256(_SCRIPT)}",
            "connect-src 'none'",
            "img-src 'none'",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        )
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

_THEME_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">'
    '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor"/></svg>'
)


@cache
def service_docs_html() -> str:
    """Return the `GET /docs` page."""

    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(TITLE)} API reference</title>\n"
        '<meta name="color-scheme" content="light dark">\n'
        f"<style>{_STYLE}</style>\n"
        f"<script>{_SCRIPT}</script>\n"
        "</head>\n<body>\n"
        '<a class="skip" href="#main">Skip to content</a>\n'
        '<header class="topbar"><div class="topbar-inner">'
        f'<a class="brand" href="#overview"><span class="logo">hf</span><span class="brand-name">{escape(TITLE)}</span></a>'
        f'<span class="version">v{escape(VERSION)}</span>'
        '<div class="topbar-links">'
        '<a class="hide-xs" href="/openapi.json">OpenAPI</a>'
        '<a class="hide-sm" href="/health">Health</a>'
        '<button type="button" class="theme-toggle" id="theme-toggle">'
        f'{_THEME_ICON}<span class="sr-only">Toggle light and dark theme</span></button>'
        "</div></div></header>\n"
        f'<div class="layout">{_nav()}<main id="main">{_main()}'
        "<footer>Generated from <code>homelab.access.ACCESS_VECTOR</code> and "
        "<code>homelab.consumers.CONSUMERS</code>.</footer>"
        "</main></div>\n</body>\n</html>\n"
    )
