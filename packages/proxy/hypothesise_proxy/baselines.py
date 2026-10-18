"""Where live baselines are kept between runs (ADR-042).

The dashboard learns from every event it ingests and writes the result here;
wrapped servers and proxies read it at startup, so a baseline survives the
agent app restarting. One writer (the dashboard), many readers — no locking
needed beyond the atomic rename.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .spool import home


def path() -> Path:
    return home() / "baselines.json"


def load_into(engine) -> int:
    try:
        rows = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return engine.baselines.load_list(rows if isinstance(rows, list) else [])


def save_from(engine) -> bool:
    try:
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".baselines.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(engine.baselines.to_list()), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except OSError:
        return False
