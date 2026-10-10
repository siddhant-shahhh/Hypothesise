#!/usr/bin/env python3
"""End-to-end smoke test: drive the proxy yourself, without an agent.

    python3 scripts/ops/smoke.py --config hypothesise.config.json

An empty dashboard has two completely different causes, and no way to tell
them apart from the dashboard:

  A. Hypothesise is broken — the proxy did not start, an upstream failed, or events
     are not reaching the control plane.
  B. Hypothesise is fine and the agent simply never called it. Coding agents have
     their own built-in file tools and often prefer them to an MCP filesystem
     server, so a session can look busy and produce no MCP traffic at all.

This script removes the agent from the picture. It starts the proxy exactly as
your MCP client would, speaks JSON-RPC to it over stdin/stdout, lists the tools
the agent would see, calls a few read-only ones, and then checks whether
`/v1/stats` went up. If this passes, Hypothesise works and the answer is B.

Imports nothing from hypothesise — it launches the CLI as a subprocess, which is the
same thing the client does, so it exercises the packaging too (ADR-032).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

OK = "  OK  "
FAIL = " FAIL "
INFO = "  ..  "

# Read-only, no destructive counterpart. `read_query` on the sqlite server
# cannot write — `write_query` is the separate tool and is not called here.
SAFE_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("list_allowed_directories", {}),
    ("list_tables", {}),
    ("read_query", {"query": "SELECT name FROM sqlite_master "
                             "WHERE type='table' LIMIT 20"}),
]


def http_json(url: str, timeout: float = 5.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode())


def base_name(tool: str) -> str:
    """The proxy may namespace tools; match on the last segment either way."""
    for sep in ("__", ".", ":", "/"):
        if sep in tool:
            tool = tool.rsplit(sep, 1)[-1]
    return tool


class Proxy:
    """The proxy as a subprocess speaking JSON-RPC on stdio — exactly how an
    MCP client runs it."""

    def __init__(self, argv: list[str], cwd: Path) -> None:
        self.argv = argv
        self.proc = subprocess.Popen(
            argv, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self.out: queue.Queue[str] = queue.Queue()
        self.err: list[str] = []
        threading.Thread(target=self._pump_out, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    def _pump_out(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            self.out.put(line.rstrip("\n"))

    def _pump_err(self) -> None:
        assert self.proc.stderr
        for line in self.proc.stderr:
            self.err.append(line.rstrip("\n"))

    def send(self, msg: dict) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def await_id(self, want: int, timeout: float) -> dict | None:
        """Responses can arrive out of order; wait for this id specifically."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = self.out.get(timeout=0.25)
            except queue.Empty:
                if self.proc.poll() is not None:
                    return None
                continue
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == want:
                return msg
        return None

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()

    def stderr_text(self) -> str:
        return "\n".join(self.err)


