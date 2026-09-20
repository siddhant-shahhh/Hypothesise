"""Event emission — bounded and non-blocking, by construction.

ARCHITECTURE section 1, rule 2: the data plane never does a synchronous write.
Events go on a bounded queue; when it is full they are DROPPED and counted.

Dropping telemetry is always better than blocking the agent. A security tool
that stalls someone's coding agent because its own database is slow gets
uninstalled, and then it secures nothing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from weaver_core.events import Event

DEFAULT_MAXSIZE = 10_000


@dataclass
class EmitStats:
    emitted: int = 0
    dropped: int = 0
    drained: int = 0

    @property
    def drop_rate(self) -> float:
        total = self.emitted + self.dropped
        return self.dropped / total if total else 0.0


@dataclass
class EventEmitter:
    """Bounded queue between the hot path and the control plane.

    `emit` is synchronous and never awaits — callers are inside a tool call
    that an agent is waiting on.
    """

    maxsize: int = DEFAULT_MAXSIZE
    stats: EmitStats = field(default_factory=EmitStats)
    _q: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=DEFAULT_MAXSIZE))

    def __post_init__(self) -> None:
        if self.maxsize != DEFAULT_MAXSIZE:
            self._q = asyncio.Queue(maxsize=self.maxsize)

    def emit(self, event: Event) -> bool:
        """Enqueue if there is room. Returns False if dropped. Never blocks, never raises."""
        try:
            self._q.put_nowait(event)
            self.stats.emitted += 1
            return True
        except asyncio.QueueFull:
            self.stats.dropped += 1
            return False

    async def drain(self, max_batch: int = 256) -> list[Event]:
        """Pull a batch for the control plane. Blocks until at least one event."""
        first = await self._q.get()
        batch = [first]
        while len(batch) < max_batch:
            try:
                batch.append(self._q.get_nowait())
            except asyncio.QueueEmpty:
                break
        self.stats.drained += len(batch)
        return batch

    def qsize(self) -> int:
        return self._q.qsize()

    @property
    def pressure(self) -> float:
        """0.0 empty, 1.0 full. Alarm above ~0.8 — drops are imminent."""
        return self._q.qsize() / self.maxsize if self.maxsize else 0.0
