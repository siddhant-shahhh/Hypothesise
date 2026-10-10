"""One ingest path for every way an event reaches the store.

`POST /v1/events` (a proxy shipping live) and the local spool (proxies and
hooks recording while no dashboard was running) both land here, so an event is
decided the same way whichever road it took.
"""

from __future__ import annotations

from typing import Any

from hypothesise_core.engine import Engine
from hypothesise_core.events import Event

from .repo import Repo


def ingest(repo: Repo, engine: Engine, batch: list[dict[str, Any]]) -> tuple[int, int]:
    """Evaluate and persist a batch. Returns (accepted, alerts).

    Malformed records are skipped, never fatal: one bad line must not reject a
    batch of good ones. Duplicates (an event already stored) are skipped too —
    the spool is re-read after a crash, by design.
    """
    accepted, alerts = 0, 0
    for raw in batch:
        if not isinstance(raw, dict):
            continue
        try:
            ev = Event.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            continue
        decision, sigs = engine.evaluate(ev)
        # A decision already made inline by the proxy wins — re-deciding
        # would overwrite what actually happened to the call.
        raw_dec = raw.get("decision") or {}
        if raw_dec.get("sentence") or raw_dec.get("verdict", "allow") != "allow":
            decision = ev.decision
            sigs = []
        else:
            ev.decision = decision
        try:
            repo.record(ev, sigs)
        except Exception as exc:  # noqa: BLE001
            if "UNIQUE" in str(exc).upper() or "duplicate" in str(exc).lower():
                continue
            raise
        accepted += 1
        if decision.verdict.value != "allow":
            alerts += 1
    return accepted, alerts
