"""The behavioural graph — nodes, edges, and the read-only snapshot the hot path uses.

ADR-004, the load-bearing decision: `risk` and `confidence` are properties of
NODES and EDGES. There is no `agents.risk_score` anywhere in this system. This
is what makes "three different agents independently flagged this MCP server"
expressible at all, and retrofitting it later is a ~30-day rewrite.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from ..events import Action, Event, ResourceClass

# --- node identity ---------------------------------------------------------
# Stable, human-readable, collision-free. These strings are primary keys.


def agent_node(agent_id: str) -> str:
    return f"agent:{agent_id}"


def server_node(server: str) -> str:
    return f"server:{server}"


def tool_node(server: str, name: str) -> str:
    return f"tool:{server}/{name}"


def resource_node(uri: str) -> str:
    return f"res:{uri}"


def edge_key(src: str, dst: str, action: Action | str) -> str:
    a = action.value if isinstance(action, Action) else str(action)
    return f"{src}|{a}|{dst}"


class NodeKind:
    AGENT = "agent"
    TOOL = "tool"
    SERVER = "server"
    RESOURCE = "resource"
    IDENTITY = "identity"


# --- risk floors -----------------------------------------------------------
# The intrinsic risk of a class. Decay never takes a node below its floor:
# a secrets store is never "safe", it is only "not currently suspicious".

RISK_FLOORS: dict[ResourceClass, float] = {
    ResourceClass.SECRET: 0.30,
    ResourceClass.SHELL: 0.25,
    ResourceClass.NETWORK_EGRESS: 0.25,
    ResourceClass.DATABASE: 0.15,
    ResourceClass.CONFIG: 0.10,
    ResourceClass.COMMUNICATION: 0.10,
    ResourceClass.SOURCE_CODE: 0.00,
    ResourceClass.UNKNOWN: 0.05,
}

# Per-class decay rate, per hour. Credentials stay hot much longer than source.
DECAY_LAMBDA: dict[ResourceClass, float] = {
    ResourceClass.SECRET: 0.008,           # ~87h half-life
    ResourceClass.SHELL: 0.012,
    ResourceClass.NETWORK_EGRESS: 0.012,
    ResourceClass.DATABASE: 0.020,
    ResourceClass.CONFIG: 0.030,
    ResourceClass.COMMUNICATION: 0.030,
    ResourceClass.SOURCE_CODE: 0.050,      # ~14h half-life
    ResourceClass.UNKNOWN: 0.020,
}

DEFAULT_LAMBDA = 0.02


def floor_for(cls: ResourceClass) -> float:
    return RISK_FLOORS.get(cls, 0.05)


def lambda_for(cls: ResourceClass) -> float:
    return DECAY_LAMBDA.get(cls, DEFAULT_LAMBDA)


# --- model -----------------------------------------------------------------


@dataclass
class Node:
    id: str
    kind: str
    cls: ResourceClass = ResourceClass.UNKNOWN
    label: str = ""
    risk: float = 0.0
    risk_floor: float = 0.0
    confidence: float = 0.0
    observations: int = 0
    first_seen: str = ""
    last_seen: str = ""
    risk_updated: str = ""
    tenant_id: str = "default"

    @property
    def lam(self) -> float:
        return lambda_for(self.cls)


@dataclass
class Edge:
    id: str
    src: str
    dst: str
    action: Action
    count: int = 0
    risk: float = 0.0
    confidence: float = 0.0
    first_seen: str = ""
    last_seen: str = ""
    risk_updated: str = ""
    tenant_id: str = "default"


@dataclass
class Path:
    """An observed n-gram of behaviour. The behavioural-pattern store."""

    seq_hash: str
    n: int
    agent_id: str
    seq: list[str]
    seen_count: int = 0
    risk: float = 0.0
    first_seen: str = ""
    last_seen: str = ""
    tenant_id: str = "default"


# --- what the hot path sees ------------------------------------------------


@dataclass
class GraphSnapshot:
    """Immutable, in-memory view of node/edge risk.

    The data plane NEVER queries the database to make a decision (ARCHITECTURE
    section 1, rule 1). It reads this, which the control plane refreshes on a
    timer. Lookups are dict hits — microseconds, not milliseconds.
    """

    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[str, Edge] = field(default_factory=dict)
    built_at: str = ""

    def node_risk(self, node_id: str) -> float:
        n = self.nodes.get(node_id)
        return n.risk if n else 0.0

    def node_confidence(self, node_id: str) -> float:
        n = self.nodes.get(node_id)
        return n.confidence if n else 0.0

    def edge_risk(self, src: str, dst: str, action: Action) -> float:
        e = self.edges.get(edge_key(src, dst, action))
        return e.risk if e else 0.0

    def has_edge(self, src: str, dst: str, action: Action) -> bool:
        return edge_key(src, dst, action) in self.edges

    def known_node(self, node_id: str) -> bool:
        return node_id in self.nodes


def nodes_for_event(ev: Event) -> list[Node]:
    """The nodes an event touches. Used by ingest to upsert the graph."""
    out = [
        Node(id=agent_node(ev.agent_id), kind=NodeKind.AGENT, label=ev.agent_id),
        Node(id=server_node(ev.tool.server), kind=NodeKind.SERVER, label=ev.tool.server),
        Node(id=tool_node(ev.tool.server, ev.tool.name), kind=NodeKind.TOOL, label=ev.tool.qualified),
    ]
    if ev.resource.uri:
        out.append(
            Node(
                id=resource_node(ev.resource.uri),
                kind=NodeKind.RESOURCE,
                cls=ev.resource.cls,
                label=ev.resource.uri,
                risk_floor=floor_for(ev.resource.cls),
                risk=floor_for(ev.resource.cls),
            )
        )
    return out


def edges_for_event(ev: Event) -> list[tuple[str, str, Action]]:
    """(src, dst, action) triples an event implies."""
    a_node = agent_node(ev.agent_id)
    t_node = tool_node(ev.tool.server, ev.tool.name)
    triples: list[tuple[str, str, Action]] = [(a_node, t_node, ev.action)]
    if ev.resource.uri:
        triples.append((t_node, resource_node(ev.resource.uri), ev.action))
    return triples


def sequence_key(events: Iterable[Event]) -> list[str]:
    """The canonical n-gram element: tool + action + resource class.

    Deliberately NOT the resource URI. `read source_code via github/get_file`
    should look the same whichever file it was, or every sequence is novel and
    `novel_sequence` fires on everything.
    """
    return [f"{e.tool.qualified}:{e.action.value}:{e.resource.cls.value}" for e in events]
