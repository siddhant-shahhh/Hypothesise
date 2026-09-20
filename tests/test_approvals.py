"""Approval flow — API side and the proxy's wait loop.

The proxy genuinely suspends an agent's tool call here, so every path is
bounded and every failure resolves to a decision rather than a hang.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from weaver_api.app import create_app
from weaver_core.events import Event, Resource, ResourceClass, Tool


@pytest.fixture
def client():
    app = create_app(db_url="sqlite:///:memory:")
    return TestClient(app), app


def _ingest(c, uri="file:///app/.env"):
    ev = Event(agent_id="a", session_id="s", step=0,
               tool=Tool("shell-mcp", "exec"),
               resource=Resource(uri, ResourceClass.SECRET))
    c.post("/v1/events", json=[ev.to_dict()])
    return ev.event_id


def test_create_and_approve(client):
    c, _ = client
    eid = _ingest(c)
    a = c.post("/v1/approvals", json={"event_id": eid, "ttl_seconds": 60,
                                      "reason": "reading credentials"}).json()
    assert a["state"] == "pending"

    assert len(c.get("/v1/approvals").json()) == 1

    d = c.post(f"/v1/approvals/{a['id']}/decide",
               json={"approve": True, "who": "pratham"}).json()
    assert d["state"] == "approved"
    assert d["decided_by"] == "pratham"
    assert c.get("/v1/approvals").json() == []


def test_deny(client):
    c, _ = client
    a = c.post("/v1/approvals", json={"event_id": _ingest(c)}).json()
    assert c.post(f"/v1/approvals/{a['id']}/decide",
                  json={"approve": False}).json()["state"] == "denied"


def test_a_decision_is_final(client):
    """A decided approval must never flip. Otherwise a late click could
    retroactively authorise something already refused."""
    c, _ = client
    a = c.post("/v1/approvals", json={"event_id": _ingest(c)}).json()
    c.post(f"/v1/approvals/{a['id']}/decide", json={"approve": False})
    again = c.post(f"/v1/approvals/{a['id']}/decide", json={"approve": True}).json()
    assert again["state"] == "denied"


def test_expiry_is_evaluated_on_read(client):
    """No sweep job. A pending row past its deadline is expired when read,
    not whenever a cleanup task happens to run."""
    c, _ = client
    a = c.post("/v1/approvals", json={"event_id": _ingest(c),
                                      "ttl_seconds": -1}).json()
    assert c.get(f"/v1/approvals/{a['id']}").json()["state"] == "expired"
    assert c.get("/v1/approvals").json() == []


def test_missing_event_id_is_rejected(client):
    c, _ = client
    assert c.post("/v1/approvals", json={}).status_code == 400


def test_unknown_approval_is_404(client):
    c, _ = client
    assert c.get("/v1/approvals/nope").status_code == 404


def test_shadow_report(client):
    """What enforce WOULD have done — the report you read before flipping."""
    c, app = client
    from weaver_api.demo import load_demo

    load_demo(app.state.repo, app.state.engine)
    s = c.get("/v1/shadow").json()
    assert s["total_events"] > 0
    assert "would_block" in s and "rate" in s


# -- proxy side -------------------------------------------------------------


class _FakeAPI:
    """Stands in for the control plane, so the wait loop can be driven."""

    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.calls = []

    async def _call(self, path, payload=None):
        self.calls.append(path)
        if path == "/v1/approvals":
            return {"id": "apr_1", "state": "pending"}
        return self.sequence.pop(0) if self.sequence else {"state": "pending"}


def _event():
    return Event(agent_id="a", session_id="s", step=0,
                 tool=Tool("shell-mcp", "exec"),
                 resource=Resource("file:///app/.env", ResourceClass.SECRET))


@pytest.mark.asyncio
async def test_proxy_waits_then_proceeds_on_approval():
    from weaver_proxy.approval import ApprovalClient

    ac = ApprovalClient("http://x", ttl=5)
    fake = _FakeAPI([{"state": "pending"}, {"state": "approved",
                                            "decided_by": "pratham"}])
    ac._call = fake._call

    out = await ac.request(_event(), "reading credentials")
    assert out.approved is True
    assert out.state == "approved"
    assert "pratham" in out.detail


@pytest.mark.asyncio
async def test_denial_blocks():
    from weaver_proxy.approval import ApprovalClient

    ac = ApprovalClient("http://x", ttl=5)
    ac._call = _FakeAPI([{"state": "denied", "decided_by": "pratham"}])._call
    out = await ac.request(_event(), "why")
    assert out.approved is False and out.state == "denied"


@pytest.mark.asyncio
async def test_timeout_denies_not_allows():
    """ADR-017. An agent running unattended overnight must not be able to
    obtain an ASK-worthy action by waiting for nobody to answer."""
    from weaver_proxy.approval import ApprovalClient

    ac = ApprovalClient("http://x", ttl=1.2)
    ac._call = _FakeAPI([])._call            # always pending
    out = await asyncio.wait_for(ac.request(_event(), "why"), timeout=6)
    assert out.approved is False
    assert out.state == "expired"
    assert "no one responded" in out.detail


@pytest.mark.asyncio
async def test_unreachable_control_plane_denies():
    """We cannot ask a human, so we cannot claim a human said yes."""
    from weaver_proxy.approval import ApprovalClient

    ac = ApprovalClient("http://127.0.0.1:1", ttl=1, timeout=0.3)
    out = await asyncio.wait_for(ac.request(_event(), "why"), timeout=8)
    assert out.approved is False
    assert out.state == "unavailable"


def test_enforce_mode_requires_a_control_plane(tmp_path, monkeypatch):
    """Nobody to ask means ASK has no honest meaning. Refuse at startup rather
    than discover it in production."""
    import json

    from weaver_proxy.__main__ import main

    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {}, "weaver": {"mode": "enforce"}}))
    monkeypatch.setattr("sys.argv", ["weaver-proxy", "--config", str(cfg)])
    assert main() == 2


def test_env_overrides_the_config_file(tmp_path, monkeypatch):
    """Documented precedence, and what people expect: you should be able to
    flip one run to enforce without editing a config that is in version
    control."""
    import json

    from weaver_proxy.config import ProxyConfig

    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {}, "weaver": {"mode": "observe"}}))
    assert ProxyConfig.load(cfg).gate.mode == "observe"

    monkeypatch.setenv("WEAVER_MODE", "enforce")
    assert ProxyConfig.load(cfg).gate.mode == "enforce"


def test_rules_load_from_config(tmp_path):
    import json

    from weaver_core.policy.rules import RuleSet
    from weaver_proxy.config import ProxyConfig

    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({
        "mcpServers": {},
        "rules": [{"name": "no prod writes", "action": "block",
                   "match": {"resource_uri": "postgres://prod-*"}}],
    }))
    loaded = ProxyConfig.load(cfg)
    assert len(RuleSet.from_config(loaded.rules).rules) == 1


def test_pending_approval_shows_context_before_the_event_arrives(client):
    """The proxy opens an approval instantly; the event ships on the next
    batch. During that window the approver must still see what they are being
    asked about, or they will rubber-stamp or blanket-deny."""
    c, _ = client
    c.post("/v1/approvals", json={
        "event_id": "evt_not_yet_shipped",
        "agent_id": "coding-agent-prod",
        "tool": "shell-mcp/exec",
        "resource": "file:///app/.env",
        "resource_class": "secret",
        "reason": "reading credentials",
    })
    row = c.get("/v1/approvals").json()[0]
    assert row["agent_id"] == "coding-agent-prod"
    assert row["tool"] == "shell-mcp/exec"
    assert row["resource"] == "file:///app/.env"
    assert row["class"] == "secret"
    assert row["sentence"] == "reading credentials"
