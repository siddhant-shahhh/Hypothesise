"""Engine creation and schema bootstrap."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine

from .schema import metadata

DEFAULT_URL = "sqlite:///./data/weaver.db"


def get_engine(url: str | None = None, echo: bool = False) -> Engine:
    url = url or os.environ.get("WEAVER_DB_URL", DEFAULT_URL)
    if url.startswith("sqlite:///") and ":memory:" not in url:
        path = Path(url.replace("sqlite:///", "", 1))
        path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, echo=echo, future=True)
    return engine


def init_db(engine: Engine) -> None:
    """Create all tables. Replaced by a real migration runner in M5."""
    metadata.create_all(engine)
