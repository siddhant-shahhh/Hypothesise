"""Load the scenario suite into the store, so the dashboard has real content.

Training on the benign scenarios first mirrors what the replay harness does —
without a baseline, the novelty detectors cannot fire and the dashboard shows
nothing interesting.
"""

from __future__ import annotations

from pathlib import Path

from weaver_core.engine import Engine
from weaver_core.scenario import load_all

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
    for sc in scenarios:
        engine.reset_sessions()
        for ev in sc.events:
            decision, sigs = engine.evaluate(ev)
            ev.decision = decision
            repo.record(ev, sigs)
            n += 1
    return n
