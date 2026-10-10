"""Tier 3 — graph-level detectors.

These are the ones that only exist because risk lives on the graph (ADR-004).
Tier 1 and Tier 2 look at an action and the session around it; these look at
what the agent has accumulated, and at what other agents have already found.

`resource_reputation` is the stigmergy payoff — the first detector whose input
comes from a *different agent's* history rather than this one's.
"""

from __future__ import annotations

from ..events import Action, ResourceClass
from ..graph.model import (
    SENSITIVE_CLASSES,
    agent_node,
    resource_node,
    sensitivity,
    tool_node,
)
from ..risk.model import Signal, Weight, effective_risk
from .base import DetectorContext, tier2

# Distinct secrets in one session before the accumulation itself is the signal.
SECRET_ACCUMULATION = 3
# Distinct sensitive resources before blast radius is worth noting.
BLAST_RADIUS_N = 5
# Effective risk (risk x confidence) a resource must carry before its
# reputation alone is evidence. Deliberately above every class floor, so a
# resource that is merely sensitive never fires this — only one that has
# actually been flagged.
REPUTATION_THRESHOLD = 0.40


def _sig(ctx: DetectorContext, detector: str, weight: float, detail: str) -> Signal:
    ev = ctx.event
    return Signal(
        detector=detector, weight=weight, detail=detail,
        node_id=resource_node(ev.resource.uri) if ev.resource.uri else "",
        agent_id=ev.agent_id, ts=ev.ts,
    )


@tier2("path_risk")
def path_risk(ctx: DetectorContext) -> Signal | None:
    """Several distinct secrets gathered across one session.

    No individual step is unusual: each is one credential read, interleaved
    with ordinary work, at ordinary speed. The accumulation is the attack, and
    it is invisible to any detector that only sees a window.

    This is what `slow_harvest` was written for, and what nothing caught until
    now.
    """
    ev = ctx.event
    if ev.resource.cls is not ResourceClass.SECRET:
        return None

    secrets = {
        e.resource.uri
        for e in ctx.window.events
        if e.resource.cls is ResourceClass.SECRET and e.resource.uri
    }
    if len(secrets) < SECRET_ACCUMULATION:
        return None

    listed = ", ".join(sorted(secrets)[:4])
    return _sig(
        ctx, "path_risk", Weight.HIGH,
        f"{len(secrets)} distinct credential resources read in one session "
        f"({listed}) — no single step is unusual, the accumulation is",
    )


@tier2("resource_reputation")
def resource_reputation(ctx: DetectorContext) -> Signal | None:
    """This resource has been flagged before — possibly by a different agent.

    The stigmergy signal. Risk lives on the resource node, so an agent that has
    never misbehaved inherits suspicion the moment it touches somewhere other
    agents got caught. Ants respond to a trail they did not lay.

    Gated two ways to keep it honest: the node must carry risk well above its
    class floor (so merely being a secret is not enough), and the risk is
    damped by confidence (so one sighting of a new node cannot condemn it).
    """
    ev = ctx.event
    if not ev.resource.uri:
        return None

    node = resource_node(ev.resource.uri)
    risk = effective_risk(
        ctx.snapshot.node_risk(node), ctx.snapshot.node_confidence(node))
    if risk < REPUTATION_THRESHOLD:
        return None

    return _sig(
        ctx, "resource_reputation", Weight.MEDIUM,
        f"{ev.resource.uri} carries an accumulated risk of {risk:.2f} from "
        f"earlier flagged activity, possibly by other agents",
    )


@tier2("blast_radius")
def blast_radius(ctx: DetectorContext) -> Signal | None:
    """How much this agent could now reach if it turned hostile.

    Not an attack on its own — an agent legitimately touching many sensitive
    resources is doing a legitimate job. It is context that makes everything
    else about this session weigh more, which is why it is LOW and cannot
    trigger a verdict alone.
    """
    reached = {
        e.resource.uri
        for e in ctx.window.events
        if e.resource.cls in SENSITIVE_CLASSES and e.resource.uri
    }
    if len(reached) < BLAST_RADIUS_N:
        return None

    worst = max(
        (e.resource.cls for e in ctx.window.events if e.resource.uri),
        key=sensitivity, default=ResourceClass.UNKNOWN)
    return _sig(
        ctx, "blast_radius", Weight.LOW,
        f"this session has reached {len(reached)} distinct sensitive resources, "
        f"up to {worst.value} class",
    )


@tier2("egress_after_broad_access")
def egress_after_broad_access(ctx: DetectorContext) -> Signal | None:
    """Wide access, then an outbound call.

    Catches the shape `sensitive_then_egress` misses: no single secret read
    immediately precedes the egress, but the session has been ranging widely
    across sensitive resources before reaching out.
    """
    ev = ctx.event
    if ev.action is not Action.EGRESS and ev.resource.cls is not ResourceClass.NETWORK_EGRESS:
        return None

    prior = {
        e.resource.uri
        for e in ctx.window.events
        if e.resource.cls in SENSITIVE_CLASSES and e.resource.uri
        and e.event_id != ev.event_id
    }
    if len(prior) < 3:
        return None

    return _sig(
        ctx, "egress_after_broad_access", Weight.HIGH,
        f"external request after this session touched {len(prior)} distinct "
        f"sensitive resources",
    )


# Node helpers kept for the graph traversal work in M5.
__all__ = ["agent_node", "tool_node", "path_risk", "resource_reputation",
           "blast_radius", "egress_after_broad_access"]
