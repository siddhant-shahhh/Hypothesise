"""Detector plumbing — the context object and the registry.

A detector is a pure function `(DetectorContext) -> Signal | None`. It gets
everything it needs as arguments and touches no I/O, so Tier-1 detectors can
run inline in the proxy's hot path without any risk of a database call
sneaking into a 50ms budget.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..baseline.model import Baseline
from ..events import Event, Status
from ..graph.model import GraphSnapshot
from ..risk.model import Signal

WINDOW_SIZE = 12


@dataclass
class SessionWindow:
    """The last N events of the current session. The detectors' short-term memory."""

    events: list[Event] = field(default_factory=list)
    size: int = WINDOW_SIZE

    def push(self, ev: Event) -> None:
        self.events.append(ev)
        if len(self.events) > self.size:
            self.events.pop(0)

    def recent(self, n: int) -> list[Event]:
        return self.events[-n:] if n else []

    def last_denial(self, within: int = 6) -> Event | None:
        """The most recent DENIED event in the window.

        Strictly `denied`, never `error`. Agents probe for optional files all
        the time and get 'not found'; treating that as a denial would make
        every dependency check look like an attack.
        """
        for ev in reversed(self.events[-within:]):
            if ev.outcome.status is Status.DENIED:
                return ev
        return None

    def __len__(self) -> int:
        return len(self.events)


@dataclass
class DetectorContext:
    event: Event
    window: SessionWindow
    baseline: Baseline
    snapshot: GraphSnapshot


Detector = Callable[[DetectorContext], Signal | None]

_TIER1: list[tuple[str, Detector]] = []
_TIER2: list[tuple[str, Detector]] = []

# A crashing detector is caught so it cannot break a decision — but silence is
# how a broken detector survives for weeks. scope_widening raised ValueError on
# EVERY call (a bad `strict=True` zip) and simply never fired; nothing said so.
# Errors are counted here and asserted on in the test suite.
DETECTOR_ERRORS: dict[str, int] = {}


def tier1(name: str) -> Callable[[Detector], Detector]:
    """Register an inline detector."""

    def wrap(fn: Detector) -> Detector:
        _TIER1.append((name, fn))
        return fn

    return wrap


def tier2(name: str) -> Callable[[Detector], Detector]:
    """Register a Tier-2 (statistical) detector.

    These also run inline. The original design had them async in the control
    plane because `argument_drift` was going to need embeddings; ADR-011
    replaced that with structural statistics, and every Tier-2 detector is now
    an O(1) arithmetic check over accumulators already in memory. Keeping them
    in the same pass means a decision uses all available evidence at the moment
    it is made, rather than catching up one event late.

    The tier distinction survives in WEIGHT, which is where it always mattered:
    Tier 1 describes actions nobody legitimate performs, Tier 2 describes
    actions that are merely unusual.
    """

    def wrap(fn: Detector) -> Detector:
        _TIER2.append((name, fn))
        return fn

    return wrap


def _run(registry: list[tuple[str, Detector]], ctx: DetectorContext) -> list[Signal]:
    out: list[Signal] = []
    for name, fn in registry:
        try:
            sig = fn(ctx)
        except Exception:  # noqa: BLE001 — a buggy detector must not block an agent
            DETECTOR_ERRORS[name] = DETECTOR_ERRORS.get(name, 0) + 1
            continue
        if sig is not None:
            out.append(sig)
    return out


def run_tier1(ctx: DetectorContext) -> list[Signal]:
    """Inline detectors. One misbehaving detector cannot break a decision."""
    return _run(_TIER1, ctx)


def run_tier2(ctx: DetectorContext) -> list[Signal]:
    return _run(_TIER2, ctx)


def run_all(ctx: DetectorContext) -> list[Signal]:
    return _run(_TIER1, ctx) + _run(_TIER2, ctx)


def tier2_names() -> list[str]:
    return [n for n, _ in _TIER2]


def tier1_names() -> list[str]:
    return [n for n, _ in _TIER1]


def detector_errors() -> dict[str, int]:
    """Detectors that raised. Non-empty means something is broken and quiet."""
    return dict(DETECTOR_ERRORS)


def reset_detector_errors() -> None:
    DETECTOR_ERRORS.clear()
