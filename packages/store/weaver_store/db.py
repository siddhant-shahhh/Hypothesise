"""Engine creation and schema bootstrap.

Two SQLite details that are easy to get wrong and fail confusingly:

1. **In-memory databases are per-connection.** Every new connection to
   `sqlite:///:memory:` gets its OWN empty database, so tables created on one
   connection are invisible to the next — "no such table" despite create_all
   having just run. StaticPool pins the whole engine to one connection.

2. **The API serves requests on a thread pool.** SQLite connections default to
   refusing use from a thread other than the one that opened them, so
   `check_same_thread=False` is required. SQLAlchemy's pool already serialises
   access, so this is safe here.

Neither applies to Postgres, which is why the switch in M5 simplifies this.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

from .schema import metadata

# Outside the user's project, deliberately. A tool people install should not
# drop a database into the directory they are working in — it pollutes their
# repo, it gets committed by accident, and on network or removable volumes
# (an external drive, a synced folder) SQLite's locking can fail outright with
# "attempt to write a readonly database".
DEFAULT_DIR = Path(os.environ.get("WEAVER_HOME", "~/.weaver")).expanduser()
DEFAULT_URL = f"sqlite:///{DEFAULT_DIR / 'weaver.db'}"


def get_engine(url: str | None = None, echo: bool = False) -> Engine:
    url = url or os.environ.get("WEAVER_DB_URL") or DEFAULT_URL

    if url.startswith("sqlite"):
        in_memory = ":memory:" in url or url == "sqlite://"
        if not in_memory:
            path = Path(url.replace("sqlite:///", "", 1))
            path.parent.mkdir(parents=True, exist_ok=True)
        return create_engine(
            url,
            echo=echo,
            future=True,
            connect_args={"check_same_thread": False},
            # One shared connection for in-memory, or the schema vanishes
            # between calls. File-backed databases pool normally.
            poolclass=StaticPool if in_memory else None,
        )

    return create_engine(url, echo=echo, future=True)


def init_db(engine: Engine) -> None:
    """Create all tables. Replaced by a versioned migration runner in M5."""
    metadata.create_all(engine)
