#!/usr/bin/env python3
"""A minimal real MCP server over stdio, for end-to-end proxy tests.

Deliberately a separate PROCESS speaking the actual wire protocol, not a mock.
The bugs this phase is expected to produce — framing, id collisions, handshake
ordering, deadlock — are exactly the ones a mock cannot reproduce.

    --name NAME        server identity
    --slow-ms N        delay every tool call (timeout tests)
    --fail-tool NAME   return isError for this tool
"""

import argparse
import json
import sys
import time


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="fake-mcp")
    ap.add_argument("--slow-ms", type=int, default=0)
    ap.add_argument("--fail-tool", default="")
    ap.add_argument("--fail-text", default="permission denied",
                    help="error text for --fail-tool (denial vs absence)")
    ap.add_argument("--tools", default="",
                    help="comma-separated subset to expose (default: all)")
    a = ap.parse_args()

    tools = [
        {"name": "read_file", "description": "Read a file",
         "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
        {"name": "write_file", "description": "Write a file",
         "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
        {"name": "exec", "description": "Run a command",
         "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}}}},
    ]

    if a.tools:
        wanted = {t.strip() for t in a.tools.split(",") if t.strip()}
        tools = [t for t in tools if t["name"] in wanted]

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": a.name, "version": "0.0.1"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
        elif method == "tools/call":
            if a.slow_ms:
                time.sleep(a.slow_ms / 1000.0)
            name = (msg.get("params") or {}).get("name", "")
            if name == a.fail_tool:
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": a.fail_text}]}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"{a.name}:{name}:ok"}]}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"no method {method}"}})


if __name__ == "__main__":
    main()
