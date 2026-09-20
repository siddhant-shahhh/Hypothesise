"""Scenario harness tests.

The suite must load and classify correctly BEFORE any detector exists —
otherwise a detector gets tuned against a broken target.
"""

from pathlib import Path

import pytest

from weaver_core.events import Action, ResourceClass, Status
from weaver_core.scenario import load_all, load_scenario

ROOT = Path(__file__).resolve().parent.parent


def test_all_scenarios_load():
    scenarios = load_all(ROOT / "scenarios")
    assert len(scenarios) >= 8
    for sc in scenarios:
        assert sc.expect in ("fire", "silent")
        assert sc.events, f"{sc.name} has no events"
        assert sc.description, f"{sc.name} has no description"


def test_suite_has_both_polarities():
    scenarios = load_all(ROOT / "scenarios")
    assert any(s.should_fire for s in scenarios)
    assert any(not s.should_fire for s in scenarios)


def test_exfil_scenario_classifies_correctly():
    sc = load_scenario(ROOT / "scenarios/attack/exfil_basic.yaml")
    classes = [e.resource.cls for e in sc.events]
    assert ResourceClass.SECRET in classes
    assert ResourceClass.NETWORK_EGRESS in classes
    # the secret must come BEFORE the egress or the detector has nothing to find
    assert classes.index(ResourceClass.SECRET) < classes.index(ResourceClass.NETWORK_EGRESS)


def test_retry_scenario_has_a_denial():
    sc = load_scenario(ROOT / "scenarios/attack/retry_after_denial.yaml")
    assert sc.events[0].outcome.status is Status.DENIED
    assert any(e.action is Action.EXECUTE for e in sc.events)


def test_internal_api_scenario_has_no_egress():
    """This benign scenario is the false-positive canary for the egress rule."""
    sc = load_scenario(ROOT / "scenarios/benign/internal_api_call.yaml")
    assert all(e.resource.cls is not ResourceClass.NETWORK_EGRESS for e in sc.events)


def test_steps_are_ordered():
    for sc in load_all(ROOT / "scenarios"):
        assert [e.step for e in sc.events] == list(range(len(sc.events)))
