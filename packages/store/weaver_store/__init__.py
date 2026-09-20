"""Weaver persistence — SQLAlchemy Core, SQLite now, Postgres-portable (ADR-002)."""

from .db import DEFAULT_URL, get_engine, init_db
from .schema import ALL_TABLES, metadata

__all__ = ["ALL_TABLES", "DEFAULT_URL", "get_engine", "init_db", "metadata"]
