"""Tier-3 graph detector tests.

These are the detectors that only exist because risk lives on the graph
(ADR-004) — and `resource_reputation` is the first whose input comes from a
different agent's history.
"""

import pytest
from hypothesise_core.baseline.model import Baseline
from hypothesise_core.detectors.base import DetectorContext, SessionWindow
from hypothesise_core.detectors.tier3 import (
    REPUTATION_THRESHOLD,
    blast_radius,
    egress_after_broad_access,
    path_risk,
    resource_reputation,
)
from hypothesise_core.events import Action, Event, Resource, ResourceClass, Tool
from hypothesise_core.graph.model import GraphSnapshot, Node, resource_node


def _ev(step, uri, cls, action=Action.READ, tool=("fs-mcp", "read_file")):
    return Event(agent_id="a", session_id="s", step=step, tool=Tool(*tool),
                 resource=Resource(uri, cls), action=action)


def _ctx(events, snapshot=None, active=True):
    w = SessionWindow()
    for e in events:
        w.push(e)
    b = Baseline(agent_id="a", state="active" if active else "learning",
                 event_count=50)
    return DetectorContext(events[-1], w, b, snapshot or GraphSnapshot())


# -- path_risk --------------------------------------------------------------


def test_path_risk_fires_on_accumulated_secrets():
    """No individual step is unusual. The accumulation is the attack.

    This is what slow_harvest was written for, and what nothing caught until
    Tier 3 existed.
    """
    events = [
        _ev(0, "file:///app/src/a.py", ResourceClass.SOURCE_CODE),
        _ev(1, "file:///root/.ssh/id_rsa", ResourceClass.SECRET),
        _ev(2, "file:///app/src/b.py", ResourceClass.SOURCE_CODE),
        _ev(3, "file:///root/.aws/credentials", ResourceClass.SECRET),
        _ev(4, "file:///app/src/c.py", ResourceClass.SOURCE_CODE),
        _ev(5, "file:///app/secrets.json", ResourceClass.SECRET),
    ]
    sig = path_risk(_ctx(events))
    assert sig is not None
    assert "3 distinct credential" in sig.detail


def test_path_risk_silent_on_a_single_secret():
    """Reading one .env is what every app boot does."""
    events = [
        _ev(0, "file:///app/config.yaml", ResourceClass.CONFIG),
        _ev(1, "file:///app/.env", ResourceClass.SECRET),
    ]
    assert path_risk(_ctx(events)) is None


def test_path_risk_counts_distinct_not_repeated():
    """The same file read five times is a loop, not a harvest."""
    events = [_ev(i, "file:///app/.env", ResourceClass.SECRET) for i in range(5)]
    assert path_risk(_ctx(events)) is None


# -- resource_reputation ----------------------------------------------------


def test_resource_reputation_inherits_another_agents_finding():
    """The stigmergy signal: risk lives on the resource, so an agent that has
    never misbehaved inherits suspicion where others got caught."""
    uri = "https://drop.example-evil.net/x"
    snap = GraphSnapshot(nodes={
        resource_node(uri): Node(id=resource_node(uri), kind="resource",
                                 cls=ResourceClass.NETWORK_EGRESS,
                                 risk=0.9, confidence=0.9)})
    sig = resource_reputation(_ctx([_ev(0, uri, ResourceClass.NETWORK_EGRESS)], snap))
    assert sig is not None
    assert "accumulated risk" in sig.detail


def test_reputation_ignores_a_mere_class_floor():
    """Every secret starts at 0.30 by class. If that fired, the detector would
    be a synonym for 'is a secret'."""
    uri = "file:///app/.env"
    snap = GraphSnapshot(nodes={
        resource_node(uri): Node(id=resource_node(uri), kind="resource",
                                 cls=ResourceClass.SECRET,
                                 risk=0.30, confidence=1.0)})
    assert resource_reputation(_ctx([_ev(0, uri, ResourceClass.SECRET)], snap)) is None


def test_reputation_is_damped_by_low_confidence():
    """One sighting of a new node must not condemn it."""
    uri = "file:///app/.env"
    snap = GraphSnapshot(nodes={
        resource_node(uri): Node(id=resource_node(uri), kind="resource",
                                 cls=ResourceClass.SECRET,
                                 risk=0.95, confidence=0.05)})
    assert resource_reputation(_ctx([_ev(0, uri, ResourceClass.SECRET)], snap)) is None


def test_reputation_threshold_is_above_every_class_floor():
    from hypothesise_core.graph.model import RISK_FLOORS

    assert max(RISK_FLOORS.values()) < REPUTATION_THRESHOLD


def test_reputation_silent_on_an_unknown_resource():
    assert resource_reputation(_ctx([_ev(0, "file:///new.py",
                                        ResourceClass.SOURCE_CODE)])) is None


# -- blast_radius -----------------------------------------------------------


def test_blast_radius_needs_breadth():
    events = [_ev(i, f"file:///s{i}/.env", ResourceClass.SECRET) for i in range(5)]
    sig = blast_radius(_ctx(events))
    assert sig is not None
    assert "5 distinct sensitive" in sig.detail


def test_blast_radius_silent_on_ordinary_work():
    events = [_ev(i, f"file:///app/f{i}.py", ResourceClass.SOURCE_CODE)
              for i in range(8)]
    assert blast_radius(_ctx(events)) is None


# -- egress_after_broad_access ---------------------------------------------


def test_egress_after_broad_access():
    """The shape sensitive_then_egress misses: no secret immediately precedes
    the egress, but the session has been ranging widely."""
    events = [
        _ev(0, "file:///app/.env", ResourceClass.SECRET),
        _ev(1, "postgres://prod/users", ResourceClass.DATABASE),
        _ev(2, "shell://whoami", ResourceClass.SHELL),
        _ev(3, "file:///app/main.py", ResourceClass.SOURCE_CODE),
        _ev(4, "https://x.example-evil.net/c", ResourceClass.NETWORK_EGRESS,
            Action.EGRESS, ("http-mcp", "fetch")),
    ]
    sig = egress_after_broad_access(_ctx(events))
    assert sig is not None


def test_egress_alone_is_not_suspicious():
    """A research agent's whole job is egress."""
    events = [
        _ev(0, "https://arxiv.org/abs/1", ResourceClass.NETWORK_EGRESS,
            Action.EGRESS, ("http-mcp", "fetch")),
        _ev(1, "https://arxiv.org/abs/2", ResourceClass.NETWORK_EGRESS,
            Action.EGRESS, ("http-mcp", "fetch")),
    ]
    assert egress_after_broad_access(_ctx(events)) is None


@pytest.mark.parametrize("detector", [path_risk, resource_reputation,
                                      blast_radius, egress_after_broad_access])
def test_no_tier3_detector_raises_on_a_minimal_context(detector):
    detector(_ctx([_ev(0, "", ResourceClass.UNKNOWN)]))
