"""The decision gate — budget, fail-open, and the mode switch.

Three non-negotiables from ARCHITECTURE section 1 live here, and this module
exists so that all three are enforced in exactly one place:

  3. Hard decision budget. On timeout or exception, apply the fail mode.
  4. `observe` is the default. Nothing is blocked until an operator opts in.
  * Every block renders a human-readable sentence. No sentence, no block.

In M1 there are no detectors, so every call returns ALLOW. That is expected.
What is being built here is the *machinery* — the timeout, the fallback, the
mode switch — because retrofitting a safety guarantee around a hot path that
was written without one does not work.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from hypothesise_core.events import Decision, Event, Verdict
from hypothesise_core.graph.model import GraphSnapshot

Evaluator = Callable[[Event, GraphSnapshot], Awaitable[Decision]]


@dataclass
class GateConfig:
    mode: str = "observe"          # observe | enforce
    fail_mode: str = "open"        # open | closed
    budget_ms: int = 50

    @classmethod
    def from_env(cls) -> GateConfig:
        return cls(
            mode=os.environ.get("HYPOTHESISE_MODE", "observe").lower(),
            fail_mode=os.environ.get("HYPOTHESISE_FAIL_MODE", "open").lower(),
            budget_ms=int(os.environ.get("HYPOTHESISE_DECISION_BUDGET_MS", "50")),
        )

    @property
    def enforcing(self) -> bool:
        return self.mode == "enforce"


@dataclass
class GateStats:
    calls: int = 0
    timeouts: int = 0
    errors: int = 0
    blocked: int = 0
    asked: int = 0
    observed_would_block: int = 0   # what enforce mode WOULD have stopped
    total_ms: float = 0.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0


async def _allow_all(_ev: Event, _snap: GraphSnapshot) -> Decision:
    """M1 evaluator. Replaced by the real engine in M2."""
    return Decision(verdict=Verdict.ALLOW, score=0.0)


@dataclass
class Gate:
    config: GateConfig = field(default_factory=GateConfig.from_env)
    evaluator: Evaluator = _allow_all
    stats: GateStats = field(default_factory=GateStats)

    async def decide(self, event: Event, snapshot: GraphSnapshot) -> Decision:
        """Evaluate within budget. Never raises, never exceeds the budget."""
        self.stats.calls += 1
        started = time.perf_counter()
        try:
            decision = await asyncio.wait_for(
                self.evaluator(event, snapshot),
                timeout=self.config.budget_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            self.stats.timeouts += 1
            decision = self._fallback("engine exceeded its decision budget")
        except Exception as exc:  # noqa: BLE001 — a crash here must not reach the agent
            self.stats.errors += 1
            decision = self._fallback(f"engine error: {type(exc).__name__}")

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.stats.total_ms += elapsed_ms
        decision.budget_ms = int(elapsed_ms)

        # --- observe mode: decide fully, enforce nothing ---------------------
        if decision.verdict is not Verdict.ALLOW and not self.config.enforcing:
            self.stats.observed_would_block += 1
            shadow = Decision(
                verdict=Verdict.ALLOW,
                score=decision.score,
                detectors=decision.detectors,
                sentence=f"[observe] would have {decision.verdict.value}ed: {decision.sentence}",
                budget_ms=decision.budget_ms,
            )
            return shadow

        # --- a block with no explanation is a bug, not a block ---------------
        if decision.verdict is Verdict.BLOCK and not decision.sentence.strip():
            decision = Decision(
                verdict=Verdict.ALLOW,
                score=decision.score,
                detectors=decision.detectors,
                sentence="[bug] block suppressed: engine produced no explanation",
                budget_ms=decision.budget_ms,
            )

        if decision.verdict is Verdict.BLOCK:
            self.stats.blocked += 1
        elif decision.verdict is Verdict.ASK:
            self.stats.asked += 1
        return decision

    def _fallback(self, why: str) -> Decision:
        """What we do when our own engine lets us down."""
        if self.config.fail_mode == "closed":
            return Decision(
                verdict=Verdict.BLOCK,
                sentence=f"Blocked: fail-closed is configured and {why}.",
                fail_open=False,
            )
        return Decision(
            verdict=Verdict.ALLOW,
            sentence=f"Allowed without evaluation: {why}.",
            fail_open=True,
        )
