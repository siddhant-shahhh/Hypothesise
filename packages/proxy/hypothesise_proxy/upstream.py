"""An upstream MCP server, running as a child process on stdio."""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass, field
from typing import Any

from . import protocol as p


@dataclass
class UpstreamConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def resolve_command(command: str, env: dict[str, str]) -> str:
    """Absolute path for `command`, searched on the PATH the child will get.

    MCP configs say `npx` or `uvx`. On Windows those are `npx.cmd` / `uvx.exe`,
    and CreateProcess does not apply PATHEXT the way a shell does, so the bare
    name fails with "file not found" even though it works in a terminal.
    `shutil.which` applies PATHEXT. Unresolvable names are returned unchanged
    so the error the user sees names what they actually configured.
    """
    import shutil

    if os.path.isabs(command):
        return command
    return shutil.which(command, path=env.get("PATH") or env.get("Path")) or command


class Upstream:
    """Owns one child MCP server: its process, its pending requests, its tools.

    Request IDs are rewritten on the way through. The agent's id space and each
    upstream's id space are independent, and two upstreams will happily both
    use id=1 — correlating responses by the client's original id would
    misroute them into each other's conversations.
    """

    def __init__(self, cfg: UpstreamConfig) -> None:
        self.cfg = cfg
        self.proc: asyncio.subprocess.Process | None = None
        self.tools: list[dict[str, Any]] = []
        self.initialized = False
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        import os

        env = {**os.environ, **self.cfg.env}
        self.proc = await asyncio.create_subprocess_exec(
            resolve_command(self.cfg.command, env),
            *self.cfg.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            if self.proc.returncode is None:
                self.proc.kill()

    # -- io -----------------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            msg = p.decode(line)
            if msg is None:
                continue
            if p.is_response(msg):
                fut = self._pending.pop(msg.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(msg)
            # Server-initiated requests and notifications are not forwarded in
            # M1. Sampling and roots land in M4 — see BUILD_PLAN.

    async def call(self, method: str, params: dict[str, Any] | None = None,
                   timeout: float = 30.0) -> dict[str, Any]:
        """Send a request, await its response. Raises on timeout or dead process."""
        if not self.proc or not self.proc.stdin:
            raise RuntimeError(f"upstream {self.cfg.name} is not running")
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self.proc.stdin.write(p.encode(p.request(rid, method, params)))
        await self.proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self.proc or not self.proc.stdin:
            return
        self.proc.stdin.write(p.encode(p.notification(method, params)))
        await self.proc.stdin.drain()

    # -- mcp handshake ------------------------------------------------------

    async def initialize(self, client_info: dict[str, Any],
                         protocol_version: str = "2024-11-05") -> dict[str, Any]:
        resp = await self.call("initialize", {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": client_info,
        })
        await self.notify("notifications/initialized")
        self.initialized = True
        return resp.get("result", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        try:
            resp = await self.call("tools/list", {})
            self.tools = (resp.get("result") or {}).get("tools", []) or []
        except (asyncio.TimeoutError, RuntimeError):
            self.tools = []
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any],
                        timeout: float = 120.0) -> dict[str, Any]:
        return await self.call("tools/call", {"name": name, "arguments": arguments},
                               timeout=timeout)
