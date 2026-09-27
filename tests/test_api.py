"""Control plane tests — ingest, queries, persistence, the dashboard."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from weaver_api.app import create_app
from weaver_api.demo import load_demo
from weaver_core.events import Event, Resource, ResourceClass, Tool


@pytest.fixture
def client():
    app = create_app(db_url="sqlite:///:memory:")
    return TestClient(app), app


def _event(step=0, uri="file:///app/main.py", cls=ResourceClass.SOURCE_CODE,
           session="sess_test", agent="test-agent"):
    return Event(agent_id=agent, session_id=session, step=step,
                 tool=Tool("fs-mcp", "read_file"),
                 resource=Resource(uri=uri, cls=cls)).to_dict()


def test_health(client):
    c, _ = client
    assert c.get("/healthz").json()["ok"] is True


def test_dashboard_serves_html(client):
    """ADR-009: the dashboard ships inside the Python package, no build step."""
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "WEAVER" in r.text
    assert "cytoscape" in r.text.lower()


def test_ingest_and_read_back(client):
    c, _ = client
    r = c.post("/v1/events", json=[_event(i) for i in range(5)])
    assert r.json()["accepted"] == 5

    sessions = c.get("/v1/sessions").json()
    assert len(sessions) == 1
    assert sessions[0]["events"] == 5

    timeline = c.get(f"/v1/sessions/{sessions[0]['session_id']}").json()
    assert [e["step"] for e in timeline] == [0, 1, 2, 3, 4]


def test_ingest_skips_malformed_without_failing_the_batch(client):
    """One bad event must not reject a batch of good ones."""
    c, _ = client
    r = c.post("/v1/events", json=[_event(0), {"garbage": True}, _event(1)])
    assert r.status_code == 200
    assert r.json()["accepted"] == 2


def test_unknown_session_is_404(client):
    c, _ = client
    assert c.get("/v1/sessions/nope").status_code == 404


def test_graph_is_built_from_events(client):
    """ADR-004: nodes and edges carry risk; there is no agent risk column."""
    c, _ = client
    c.post("/v1/events", json=[_event(0)])
    g = c.get("/v1/graph").json()
    kinds = {n["kind"] for n in g["nodes"]}
    assert {"agent", "tool", "server", "resource"} <= kinds
    assert all("risk" in n and "confidence" in n for n in g["nodes"])
    assert g["edges"]


def test_secret_resource_gets_its_risk_floor(client):
    """A secrets store is never 'safe' — only 'not currently suspicious'."""
    c, _ = client
    c.post("/v1/events", json=[_event(0, "file:///app/.env", ResourceClass.SECRET)])
    g = c.get("/v1/graph").json()
    secret = [n for n in g["nodes"] if n["class"] == "secret"]
    assert secret and secret[0]["risk"] >= 0.30


def test_detectors_endpoint_reports_no_errors(client):
    c, _ = client
    d = c.get("/v1/detectors").json()
    assert len(d["registered"]) >= 6
    assert d["errors"] == {}


def test_demo_load_populates_everything(client):
    """End to end: scenario suite -> engine -> store -> API."""
    c, app = client
    n = load_demo(app.state.repo, app.state.engine)
    assert n > 40

    stats = c.get("/v1/stats").json()
    assert stats["events"] == n
    assert stats["agents"] >= 5
    assert stats["verdicts"].get("block", 0) >= 1, "no blocks — detectors not wired"

    alerts = c.get("/v1/alerts").json()
    assert alerts
    assert all(a["sentence"] for a in alerts), "an alert with no explanation is a bug"

    riskiest = stats["riskiest"]
    assert riskiest and riskiest[0]["risk"] > 0


def test_alerts_carry_evidence_and_a_sentence(client):
    c, app = client
    load_demo(app.state.repo, app.state.engine)
    blocks = [a for a in c.get("/v1/alerts").json() if a["verdict"] == "block"]
    assert blocks
    top = blocks[0]
    assert top["detectors"]
    assert len(top["sentence"]) > 20


def test_trust_endpoints_roundtrip(client):
    """Trust must survive a proxy restart (ADR-023), so it goes through the
    control plane rather than living only in the proxy."""
    c, _ = client
    r = c.post("/v1/trust", json=[
        {"agent_id": "a1", "trust": 0.2, "zone": "hot",
         "hot_signals": 4, "quarantined": True}])
    assert r.json()["saved"] == 1

    rows = c.get("/v1/trust").json()
    assert rows[0]["agent_id"] == "a1"
    assert rows[0]["quarantined"] is True

    agents = c.get("/v1/agents").json()
    assert agents[0]["zone"] == "hot"


def test_release_clears_a_quarantine(client):
    """Only a human clears a quarantine — time alone never does."""
    c, _ = client
    c.post("/v1/trust", json=[{"agent_id": "a1", "trust": 0.0, "zone": "hot",
                               "hot_signals": 5, "quarantined": True}])
    row = c.post("/v1/agents/a1/release").json()
    assert row["quarantined"] is False
    assert row["hot_signals"] == 0
    assert row["trust"] >= 0.70


def test_release_unknown_agent_is_404(client):
    c, _ = client
    assert c.post("/v1/agents/nope/release").status_code == 404


def test_dashboard_has_the_agents_view(client):
    c, _ = client
    body = c.get("/").text
    for token in ("agents", "QUARANTINED", "Release", "tmeter"):
        assert token in body


def test_readyz_reports_schema_state(client):
    c, _ = client
    body = c.get("/readyz").json()
    assert body["ok"] is True
    assert body["schema"]["up_to_date"] is True


def test_metrics_is_prometheus_text(client):
    c, app = client
    from weaver_api.demo import load_demo

    load_demo(app.state.repo, app.state.engine)
    r = c.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")

    body = r.text
    for metric in ("weaver_events_total", "weaver_decisions_total",
                   "weaver_agents", "weaver_approvals_pending",
                   "weaver_detector_errors_total"):
        assert metric in body
    # every metric carries HELP and TYPE, or scrapers complain
    assert body.count("# HELP") >= 6
    assert body.count("# TYPE") >= 6


def test_healthz_touches_nothing():
    """A liveness probe that queries the database turns a slow query into an
    outage. This one must answer with a broken database."""
    app = create_app(db_url="sqlite:///:memory:")
    c = TestClient(app)
    app.state.repo.engine.dispose()
    assert c.get("/healthz").json()["ok"] is True


def test_stats_reports_decision_latency(client):
    """Latency is what the agent feels. Past the gate's budget it fails open and
    detection stops silently, so this has to be visible."""
    c, app = client
    from weaver_api.demo import load_demo

    load_demo(app.state.repo, app.state.engine)
    lat = c.get("/v1/stats").json()["latency_ms"]
    for key in ("mean", "max", "measured", "near_budget", "failed_open"):
        assert key in lat
    assert lat["failed_open"] == 0, "demo traffic should never fail open"


def test_metrics_exposes_latency_and_fail_open(client):
    c, app = client
    from weaver_api.demo import load_demo

    load_demo(app.state.repo, app.state.engine)
    body = c.get("/metrics").text
    for metric in ("weaver_decision_latency_ms",
                   "weaver_decisions_near_budget_total",
                   "weaver_decisions_failed_open_total"):
        assert metric in body


# --- dashboard liveness ----------------------------------------------------
# The dashboard shipped with only the approvals badge on a timer, so a session
# timeline left open while an agent worked never updated. Events were recorded
# correctly and the screen showed nothing — indistinguishable, to the person
# watching, from Weaver not working at all.

STATIC = (
    Path(__file__).resolve().parents[1]
    / "packages" / "api" / "weaver_api" / "static" / "index.html"
)


def test_dashboard_refreshes_the_current_view_on_a_timer():
    html = STATIC.read_text()
    assert "liveRefresh" in html
    assert "setInterval(liveRefresh" in html


def test_live_refresh_does_not_go_through_render():
    """render() nulls `session`, which would eject a reader from a timeline
    every five seconds. The refresh must re-run the view loader in place."""
    html = STATIC.read_text()
    body = html.split("async function liveRefresh(){", 1)[1].split("}", 1)[0]
    assert "render()" not in body
    assert "views[view]()" in body


def test_live_refresh_skips_the_views_it_would_break():
    html = STATIC.read_text()
    body = html.split("async function liveRefresh(){", 1)[1].split("setInterval", 1)[0]
    assert "'approvals'" in body
    assert "'graph'" in body


def test_live_refresh_stops_when_the_tab_is_hidden():
    """Otherwise every forgotten tab polls a local server forever."""
    html = STATIC.read_text()
    assert "visibilitychange" in html
