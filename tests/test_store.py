"""Schema tests — ADR-002 constraints are load-bearing, so they get pinned."""

from hypothesise_store.db import get_engine, init_db
from hypothesise_store.schema import ALL_TABLES, agents, nodes
from sqlalchemy import insert, inspect, select


def test_schema_creates():
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    names = set(inspect(engine).get_table_names())
    for t in ALL_TABLES:
        assert t.name in names


def test_every_table_has_tenant_id():
    """ADR-002: tenant_id everywhere from the first migration, or SaaS is a rewrite."""
    for t in ALL_TABLES:
        assert "tenant_id" in t.c, f"{t.name} is missing tenant_id"


def test_agents_table_has_no_risk_column():
    """ADR-004, the load-bearing decision. Risk lives on the graph, not on agents.

    If someone adds `agents.risk_score` this test fails, and it should — that
    change quietly kills resource reputation and costs ~30 days to undo.
    """
    cols = set(agents.c.keys())
    assert "trust" in cols
    assert "risk" not in cols
    assert "risk_score" not in cols


def test_nodes_and_edges_carry_risk():
    from hypothesise_store.schema import edges

    for t in (nodes, edges):
        assert "risk" in t.c
        assert "confidence" in t.c
        assert "risk_updated" in t.c, f"{t.name} needs risk_updated for lazy decay"


def test_roundtrip_insert():
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(nodes).values(
                id="res:file:///app/.env",
                kind="resource",
                risk=0.3,
                risk_floor=0.3,
                first_seen="2026-09-20T00:00:00+00:00",
                last_seen="2026-09-20T00:00:00+00:00",
                risk_updated="2026-09-20T00:00:00+00:00",
            )
        )
        row = conn.execute(select(nodes).where(nodes.c.id == "res:file:///app/.env")).one()
        assert row.risk == 0.3
        assert row.tenant_id == "default"
