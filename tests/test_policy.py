"""Scoring and explanation tests."""

import pytest
from hypothesise_core.events import Verdict
from hypothesise_core.graph.model import GraphSnapshot
from hypothesise_core.policy.score import (
    ASK_THRESHOLD,
    BLOCK_THRESHOLD,
    ScoreInput,
    combine_signals,
    decide,
    explain,
    severity,
)
from hypothesise_core.risk.model import Signal, Weight


def sig(name: str, w: float) -> Signal:
    return Signal(detector=name, weight=w, detail=f"{name} detail")


def test_combine_is_saturating_not_additive():
    """Three MEDIUMs must not outweigh one CRITICAL.

    Summation lets a pile of weak hints beat a single hard fact — which is how
    anomaly products earn their reputation for noise.
    """
    three_medium = combine_signals([sig("a", Weight.MEDIUM)] * 3)
    one_critical = combine_signals([sig("b", Weight.CRITICAL)])
    assert three_medium < one_critical
    assert three_medium < 1.0


def test_combine_never_exceeds_one():
    assert combine_signals([sig(f"d{i}", Weight.CRITICAL) for i in range(10)]) <= 1.0


def test_empty_signals_score_zero():
    assert combine_signals([]) == 0.0


def _inp(signals, trust=1.0):
    return ScoreInput(signals=signals, agent_id="a", tool_server="fs-mcp",
                      tool_name="read_file", resource_uri="file:///x", trust=trust)


def test_critical_detectors_short_circuit_to_block():
    """retry_after_denial and sensitive_then_egress describe actions no
    legitimate workflow performs. No amount of trust argues them down."""
    for name in ("retry_after_denial", "sensitive_then_egress"):
        d = decide(_inp([sig(name, Weight.CRITICAL)]), GraphSnapshot())
        assert d.verdict is Verdict.BLOCK, name


def test_clean_traffic_is_allowed_silently():
    d = decide(_inp([]), GraphSnapshot())
    assert d.verdict is Verdict.ALLOW
    assert d.sentence == ""


def test_thresholds_are_ordered():
    assert 0 < ASK_THRESHOLD < BLOCK_THRESHOLD <= 1.0


def test_low_signal_alone_does_not_trigger_approval():
    """A novel ordering of ordinary reads must not queue a human."""
    d = decide(_inp([sig("novel_sequence", Weight.LOW)]), GraphSnapshot())
    assert d.verdict is Verdict.ALLOW


def test_every_non_allow_decision_has_a_sentence():
    """ARCHITECTURE section 9, rule 3 — the Gate enforces it, this makes it possible."""
    for w in (Weight.MEDIUM, Weight.HIGH, Weight.CRITICAL):
        d = decide(_inp([sig("scope_widening", w)]), GraphSnapshot())
        if d.verdict is not Verdict.ALLOW:
            assert d.sentence.strip(), f"no sentence at weight {w}"


def test_explanation_names_the_strongest_signal():
    signals = [sig("novel_sequence", Weight.LOW), sig("sensitive_then_egress", Weight.CRITICAL)]
    text = explain(Verdict.BLOCK, signals, {})
    assert "sensitive_then_egress detail" in text
    assert "novel_sequence" in text          # listed as corroborating


def test_evidence_is_sorted_strongest_first():
    d = decide(_inp([sig("weak", Weight.LOW), sig("strong", Weight.HIGH)]), GraphSnapshot())
    assert d.detectors[0]["detector"] == "strong"


def test_low_trust_raises_the_score():
    clean = decide(_inp([sig("novel_edge", Weight.MEDIUM)], trust=1.0), GraphSnapshot())
    shaky = decide(_inp([sig("novel_edge", Weight.MEDIUM)], trust=0.2), GraphSnapshot())
    assert shaky.score > clean.score


@pytest.mark.parametrize("w,name", [
    (Weight.CRITICAL, "CRITICAL"), (Weight.HIGH, "HIGH"),
    (Weight.MEDIUM, "MEDIUM"), (Weight.LOW, "LOW"),
])
def test_severity_labels(w, name):
    assert severity(w) == name
