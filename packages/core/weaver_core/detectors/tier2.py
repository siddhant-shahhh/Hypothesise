"""Tier 2 — statistical, baseline-dependent.

These run in the control plane after ingest, not in the proxy's hot path. They
are weaker evidence than Tier 1 by nature: Tier 1 describes an action nobody
legitimate performs, while Tier 2 describes an action that is merely unusual.
They are weighted accordingly and none of them short-circuits.

Every one requires a settled baseline and returns None without it. Statistics
over eight samples are not statistics.
"""

from __future__ import annotations

from datetime import datetime

from ..events import Action, ResourceClass
from ..graph.model import SENSITIVE_CLASSES, resource_node
from ..risk.model import Signal, Weight
from .base import DetectorContext, tier2

VELOCITY_Z = 3.0
ARG_LEN_Z = 4.0


def _sig(ctx: DetectorContext, detector: str, weight: float, detail: str) -> Signal:
    ev = ctx.event
    return Signal(
        detector=detector, weight=weight, detail=detail,
        node_id=resource_node(ev.resource.uri) if ev.resource.uri else "",
        agent_id=ev.agent_id, ts=ev.ts,
    )


@tier2("argument_anomaly")
def argument_anomaly(ctx: DetectorContext) -> Signal | None:
    """An argument payload far larger than this tool has ever carried.

    Replaces the embedding-based `argument_drift` in the original design
    (ADR-011). Structural: an exfiltration POST is enormous compared to a
    normal request, and that is visible in the byte count alone — no model, no
    inference latency, and the arguments themselves are never stored.
    """
    ev = ctx.event
    if not ctx.baseline.is_active or ev.arg_bytes <= 0:
        return None

    z = ctx.baseline.arg_len_z(ev.tool.qualified, ev.arg_bytes)
    if z is None or z < ARG_LEN_Z:
        return None

    outbound = ev.action is Action.EGRESS or ev.resource.cls is ResourceClass.NETWORK_EGRESS
    weight = Weight.HIGH if outbound else Weight.LOW

    # A tool whose arguments are always near-identical has almost no variance,
    # so the z-score explodes into the tens of thousands. Reporting that
    # verbatim reads as a broken gauge rather than a finding, and an
    # explanation nobody believes is worth less than no explanation.
    n, mean, _ = ctx.baseline.arg_len.get(ev.tool.qualified, (0, 0.0, 0.0))
    ratio = ev.arg_bytes / mean if mean > 0 else 0.0
    magnitude = f"{z:.1f} standard deviations" if z < 100 else "far"
    return _sig(
        ctx, "argument_anomaly", weight,
        f"{ev.tool.qualified} called with {ev.arg_bytes:,} bytes of arguments — "
        f"{ratio:.0f}x its usual {mean:,.0f} bytes, {magnitude} above normal "
        f"across {n} prior calls",
    )


@tier2("novel_arg_shape")
def novel_arg_shape(ctx: DetectorContext) -> Signal | None:
    """A tool invoked with a set of parameters it has never been given before."""
    ev = ctx.event
    if not ctx.baseline.is_active or not ev.arg_keys:
        return None
    if ctx.baseline.knows_keyset(ev.tool.qualified, ev.arg_keys):
        return None
    if ev.resource.cls not in SENSITIVE_CLASSES:
        return None
    return _sig(
        ctx, "novel_arg_shape", Weight.LOW,
        f"{ev.tool.qualified} called with parameters never used before "
        f"({', '.join(ev.arg_keys)}) against a {ev.resource.cls.value} resource",
    )


@tier2("velocity_anomaly")
def velocity_anomaly(ctx: DetectorContext) -> Signal | None:
    """Calls arriving far faster than this agent normally works.

    Only counts when it lands on something sensitive. A fast edit-test loop is
    an agent doing its job — see scenarios/benign/repeated_same_read.yaml.
    """
    ev = ctx.event
    if not ctx.baseline.is_active:
        return None
    recent = ctx.window.recent(2)
    if len(recent) < 2:
        return None

    try:
        gap = abs((datetime.fromisoformat(ev.ts)
                   - datetime.fromisoformat(recent[-2].ts)).total_seconds())
    except (ValueError, TypeError):
        return None

    z = ctx.baseline.gap_z(gap)
    if z is None or z > -VELOCITY_Z:      # negative z = gap much SHORTER than normal
        return None
    if ev.resource.cls not in SENSITIVE_CLASSES:
        return None
    return _sig(
        ctx, "velocity_anomaly", Weight.LOW,
        f"call arrived {gap:.2f}s after the previous one, far faster than this "
        f"agent's normal pace, and reached a {ev.resource.cls.value} resource",
    )


@tier2("off_hours")
def off_hours(ctx: DetectorContext) -> Signal | None:
    """Activity in an hour this agent has never worked in."""
    ev = ctx.event
    if not ctx.baseline.is_active:
        return None
    try:
        hour = datetime.fromisoformat(ev.ts).hour
    except (ValueError, TypeError):
        return None
    if ctx.baseline.active_hour(hour):
        return None
    if ev.resource.cls not in SENSITIVE_CLASSES:
        return None
    return _sig(
        ctx, "off_hours", Weight.LOW,
        f"activity at {hour:02d}:00, an hour this agent has never operated in, "
        f"reaching a {ev.resource.cls.value} resource",
    )
