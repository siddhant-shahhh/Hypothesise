"""Proxy configuration — JSON file, same shape as an MCP client config.

Deliberately mirrors the `mcpServers` block people already have in their
Claude Desktop / Cursor config, so adopting Weaver is moving a block of JSON
rather than learning a new format.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .gate import GateConfig
from .upstream import UpstreamConfig


@dataclass
class ProxyConfig:
    upstreams: list[UpstreamConfig] = field(default_factory=list)
    gate: GateConfig = field(default_factory=GateConfig.from_env)
    agent_id: str = ""            # falls back to the client's own name
    queue_maxsize: int = 10_000
    api_url: str = ""             # empty = local-only, no control plane yet

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
        gate = GateConfig(
            mode=g.get("mode", "observe"),
            fail_mode=g.get("failMode", "open"),
            budget_ms=int(g.get("budgetMs", 50)),
        )
        return cls(
            upstreams=upstreams,
            gate=gate,
            agent_id=g.get("agentId", ""),
            queue_maxsize=int(g.get("queueMaxsize", 10_000)),
            api_url=g.get("apiUrl", ""),
        )
