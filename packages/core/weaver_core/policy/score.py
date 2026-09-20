"""Scoring, thresholds, and the explanation sentence.

ARCHITECTURE section 6. The Tier-1 term dominates deliberately: a deterministic
detector firing is hard evidence, while graph risk and trust are priors. A
product that blocks on accumulated suspicion rather than on an identifiable
event cannot explain itself, and an unexplainable block is worse than a miss.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..events import Decision, Verdict
from ..graph.model import GraphSnapshot, agent_node, resource_node, tool_node
from ..risk.model import Signal, Weight, effective_risk

W_TIER1 = 1.00
W_NODE = 0.15
W_EDGE = 0.10
W_TRUST = 0.20

ASK_THRESHOLD = 0.30
BLOCK_THRESHOLD = 0.65

# Detectors that block on their own. These describe an action no legitimate
# workflow performs, so no amount of accumulated trust should talk us out of it.
SHORT_CIRCUIT = frozenset({"retry_after_denial", "sensitive_then_egress"})


def combine_signals(signals: list[Signal]) -> float:
    """Saturating combination: 1 - prod(1 - w).

    Not a sum. Three MEDIUM detectors should raise concern without adding up to
    the same certainty as one CRITICAL — summation makes a pile of weak hints
    outrank a single hard fact, which is how anomaly products earn their
    reputation for noise.
    """
    acc = 1.0
    for s in signals:
        acc *= (1.0 - min(max(s.weight, 0.0), 1.0))
    return 1.0 - acc


@dataclass
class ScoreInput:
    signals: list[Signal]
    agent_id: str
    tool_server: str
    tool_name: str
    resource_uri: str
    trust: float = 1.0


def score(inp: ScoreInput, snapshot: GraphSnapshot) -> tuple[float, dict]:
    t1 = combine_signals(inp.signals)

    node_ids = [agent_node(inp.agent_id), tool_node(inp.tool_server, inp.tool_name)]
    if inp.resource_uri:
        node_ids.append(resource_node(inp.resource_uri))
    node_risk = max(
        (effective_risk(snapshot.node_risk(n), snapshot.node_confidence(n)) for n in node_ids),
        default=0.0,
    )

    trust_term = 1.0 - min(max(inp.trust, 0.0), 1.0)
    total = min(1.0, W_TIER1 * t1 + W_NODE * node_risk + W_TRUST * trust_term)
    return total, {"tier1": round(t1, 4), "node": round(node_risk, 4),
                   "trust": round(trust_term, 4)}


def decide(inp: ScoreInput, snapshot: GraphSnapshot) -> Decision:
    total, parts = score(inp, snapshot)

    fired = {s.detector for s in inp.signals}
    if fired & SHORT_CIRCUIT or total >= BLOCK_THRESHOLD:
        verdict = Verdict.BLOCK
    elif total >= ASK_THRESHOLD:
        verdict = Verdict.ASK
    else:
        verdict = Verdict.ALLOW

    evidence = [
        {"detector": s.detector, "weight": round(s.weight, 3), "detail": s.detail}
        for s in sorted(inp.signals, key=lambda s: s.weight, reverse=True)
    ]
    return Decision(
        verdict=verdict,
        score=total,
        detectors=evidence,
        sentence=explain(verdict, inp.signals, parts),
    )


def explain(verdict: Verdict, signals: list[Signal], parts: dict) -> str:
    """Render the evidence as a sentence a non-engineer can act on.

    ARCHITECTURE section 9, rule 3: a decision that cannot produce one of these
    must not block. The Gate enforces that; this function is why it can.
    """
    if verdict is Verdict.ALLOW and not signals:
        return ""
    if not signals:
        return f"{verdict.value.title()}: accumulated risk on this path (score {parts})."

    top = max(signals, key=lambda s: s.weight)
    lead = {
        Verdict.BLOCK: "Blocked",
        Verdict.ASK: "Held for approval",
        Verdict.ALLOW: "Flagged",
    }[verdict]

    sentence = f"{lead}: {top.detail}."
    if len(signals) > 1:
        others = ", ".join(s.detector for s in signals if s is not top)
        sentence += f" Also flagged by: {others}."
    return sentence


def severity(weight: float) -> str:
    if weight >= Weight.CRITICAL:
        return "CRITICAL"
    if weight >= Weight.HIGH:
        return "HIGH"
    if weight >= Weight.MEDIUM:
        return "MEDIUM"
    return "LOW"
