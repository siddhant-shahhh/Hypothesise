"""Adapter: the pure engine, behind the Gate's async evaluator signature.

The engine is synchronous and pure by design (ADR-005). The Gate wraps this in
a timeout and a fail-open fallback, so nothing here needs its own defences —
and deliberately does nothing slow, because everything it touches is in memory.
"""

from __future__ import annotations

from weaver_core.engine import Engine
from weaver_core.events import Decision, Event
from weaver_core.graph.model import GraphSnapshot


def engine_evaluator(engine: Engine):
    """Build a Gate-compatible evaluator bound to `engine`."""

    async def evaluate(event: Event, snapshot: GraphSnapshot) -> Decision:
        engine.snapshot = snapshot or engine.snapshot
        decision, _signals = engine.evaluate(event)
        return decision

    return evaluate
