"""Per-agent behavioural baselines — what normal looks like for THIS agent.

Baselines are per-agent and not per-fleet, because "normal" is not a property
of the action. A research agent making a hundred external HTTP calls an hour is
doing its job; a coding agent making one is worth a look. A fleet-wide baseline
would have to call both normal, and then it catches nothing.

Lifecycle: `learning` -> `active`. Nothing novelty-based fires while an agent
is still learning, or every agent's first hour is an incident.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from ..events import Action, Event, ResourceClass
from ..graph.model import agent_node, edge_key, resource_node, tool_node

MIN_EVENTS_TO_ACTIVATE = 12
# Live learning (ADR-042) activates much later than the training harness: a
# baseline that turns on after 12 real calls calls the 13th unfamiliar thing an
# anomaly. Two hundred clean calls is roughly a working session or two.
LIVE_MIN_EVENTS = 200
NGRAM_SIZES = (3, 4, 5)


def ngram_hash(items: list[str]) -> str:
    return hashlib.sha256("|".join(items).encode("utf-8")).hexdigest()[:32]


def step_key(ev: Event) -> str:
    """The canonical n-gram element: tool + action + resource CLASS.

    Deliberately excludes the resource URI. `read source_code via
    github/get_file` must look the same whichever file it was, or every
    sequence is novel and novel_sequence fires on literally everything.
    """
    return f"{ev.tool.qualified}:{ev.action.value}:{ev.resource.cls.value}"


@dataclass
class Baseline:
    agent_id: str
    state: str = "learning"                                   # learning | active
    event_count: int = 0
    tools: Counter = field(default_factory=Counter)
    classes: Counter = field(default_factory=Counter)
    edges: set[str] = field(default_factory=set)
    ngrams: set[str] = field(default_factory=set)
    invoke_targets: set[str] = field(default_factory=set)
    hours: Counter = field(default_factory=Counter)
    class_pairs: set[tuple[str, str]] = field(default_factory=set)
    # Tier-2 inputs. Welford accumulators rather than stored samples: constant
    # memory per tool, and a baseline that never grows without bound.
    arg_keysets: dict[str, set[str]] = field(default_factory=dict)   # tool -> seen key-sets (joined)
    arg_len: dict[str, tuple[int, float, float]] = field(default_factory=dict)  # tool -> (n, mean, M2)
    gap_stats: tuple[int, float, float] = (0, 0.0, 0.0)              # (n, mean, M2) seconds
    activate_after: int = MIN_EVENTS_TO_ACTIVATE

    @property
    def is_active(self) -> bool:
        return self.state == "active"

    # -- learning -----------------------------------------------------------

    def observe(self, ev: Event, history: list[Event] | None = None) -> None:
        """Fold one event into the baseline."""
        self.event_count += 1
        self.tools[ev.tool.qualified] += 1
        self.classes[ev.resource.cls.value] += 1

        a, t = agent_node(ev.agent_id), tool_node(ev.tool.server, ev.tool.name)
        self.edges.add(edge_key(a, t, ev.action))
        if ev.resource.uri:
            self.edges.add(edge_key(t, resource_node(ev.resource.uri), ev.action))

        if ev.action is Action.INVOKE and ev.resource.uri:
            self.invoke_targets.add(ev.resource.uri)

        with contextlib.suppress(ValueError, TypeError):
            self.hours[datetime.fromisoformat(ev.ts).hour] += 1

        if history:
            prev = history[-1]
            self.class_pairs.add((prev.resource.cls.value, ev.resource.cls.value))
            seq = [*history, ev]
            for n in NGRAM_SIZES:
                if len(seq) >= n:
                    self.ngrams.add(ngram_hash([step_key(e) for e in seq[-n:]]))

        self._observe_args(ev)
        if history:
            self._observe_gap(history[-1], ev)

        if self.event_count >= self.activate_after:
            self.state = "active"

    # -- persistence ----------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id, "state": self.state, "event_count": self.event_count,
            "tools": dict(self.tools), "classes": dict(self.classes),
            "edges": sorted(self.edges), "ngrams": sorted(self.ngrams),
            "invoke_targets": sorted(self.invoke_targets),
            "hours": {str(k): v for k, v in self.hours.items()},
            "class_pairs": sorted([list(p) for p in self.class_pairs]),
            "arg_keysets": {k: sorted(v) for k, v in self.arg_keysets.items()},
            "arg_len": {k: list(v) for k, v in self.arg_len.items()},
            "gap_stats": list(self.gap_stats),
        }

    @classmethod
    def from_dict(cls, d: dict, activate_after: int = MIN_EVENTS_TO_ACTIVATE) -> Baseline:
        b = cls(agent_id=str(d.get("agent_id", "")), activate_after=activate_after)
        b.event_count = int(d.get("event_count", 0))
        b.tools = Counter(d.get("tools") or {})
        b.classes = Counter(d.get("classes") or {})
        b.edges = set(d.get("edges") or [])
        b.ngrams = set(d.get("ngrams") or [])
        b.invoke_targets = set(d.get("invoke_targets") or [])
        b.hours = Counter({int(k): int(v) for k, v in (d.get("hours") or {}).items()})
        b.class_pairs = {tuple(p) for p in d.get("class_pairs") or [] if len(p) == 2}
        b.arg_keysets = {k: set(v) for k, v in (d.get("arg_keysets") or {}).items()}
        b.arg_len = {k: (int(v[0]), float(v[1]), float(v[2]))
                     for k, v in (d.get("arg_len") or {}).items() if len(v) == 3}
        g = d.get("gap_stats") or [0, 0.0, 0.0]
        b.gap_stats = (int(g[0]), float(g[1]), float(g[2]))
        b.state = "active" if b.event_count >= activate_after else "learning"
        return b

    # -- tier 2 accumulators ------------------------------------------------

    def _observe_args(self, ev: Event) -> None:
        tool = ev.tool.qualified
        if ev.arg_keys:
            self.arg_keysets.setdefault(tool, set()).add(",".join(sorted(ev.arg_keys)))
        if ev.arg_bytes > 0:
            self.arg_len[tool] = _welford(self.arg_len.get(tool, (0, 0.0, 0.0)), ev.arg_bytes)

    def _observe_gap(self, prev: Event, ev: Event) -> None:
        gap = _seconds_between(prev.ts, ev.ts)
        if gap is not None:
            self.gap_stats = _welford(self.gap_stats, gap)

    def arg_len_z(self, tool: str, nbytes: int) -> float | None:
        """z-score of an argument payload size against this tool's history."""
        n, mean, m2 = self.arg_len.get(tool, (0, 0.0, 0.0))
        if n < 5:
            return None
        var = m2 / (n - 1)
        if var <= 0:
            return None
        return (nbytes - mean) / (var ** 0.5)

    def gap_z(self, gap: float) -> float | None:
        n, mean, m2 = self.gap_stats
        if n < 8:
            return None
        var = m2 / (n - 1)
        if var <= 0:
            return None
        return (gap - mean) / (var ** 0.5)

    def knows_keyset(self, tool: str, keys: list[str]) -> bool:
        seen = self.arg_keysets.get(tool)
        if not seen:
            return True          # nothing learned yet -> do not call it novel
        return ",".join(sorted(keys)) in seen

    def active_hour(self, hour: int) -> bool:
        if sum(self.hours.values()) < 20:
            return True          # too little history to call any hour unusual
        return self.hours.get(hour, 0) > 0

    # -- queries the detectors use ------------------------------------------

    def knows_edge(self, src: str, dst: str, action: Action) -> bool:
        return edge_key(src, dst, action) in self.edges

    def knows_ngram(self, seq: list[Event]) -> bool:
        return ngram_hash([step_key(e) for e in seq]) in self.ngrams

    def knows_tool(self, qualified: str) -> bool:
        return qualified in self.tools

    def knows_class(self, cls: ResourceClass) -> bool:
        return self.classes.get(cls.value, 0) > 0

    def knows_class_pair(self, a: ResourceClass, b: ResourceClass) -> bool:
        return (a.value, b.value) in self.class_pairs


