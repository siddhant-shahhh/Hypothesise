"""Hypothesise MCP proxy — the data plane.

Never blocks on the engine, never writes synchronously, always fails open.

Names are resolved lazily (PEP 562) so `hypothesise_proxy.spool`, which the
Claude Code hook imports on every tool call, does not drag in the server, the
engine and asyncio subprocess machinery it never uses.
"""

from __future__ import annotations

import importlib
from typing import Any

__version__ = "0.1.0a4"

_EXPORTS = {
    "ProxyConfig": "config", "EventEmitter": "emit", "Gate": "gate",
    "GateConfig": "gate", "HypothesiseProxy": "server", "Shipper": "shipper",
    "ControlPlaneSync": "sync", "Upstream": "upstream", "UpstreamConfig": "upstream",
    "Spooler": "spooler",
}

__all__ = [*_EXPORTS, "__version__"]


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module 'hypothesise_proxy' has no attribute {name!r}")
    return getattr(importlib.import_module(f".{mod}", __name__), name)
