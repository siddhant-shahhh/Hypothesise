"""The Weaver MCP proxy — the data plane.

Presents itself to the agent as one MCP server exposing the union of every
upstream's tools. Presents itself to each upstream as an ordinary client.
Every `tools/call` passes through the gate on the way.

    agent ──stdio──> [ WeaverProxy ] ──stdio──> github-mcp
                          │           ──stdio──> postgres-mcp
                          │           ──stdio──> shell-mcp
                          └──> emit(event) ──> bounded queue ──> control plane
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from weaver_core.classify import classify_action, classify_resource
from weaver_core.engine import Engine
from weaver_core.events import (
    Decision,
    Event,
    Outcome,
    Resource,
    Status,
    Tool,
    Verdict,
    digest_args,
    new_id,
)
from weaver_core.graph.model import GraphSnapshot

from . import protocol as p
from .config import ProxyConfig
from .emit import EventEmitter
from .gate import Gate
from .upstream import Upstream

SERVER_INFO = {"name": "weaver-proxy", "version": "0.1.0"}


def extract_resource_uri(tool_name: str, args: dict[str, Any]) -> str:
    """Best-effort resource URI from tool arguments.

    Arguments are free-form per MCP server, so this is a heuristic over the
    keys that conventionally carry a target. An unrecognised shape yields ""
    and the resource classifies as UNKNOWN — which is the correct failure:
    UNKNOWN carries a low risk floor and fires nothing, whereas guessing wrong
    would attach risk to a resource that was never touched.
    """
    for key in ("uri", "url", "path", "file_path", "filepath", "file",
                "resource", "target", "repo", "repository", "query", "command", "cmd"):
        val = args.get(key)
        if isinstance(val, str) and val:
            if key in ("command", "cmd"):
                return f"shell://{val}"
            if key == "query":
                return f"query://{val[:120]}"
            return val
    return ""


class WeaverProxy:
    def __init__(self, config: ProxyConfig, engine: Engine | None = None) -> None:
        self.config = config
        # The engine is optional so the proxy can run bare (M1 behaviour:
        # observe and record, decide nothing). Pass one to enable detection.
        self.engine = engine
        if engine is not None:
            from .evaluator import engine_evaluator

            self.gate = Gate(config=config.gate, evaluator=engine_evaluator(engine))
        else:
            self.gate = Gate(config=config.gate)
        self.emitter = EventEmitter(maxsize=config.queue_maxsize)
        self.snapshot = GraphSnapshot()
        self.upstreams: dict[str, Upstream] = {}
        self.routes: dict[str, tuple[str, str]] = {}   # exposed name -> (server, real name)
        self.session_id = new_id("sess")
        self.agent_id = config.agent_id or "unknown-agent"
        self.step = 0
        self._out_lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------

    async def start_upstreams(self) -> None:
        for cfg in self.config.upstreams:
            up = Upstream(cfg)
            try:
                await up.start()
                self.upstreams[cfg.name] = up
            except (OSError, RuntimeError) as exc:
                # One dead upstream must not stop the proxy. Its tools simply
                # will not appear; the agent works with what is available.
                print(f"weaver: upstream {cfg.name} failed to start: {exc}", file=sys.stderr)

    async def stop_upstreams(self) -> None:
        await asyncio.gather(*(u.stop() for u in self.upstreams.values()),
                             return_exceptions=True)

    # -- routing ------------------------------------------------------------

    async def build_routes(self, client_info: dict[str, Any]) -> list[dict[str, Any]]:
        """Initialize every upstream, aggregate their tools, resolve collisions."""
        exposed: list[dict[str, Any]] = []
        seen: set[str] = set()

        for name, up in self.upstreams.items():
            try:
                await up.initialize(client_info)
                tools = await up.list_tools()
            except (asyncio.TimeoutError, RuntimeError, OSError) as exc:
                print(f"weaver: upstream {name} handshake failed: {exc}", file=sys.stderr)
                continue

            for tool in tools:
                real = tool.get("name", "")
                if not real:
                    continue
                # Namespace only on collision, so tool names stay familiar and
                # the agent's existing prompts keep working.
                shown = real if real not in seen else f"{name}__{real}"
                seen.add(shown)
                self.routes[shown] = (name, real)
                exposed.append({**tool, "name": shown})

        return exposed

    # -- message handling ---------------------------------------------------

    async def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method", "")
        mid = msg.get("id")

        if method == "initialize":
            params = msg.get("params") or {}
            client_info = params.get("clientInfo") or {}
            if not self.config.agent_id:
                self.agent_id = client_info.get("name") or "unknown-agent"
            tools = await self.build_routes(client_info)
            self._tools_cache = tools
            return p.result(mid, {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })

        if method == "tools/list":
            return p.result(mid, {"tools": getattr(self, "_tools_cache", [])})

        if method == "tools/call":
            return await self.handle_tool_call(msg)

        if method in ("ping", "notifications/initialized", "notifications/cancelled"):
            return p.result(mid, {}) if p.is_request(msg) else None

        if p.is_request(msg):
            return p.error(mid, p.METHOD_NOT_FOUND, f"method not supported by weaver: {method}")
        return None

    async def handle_tool_call(self, msg: dict[str, Any]) -> dict[str, Any]:
        mid = msg.get("id")
        shown, args = p.tool_call_params(msg)

        route = self.routes.get(shown)
        if route is None:
            return p.error(mid, p.INVALID_PARAMS, f"unknown tool: {shown}")
        server, real = route

        # --- build the event ------------------------------------------------
        uri = extract_resource_uri(real, args)
        cls = classify_resource(uri, server, real)
        event = Event(
            agent_id=self.agent_id,
            session_id=self.session_id,
            step=self.step,
            tool=Tool(server=server, name=real),
            resource=Resource(uri=uri, cls=cls),
            action=classify_action(real, cls),
            args_digest=digest_args(args),
        )
        self.step += 1

        # --- decide (budgeted, fail-open) ------------------------------------
        decision = await self.gate.decide(event, self.snapshot)
        event.decision = decision

        if decision.verdict is Verdict.BLOCK:
            event.outcome = Outcome(status=Status.BLOCKED)
            self.emitter.emit(event)
            # A structured error, so the agent can tell its operator WHY rather
            # than just failing opaquely.
            return p.error(mid, p.BLOCKED_BY_POLICY, decision.sentence, {
                "weaver": {
                    "verdict": "block",
                    "score": round(decision.score, 4),
                    "detectors": decision.detectors,
                    "event_id": event.event_id,
                }
            })

        # ASK lands in M3. Until then it degrades to allow-and-flag, never to
        # a silent block.
        if decision.verdict is Verdict.ASK:
            event.decision = Decision(
                verdict=Verdict.ALLOW,
                score=decision.score,
                detectors=decision.detectors,
                sentence=f"[ask-not-implemented] {decision.sentence}",
                budget_ms=decision.budget_ms,
            )

        # --- forward ---------------------------------------------------------
        up = self.upstreams.get(server)
        if up is None:
            event.outcome = Outcome(status=Status.ERROR, error="upstream unavailable")
            self.emitter.emit(event)
            return p.error(mid, p.INTERNAL_ERROR, f"upstream {server} unavailable")

        import time

        started = time.perf_counter()
        try:
            resp = await up.call_tool(real, args)
            latency = int((time.perf_counter() - started) * 1000)
            is_err = "error" in resp or (resp.get("result") or {}).get("isError")
            event.outcome = Outcome(
                status=Status.ERROR if is_err else Status.OK,
                latency_ms=latency,
            )
            self.emitter.emit(event)
            if "error" in resp:
                return p.error(mid, resp["error"].get("code", p.INTERNAL_ERROR),
                               resp["error"].get("message", "upstream error"))
            return p.result(mid, resp.get("result", {}))
        except asyncio.TimeoutError:
            event.outcome = Outcome(
                status=Status.ERROR,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error="upstream timeout",
            )
            self.emitter.emit(event)
            return p.error(mid, p.INTERNAL_ERROR, f"upstream {server} timed out")

    # -- main loop ----------------------------------------------------------

    async def _write(self, msg: dict[str, Any]) -> None:
        async with self._out_lock:
            sys.stdout.buffer.write(p.encode(msg))
            sys.stdout.buffer.flush()

    async def run(self) -> None:
        await self.start_upstreams()
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = p.decode(line)
                if msg is None:
                    continue
                try:
                    reply = await self.handle(msg)
                except Exception as exc:  # noqa: BLE001 — never die mid-conversation
                    reply = (p.error(msg.get("id"), p.INTERNAL_ERROR, f"weaver: {exc}")
                             if p.is_request(msg) else None)
                if reply is not None:
                    await self._write(reply)
        finally:
            await self.stop_upstreams()
