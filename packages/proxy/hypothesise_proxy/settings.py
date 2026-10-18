"""Machine-wide settings written by `hypothesise mode` (ADR-041).

One small JSON file, ~/.hypothesise/settings.json, read by every wrapped server
and proxy at startup and re-read while they run. That is what lets one command
switch every connected agent between observe and enforce, without editing app
configs or restarting anything.

Precedence, highest first: HYPOTHESISE_* environment variable, this file, the
proxy config file, the built-in default (observe). Standard library only, and
every read tolerates a missing or broken file by returning nothing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .spool import home

DEFAULT_API = "http://127.0.0.1:8080"
MODES = ("observe", "enforce")


def path() -> Path:
    return home() / "settings.json"


def load() -> dict[str, Any]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data: dict[str, Any]) -> Path:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".settings.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def mtime() -> float:
    try:
        return path().stat().st_mtime
    except OSError:
        return 0.0


def mode(default: str = "observe") -> str:
    """Effective mode: env, then settings, then `default`."""
    env = os.environ.get("HYPOTHESISE_MODE", "").strip().lower()
    if env in MODES:
        return env
    m = str(load().get("mode", "")).lower()
    return m if m in MODES else default


def api_url(default: str = "") -> str:
    """Control plane URL: env, then settings (only meaningful when enforcing)."""
    env = os.environ.get("HYPOTHESISE_API_URL", "")
    if env:
        return env
    s = load()
    if str(s.get("mode", "")).lower() == "enforce":
        return str(s.get("apiUrl") or DEFAULT_API)
    return default


def notify_enabled() -> bool:
    env = os.environ.get("HYPOTHESISE_NOTIFY", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    return bool(load().get("notify", True))


def rules() -> list[dict[str, Any]]:
    """Personal-data rules written by `hypothesise mode enforce` (ADR-042).

    Applied by every wrapped server, proxy entry and blocking hook, on top of
    any rules in a proxy's own config file. A broken entry is skipped by the
    rule loader, so a hand-edited file cannot take protection down with it.
    """
    raw = load().get("rules")
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