def _welford(acc: tuple[int, float, float], x: float) -> tuple[int, float, float]:
    """Online mean/variance. Constant memory, no sample retention."""
    n, mean, m2 = acc
    n += 1
    delta = x - mean
    mean += delta / n
    m2 += delta * (x - mean)
    return (n, mean, m2)


def _seconds_between(a: str, b: str) -> float | None:
    try:
        return abs((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds())
    except (ValueError, TypeError):
        return None


class BaselineStore:
    """Baselines keyed by agent. `activate_after` is MIN_EVENTS_TO_ACTIVATE for
    the training harness and LIVE_MIN_EVENTS for live learning (ADR-042)."""

    def __init__(self, activate_after: int = MIN_EVENTS_TO_ACTIVATE) -> None:
        self._by_agent: dict[str, Baseline] = {}
        self.activate_after = activate_after

    def get(self, agent_id: str) -> Baseline:
        if agent_id not in self._by_agent:
            self._by_agent[agent_id] = Baseline(agent_id=agent_id,
                                                activate_after=self.activate_after)
        return self._by_agent[agent_id]

    def observe(self, ev: Event, history: list[Event] | None = None) -> None:
        self.get(ev.agent_id).observe(ev, history)

    def activate_all(self) -> None:
        """Force every baseline active. Training helper — never used in production."""
        for b in self._by_agent.values():
            if b.event_count > 0:
                b.state = "active"

    def to_list(self) -> list[dict]:
        return [b.to_dict() for b in self._by_agent.values() if b.event_count]

    def load_list(self, rows: list[dict]) -> int:
        n = 0
        for r in rows or []:
            if isinstance(r, dict) and r.get("agent_id"):
                self._by_agent[r["agent_id"]] = Baseline.from_dict(r, self.activate_after)
                n += 1
        return n

    def __len__(self) -> int:
        return len(self._by_agent)
