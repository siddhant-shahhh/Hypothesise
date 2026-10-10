"""Local spool — where events go when no control plane is listening.

Append-only JSON lines under ``~/.hypothesise/spool/``. Standard library only:
the Claude Code hook imports this on every tool call, and its startup time is
paid by the person waiting on their agent.

Why a file and not the SQLite database: there are many writers (one proxy per
wrapped MCP server per client, plus a short-lived hook process per tool call)
and SQLite's locking is unreliable on network and removable volumes. Appending
one line under a short lock is the cheapest write that is safe from all of
them. The dashboard is the single reader, and it ingests the spool through the
same path as ``POST /v1/events`` — so a spooled event and a shipped event are
decided identically.

Bounded like the rest of the data plane: past ``MAX_SPOOL_BYTES`` new events
are dropped, never allowed to fill the disk. Every function here swallows its
own errors — losing telemetry is the correct failure, breaking the agent is not.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

MAX_SPOOL_BYTES = 200 * 1024 * 1024
STATE_FILE = ".offsets.json"
LOCK_FILE = ".lock"
STEPS_FILE = ".steps.json"


def home() -> Path:
    return Path(os.environ.get("HYPOTHESISE_HOME", "~/.hypothesise")).expanduser()


def spool_dir() -> Path:
    return home() / "spool"


def _today() -> str:
    return time.strftime("%Y%m%d", time.gmtime())


@contextlib.contextmanager
def locked(directory: Path, attempts: int = 200) -> Iterator[None]:
    """Cross-process exclusive lock on ``directory/.lock``.

    Best effort: a filesystem that cannot lock (some network mounts) still gets
    the write. Interleaving is unlikely for single short lines; blocking the
    agent on a lock that will never be granted is certain.
    """
    directory.mkdir(parents=True, exist_ok=True)
    f = open(directory / LOCK_FILE, "a+b")  # noqa: SIM115 — closed in finally
    got = False
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            for _ in range(attempts):
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    got = True
                    break
                except OSError:
                    time.sleep(0.005)
        else:
            import fcntl

            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                got = True
            except OSError:
                pass
        yield
    finally:
        if got:
            try:
                if os.name == "nt":
                    import msvcrt

                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


def spool_files(directory: Path | None = None) -> list[Path]:
    d = directory or spool_dir()
    if not d.is_dir():
        return []
    return sorted(d.glob("events-*.jsonl"))


def spool_bytes(directory: Path | None = None) -> int:
    total = 0
    for p in spool_files(directory):
        with contextlib.suppress(OSError):
            total += p.stat().st_size
    return total


def append(records: list[dict[str, Any]], directory: Path | None = None) -> int:
    """Append records as JSON lines. Returns how many were written; never raises."""
    if not records:
        return 0
    try:
        d = directory or spool_dir()
        d.mkdir(parents=True, exist_ok=True)
        if spool_bytes(d) > MAX_SPOOL_BYTES:
            return 0
        data = "".join(
            json.dumps(r, separators=(",", ":"), default=str) + "\n" for r in records
        ).encode("utf-8")
        with locked(d), open(d / f"events-{_today()}.jsonl", "ab") as f:
            f.write(data)
        return len(records)
    except Exception:  # noqa: BLE001 — the data plane never raises
        return 0


def next_step(session_id: str, directory: Path | None = None) -> int:
    """Per-session step counter for writers with no memory between calls (hooks).

    Held in one small JSON file under the spool lock. Sessions idle for a day
    are forgotten so the file stays small.
    """
    d = directory or spool_dir()
    try:
        with locked(d):
            path = d / STEPS_FILE
            try:
                steps = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                steps = {}
            now = time.time()
            steps = {k: v for k, v in steps.items()
                     if isinstance(v, list) and now - v[1] < 86400}
            n = steps.get(session_id, [-1, now])[0] + 1
            steps[session_id] = [n, now]
            _atomic_write(path, json.dumps(steps))
            return n
    except Exception:  # noqa: BLE001
        return int(time.time() * 1000) % 1_000_000


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class SpoolReader:
    """Single consumer. Reads complete lines past a saved offset per file.

    ``read()`` returns records plus the offsets to save; call ``commit()`` only
    after the records are safely ingested, so a crash re-reads rather than
    loses. A trailing line without a newline is a write still in progress and
    is left for the next read.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or spool_dir()
        self.state_path = self.dir / STATE_FILE

    def _load(self) -> dict[str, int]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return {str(k): int(v) for k, v in data.items()}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def pending_bytes(self) -> int:
        offsets = self._load()
        total = 0
        for p in spool_files(self.dir):
            with contextlib.suppress(OSError):
                total += max(0, p.stat().st_size - offsets.get(p.name, 0))
        return total

    def read(self, max_records: int = 5000) -> tuple[list[dict[str, Any]], dict[str, int]]:
        offsets = self._load()
        out: list[dict[str, Any]] = []
        for path in spool_files(self.dir):
            off = offsets.get(path.name, 0)
            try:
                if path.stat().st_size <= off:
                    continue
                with open(path, "rb") as f:
                    f.seek(off)
                    while len(out) < max_records:
                        line = f.readline()
                        if not line or not line.endswith(b"\n"):
                            break
                        off += len(line)
                        with contextlib.suppress(ValueError):
                            rec = json.loads(line)
                            if isinstance(rec, dict):
                                out.append(rec)
            except OSError:
                continue
            offsets[path.name] = off
            if len(out) >= max_records:
                break
        return out, offsets

    def commit(self, offsets: dict[str, int]) -> None:
        live = {p.name for p in spool_files(self.dir)}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.state_path,
                          json.dumps({k: v for k, v in offsets.items() if k in live}))
        except OSError:
            pass

    def prune(self) -> int:
        """Delete files from previous days that have been read to the end."""
        offsets = self._load()
        today = f"events-{_today()}.jsonl"
        removed = 0
        for p in spool_files(self.dir):
            if p.name == today:
                continue
            try:
                if offsets.get(p.name, 0) >= p.stat().st_size:
                    p.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
