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

        if self.event_count >= MIN_EVENTS_TO_ACTIVATE:
            self.state = "active"

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


class BaselineStore:
    """In-memory baselines keyed by agent. Persisted in M3."""

    def __init__(self) -> None:
        self._by_agent: dict[str, Baseline] = {}

    def get(self, agent_id: str) -> Baseline:
        if agent_id not in self._by_agent:
            self._by_agent[agent_id] = Baseline(agent_id=agent_id)
        return self._by_agent[agent_id]

    def observe(self, ev: Event, history: list[Event] | None = None) -> None:
        self.get(ev.agent_id).observe(ev, history)

    def activate_all(self) -> None:
        """Force every baseline active. Training helper — never used in production."""
        for b in self._by_agent.values():
            if b.event_count > 0:
                b.state = "active"

    def __len__(self) -> int:
        return len(self._by_agent)
