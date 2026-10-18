"""One ingest path for every way an event reaches the store.

`POST /v1/events` (a proxy shipping live) and the local spool (proxies and
hooks recording while no dashboard was running) both land here, so an event is
decided the same way whichever road it took.
"""

from __future__ import annotations

import time
from typing import Any

from hypothesise_core.engine import Engine
from hypothesise_core.events import Decision, Event, Verdict

from .repo import Repo


def ingest(repo: Repo, engine: Engine, batch: list[dict[str, Any]]) -> tuple[int, int]:
    """Evaluate and persist a batch. Returns (accepted, alerts).

    Malformed records are skipped, never fatal: one bad line must not reject a
    batch of good ones. Duplicates (an event already stored) are skipped too —
    the spool is re-read after a crash, by design.
    """
    accepted, alerts = 0, 0
    touched: set[str] = set()
    for raw in batch:
        if not isinstance(raw, dict):
            continue
        try:
            ev = Event.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            continue
        started = time.perf_counter()
        decision, sigs = engine.evaluate(ev)
        decision.elapsed_ms = (time.perf_counter() - started) * 1000.0
        # A decision already made inline by the proxy wins — re-deciding
        # would overwrite what actually happened to the call. Its signals are
        # still kept: they are this engine's evidence about the same event, and
        # dropping them left Threat signals and the graph blind to everything a
        # proxy decided (ADR-042).
        raw_dec = raw.get("decision") or {}
        if raw_dec.get("sentence") or raw_dec.get("verdict", "allow") != "allow":
            measured = decision.elapsed_ms
            decision = ev.decision
            if not decision.elapsed_ms:
                decision.elapsed_ms = measured
        else:
            # Decided here, after the call already ran (agent hooks, or a proxy
            # running without an engine). Nothing enforced it, so a non-ALLOW
            # verdict is a "would have" — stored the way the gate stores an
            # observe-mode decision, never as a block that did not happen.
            if decision.verdict is not Verdict.ALLOW:
                decision = Decision(
                    verdict=Verdict.ALLOW, score=decision.score,
                    detectors=decision.detectors,
                    sentence=(f"[observe] would have {decision.verdict.value}ed: "
                              f"{decision.sentence}"),
                    budget_ms=decision.budget_ms, elapsed_ms=decision.elapsed_ms)
            # The proxy's own timing is what the agent actually waited for.
            if ev.decision.elapsed_ms:
                decision.elapsed_ms = ev.decision.elapsed_ms
            ev.decision = decision
        try:
            repo.record(ev, sigs)
        except Exception as exc:  # noqa: BLE001
            if "UNIQUE" in str(exc).upper() or "duplicate" in str(exc).lower():
                continue
            raise
        accepted += 1
        if sigs:
            touched.add(ev.agent_id)
        if decision.verdict.value != "allow" or decision.sentence.startswith("[observe]"):
            alerts += 1
    # Trust this engine lowered must reach the Agents and Trust zones tabs;
    # before, only a proxy with --api ever wrote trust, so hook-only agents
    # sat at 1.00 forever whatever they did.
    if touched:
        repo.save_trust([engine.trust.get(a).to_dict() for a in sorted(touched)])
    return accepted, alerts
