"""Trust machine tests — asymmetry, zones, quarantine, persistence."""

import pytest
from weaver_core.events import Event, Resource, ResourceClass, Tool, Verdict
from weaver_core.risk.model import Weight
from weaver_core.trust.model import (
    HOT_BELOW,
    QUARANTINE_AFTER,
    WARM_BELOW,
    TrustState,
    TrustStore,
    Zone,
    gate,
    zone_for,
)


def _ev(cls=ResourceClass.SECRET, uri="file:///app/.env"):
    return Event(agent_id="a", session_id="s", step=0,
                 tool=Tool("fs-mcp", "read_file"), resource=Resource(uri, cls))


# -- zones ------------------------------------------------------------------


def test_zone_boundaries():
    assert zone_for(1.0) == Zone.COLD
    assert zone_for(WARM_BELOW) == Zone.COLD
    assert zone_for(WARM_BELOW - 0.01) == Zone.WARM
    assert zone_for(HOT_BELOW) == Zone.WARM
    assert zone_for(HOT_BELOW - 0.01) == Zone.HOT
    assert zone_for(0.0) == Zone.HOT


def test_trust_is_asymmetric():
    """Cheap to lose, expensive to regain — the correct shape for security."""
    s = TrustState("a")
    s.on_signal(Weight.CRITICAL)
    lost = 1.0 - s.trust
    before = s.trust
    s.on_clean(hours=1)
    assert s.trust - before < lost / 10


def test_recovery_is_gradual_and_never_certain():
    s = TrustState("a", trust=0.1)
    s.on_clean(hours=24)
    assert 0.1 < s.trust < 1.0


# -- quarantine -------------------------------------------------------------


def test_quarantine_needs_repeated_signals_while_already_hot():
    """The signal that pushes an agent into HOT is expressed by the zone.

    Counting it toward quarantine too would quarantine on a single bad action.
    """
    s = TrustState("a")
    for _ in range(3):
        s.on_signal(Weight.CRITICAL)
    assert s.zone == Zone.HOT
    assert s.hot_signals == 0
    assert not s.quarantined

    for _ in range(QUARANTINE_AFTER):
        s.on_signal(Weight.HIGH)
    assert s.quarantined


def test_recovering_out_of_hot_resets_the_count():
    """Otherwise an agent sits one signal from quarantine forever."""
    s = TrustState("a", trust=0.2)
    s.on_signal(Weight.LOW)
    assert s.hot_signals == 1
    s.on_clean(hours=48)
    assert s.zone != Zone.HOT
    assert s.hot_signals == 0


def test_quarantine_survives_trust_recovery():
    """Time alone must not clear a quarantine — only a human does."""
    s = TrustState("a", trust=0.05, hot_signals=QUARANTINE_AFTER, quarantined=True)
    s.on_clean(hours=72)
    assert s.quarantined
    s.release()
    assert not s.quarantined
    assert s.zone != Zone.HOT


# -- gating -----------------------------------------------------------------


def test_cold_agent_is_ungated():
    s = TrustState("a", trust=1.0)
    assert gate(s, _ev(), Verdict.ALLOW) == (Verdict.ALLOW, "")


def test_warm_gates_sensitive_but_not_ordinary_work():
    """ADR-022: zones gate sensitive classes; they do not shade thresholds.

    A bad trust score must not turn into blanket noise on ordinary work.
    """
    s = TrustState("a", trust=0.5)
    assert s.zone == Zone.WARM

    v, why = gate(s, _ev(ResourceClass.SECRET), Verdict.ALLOW)
    assert v is Verdict.ASK and why

    v, why = gate(s, _ev(ResourceClass.SOURCE_CODE, "file:///app/main.py"), Verdict.ALLOW)
    assert v is Verdict.ALLOW and why == ""


def test_hot_blocks_sensitive():
    s = TrustState("a", trust=0.1)
    v, why = gate(s, _ev(ResourceClass.SECRET), Verdict.ALLOW)
    assert v is Verdict.BLOCK and "hot" in why


def test_quarantine_blocks_sensitive_and_asks_for_the_rest():
    """The answer chosen for M4: keep the agent working on safe things, and
    keep it away from anything dangerous."""
    s = TrustState("a", trust=0.0, hot_signals=5, quarantined=True)

    v, _ = gate(s, _ev(ResourceClass.SECRET), Verdict.ALLOW)
    assert v is Verdict.BLOCK

    v, why = gate(s, _ev(ResourceClass.SOURCE_CODE, "file:///app/main.py"), Verdict.ALLOW)
    assert v is Verdict.ASK and "quarantined" in why


def test_gating_only_ever_raises():
    """A trust score can make an agent less free. It can never make a flagged
    action allowed."""
    for trust in (1.0, 0.5, 0.1, 0.0):
        s = TrustState("a", trust=trust)
        v, _ = gate(s, _ev(ResourceClass.SOURCE_CODE, "file:///x.py"), Verdict.BLOCK)
        assert v is Verdict.BLOCK


# -- persistence ------------------------------------------------------------


def test_store_roundtrip():
    """Trust must survive a proxy restart (ADR-023). Restarting is the first
    thing anyone does when a tool starts blocking them."""
    store = TrustStore()
    s = store.get("agent-1")
    for _ in range(4):
        s.on_signal(Weight.CRITICAL)
    snap = store.snapshot()

    restored = TrustStore()
    restored.load(snap)
    got = restored.get("agent-1")
    assert got.trust == pytest.approx(s.trust)
    assert got.zone == s.zone
    assert got.quarantined == s.quarantined


def test_store_ignores_rows_without_an_agent_id():
    store = TrustStore()
    store.load([{"trust": 0.1}, {"agent_id": "ok", "trust": 0.4}])
    assert list(store.states) == ["ok"]


# -- through the engine -----------------------------------------------------


def test_trust_carries_across_a_session():
    """The behaviour the suites deliberately reset: within one real session,
    damage accumulates and later actions are gated because of earlier ones."""
    from weaver_core.engine import Engine

    eng = Engine()
    agent = "coding-agent-prod"
    eng.trust.get(agent).trust = 0.55          # already warm

    ev = Event(agent_id=agent, session_id="s", step=0,
               tool=Tool("fs-mcp", "read_file"),
               resource=Resource("file:///app/.env", ResourceClass.SECRET))
    decision, _ = eng.evaluate(ev)

    assert decision.verdict is Verdict.ASK
    assert any(d["detector"] == "trust_zone" for d in decision.detectors)
    assert "warm" in decision.sentence.lower()
