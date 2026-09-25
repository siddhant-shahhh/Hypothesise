"""Control-plane sync tests.

Without the graph pull, node risk in the proxy stays at its class floor
forever and `resource_reputation` can never fire — stigmergy needs this loop
to exist at all.
"""

import pytest
from weaver_core.events import Action, ResourceClass
from weaver_core.graph.model import GraphSnapshot, edge_key, resource_node
from weaver_core.risk.model import Weight
from weaver_core.trust.model import TrustStore
from weaver_proxy.sync import ControlPlaneSync


def _sync(**kw):
    return ControlPlaneSync(api_url="http://x", snapshot=GraphSnapshot(),
                            trust=TrustStore(), **kw)


def test_apply_graph_populates_risk():
    s = _sync()
    uri = "file:///app/.env"
    n = s.apply_graph({
        "nodes": [{"id": resource_node(uri), "kind": "resource", "class": "secret",
                   "label": uri, "risk": 0.82, "confidence": 0.9,
                   "observations": 40}],
        "edges": [{"id": edge_key("agent:a", "tool:fs-mcp/read_file", "read"),
                   "src": "agent:a", "dst": "tool:fs-mcp/read_file",
                   "action": "read", "count": 12, "risk": 0.1}],
    })
    assert n == 1
    assert s.snapshot.node_risk(resource_node(uri)) == pytest.approx(0.82)
    assert s.snapshot.node_confidence(resource_node(uri)) == pytest.approx(0.9)
    assert s.snapshot.has_edge("agent:a", "tool:fs-mcp/read_file", Action.READ)


def test_apply_graph_replaces_rather_than_merges():
    """The control plane is authoritative and has already applied decay on
    read. Merging would let a stale local value outlive the real one."""
    s = _sync()
    s.apply_graph({"nodes": [{"id": "res:old", "kind": "resource", "risk": 0.9}]})
    s.apply_graph({"nodes": [{"id": "res:new", "kind": "resource", "risk": 0.1}]})
    assert "res:old" not in s.snapshot.nodes
    assert "res:new" in s.snapshot.nodes


def test_apply_graph_survives_junk():
    """A malformed payload from the control plane must not break the proxy."""
    s = _sync()
    n = s.apply_graph({"nodes": [{"id": "res:x", "kind": "resource",
                                  "class": "not-a-real-class"}],
                       "edges": [{"id": "e", "src": "a", "dst": "b",
                                  "action": "nonsense"}]})
    assert n == 1
    assert s.snapshot.nodes["res:x"].cls is ResourceClass.UNKNOWN
    assert s.snapshot.edges["e"].action is Action.UNKNOWN


def test_apply_graph_handles_empty():
    s = _sync()
    assert s.apply_graph({}) == 0


@pytest.mark.asyncio
async def test_unreachable_control_plane_is_counted_not_raised():
    s = ControlPlaneSync(api_url="http://127.0.0.1:1", snapshot=GraphSnapshot(),
                         trust=TrustStore(), timeout=0.2)
    assert await s.pull_graph_once() == 0
    assert await s.load_trust_once() == 0
    assert s.stats.errors >= 2
    assert s.stats.last_error


@pytest.mark.asyncio
async def test_trust_is_pushed_and_reloaded():
    store = TrustStore()
    store.get("a1").on_signal(Weight.CRITICAL)
    s = ControlPlaneSync(api_url="http://x", snapshot=GraphSnapshot(), trust=store)

    sent = {}

    async def fake_call(path, payload=None):
        sent[path] = payload
        return {"saved": len(payload or [])}

    s._call = fake_call
    assert await s.push_trust_once() == 1
    assert sent["/v1/trust"][0]["agent_id"] == "a1"
    assert sent["/v1/trust"][0]["trust"] < 1.0


@pytest.mark.asyncio
async def test_push_is_a_noop_with_nothing_to_send():
    s = _sync()
    assert await s.push_trust_once() == 0
