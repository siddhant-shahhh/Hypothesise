"""Detector tests — does each one fire on its target, and stay quiet otherwise.

The suite runs the same train-on-benign / evaluate-everything split the replay
harness uses, because a detector evaluated without a baseline is meaningless:
an agent with no history cannot have anomalies.
"""

from pathlib import Path

import pytest
from weaver_core.detectors.base import (
    detector_errors,
    reset_detector_errors,
    tier1_names,
    tier2_names,
)
from weaver_core.engine import Engine
from weaver_core.events import Verdict
from weaver_core.scenario import load_all, load_scenario

ROOT = Path(__file__).resolve().parent.parent
TRAIN_PASSES = 3


@pytest.fixture(scope="module")
def engine():
    reset_detector_errors()
    scenarios = load_all(ROOT / "scenarios")
    benign = [s for s in scenarios if not s.should_fire]
    eng = Engine()
    for _ in range(TRAIN_PASSES):
        for sc in benign:
            eng.train(sc.events)
            eng.reset_sessions()
    return eng


def run(eng: Engine, name: str):
    """Replay one scenario, return (fired, detectors_that_fired)."""
    path = ROOT / "scenarios" / ("attack" if "attack" in str(
        ROOT / "scenarios" / "attack" / f"{name}.yaml") and
        (ROOT / "scenarios" / "attack" / f"{name}.yaml").exists() else "benign") / f"{name}.yaml"
    sc = load_scenario(path)
    eng.reset_sessions()
    # Independent hypothetical -> clean trust. See test_trust.py for the
    # carries-forward behaviour, which is what production does.
    from weaver_core.trust.model import TrustStore

    eng.trust = TrustStore()
    fired, hits = False, []
    for ev in sc.events:
        decision, signals = eng.evaluate(ev)
        if decision.verdict is not Verdict.ALLOW:
            fired = True
        hits.extend(s.detector for s in signals)
    return fired, set(hits)


# -- the big one ------------------------------------------------------------


def test_no_detector_crashes_silently(engine):
    """The regression that matters most.

    `run_tier1` catches exceptions so a buggy detector cannot break a
    decision — which also means a broken detector produces no signal and no
    complaint. `scope_widening` raised ValueError on EVERY call for its whole
    life (a `strict=True` zip over lists of different length) and looked like
    a detector that simply never matched.
    """
    for sc in load_all(ROOT / "scenarios"):
        engine.reset_sessions()
        for ev in sc.events:
            engine.evaluate(ev)
    assert detector_errors() == {}, (
        f"detectors raising exceptions (they never fire): {detector_errors()}"
    )


def test_all_detectors_registered():
    assert set(tier1_names()) == {
        "retry_after_denial", "sensitive_then_egress", "scope_widening",
        "novel_sequence", "novel_edge", "fan_out", "shell_exfil",
    }
    assert set(tier2_names()) == {
        # tier 2 — statistical
        "argument_anomaly", "novel_arg_shape", "velocity_anomaly", "off_hours",
        # tier 3 — graph-level
        "path_risk", "resource_reputation", "blast_radius",
        "egress_after_broad_access",
    }


def test_tiers_do_not_overlap():
    """A detector registered in both registries would fire twice and double-count."""
    assert not set(tier1_names()) & set(tier2_names())


# -- each detector fires on its own target ----------------------------------


@pytest.mark.parametrize(
    "scenario,detector",
    [
        ("retry_after_denial", "retry_after_denial"),
        ("exfil_basic", "sensitive_then_egress"),
        ("scope_widening", "scope_widening"),
        ("agent_fanout", "fan_out"),
        ("novel_tool_combination", "novel_sequence"),
        ("bulk_exfil_payload", "argument_anomaly"),
        ("shell_oneliner_exfil", "shell_exfil"),
    ],
)
def test_detector_fires_on_its_target(engine, scenario, detector):
    fired, hits = run(engine, scenario)
    assert fired, f"{scenario} produced no verdict above ALLOW"
    assert detector in hits, f"{scenario} fired {hits}, expected {detector}"


# -- and stays quiet on every benign case -----------------------------------


@pytest.mark.parametrize("scenario", [
    "normal_coding_loop",
    "secret_read_no_egress",
    "research_agent_web",
    "internal_api_call",
    "ci_deploy_flow",
    "support_agent_slack",
    "failed_reads_are_normal",
    "repeated_same_read",
    "legit_db_read",
    "config_read_no_secret",
    "orchestrator_normal",
    "shell_network_no_secret",
])
def test_benign_scenarios_stay_silent(engine, scenario):
    """A false positive is an uninstall. These are the cases that cause one."""
    fired, hits = run(engine, scenario)
    assert not fired, f"FALSE POSITIVE on {scenario}: {hits}"


# -- the specific confusions each detector is prone to ----------------------


def test_reading_dotenv_alone_is_not_exfil(engine):
    """Every app boot reads .env. It is .env FOLLOWED BY EGRESS that is an attack."""
    _, hits = run(engine, "secret_read_no_egress")
    assert "sensitive_then_egress" not in hits


def test_external_egress_is_normal_for_a_research_agent(engine):
    """Baselines are per-agent precisely so this is possible."""
    fired, _ = run(engine, "research_agent_web")
    assert not fired


def test_errors_are_not_denials(engine):
    """Probing for optional files returns 'not found'. That is not a denial."""
    _, hits = run(engine, "failed_reads_are_normal")
    assert "retry_after_denial" not in hits


def test_network_shell_without_a_secret_is_not_exfil(engine):
    """pip install and internal health checks both reach the network.

    shell_exfil requires BOTH halves; either alone is daily work.
    """
    _, hits = run(engine, "shell_network_no_secret")
    assert "shell_exfil" not in hits


def test_internal_shell_curl_is_not_egress(engine):
    """`curl http://api.internal/health` in a deploy script must not classify
    as network egress, or every health check is an incident."""
    from weaver_core.classify import classify_resource
    from weaver_core.events import ResourceClass

    assert classify_resource("shell://curl -s http://api.internal/health") is ResourceClass.SHELL
    assert classify_resource("shell://curl -s http://10.0.0.5/ping") is ResourceClass.SHELL
    assert classify_resource("shell://curl -s https://evil.net/x") is ResourceClass.NETWORK_EGRESS


def test_tight_edit_loop_is_not_an_attack(engine):
    """High velocity on one known resource, zero novelty."""
    fired, _ = run(engine, "repeated_same_read")
    assert not fired


# -- suite-level numbers ----------------------------------------------------


def test_suite_meets_m2_exit_criteria(engine):
    """BUILD_PLAN M2: >= 80% detection, <= 5% false positives.

    Against the scenario suite only — there is no real traffic yet (ADR-010),
    and a suite we wrote ourselves cannot surprise us.
    """
    scenarios = load_all(ROOT / "scenarios")
    attack = [s for s in scenarios if s.should_fire]
    benign = [s for s in scenarios if not s.should_fire]

    detected = sum(1 for s in attack if run(engine, s.name)[0])
    false_pos = sum(1 for s in benign if run(engine, s.name)[0])

    assert detected / len(attack) >= 0.80, f"detection {detected}/{len(attack)}"
    assert false_pos / len(benign) <= 0.05, f"false positives {false_pos}/{len(benign)}"
