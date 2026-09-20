"""Persistence schema — SQLAlchemy Core.

ADR-002 binding constraints, all enforced here:
  * SQLAlchemy Core only, no SQLite-specific SQL
  * ISO-8601 TEXT timestamps (not DATETIME) so Postgres is a config switch
  * JSON stored as TEXT
  * `tenant_id` on EVERY table from the first migration, even though v1 is
    single-tenant. It costs nothing now and saves a migration later.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()


def _tenant() -> Column:
    return Column("tenant_id", String(64), nullable=False, server_default="default")


nodes = Table(
    "nodes",
    metadata,
    Column("id", String(512), primary_key=True),
    _tenant(),
    Column("kind", String(32), nullable=False),          # agent|tool|server|resource|identity
    Column("class", String(32)),                          # ResourceClass
    Column("label", Text),
    Column("risk", Float, nullable=False, server_default="0"),
    Column("risk_floor", Float, nullable=False, server_default="0"),
    Column("confidence", Float, nullable=False, server_default="0"),
    Column("observations", Integer, nullable=False, server_default="0"),
    Column("first_seen", String(40), nullable=False),
    Column("last_seen", String(40), nullable=False),
    Column("risk_updated", String(40), nullable=False),   # drives lazy decay
    Column("attrs", Text),                                 # JSON
    Index("ix_nodes_tenant_kind", "tenant_id", "kind"),
    Index("ix_nodes_risk", "risk"),
)

edges = Table(
    "edges",
    metadata,
    Column("id", String(1024), primary_key=True),          # src|action|dst
    _tenant(),
    Column("src", String(512), nullable=False),
    Column("dst", String(512), nullable=False),
    Column("action", String(32), nullable=False),
    Column("count", Integer, nullable=False, server_default="0"),
    Column("risk", Float, nullable=False, server_default="0"),
    Column("confidence", Float, nullable=False, server_default="0"),
    Column("first_seen", String(40), nullable=False),
    Column("last_seen", String(40), nullable=False),
    Column("risk_updated", String(40), nullable=False),
    UniqueConstraint("tenant_id", "src", "dst", "action", name="uq_edge"),
    Index("ix_edges_src", "tenant_id", "src"),
    Index("ix_edges_dst", "tenant_id", "dst"),
)

paths = Table(
    "paths",
    metadata,
    Column("seq_hash", String(64), primary_key=True),
    _tenant(),
    Column("n", Integer, nullable=False),
    Column("agent_id", String(256), nullable=False),
    Column("seq", Text, nullable=False),                   # JSON array
    Column("seen_count", Integer, nullable=False, server_default="0"),
    Column("risk", Float, nullable=False, server_default="0"),
    Column("first_seen", String(40), nullable=False),
    Column("last_seen", String(40), nullable=False),
    Index("ix_paths_agent", "tenant_id", "agent_id", "n"),
)

events = Table(
    "events",
    metadata,
    Column("event_id", String(64), primary_key=True),
    _tenant(),
    Column("ts", String(40), nullable=False),
    Column("session_id", String(64), nullable=False),
    Column("agent_id", String(256), nullable=False),
    Column("step", Integer, nullable=False),
    Column("tool_server", String(256), nullable=False),
    Column("tool_name", String(256), nullable=False),
    Column("resource_uri", Text),
    Column("resource_class", String(32)),
    Column("action", String(32), nullable=False),
    Column("args_digest", String(80)),
    Column("status", String(16), nullable=False),
    Column("latency_ms", Integer, server_default="0"),
    Column("bytes", Integer, server_default="0"),
    Column("error", Text),
    Index("ix_events_session", "tenant_id", "session_id", "step"),
    Index("ix_events_agent_ts", "tenant_id", "agent_id", "ts"),
)

# Full tool arguments live here, apart from `events`, so they can be redacted
# or purged on their own retention clock (ARCHITECTURE section 9, rule 5).
event_args = Table(
    "event_args",
    metadata,
    Column("event_id", String(64), primary_key=True),
    _tenant(),
    Column("args", Text),                                  # JSON, redactable
    Column("redacted", Integer, nullable=False, server_default="0"),
    Column("expires_at", String(40)),
)

decisions = Table(
    "decisions",
    metadata,
    Column("event_id", String(64), primary_key=True),
    _tenant(),
    Column("ts", String(40), nullable=False),
    Column("verdict", String(16), nullable=False),
    Column("score", Float, nullable=False, server_default="0"),
    Column("detectors", Text),                             # JSON array of evidence
    Column("sentence", Text, nullable=False, server_default=""),
    Column("budget_ms", Integer, server_default="0"),
    Column("fail_open", Integer, nullable=False, server_default="0"),
    Column("mode", String(16), nullable=False, server_default="observe"),
    Index("ix_decisions_verdict", "tenant_id", "verdict", "ts"),
)

signals = Table(
    "signals",
    metadata,
    Column("id", String(64), primary_key=True),
    _tenant(),
    Column("ts", String(40), nullable=False),
    Column("event_id", String(64)),
    Column("detector", String(64), nullable=False),
    Column("weight", Float, nullable=False),
    Column("detail", Text),
    Column("agent_id", String(256)),
    Column("node_id", String(512)),
    Column("edge_id", String(1024)),
    # This index is what makes the corroboration query cheap: distinct agents
    # that reported a given node inside a window.
    Index("ix_signals_node_agent_ts", "tenant_id", "node_id", "agent_id", "ts"),
)

agents = Table(
    "agents",
    metadata,
    Column("agent_id", String(256), primary_key=True),
    _tenant(),
    # NOTE: trust is agent state (the temperature machine). RISK is NOT here —
    # risk lives on nodes and edges. See ADR-004.
    Column("trust", Float, nullable=False, server_default="1.0"),
    Column("zone", String(16), nullable=False, server_default="cold"),
    Column("baseline_state", String(16), nullable=False, server_default="learning"),
    Column("event_count", Integer, nullable=False, server_default="0"),
    Column("first_seen", String(40), nullable=False),
    Column("last_seen", String(40), nullable=False),
    Column("trust_updated", String(40), nullable=False),
)

approvals = Table(
    "approvals",
    metadata,
    Column("id", String(64), primary_key=True),
    _tenant(),
    Column("event_id", String(64), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("expires_at", String(40), nullable=False),
    Column("state", String(16), nullable=False, server_default="pending"),
    Column("decided_at", String(40)),
    Column("decided_by", String(256)),
    Column("reason", Text),
    Index("ix_approvals_state", "tenant_id", "state", "created_at"),
)

audit = Table(
    "audit",
    metadata,
    Column("id", String(64), primary_key=True),
    _tenant(),
    Column("ts", String(40), nullable=False),
    Column("actor", String(256), nullable=False),
    Column("action", String(64), nullable=False),
    Column("target", Text),
    Column("detail", Text),
)

ALL_TABLES = [
    nodes, edges, paths, events, event_args,
    decisions, signals, agents, approvals, audit,
]
