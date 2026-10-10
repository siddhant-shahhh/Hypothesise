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

from .schema import metadata  # noqa: F401

# Outside the user's project, deliberately. A tool people install should not
# drop a database into the directory they are working in — it pollutes their
# repo, it gets committed by accident, and on network or removable volumes
# (an external drive, a synced folder) SQLite's locking can fail outright with
# "attempt to write a readonly database".
DEFAULT_DIR = Path(os.environ.get("HYPOTHESISE_HOME", "~/.hypothesise")).expanduser()
DEFAULT_URL = f"sqlite:///{DEFAULT_DIR / 'hypothesise.db'}"


LEGACY_DB = Path("~/.weaver/weaver.db").expanduser()


def adopt_legacy_db(target: Path = DEFAULT_DIR / "hypothesise.db",
                    legacy: Path = LEGACY_DB) -> bool:
    """Copy the pre-rename database into place, once (ADR-037).

    Copied, not moved: the old file stays as a backup, and an old install that
    is still running keeps working. Only ever fills an empty slot — an existing
    hypothesise.db is never overwritten.
    """
    import shutil

    if target.exists() or not legacy.is_file():
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, target)
        return True
    except OSError:
        return False


def get_engine(url: str | None = None, echo: bool = False) -> Engine:
    url = url or os.environ.get("HYPOTHESISE_DB_URL") or DEFAULT_URL
    if url == DEFAULT_URL and adopt_legacy_db():
        print(f"hypothesise: brought over earlier data from {LEGACY_DB}")

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

    # Postgres. Pool settings matter here in a way they do not for SQLite: the
    # API serves on a thread pool and the proxy's sync loop is a second client.
    if url.startswith(("postgresql", "postgres:")):
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "Postgres support needs the driver:\n"
                "    pip install 'hypothesise-store[postgres]'"
            ) from exc
        return create_engine(url, echo=echo, future=True,
                             pool_pre_ping=True, pool_size=5, max_overflow=5)

    return create_engine(url, echo=echo, future=True)


def init_db(engine: Engine) -> None:
    """Bring the schema up to date.

    Runs versioned migrations rather than `create_all` alone. create_all
    creates missing TABLES but silently ignores missing COLUMNS, so a database
    created before M4 would never gain `agents.hot_signals` — the proxy would
    start, fail to persist quarantine state, and say nothing about it.
    """
    from .migrate import migrate

    migrate(engine)
