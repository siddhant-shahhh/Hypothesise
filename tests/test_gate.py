"""Gate tests — the three safety guarantees, pinned.

These are the tests that stop the product from breaking a customer:
budget enforcement, fail-open, observe-mode, and no-sentence-no-block.
"""

import asyncio

import pytest
from weaver_core.events import Decision, Event, Resource, Tool, Verdict
from weaver_core.graph.model import GraphSnapshot
from weaver_proxy.gate import Gate, GateConfig


def _ev() -> Event:
    return Event(agent_id="a", session_id="s", step=0,
                 tool=Tool("fs-mcp", "read_file"), resource=Resource("file:///x"))


@pytest.mark.asyncio
async def test_allows_by_default():
    g = Gate(config=GateConfig(mode="enforce"))
    assert (await g.decide(_ev(), GraphSnapshot())).verdict is Verdict.ALLOW


@pytest.mark.asyncio
async def test_slow_evaluator_fails_open_within_budget():
    """An engine that hangs must not hang the agent."""
    async def molasses(_e, _s):
        await asyncio.sleep(5)
        return Decision(verdict=Verdict.BLOCK, sentence="never gets here")

    g = Gate(config=GateConfig(mode="enforce", fail_mode="open", budget_ms=30),
             evaluator=molasses)
    d = await asyncio.wait_for(g.decide(_ev(), GraphSnapshot()), timeout=1.0)
    assert d.verdict is Verdict.ALLOW
    assert d.fail_open is True
    assert g.stats.timeouts == 1
    assert d.budget_ms < 500


@pytest.mark.asyncio
async def test_crashing_evaluator_fails_open():
    async def boom(_e, _s):
        raise ValueError("engine exploded")

    g = Gate(config=GateConfig(mode="enforce", fail_mode="open"), evaluator=boom)
    d = await g.decide(_ev(), GraphSnapshot())
    assert d.verdict is Verdict.ALLOW
    assert d.fail_open is True
    assert "ValueError" in d.sentence


@pytest.mark.asyncio
async def test_fail_closed_is_opt_in():
    async def boom(_e, _s):
        raise RuntimeError("nope")

    g = Gate(config=GateConfig(mode="enforce", fail_mode="closed"), evaluator=boom)
    assert (await g.decide(_ev(), GraphSnapshot())).verdict is Verdict.BLOCK


@pytest.mark.asyncio
async def test_observe_mode_never_blocks():
    """A fresh install must never block anything, however loud the engine is."""
    async def always_block(_e, _s):
        return Decision(verdict=Verdict.BLOCK, score=0.99, sentence="credential exfil")

    g = Gate(config=GateConfig(mode="observe"), evaluator=always_block)
    d = await g.decide(_ev(), GraphSnapshot())
    assert d.verdict is Verdict.ALLOW
    assert "[observe] would have blocked" in d.sentence
    assert g.stats.observed_would_block == 1
    assert g.stats.blocked == 0


@pytest.mark.asyncio
async def test_observe_mode_preserves_evidence():
    """Shadow mode must record WHAT it would have done, or the diff report is useless."""
    async def blocker(_e, _s):
        return Decision(verdict=Verdict.BLOCK, score=0.9,
                        detectors=[{"detector": "sensitive_then_egress", "weight": 1.0}],
                        sentence="read creds then egressed")

    g = Gate(config=GateConfig(mode="observe"), evaluator=blocker)
    d = await g.decide(_ev(), GraphSnapshot())
    assert d.score == 0.9
    assert d.detectors[0]["detector"] == "sensitive_then_egress"


@pytest.mark.asyncio
async def test_block_without_explanation_is_suppressed():
    """ARCHITECTURE section 9, rule 3: no sentence, no block.

    An unexplainable block is a bug. Shipping it costs more trust than the
    attack it might have stopped.
    """
    async def mute_blocker(_e, _s):
        return Decision(verdict=Verdict.BLOCK, score=0.95, sentence="   ")

    g = Gate(config=GateConfig(mode="enforce"), evaluator=mute_blocker)
    d = await g.decide(_ev(), GraphSnapshot())
    assert d.verdict is Verdict.ALLOW
    assert "block suppressed" in d.sentence


@pytest.mark.asyncio
async def test_enforce_mode_blocks_with_explanation():
    async def blocker(_e, _s):
        return Decision(verdict=Verdict.BLOCK, score=0.95,
                        sentence="Blocked: read credentials then called an unknown host.")

    g = Gate(config=GateConfig(mode="enforce"), evaluator=blocker)
    d = await g.decide(_ev(), GraphSnapshot())
    assert d.verdict is Verdict.BLOCK
    assert g.stats.blocked == 1


@pytest.mark.asyncio
async def test_stats_track_latency():
    g = Gate(config=GateConfig(mode="observe"))
    for _ in range(5):
        await g.decide(_ev(), GraphSnapshot())
    assert g.stats.calls == 5
    assert g.stats.avg_ms >= 0.0
