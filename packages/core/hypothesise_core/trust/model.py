"""Adaptive trust — the COLD / WARM / HOT temperature machine.

An agent's autonomy is not fixed. It earns freedom by behaving, and loses it by
misbehaving, and the two happen at deliberately different speeds.

Design decisions, both load-bearing:

**Trust moves asymmetrically.** One critical signal costs a quarter of an
agent's trust; recovering it takes roughly a day of clean operation. Cheap to
lose, expensive to regain, which is the correct shape for security.

**Zones gate sensitive classes; they do not shade thresholds** (ADR-022). A
WARM agent needs approval to touch a secret, a shell or a database, and its
ordinary work is untouched. The alternative — making every detector more
sensitive as trust drops — turns one bad score into false positives across the
board, which is the `novel_sequence` failure again with extra steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..events import Event, ResourceClass, Verdict
from ..graph.model import SENSITIVE_CLASSES
from ..risk.model import trust_after_clean, trust_after_signal

WARM_BELOW = 0.70
HOT_BELOW = 0.30

# Signals while already HOT before the session is quarantined. An agent that
# keeps tripping detectors after losing most of its trust is not having a bad
# day.
QUARANTINE_AFTER = 3


class Zone:
    COLD = "cold"
    WARM = "warm"
    HOT = "hot"


def zone_for(trust: float) -> str:
    if trust < HOT_BELOW:
        return Zone.HOT
    if trust < WARM_BELOW:
        return Zone.WARM
    return Zone.COLD


@dataclass
class TrustState:
    agent_id: str
    trust: float = 1.0
    hot_signals: int = 0
    quarantined: bool = False
    updated: str = ""

    @property
    def zone(self) -> str:
        return zone_for(self.trust)

    # -- transitions --------------------------------------------------------

    def on_signal(self, weight: float, ts: str = "") -> TrustState:
        was_hot = self.zone == Zone.HOT
        self.trust = trust_after_signal(self.trust, weight)
        self.updated = ts or self.updated

        if self.zone == Zone.HOT:
            # Only count signals that land while ALREADY hot. The signal that
            # pushed the agent into HOT is what the zone itself expresses;
            # counting it too would quarantine on a single bad action.
            if was_hot:
                self.hot_signals += 1
            if self.hot_signals >= QUARANTINE_AFTER:
                self.quarantined = True
        return self

    def on_clean(self, hours: float, ts: str = "") -> TrustState:
        before = self.zone
        self.trust = trust_after_clean(self.trust, hours)
        self.updated = ts or self.updated
        if before == Zone.HOT and self.zone != Zone.HOT:
            # Climbed back out. Start the count again rather than leaving an
            # agent one signal from quarantine forever.
            self.hot_signals = 0
        return self

    def release(self) -> TrustState:
        """Clear a quarantine. Only ever done by a human."""
        self.quarantined = False
        self.hot_signals = 0
        self.trust = max(self.trust, WARM_BELOW)
        return self

    def to_dict(self) -> dict:
        return {"agent_id": self.agent_id, "trust": round(self.trust, 4),
                "zone": self.zone, "hot_signals": self.hot_signals,
                "quarantined": self.quarantined, "updated": self.updated}


# -- what a zone does -------------------------------------------------------


def gate(state: TrustState, event: Event, verdict: Verdict) -> tuple[Verdict, str]:
    """Apply zone policy. Returns (verdict, reason) — reason is "" if unchanged.

    Only ever raises a verdict. A trust score can make an agent less free; it
    can never make a flagged action allowed.
    """
    sensitive = event.resource.cls in SENSITIVE_CLASSES or (
        event.resource.cls is ResourceClass.NETWORK_EGRESS)

    if state.quarantined:
        if sensitive:
            return Verdict.BLOCK, (
                f"agent is quarantined after {state.hot_signals} signals while "
                f"already at low trust, and this reaches a "
                f"{event.resource.cls.value} resource")
        if verdict is Verdict.ALLOW:
            return Verdict.ASK, "agent is quarantined; every action needs approval"
        return verdict, ""

    if not sensitive:
        return verdict, ""

    if state.zone == Zone.HOT:
        return Verdict.BLOCK, (
            f"agent trust is {state.trust:.2f} (hot) and this reaches a "
            f"{event.resource.cls.value} resource")

    if state.zone == Zone.WARM and verdict is Verdict.ALLOW:
        return Verdict.ASK, (
            f"agent trust is {state.trust:.2f} (warm) and this reaches a "
            f"{event.resource.cls.value} resource")

    return verdict, ""


@dataclass
class TrustStore:
    """Trust by agent. Loaded from and written back to the control plane."""

    states: dict[str, TrustState] = field(default_factory=dict)

    def get(self, agent_id: str) -> TrustState:
        if agent_id not in self.states:
            self.states[agent_id] = TrustState(agent_id=agent_id)
        return self.states[agent_id]

    def load(self, rows: list[dict]) -> None:
        """Seed from persisted rows.

        Trust must survive a proxy restart (ADR-023). Restarting is the first
        thing anyone does when a tool starts blocking them, and in-memory trust
        would make that an escape hatch.
        """
        for r in rows:
            aid = r.get("agent_id")
            if not aid:
                continue
            self.states[aid] = TrustState(
                agent_id=aid,
                trust=float(r.get("trust", 1.0)),
                hot_signals=int(r.get("hot_signals", 0)),
                quarantined=bool(r.get("quarantined", False)),
                updated=r.get("trust_updated", "") or r.get("updated", ""),
            )

    def snapshot(self) -> list[dict]:
        return [s.to_dict() for s in self.states.values()]
