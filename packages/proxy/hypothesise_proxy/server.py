"""The Hypothesise MCP proxy — the data plane.

Presents itself to the agent as one MCP server exposing the union of every
upstream's tools. Presents itself to each upstream as an ordinary client.
Every `tools/call` passes through the gate on the way.

    agent ──stdio──> [ HypothesiseProxy ] ──stdio──> github-mcp
                          │           ──stdio──> postgres-mcp
                          │           ──stdio──> shell-mcp
                          └──> emit(event) ──> bounded queue ──> control plane
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from hypothesise_core.classify import (  # noqa: F401 — extract_resource_uri re-exported
    classify_action,
    classify_resource,
    extract_resource_uri,
    looks_denied,
)
from hypothesise_core.engine import Engine
from hypothesise_core.events import (
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
from hypothesise_core.graph.model import GraphSnapshot

from . import notify
from . import protocol as p
from .approval import ApprovalClient
from .config import ProxyConfig
from .emit import EventEmitter
from .gate import Gate
from .shipper import Shipper
from .spooler import Spooler
from .sync import ControlPlaneSync
from .upstream import Upstream

SERVER_INFO = {"name": "hypothesise-proxy", "version": "0.1.0a3"}


def _error_text(resp: dict[str, Any]) -> str:
    """Pull human-readable error text out of either MCP error shape."""
    if "error" in resp:
        return str((resp["error"] or {}).get("message", ""))
    content = (resp.get("result") or {}).get("content") or []
    return " ".join(
        str(c.get("text", "")) for c in content if isinstance(c, dict)
    )[:500]


def _stdin_lines() -> asyncio.Queue:
    """Lines from stdin, read on a daemon thread.

    `loop.connect_read_pipe(sys.stdin)` is the textbook approach and it fails
    on Windows, where the proactor loop cannot always wrap the pipe handle an
    MCP client hands its server. A thread works the same on every OS. EOF
    arrives as b"", which ends the main loop exactly as before.
    """
    import threading

    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def pump() -> None:
        stream = sys.stdin.buffer
        while True:
            try:
                line = stream.readline()
            except (OSError, ValueError):
                line = b""
            loop.call_soon_threadsafe(q.put_nowait, line)
            if not line:
                return

    threading.Thread(target=pump, name="hypothesise-stdin", daemon=True).start()
    return q


class HypothesiseProxy:
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
        self.approvals: ApprovalClient | None = (
            ApprovalClient(config.api_url, ttl=config.ask_timeout)
            if config.api_url else None
        )
        self.shipper: Shipper | None = (
            Shipper(api_url=config.api_url, emitter=self.emitter, spool_fallback=True)
            if config.api_url else None
        )
        # No control plane: spool locally so nothing is lost, and the dashboard
        # picks it up whenever it next runs. Previously events were simply
        # discarded here, which made `hypothesise api` a hard dependency.
        self.spooler: Spooler | None = (
            Spooler(emitter=self.emitter) if not config.api_url else None
        )
        self.sync: ControlPlaneSync | None = (
            ControlPlaneSync(api_url=config.api_url, snapshot=self.snapshot,
                             trust=engine.trust)
            if config.api_url and engine is not None else None
        )
        self.session_id = config.session_id or new_id("sess")
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
                print(f"hypothesise: upstream {cfg.name} failed to start: {exc}", file=sys.stderr)

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
                print(f"hypothesise: upstream {name} handshake failed: {exc}", file=sys.stderr)
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
            return p.error(mid, p.METHOD_NOT_FOUND, f"method not supported by hypothesise: {method}")
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
            notify.blocked(decision.sentence, self.agent_id, f"{server}/{real}")
            # A structured error, so the agent can tell its operator WHY rather
            # than just failing opaquely.
            return p.error(mid, p.BLOCKED_BY_POLICY, decision.sentence, {
                "hypothesise": {
                    "verdict": "block",
                    "score": round(decision.score, 4),
                    "detectors": decision.detectors,
                    "event_id": event.event_id,
                }
            })

        # --- ASK: suspend the call and wait for a human ----------------------
        if decision.verdict is Verdict.ASK:
            if self.approvals is None:
                # Nobody to ask. Flag it and let it through rather than
                # pretending a human approved — enforce mode refuses to start
                # without a control plane, so this only happens in observe.
                event.decision = Decision(
                    verdict=Verdict.ALLOW, score=decision.score,
                    detectors=decision.detectors,
                    sentence=f"[no-approver] {decision.sentence}",
                    budget_ms=decision.budget_ms)
            else:
                outcome = await self.approvals.request(event, decision.sentence)
                if not outcome.approved:
                    event.decision = Decision(
                        verdict=Verdict.BLOCK, score=decision.score,
                        detectors=decision.detectors,
                        sentence=f"{decision.sentence} Approval {outcome.state}: "
                                 f"{outcome.detail}.",
                        budget_ms=decision.budget_ms)
                    event.outcome = Outcome(status=Status.BLOCKED)
                    self.emitter.emit(event)
                    notify.blocked(event.decision.sentence, self.agent_id, f"{server}/{real}")
                    code = (p.APPROVAL_DENIED if outcome.state == "denied"
                            else p.APPROVAL_TIMEOUT)
                    return p.error(mid, code, event.decision.sentence, {
                        "hypothesise": {"verdict": "block",
                                   "approval_id": outcome.approval_id,
                                   "approval_state": outcome.state,
                                   "event_id": event.event_id}})
                event.decision = Decision(
                    verdict=Verdict.ALLOW, score=decision.score,
                    detectors=decision.detectors,
                    sentence=f"{decision.sentence} {outcome.detail.capitalize()}.",
                    budget_ms=decision.budget_ms)

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
            status = Status.OK
            error_text = None
            if is_err:
                error_text = _error_text(resp)
                # A refusal and a missing file both arrive as an error over
                # MCP. Only a refusal is a denial, and only a denial should
                # make a later attempt on the same target look like evasion.
                status = Status.DENIED if looks_denied(error_text) else Status.ERROR
            event.outcome = Outcome(
                status=status, latency_ms=latency, error=error_text,
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

    async def _watch_settings(self, interval: float = 2.0) -> None:
        """Follow `hypothesise mode` while running (ADR-041).

        The gate reads its mode on every call, so flipping it here takes effect
        on the very next tool call — no restart of the user's agent app. An
        explicit HYPOTHESISE_MODE in the environment wins and is left alone.
        """
        import os

        from . import settings

        seen = settings.mtime()
        while True:
            await asyncio.sleep(interval)
            now = settings.mtime()
            if now == seen or os.environ.get("HYPOTHESISE_MODE"):
                continue
            seen = now
            new = settings.mode("observe")
            if new != self.gate.config.mode:
                self.gate.config.mode = new
                print(f"hypothesise: mode is now {new}", file=sys.stderr)
            if new == "enforce" and self.approvals is None:
                url = settings.api_url("")
                if url:
                    self.approvals = ApprovalClient(url, ttl=self.config.ask_timeout)

    async def run(self) -> None:
        await self.start_upstreams()
        watcher = asyncio.create_task(self._watch_settings())
        if self.shipper:
            self.shipper.start()
            print(f"hypothesise: shipping events to {self.shipper.endpoint}", file=sys.stderr)
        if self.spooler:
            self.spooler.start()
            print(f"hypothesise: recording events to {self.spooler.target}", file=sys.stderr)
        if self.sync:
            # Load before serving: an agent must not get a clean slate simply
            # because the proxy restarted (ADR-023).
            agents = await self.sync.load_trust_once()
            nodes = await self.sync.pull_graph_once()
            self.sync.start()
            print(f"hypothesise: restored trust for {agents} agents, "
                  f"{nodes} graph nodes", file=sys.stderr)
        lines = _stdin_lines()
        try:
            while True:
                line = await lines.get()
                if not line:
                    break
                msg = p.decode(line)
                if msg is None:
                    continue
                try:
                    reply = await self.handle(msg)
                except Exception as exc:  # noqa: BLE001 — never die mid-conversation
                    reply = (p.error(msg.get("id"), p.INTERNAL_ERROR, f"hypothesise: {exc}")
                             if p.is_request(msg) else None)
                if reply is not None:
                    await self._write(reply)
        finally:
            watcher.cancel()
            if self.sync:
                await self.sync.stop()
            if self.shipper:
                await self.shipper.stop()
            if self.spooler:
                await self.spooler.stop()
            await self.stop_upstreams()
