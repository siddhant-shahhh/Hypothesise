"""Ships events from the proxy's queue to the control plane.

Uses stdlib `urllib` on a thread executor rather than httpx. The proxy is the
component users install and run in front of their agents, and every dependency
it carries is one more thing that can fail at install time (ADR-009). POSTing
JSON to a local API does not justify a dependency.

The guarantees that matter, in order:

1. **Never block the agent.** Nothing here runs on the request path. The proxy
   calls `emit()`, which is synchronous and non-blocking, and this task drains
   the queue on its own schedule.
2. **Never grow without bound.** A dead control plane must not turn into
   unbounded memory. Batches are retried a few times, then DROPPED and counted.
   Losing telemetry is the correct failure; an OOM in the security layer is not.
3. **Never crash the proxy.** Every exception is swallowed and counted.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from hypothesise_core.events import Event

from .emit import EventEmitter

DEFAULT_BATCH = 64
DEFAULT_INTERVAL = 2.0
MAX_ATTEMPTS = 3
BACKOFF_BASE = 0.5
MAX_BACKOFF = 30.0


@dataclass
class ShipperStats:
    shipped: int = 0
    batches: int = 0
    failed_batches: int = 0
    dropped: int = 0
    spooled: int = 0          # kept locally instead of dropped (spool_fallback)
    last_error: str = ""
    offline: bool = False


@dataclass
class Shipper:
    """Drains an EventEmitter and POSTs batches to /v1/events."""

    api_url: str
    emitter: EventEmitter
    batch_size: int = DEFAULT_BATCH
    interval: float = DEFAULT_INTERVAL
    timeout: float = 5.0
    api_key: str = ""
    # When the dashboard is down, keep the batch in the local spool rather than
    # dropping it; the dashboard reads the spool when it comes back (ADR-035).
    # Off by default so the shipper alone still behaves as ADR-015 describes.
    spool_fallback: bool = False
    stats: ShipperStats = field(default_factory=ShipperStats)
    _task: asyncio.Task | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def endpoint(self) -> str:
        return self.api_url.rstrip("/") + "/v1/events"

    # -- transport ----------------------------------------------------------

    def _post_sync(self, payload: list[dict]) -> None:
        """Blocking POST. Runs on an executor thread, never the event loop."""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=body, method="POST",
            headers={"content-type": "application/json",
                     **({"x-api-key": self.api_key} if self.api_key else {})},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            if resp.status >= 400:
                raise urllib.error.HTTPError(
                    self.endpoint, resp.status, "rejected", resp.headers, None)

    async def _ship(self, batch: list[Event]) -> bool:
        payload = [e.to_dict() for e in batch]
        loop = asyncio.get_running_loop()
        for attempt in range(MAX_ATTEMPTS):
            try:
                await loop.run_in_executor(None, self._post_sync, payload)
                if self.stats.offline:
                    self.stats.offline = False
                return True
            except Exception as exc:  # noqa: BLE001 — shipping must never raise
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(min(BACKOFF_BASE * (2 ** attempt), MAX_BACKOFF))
        return False

    # -- loop ---------------------------------------------------------------

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                batch = await asyncio.wait_for(
                    self.emitter.drain(max_batch=self.batch_size),
                    timeout=self.interval,
                )
            except asyncio.TimeoutError:
                continue
            except Exception:  # noqa: BLE001
                await asyncio.sleep(self.interval)
                continue

            if not batch:
                continue

            if await self._ship(batch):
                self.stats.shipped += len(batch)
                self.stats.batches += 1
            else:
                # Retries exhausted. Never retry forever, or a dead control
                # plane becomes unbounded memory in the proxy. Spool if allowed
                # (bounded on disk), otherwise drop and count.
                self.stats.failed_batches += 1
                self.stats.offline = True
                kept = 0
                if self.spool_fallback:
                    from .spool import append

                    loop = asyncio.get_running_loop()
                    kept = await loop.run_in_executor(
                        None, append, [e.to_dict() for e in batch], None)
                    self.stats.spooled += kept
                self.stats.dropped += len(batch) - kept

        # Best-effort flush of whatever is left when shutting down.
        try:
            leftover = await asyncio.wait_for(
                self.emitter.drain(max_batch=self.batch_size), timeout=0.2)
            if leftover:
                await self._ship(leftover)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
