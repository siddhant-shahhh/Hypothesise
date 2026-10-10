"""Risk arithmetic — reinforcement, decay, corroboration, confidence.

This is the stigmergy mechanic (ARCHITECTURE section 5). It is about forty
lines of maths. The value is not the formula — it is that the schema can hold
it (ADR-004).

Pure functions. No I/O, no clock reads, no config lookups: every input is an
argument, so every branch is testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

ALPHA = 0.35   # reinforcement rate
BETA = 0.25    # trust loss per signal
GAMMA = 0.02   # trust recovery per clean hour


class Weight(float, Enum):
    """Detector weights, normalised to 0..1."""

    LOW = 0.1
    MEDIUM = 0.3
    HIGH = 0.6
    CRITICAL = 1.0


def reinforce(risk: float, weight: float, alpha: float = ALPHA) -> float:
    """Saturating increase. Approaches 1.0 asymptotically, never exceeds it.

        risk <- risk + alpha * w * (1 - risk)

    Saturation matters: the tenth bad signal should move a node far less than
    the first, otherwise every flagged resource pins to 1.0 and the score
    stops discriminating.
    """
    risk = _clamp(risk)
    return _clamp(risk + alpha * weight * (1.0 - risk))


def decay(risk: float, floor: float, hours: float, lam: float) -> float:
    """Exponential decay toward the class floor.

        risk <- floor + (risk - floor) * e^(-lambda * dt)

    Applied lazily on read using `risk_updated`, so there is no background
    sweep job to run, monitor or get wrong.
    """
    if hours <= 0:
        return _clamp(risk)
    if risk <= floor:
        return _clamp(floor)
    return _clamp(floor + (risk - floor) * math.exp(-lam * hours))


def corroborate(weight: float, distinct_reporters: int) -> float:
    """The ant-colony term.

        w <- w * (1 + ln(max(distinct, 1)))

    Three DIFFERENT agents finding the same resource suspicious is much
    stronger evidence than one agent finding it suspicious three times. Keying
    on distinct reporters is what stops a single looping agent from
    manufacturing its own corroboration.
    """
    d = max(int(distinct_reporters), 1)
    return weight * (1.0 + math.log(d))


CONFIDENCE_K = 10.0  # observations needed to reach 0.5 confidence


def confidence_from_observations(observations: int, k: float = CONFIDENCE_K) -> float:
    """Confidence as a saturating function of how much we have actually seen.

        confidence = n / (n + k)

    Derived from the observation count rather than accumulated incrementally.
    The incremental form `c + (1-c)/(n+1)` looks equivalent but saturates to
    1.0 on the FIRST observation, which would let a single sighting of a new
    node carry full weight into the score — exactly the false positive the
    confidence term exists to prevent.

    With k=10: 1 obs -> 0.09, 10 obs -> 0.50, 100 obs -> 0.91.
    """
    n = max(int(observations), 0)
    return _clamp(n / (n + k))


def update_confidence(confidence: float, observations: int) -> float:
    """Back-compat shim. Prefer `confidence_from_observations`."""
    return confidence_from_observations(observations + 1)


def effective_risk(risk: float, confidence: float) -> float:
    """What scoring actually consumes: risk discounted by how much we know."""
    return _clamp(risk) * _clamp(confidence)


# --- trust -----------------------------------------------------------------


def trust_after_signal(trust: float, weight: float, beta: float = BETA) -> float:
    """Fast down. One critical signal costs a quarter of the agent's trust."""
    return _clamp(trust - beta * weight)


def trust_after_clean(trust: float, hours: float, gamma: float = GAMMA) -> float:
    """Slow up. Roughly a day of clean operation to fully recover.

    The asymmetry is deliberate and is the correct shape for security: cheap to
    lose, expensive to regain.
    """
    t = _clamp(trust)
    for _ in range(int(max(hours, 0))):
        t = t + gamma * (1.0 - t)
    return _clamp(t)


# --- helpers ---------------------------------------------------------------


def hours_between(earlier_iso: str, later_iso: str) -> float:
    """Hours between two ISO-8601 timestamps. Returns 0.0 on anything unparseable."""
    try:
        a = datetime.fromisoformat(earlier_iso)
        b = datetime.fromisoformat(later_iso)
    except (ValueError, TypeError):
        return 0.0
    return max((b - a).total_seconds() / 3600.0, 0.0)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


@dataclass(frozen=True)
class Signal:
    """One detector firing. The unit of evidence everything else consumes."""

    detector: str
    weight: float
    detail: str
    node_id: str = ""
    edge_id: str = ""
    agent_id: str = ""
    ts: str = ""
