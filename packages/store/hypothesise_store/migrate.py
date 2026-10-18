"""Versioned schema migrations.

`metadata.create_all()` creates tables that do not exist and silently ignores
columns that do. M4 added `agents.hot_signals` and `agents.quarantined`, and
any database created before that would never have gained them — the proxy would
start, fail to persist quarantine state, and say nothing. That is the bug this
module exists to prevent.

Hand-rolled rather than Alembic (ADR-026): a numbered list of SQL steps and one
`schema_version` table, with no dependency and no autogenerate to be wrong
about two backends.

Rules for adding a migration:
  * append, never edit — a shipped migration has already run somewhere
  * each step must be safe to re-run (IF NOT EXISTS, or guarded in Python)
  * SQL must work on both SQLite and Postgres, or branch on the dialect
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Engine, inspect, text

from .schema import metadata

log = logging.getLogger("hypothesise.migrate")


@dataclass
class Migration:
    version: int
    name: str
    apply: Callable[[Engine], None]


# --- helpers ---------------------------------------------------------------


def _has_column(engine: Engine, table: str, column: str) -> bool:
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def _add_column(engine: Engine, table: str, column: str, ddl: str) -> None:
    """Idempotent ALTER TABLE ADD COLUMN.

    SQLite has no `ADD COLUMN IF NOT EXISTS`, so the check is done in Python
    and the operation is safe to re-run on either backend.
    """
    if _has_column(engine, table, column):
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    log.info("added %s.%s", table, column)


# --- migrations ------------------------------------------------------------


def _m1_baseline(engine: Engine) -> None:
    """Create everything that does not exist yet.

    Also the migration that adopts a pre-migration database: create_all leaves
    existing tables untouched, so an old database passes through here and is
    then corrected by the later steps.
    """
    metadata.create_all(engine)


def _m2_agent_quarantine(engine: Engine) -> None:
    """M4 added quarantine state to `agents`."""
    _add_column(engine, "agents", "hot_signals", "INTEGER NOT NULL DEFAULT 0")
    _add_column(engine, "agents", "quarantined", "INTEGER NOT NULL DEFAULT 0")


def _m3_approval_context(engine: Engine) -> None:
    """M3 denormalised context onto `approvals` (ADR-019)."""
    _add_column(engine, "approvals", "context", "TEXT")


def _m4_event_arg_shape(engine: Engine) -> None:
    """M2 Tier-2 detectors need argument shape, not just the digest."""
    _add_column(engine, "events", "arg_keys", "TEXT")
    _add_column(engine, "events", "arg_bytes", "INTEGER NOT NULL DEFAULT 0")


def _m5_decision_elapsed(engine: Engine) -> None:
    """Sub-millisecond decision time (ADR-042). `budget_ms` is an integer and
    rounded every real decision down to 0."""
    _add_column(engine, "decisions", "elapsed_ms", "REAL NOT NULL DEFAULT 0")


MIGRATIONS: list[Migration] = [
    Migration(1, "baseline", _m1_baseline),
    Migration(2, "agent_quarantine", _m2_agent_quarantine),
    Migration(3, "approval_context", _m3_approval_context),
    Migration(4, "event_arg_shape", _m4_event_arg_shape),
    Migration(5, "decision_elapsed", _m5_decision_elapsed),
]

TARGET_VERSION = max(m.version for m in MIGRATIONS)


# --- runner ----------------------------------------------------------------


def _ensure_version_table(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER NOT NULL,"
            " name TEXT NOT NULL,"
            " applied_at TEXT NOT NULL)"))


def current_version(engine: Engine) -> int:
    _ensure_version_table(engine)
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT MAX(version) FROM schema_version")).scalar()
    return int(row or 0)


def migrate(engine: Engine, target: int | None = None) -> list[str]:
    """Bring the database up to `target` (default: latest). Returns what ran."""
    from hypothesise_core.events import utcnow

    target = TARGET_VERSION if target is None else target
    at = current_version(engine)
    applied: list[str] = []

    for m in sorted(MIGRATIONS, key=lambda x: x.version):
        if m.version <= at or m.version > target:
            continue
        log.info("applying migration %d: %s", m.version, m.name)
        m.apply(engine)
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO schema_version (version, name, applied_at) "
                     "VALUES (:v, :n, :t)"),
                {"v": m.version, "n": m.name, "t": utcnow()})
        applied.append(f"{m.version}:{m.name}")

    return applied


def status(engine: Engine) -> dict:
    at = current_version(engine)
    return {
        "current": at,
        "target": TARGET_VERSION,
        "up_to_date": at >= TARGET_VERSION,
        "pending": [f"{m.version}:{m.name}" for m in MIGRATIONS if m.version > at],
    }
