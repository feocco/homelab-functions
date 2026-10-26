"""Newline-delimited JSON-RPC MCP server for homelab-functions actions.

The process speaks MCP on stdout and keeps logs on stderr. It uses the
standard library plus homelab.access and homelab.client.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .access import ACCESS_VECTOR
from .client import HomelabFunctionsClient, HomelabFunctionsError


SUPPORTED_PROTOCOL_VERSIONS = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
)
SERVER_NAME = "homelab-functions"
SERVER_VERSION = "0.1.0"


def main() -> None:
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            return
        if not line.strip():
            continue
        response = _dispatch_line(line)
        if response is not None:
            _write(response)


def _dispatch_line(line: bytes) -> dict[str, Any] | None:
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return _rpc_error(None, -32700, "Parse error")
    try:
        message = json.loads(text)
    except json.JSONDecodeError:
        return _rpc_error(None, -32700, "Parse error")
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    return _dispatch(message)


def _dispatch(message: dict[str, Any]) -> dict[str, Any] | None:
    if message.get("jsonrpc") != "2.0":
        return _rpc_error(message.get("id"), -32600, "Invalid Request")
    if _is_response(message):
        return None
    method = message.get("method")
    if not isinstance(method, str):
        return _rpc_error(message.get("id"), -32600, "Invalid Request")
    if "id" not in message:
        return None

    request_id = message["id"]
    params = message.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _rpc_error(request_id, -32602, "Invalid params")

    if method == "initialize":
        return _result(request_id, _initialize_result(params))
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": _tools()})
    if method == "tools/call":
        return _call_tool(request_id, params)
    return _rpc_error(request_id, -32601, "Method not found")


def _is_response(message: dict[str, Any]) -> bool:
    if isinstance(message.get("method"), str):
        return False
    return "result" in message or "error" in message


def _initialize_result(params: dict[str, Any]) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        version = requested
    else:
        version = SUPPORTED_PROTOCOL_VERSIONS[-1]
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": operation.operation_id,
            "description": operation.description,
            "inputSchema": operation.input_schema(),
        }
        for operation in ACCESS_VECTOR
    ]


def _call_tool(request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or name not in ACCESS_VECTOR:
        return _rpc_error(request_id, -32602, "Unknown tool")

    arguments = params.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _rpc_error(request_id, -32602, "Invalid params")

    client = HomelabFunctionsClient()
    try:
        payload = client.call(name, arguments)
    except HomelabFunctionsError as exc:
        return _result(request_id, _tool_error(exc, client.token))
    except Exception as exc:
        _log(_redact(f"{type(exc).__name__}: {exc}", client.token))
        return _rpc_error(request_id, -32603, "Internal error")
    return _result(request_id, _tool_result(payload))


def _tool_result(payload: Any) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": _deterministic_json(payload)}],
        "isError": False,
    }


def _tool_error(exc: HomelabFunctionsError, token: str) -> dict[str, Any]:
    body = {
        "code": _redact(exc.code or "", token),
        "detail": _redact(exc.detail or "", token),
        "message": _redact(str(exc), token),
        "status": exc.status,
    }
    return {
        "content": [{"type": "text", "text": _deterministic_json(body)}],
        "isError": True,
    }


def _deterministic_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _redact(text: str, token: str) -> str:
    if token:
        return text.replace(token, "[redacted]")
    return text


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.buffer.write((_deterministic_json(payload) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _log(text: str) -> None:
    sys.stderr.buffer.write(text.encode("utf-8") + b"\n")
    sys.stderr.buffer.flush()


if __name__ == "__main__":
    main()
