"""Load the scenario suite into the store, so the dashboard has real content.

Training on the benign scenarios first mirrors what the replay harness does —
without a baseline, the novelty detectors cannot fire and the dashboard shows
nothing interesting.
"""

from __future__ import annotations

import time
from pathlib import Path

from hypothesise_core.engine import Engine
from hypothesise_core.scenario import load_all

TRAIN_PASSES = 3


def find_scenarios() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "scenarios"
        if candidate.is_dir():
            return candidate
    return None


def load_demo(repo, engine: Engine) -> int:
    root = find_scenarios()
    if root is None:
        return 0

    scenarios = load_all(root)
    benign = [s for s in scenarios if not s.should_fire]

    for _ in range(TRAIN_PASSES):
        for sc in benign:
            engine.train(sc.events)
            engine.reset_sessions()

    repo.clear()
    n = 0
    from hypothesise_core.trust.model import TrustStore

    for sc in scenarios:
        engine.reset_sessions()
        # Each scenario is an independent hypothetical, so trust starts clean.
        # Without this, damage from the attack scenarios leaks into unrelated
        # benign ones and the demo dashboard looks like it is full of false
        # positives. Trust carrying across a real session is correct and is
        # what production does.
        engine.trust = TrustStore()
        for ev in sc.events:
            started = time.perf_counter()
            decision, sigs = engine.evaluate(ev)
            decision.elapsed_ms = (time.perf_counter() - started) * 1000.0
            ev.decision = decision
            repo.record(ev, sigs)
            n += 1
    return n
