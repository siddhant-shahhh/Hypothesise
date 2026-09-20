"""MCP wire protocol — JSON-RPC 2.0 over newline-delimited stdio.

Implemented directly rather than via an SDK. MCP is JSON-RPC with a small
handshake; taking the dependency would pin us to one library's release cycle
and one language's idea of the spec, and we need to proxy messages we do not
understand rather than deserialise them into typed objects.

Framing: one JSON object per line, UTF-8, newline-terminated.
"""

from __future__ import annotations

import json
from typing import Any

JSONRPC = "2.0"

# Standard JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Ours. Outside the reserved -32768..-32000 range on purpose, so a client can
# tell a Weaver decision apart from a transport fault.
BLOCKED_BY_POLICY = -31000
APPROVAL_TIMEOUT = -31001
APPROVAL_DENIED = -31002


def request(id_: Any, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": JSONRPC, "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": JSONRPC, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def result(id_: Any, payload: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC, "id": id_, "result": payload}


def error(id_: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC, "id": id_, "error": err}


def is_request(msg: dict[str, Any]) -> bool:
    """A request expects a response. A notification does not.

    The distinction matters: replying to a notification is a protocol
    violation, and silently swallowing a request hangs the client forever.
    """
    return "method" in msg and "id" in msg


def is_notification(msg: dict[str, Any]) -> bool:
    return "method" in msg and "id" not in msg


def is_response(msg: dict[str, Any]) -> bool:
    return "id" in msg and ("result" in msg or "error" in msg)


def encode(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes | str) -> dict[str, Any] | None:
    """Parse one framed message. Returns None for blank or malformed lines.

    Never raises. A malformed line from an upstream server must not take the
    proxy down — the agent on the other side is mid-conversation.
    """
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    line = line.strip()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return None
    return msg if isinstance(msg, dict) else None


def tool_call_params(msg: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Extract (tool_name, arguments) from a tools/call request."""
    params = msg.get("params") or {}
    return params.get("name", ""), params.get("arguments") or {}
