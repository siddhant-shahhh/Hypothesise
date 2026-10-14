"""Proxy configuration — JSON file, same shape as an MCP client config.

Deliberately mirrors the `mcpServers` block people already have in their
Claude Desktop / Cursor config, so adopting Hypothesise is moving a block of JSON
rather than learning a new format.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .gate import GateConfig
from .upstream import UpstreamConfig

_RENAMED = {"weaver.config.json": "hypothesise.config.json",
            "hypothesise.config.json": "weaver.config.json"}


def resolve_config_path(path: str | Path) -> Path:
    """The named file, or its pre/post-rename twin beside it (ADR-037).

    A client config written before the rename names weaver.config.json; the
    repo now holds hypothesise.config.json. Either spelling finds whichever
    exists, so an old entry keeps working instead of dying with "file not found".
    """
    p = Path(path)
    if p.exists():
        return p
    twin = _RENAMED.get(p.name)
    if twin and (p.with_name(twin)).exists():
        return p.with_name(twin)
    return p


@dataclass
class ProxyConfig:
    upstreams: list[UpstreamConfig] = field(default_factory=list)
    gate: GateConfig = field(default_factory=GateConfig.from_env)
    rules: list = field(default_factory=list)
    agent_id: str = ""            # falls back to the client's own name
    queue_maxsize: int = 10_000
    api_url: str = ""             # empty = local-only, no control plane
    ask_timeout: float = 120.0    # ASK deadline; expiry DENIES (ADR-017)
    session_id: str = ""          # fixed by `hypothesise wrap`; random otherwise

    @classmethod
    def load(cls, path: str | Path) -> ProxyConfig:
        raw = json.loads(resolve_config_path(path).read_text())
        servers = raw.get("mcpServers") or raw.get("upstreams") or {}
        upstreams = [
            UpstreamConfig(
                name=name,
                command=spec["command"],
                args=list(spec.get("args", [])),
                env=dict(spec.get("env", {})),
            )
            for name, spec in servers.items()
        ]
        # "weaver" was the codename until 0.1.0; existing configs keep working.
        g = raw.get("hypothesise") or raw.get("weaver") or {}

        # Environment overrides the file. Documented that way, and it is what
        # people expect — you should be able to flip a single run to enforce
        # without editing a config that is probably in version control.
        from . import settings

        gate = GateConfig(
            # env > `hypothesise mode` > this file > observe (ADR-041)
            mode=settings.mode(str(g.get("mode", "observe")).lower()),
            fail_mode=os.environ.get(
                "HYPOTHESISE_FAIL_MODE", g.get("failMode", "open")).lower(),
            budget_ms=int(os.environ.get(
                "HYPOTHESISE_DECISION_BUDGET_MS", g.get("budgetMs", 50))),
        )
        return cls(
            upstreams=upstreams,
            gate=gate,
            rules=list(raw.get("rules") or g.get("rules") or []),
            agent_id=g.get("agentId", ""),
            queue_maxsize=int(g.get("queueMaxsize", 10_000)),
            api_url=os.environ.get("HYPOTHESISE_API_URL") or g.get("apiUrl", "")
            or settings.api_url(""),
            ask_timeout=float(g.get("askTimeoutSeconds", 120)),
        )
