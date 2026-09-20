"""Proxy configuration — JSON file, same shape as an MCP client config.

Deliberately mirrors the `mcpServers` block people already have in their
Claude Desktop / Cursor config, so adopting Weaver is moving a block of JSON
rather than learning a new format.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .gate import GateConfig
from .upstream import UpstreamConfig


@dataclass
class ProxyConfig:
    upstreams: list[UpstreamConfig] = field(default_factory=list)
    gate: GateConfig = field(default_factory=GateConfig.from_env)
    rules: list = field(default_factory=list)
    agent_id: str = ""            # falls back to the client's own name
    queue_maxsize: int = 10_000
    api_url: str = ""             # empty = local-only, no control plane
    ask_timeout: float = 120.0    # ASK deadline; expiry DENIES (ADR-017)

    @classmethod
    def load(cls, path: str | Path) -> ProxyConfig:
        raw = json.loads(Path(path).read_text())
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
        g = raw.get("weaver", {})

        # Environment overrides the file. Documented that way, and it is what
        # people expect — you should be able to flip a single run to enforce
        # without editing a config that is probably in version control.
        gate = GateConfig(
            mode=os.environ.get("WEAVER_MODE", g.get("mode", "observe")).lower(),
            fail_mode=os.environ.get(
                "WEAVER_FAIL_MODE", g.get("failMode", "open")).lower(),
            budget_ms=int(os.environ.get(
                "WEAVER_DECISION_BUDGET_MS", g.get("budgetMs", 50))),
        )
        return cls(
            upstreams=upstreams,
            gate=gate,
            rules=list(raw.get("rules") or g.get("rules") or []),
            agent_id=g.get("agentId", ""),
            queue_maxsize=int(g.get("queueMaxsize", 10_000)),
            api_url=os.environ.get("WEAVER_API_URL", g.get("apiUrl", "")),
            ask_timeout=float(g.get("askTimeoutSeconds", 120)),
        )
