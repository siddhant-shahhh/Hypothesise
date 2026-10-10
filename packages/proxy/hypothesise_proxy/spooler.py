"""Drains the proxy's queue into the local spool when there is no control plane.

The Shipper's twin for local-first installs: same guarantees (never on the
request path, never unbounded, never raises), but the destination is a file
the dashboard reads later instead of an API that may not be running.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from .emit import EventEmitter
from .spool import append, spool_dir


@dataclass
class SpoolerStats:
    written: int = 0
    dropped: int = 0


@dataclass
class Spooler:
    emitter: EventEmitter
    directory: Path | None = None
    batch_size: int = 64
    interval: float = 1.0
    stats: SpoolerStats = field(default_factory=SpoolerStats)
    _task: asyncio.Task | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def target(self) -> Path:
        return self.directory or spool_dir()

    async def _write(self, batch: list) -> None:
        records = [e.to_dict() for e in batch]
        loop = asyncio.get_running_loop()
        n = await loop.run_in_executor(None, append, records, self.directory)
        self.stats.written += n
        self.stats.dropped += len(records) - n

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                batch = await asyncio.wait_for(
                    self.emitter.drain(max_batch=self.batch_size), timeout=self.interval)
            except asyncio.TimeoutError:
                continue
            except Exception:  # noqa: BLE001
                await asyncio.sleep(self.interval)
                continue
            if batch:
                await self._write(batch)
        try:
            leftover = await asyncio.wait_for(
                self.emitter.drain(max_batch=10_000), timeout=0.2)
            if leftover:
                await self._write(leftover)
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
