"""Tier 1 — deterministic, inline, near-zero false positive.

These run in the data plane's hot path. Every one of them is paired with at
least one benign scenario designed to make it misfire; if you add a detector
here, add the benign case first.
"""

from __future__ import annotations

from ..baseline.model import step_key
from ..events import Action, ResourceClass
from ..graph.model import (
    SENSITIVE_CLASSES,
    agent_node,
    resource_node,
    sensitivity,
    tool_node,
)
from ..risk.model import Signal, Weight
from .base import DetectorContext, tier1


def _sig(ctx: DetectorContext, detector: str, weight: float, detail: str) -> Signal:
    ev = ctx.event
    return Signal(
        detector=detector,
        weight=weight,
        detail=detail,
        node_id=resource_node(ev.resource.uri) if ev.resource.uri else "",
        agent_id=ev.agent_id,
        ts=ev.ts,
    )


@tier1("retry_after_denial")
def retry_after_denial(ctx: DetectorContext) -> Signal | None:
    """An agent denied on a sensitive resource, now reaching it another way.

    The signature detector. This is the agent equivalent of port scanning, and
    almost nothing on the market catches it, because per-call policy engines
    have no memory of the denial.

    Keyed strictly on DENIED, never on ERROR — see SessionWindow.last_denial.
    """
    ev = ctx.event
    denial = ctx.window.last_denial(within=6)
    if denial is None:
        return None
    if denial.resource.cls not in SENSITIVE_CLASSES:
        return None
    # Same sensitivity class, different route. Same tool again is a retry, not
    # an evasion, and retries are ordinary.
    if ev.resource.cls is not denial.resource.cls:
        return None
    if ev.tool.qualified == denial.tool.qualified:
        return None
    return _sig(
        ctx, "retry_after_denial", Weight.CRITICAL,
        f"denied on {denial.resource.uri} via {denial.tool.qualified} at step "
        f"{denial.step}, now reaching {ev.resource.cls.value} via "
        f"{ev.tool.qualified} instead",
    )


@tier1("sensitive_then_egress")
def sensitive_then_egress(ctx: DetectorContext) -> Signal | None:
    """Credentials read, then data leaving the network. The canonical exfil shape.

    Requires BOTH halves. Reading .env on its own is what every app boot does —
    see scenarios/benign/secret_read_no_egress.yaml.
    """
    ev = ctx.event
    if ev.action is not Action.EGRESS and ev.resource.cls is not ResourceClass.NETWORK_EGRESS:
        return None
    for prior in reversed(ctx.window.recent(8)):
        if prior.event_id == ev.event_id:
            continue
        if prior.resource.cls is ResourceClass.SECRET:
            return _sig(
                ctx, "sensitive_then_egress", Weight.CRITICAL,
                f"read {prior.resource.uri} (secret) at step {prior.step}, then "
                f"external request to {ev.resource.uri} at step {ev.step}",
            )
    return None


@tier1("scope_widening")
def scope_widening(ctx: DetectorContext) -> Signal | None:
    """Sensitivity climbing monotonically. Each step is dull; the gradient is not."""
    recent = ctx.window.recent(5)
    if len(recent) < 4:
        return None
    ranks = [sensitivity(e.resource.cls) for e in recent]
    tail = ranks[-4:]
    if not all(b > a for a, b in zip(tail[:-1], tail[1:], strict=True)):
        return None
    if ctx.event.resource.cls not in SENSITIVE_CLASSES:
        return None
    path = " -> ".join(e.resource.cls.value for e in recent[-4:])
    return _sig(
        ctx, "scope_widening", Weight.HIGH,
        f"access sensitivity climbed steadily over 4 steps: {path}",
    )


@tier1("novel_sequence")
def novel_sequence(ctx: DetectorContext) -> Signal | None:
    """A tool ordering this agent has never used, built from tools it may use daily."""
    if not ctx.baseline.is_active:
        return None
    recent = ctx.window.recent(3)
    if len(recent) < 3:
        return None
    if ctx.baseline.knows_ngram(recent):
        return None
    if ctx.baseline.knows_class_pair(recent[-2].resource.cls, recent[-1].resource.cls):
        # The pairing is familiar even if this exact triple is not. Firing here
        # would light up on every reordering of an ordinary workflow.
        return None
    # Graduated by destination. A novel ordering of ordinary reads is how
    # agents work; a novel ordering that reaches a secret or a database is
    # worth a look. Flat MEDIUM here sat exactly on the ASK threshold, so
    # every unseen triple queued a human approval — unusable on real traffic,
    # where thousands of distinct triples are normal.
    touches_sensitive = any(e.resource.cls in SENSITIVE_CLASSES for e in recent)
    weight = Weight.MEDIUM if touches_sensitive else Weight.LOW
    seq = " -> ".join(step_key(e) for e in recent)
    return _sig(ctx, "novel_sequence", weight,
                f"tool sequence not seen in {ctx.baseline.event_count} prior events: {seq}")


@tier1("novel_edge")
def novel_edge(ctx: DetectorContext) -> Signal | None:
    """This agent has never used this tool, or this tool has never touched this class."""
    if not ctx.baseline.is_active:
        return None
    ev = ctx.event
    if ctx.baseline.knows_tool(ev.tool.qualified):
        return None
    if ev.resource.cls not in SENSITIVE_CLASSES:
        # A new tool reaching for ordinary files is how agents grow. Only flag
        # a first contact that lands somewhere sensitive.
        return None
    a, t = agent_node(ev.agent_id), tool_node(ev.tool.server, ev.tool.name)
    if ctx.baseline.knows_edge(a, t, ev.action):
        return None
    return _sig(ctx, "novel_edge", Weight.MEDIUM,
                f"first use of {ev.tool.qualified} by this agent, against a "
                f"{ev.resource.cls.value} resource")


@tier1("fan_out")
def fan_out(ctx: DetectorContext) -> Signal | None:
    """Recruiting agents it has never invoked. Reach spreads without any single
    permission check failing."""
    if ctx.event.action is not Action.INVOKE:
        return None
    if not ctx.baseline.is_active:
        return None
    novel = {
        e.resource.uri
        for e in ctx.window.recent(6)
        if e.action is Action.INVOKE
        and e.resource.uri
        and e.resource.uri not in ctx.baseline.invoke_targets
    }
    if len(novel) < 2:
        return None
    return _sig(ctx, "fan_out", Weight.HIGH,
                f"invoked {len(novel)} agents never invoked before: "
                f"{', '.join(sorted(novel))}")
