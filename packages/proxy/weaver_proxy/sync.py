"""Keeps the proxy's in-memory state in step with the control plane.

Two directions, both background, neither ever on the request path:

**Pull the graph snapshot.** ARCHITECTURE §1 rule 1 says the data plane never
queries the database to make a decision — it reads an in-memory snapshot that
the control plane refreshes on a timer. That refresh was never built, which
meant node risk in the proxy stayed at its class floor forever and
`resource_reputation` could not fire no matter what any other agent had done.
Stigmergy needs this loop to exist at all.

**Push trust state.** Trust must survive a proxy restart (ADR-023), so it is
written back as it changes rather than held only in memory.

Both use stdlib urllib on an executor thread, like the shipper (ADR-015), and
both swallow every error — a control plane that is down must degrade the
product, not break the agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import urllib.request
from dataclasses import dataclass, field

from weaver_core.events import ResourceClass
from weaver_core.graph.model import Edge, GraphSnapshot, Node
from weaver_core.trust.model import TrustStore

GRAPH_REFRESH = 30.0
TRUST_PUSH = 15.0


@dataclass
class SyncStats:
    graph_pulls: int = 0
    nodes_loaded: int = 0
    trust_pushes: int = 0
    errors: int = 0
    last_error: str = ""


@dataclass
class ControlPlaneSync:
    api_url: str
    snapshot: GraphSnapshot
    trust: TrustStore
    graph_interval: float = GRAPH_REFRESH
    trust_interval: float = TRUST_PUSH
    timeout: float = 5.0
    stats: SyncStats = field(default_factory=SyncStats)
    _tasks: list[asyncio.Task] = field(default_factory=list)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    # -- transport ----------------------------------------------------------

    def _http(self, path: str, payload: object | None = None) -> object:
        url = self.api_url.rstrip("/") + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method="POST" if data is not None else "GET",
            headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "null")

    async def _call(self, path: str, payload: object | None = None) -> object:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._http, path, payload)
        except Exception as exc:  # noqa: BLE001
            self.stats.errors += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            return None

    # -- graph --------------------------------------------------------------

    def apply_graph(self, data: dict) -> int:
        """Replace the snapshot with what the control plane knows.

        Rebuilt rather than merged: the control plane is authoritative, it has
        already applied decay on read, and a merge would let a stale local risk
        value outlive the real one.
        """
        nodes: dict[str, Node] = {}
        for n in data.get("nodes") or []:
            try:
                cls = ResourceClass(n.get("class") or "unknown")
            except ValueError:
                cls = ResourceClass.UNKNOWN
            nodes[n["id"]] = Node(
                id=n["id"], kind=n.get("kind", "resource"), cls=cls,
                label=n.get("label", ""), risk=float(n.get("risk", 0.0)),
                confidence=float(n.get("confidence", 0.0)),
                observations=int(n.get("observations", 0)),
                last_seen=n.get("last_seen", ""))

        edges: dict[str, Edge] = {}
        for e in data.get("edges") or []:
            from weaver_core.events import Action

            try:
                action = Action(e.get("action", "unknown"))
            except ValueError:
                action = Action.UNKNOWN
            edges[e["id"]] = Edge(
                id=e["id"], src=e["src"], dst=e["dst"], action=action,
                count=int(e.get("count", 0)), risk=float(e.get("risk", 0.0)))

        self.snapshot.nodes = nodes
        self.snapshot.edges = edges
        self.stats.nodes_loaded = len(nodes)
        return len(nodes)

    async def pull_graph_once(self) -> int:
        data = await self._call("/v1/graph?limit=2000")
        if not isinstance(data, dict):
            return 0
        self.stats.graph_pulls += 1
        return self.apply_graph(data)

    async def load_trust_once(self) -> int:
        rows = await self._call("/v1/trust")
        if not isinstance(rows, list):
            return 0
        self.trust.load(rows)
        return len(rows)

    async def push_trust_once(self) -> int:
        snap = self.trust.snapshot()
        if not snap:
            return 0
        result = await self._call("/v1/trust", snap)
        if result is None:
            return 0
        self.stats.trust_pushes += 1
        return len(snap)

    # -- loops --------------------------------------------------------------

    async def _loop(self, fn, interval: float) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            if self._stop.is_set():
                break
            await fn()

    def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._loop(self.pull_graph_once, self.graph_interval)),
            asyncio.create_task(self._loop(self.push_trust_once, self.trust_interval)),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(t, timeout=2.0)
            t.cancel()
        self._tasks = []
        # Final push, so a clean shutdown does not lose the last trust changes.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.push_trust_once(), timeout=3.0)
