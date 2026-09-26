"""Migration tests.

The bug these exist for: `create_all` creates missing TABLES and silently
ignores missing COLUMNS. M4 added `agents.hot_signals` and `agents.quarantined`,
so any database created before it would never gain them — the proxy would start,
fail to persist quarantine state, and say nothing.
"""

import pytest
from sqlalchemy import create_engine, inspect, text
from weaver_store.db import get_engine
from weaver_store.migrate import (
    MIGRATIONS,
    TARGET_VERSION,
    current_version,
    migrate,
    status,
)


def _cols(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_fresh_database_reaches_target():
    e = get_engine("sqlite:///:memory:")
    assert current_version(e) == 0
    applied = migrate(e)
    assert len(applied) == len(MIGRATIONS)
    assert current_version(e) == TARGET_VERSION
    assert status(e)["up_to_date"]


def test_migrate_is_idempotent():
    e = get_engine("sqlite:///:memory:")
    migrate(e)
    assert migrate(e) == []


def test_upgrades_a_pre_m4_database_in_place(tmp_path):
    """The actual regression. An old database keeps its data and gains the
    columns."""
    url = f"sqlite:///{tmp_path / 'old.db'}"
    raw = create_engine(url)
    with raw.begin() as c:
        c.execute(text(
            "CREATE TABLE agents (agent_id TEXT PRIMARY KEY, tenant_id TEXT, "
            "trust REAL, zone TEXT, baseline_state TEXT, event_count INTEGER, "
            "first_seen TEXT, last_seen TEXT, trust_updated TEXT)"))
        c.execute(text("INSERT INTO agents VALUES "
                       "('old',:t,0.4,'warm','active',99,'t','t','t')"),
                  {"t": "default"})

    e = get_engine(url)
    assert "hot_signals" not in _cols(e, "agents")
    migrate(e)
    assert {"hot_signals", "quarantined"} <= _cols(e, "agents")

    with e.begin() as c:
        rows = c.execute(text("SELECT agent_id, trust FROM agents")).fetchall()
    assert rows == [("old", 0.4)]


def test_partial_migration_by_target():
    e = get_engine("sqlite:///:memory:")
    migrate(e, target=2)
    assert current_version(e) == 2
    st = status(e)
    assert not st["up_to_date"]
    assert len(st["pending"]) == TARGET_VERSION - 2
    migrate(e)
    assert status(e)["up_to_date"]


def test_versions_are_unique_and_contiguous():
    """A gap or duplicate means someone edited a shipped migration instead of
    appending one."""
    versions = sorted(m.version for m in MIGRATIONS)
    assert versions == list(range(1, len(MIGRATIONS) + 1))


def test_every_migration_is_safe_to_rerun():
    """Each step must be individually idempotent, not just the runner."""
    e = get_engine("sqlite:///:memory:")
    migrate(e)
    for m in MIGRATIONS:
        m.apply(e)          # must not raise
    assert status(e)["up_to_date"]


def test_init_db_runs_migrations_not_create_all():
    from weaver_store.db import init_db

    e = get_engine("sqlite:///:memory:")
    init_db(e)
    assert current_version(e) == TARGET_VERSION


def test_postgres_without_the_driver_says_what_to_install():
    """ADR-028: Postgres is an extra, so the error must name the fix."""
    try:
        import psycopg  # noqa: F401
        pytest.skip("psycopg is installed; nothing to assert")
    except ImportError:
        pass
    with pytest.raises(RuntimeError, match="postgres"):
        get_engine("postgresql+psycopg://u:p@localhost/db")
