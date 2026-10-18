"""The engine — event in, decision out.

Wires baselines, session windows, detectors and scoring together. Pure: no I/O,
no clock beyond what the event carries. The proxy's Gate wraps this in a
timeout and a fail-open fallback, so nothing here needs to defend itself
against hanging.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .baseline.model import BaselineStore
from .detectors import tier1 as _tier1_detectors  # noqa: F401 — registers them
from .detectors import tier2 as _tier2_detectors  # noqa: F401 — registers them
from .detectors import tier3 as _tier3_detectors  # noqa: F401 — registers them
from .detectors.base import DetectorContext, SessionWindow, run_all
from .events import Decision, Event, Verdict
from .graph.model import Edge, GraphSnapshot, edge_key, edges_for_event, nodes_for_event
from .policy.rules import RuleSet, describe
from .policy.score import SHORT_CIRCUIT, ScoreInput, decide
from .risk.model import Signal
from .trust.model import TrustStore, gate


@dataclass
class Engine:
    baselines: BaselineStore = field(default_factory=BaselineStore)
    snapshot: GraphSnapshot = field(default_factory=GraphSnapshot)
    windows: dict[str, SessionWindow] = field(default_factory=dict)
    trust: TrustStore = field(default_factory=TrustStore)
    rules: RuleSet = field(default_factory=RuleSet)
    learning: bool = False          # True = fold events into baselines, decide nothing
    # Live learning (ADR-042): fold every clean, allowed call into this agent's
    # baseline as it happens. Before this, baselines were only ever built by
    # the test harness, so in real use every novelty detector stayed asleep.
    online: bool = False

    @classmethod
    def live(cls, rules: RuleSet | None = None) -> Engine:
        """The engine real traffic runs through: learns as it goes, and only
        starts calling things novel after LIVE_MIN_EVENTS clean calls."""
        from .baseline.model import LIVE_MIN_EVENTS

        return cls(baselines=BaselineStore(activate_after=LIVE_MIN_EVENTS),
                   rules=rules or RuleSet(), online=True)

    # -- evaluation ---------------------------------------------------------

    def window_for(self, session_id: str) -> SessionWindow:
        if session_id not in self.windows:
            self.windows[session_id] = SessionWindow()
        return self.windows[session_id]

    def evaluate(self, ev: Event) -> tuple[Decision, list[Signal]]:
        window = self.window_for(ev.session_id)
        history = list(window.events)
        baseline = self.baselines.get(ev.agent_id)
        window.push(ev)

        if self.learning:
            baseline.observe(ev, history)
            self._grow_graph(ev)
            return Decision(verdict=Verdict.ALLOW), []

        signals = run_all(DetectorContext(ev, window, baseline, self.snapshot))
        decision = decide(
            ScoreInput(
                signals=signals,
                agent_id=ev.agent_id,
                tool_server=ev.tool.server,
                tool_name=ev.tool.name,
                resource_uri=ev.resource.uri,
                trust=self.trust.get(ev.agent_id).trust,
            ),
            self.snapshot,
        )
        # --- adaptive trust ---------------------------------------------
        # Zones gate sensitive classes; they never shade thresholds (ADR-022).
        # Applied BEFORE operator rules, so an explicit `allow` rule can still
        # override a trust-driven ASK — the operator is more informed than a
        # score. A CRITICAL detector remains unoverridable either way.
        state = self.trust.get(ev.agent_id)
        zoned, why = gate(state, ev, decision.verdict)
        if why:
            decision.verdict = zoned
            decision.sentence = (
                f"{decision.sentence} {zoned.value.title()}: {why}."
                if decision.sentence
                else f"{zoned.value.title()}: {why}."
            )
            decision.detectors = [
                *decision.detectors,
                {"detector": "trust_zone", "weight": 0.0,
                 "detail": f"zone={state.zone} trust={state.trust:.2f} — {why}"},
            ]

        # Signals cost trust. Done after gating so a signal does not punish the
        # action that produced it twice.
        for sig in signals:
            state.on_signal(sig.weight, ts=ev.ts)

        # Operator rules layer over the behavioural verdict. block/ask raise
        # it; allow lowers it, but never past a CRITICAL detector.
        has_critical = any(s.detector in SHORT_CIRCUIT for s in signals)
        ruled, rule = self.rules.apply(ev, decision.verdict, has_critical)
        if rule is not None and ruled is not decision.verdict:
            decision.verdict = ruled
            note = describe(rule)
            # The sentence a person reads first: what happened and why, in
            # their own words from the rule. The pattern detail stays in the
            # evidence row for whoever wants it.
            said = (f"Blocked by your rule '{rule.name}'" if ruled is Verdict.BLOCK
                    else f"Held for approval by your rule '{rule.name}'"
                    if ruled is Verdict.ASK else f"Allowed by your rule '{rule.name}'")
            said = f"{said}: {rule.reason}" if rule.reason else said
            # Your rule leads: it is the reason the action stopped. Whatever
            # the behaviour score said comes after, as context.
            decision.sentence = (
                f"{said.rstrip('.')}."
                if not decision.sentence
                else f"{said.rstrip('.')}. Also: {decision.sentence}"
            )
            decision.detectors = [
                *decision.detectors,
                {"detector": "policy_rule", "weight": 0.0, "detail": note},
            ]

        # Learn only from what passed cleanly: a flagged call must not become
        # tomorrow's normal, which is how a slow attacker would teach the model.
        if self.online and decision.verdict is Verdict.ALLOW and not signals:
            baseline.observe(ev, history)

        self._grow_graph(ev)
        return decision, signals

    # -- graph --------------------------------------------------------------

    def _grow_graph(self, ev: Event) -> None:
        for n in nodes_for_event(ev):
            self.snapshot.nodes.setdefault(n.id, n)
            self.snapshot.nodes[n.id].observations += 1
        for src, dst, action in edges_for_event(ev):
            k = edge_key(src, dst, action)
            if k not in self.snapshot.edges:
                self.snapshot.edges[k] = Edge(id=k, src=src, dst=dst, action=action)
            self.snapshot.edges[k].count += 1

    # -- training -----------------------------------------------------------

    def train(self, events: list[Event]) -> None:
        """Fold a run of known-good traffic into the baselines."""
        was, self.learning = self.learning, True
        try:
            for ev in events:
                self.evaluate(ev)
        finally:
            self.learning = was

    def reset_sessions(self) -> None:
        """Clear session windows, keeping baselines and the graph."""
        self.windows.clear()