def fs_root_from(config: dict) -> str | None:
    """The filesystem server's allowed directory, for a `list_directory` call."""
    for entry in (config.get("mcpServers") or {}).values():
        args = [str(a) for a in entry.get("args", [])]
        if any("server-filesystem" in a for a in args) and args:
            return args[-1]
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="hypothesise.config.json")
    ap.add_argument("--api", default="http://127.0.0.1:8080")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="first-response timeout; npx may download servers")
    args = ap.parse_args(argv)

    cfg_path = Path(os.path.expanduser(args.config)).resolve()
    api = args.api.rstrip("/")

    print("=" * 70)
    print("HYPOTHESISE SMOKE TEST — proxy driven by hand, no agent involved")
    print("=" * 70)
    print(f"\n  config       {cfg_path}")
    print(f"  control      {api}")
    print(f"  interpreter  {sys.executable}\n")

    if not cfg_path.exists():
        print(f"{FAIL}  no such config: {cfg_path}")
        return 1
    try:
        config = json.loads(cfg_path.read_text())
    except ValueError as exc:
        print(f"{FAIL}  config is not valid JSON: {exc}")
        return 1

    # 1 ---------------------------------------------------------- control plane
    try:
        http_json(f"{api}/healthz")
        print(f"{OK}  control plane is up")
    except (urllib.error.URLError, OSError) as exc:
        print(f"{FAIL}  control plane unreachable: {exc}")
        print("        Start it first:  hypothesise api")
        return 1

    before = http_json(f"{api}/v1/stats").get("events", 0)
    print(f"{INFO}  events in the database before this run: {before}")

    # 2 ---------------------------------------------------------------- launch
    argv_proxy = [sys.executable, "-m", "hypothesise", "proxy",
                  "--config", str(cfg_path), "--detect", "--api", api]
    print(f"{INFO}  launching: {' '.join(argv_proxy)}")
    print(f"{INFO}  first call may take a minute — npx downloads the servers")
    proxy = Proxy(argv_proxy, cwd=cfg_path.parent)

    try:
        proxy.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05",
                               "capabilities": {},
                               "clientInfo": {"name": "hypothesise-smoke",
                                              "version": "1"}}})
        init = proxy.await_id(1, args.timeout)
        if init is None:
            print(f"{FAIL}  no response to initialize")
            print("\n  proxy stderr:\n" + (proxy.stderr_text() or "  (none)"))
            return 1
        server = (init.get("result") or {}).get("serverInfo", {}).get("name", "?")
        print(f"{OK}  initialize answered by {server!r}")

        proxy.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 3 ------------------------------------------------------------- tools
        proxy.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = proxy.await_id(2, args.timeout)
        tools = [t.get("name", "?")
                 for t in ((listed or {}).get("result") or {}).get("tools", [])]
        if not tools:
            print(f"{FAIL}  tools/list returned nothing — no upstream started")
            print("\n  proxy stderr:\n" + (proxy.stderr_text() or "  (none)"))
            return 1
        print(f"{OK}  {len(tools)} tools available through the proxy:")
        for name in tools:
            print(f"          {name}")
        print("\n        This is the list your agent sees. If the agent has its")
        print("        own built-in file tools, it may use those instead and")
        print("        none of this will reach Hypothesise.")

        # 4 -------------------------------------------------------------- call
        by_base = {base_name(t): t for t in tools}
        planned = [(by_base[b], a) for b, a in SAFE_CALLS if b in by_base]
        root = fs_root_from(config)
        if "list_directory" in by_base and root:
            planned.append((by_base["list_directory"], {"path": root}))

        if not planned:
            print(f"{FAIL}  none of the known read-only tools are present")
            return 1

        print()
        made = 0
        for i, (name, call_args) in enumerate(planned, start=10):
            proxy.send({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                        "params": {"name": name, "arguments": call_args}})
            resp = proxy.await_id(i, args.timeout)
            if resp is None:
                print(f"{FAIL}  {name}: no response")
                continue
            if "error" in resp:
                print(f"{FAIL}  {name}: {resp['error'].get('message')}")
                continue
            made += 1
            print(f"{OK}  called {name}")

        if not made:
            print(f"\n{FAIL}  every tool call failed — check the stderr below")
            print("\n  proxy stderr:\n" + (proxy.stderr_text() or "  (none)"))
            return 1

        # The shipper batches on a 2s interval and flushes on shutdown.
        print(f"{INFO}  waiting for the shipper to flush")
        time.sleep(4)
    finally:
        proxy.close()

    # 5 ----------------------------------------------------------- did it land
    after = before
    deadline = time.time() + 20
    while time.time() < deadline:
        with contextlib.suppress(urllib.error.URLError, OSError):
            after = http_json(f"{api}/v1/stats").get("events", 0)
        if after > before:
            break
        time.sleep(1)

    print("\n" + "=" * 70)
    stderr = proxy.stderr_text()

    if after > before:
        print(f"PASS — {after - before} new events reached the control plane.")
        print(f"       Open {api} → Sessions. The newest session is this run.")
        print("\n       Hypothesise works. If your agent produces nothing, the agent")
        print("       is not calling these tools — not a Hypothesise fault.")
        print("=" * 70)
        return 0

    print("FAIL — the proxy ran and answered, but no events were recorded.")
    print("       The tool calls worked, so interception is fine; the events")
    print("       are being lost on the way to the control plane.")
    print("\n       Check for this line in the stderr below:")
    print("           hypothesise: shipping events to <url>/v1/events")
    print("       Missing it means --api never took effect.")
    if stderr:
        print("\n  proxy stderr:")
        for line in stderr.splitlines():
            print(f"      {line}")
    print("=" * 70)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
