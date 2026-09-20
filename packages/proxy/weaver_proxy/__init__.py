"""Weaver MCP proxy — the data plane.

Never blocks on the engine, never writes synchronously, always fails open.
"""

from .config import ProxyConfig
from .emit import EventEmitter
from .gate import Gate, GateConfig
from .server import WeaverProxy
from .shipper import Shipper
from .upstream import Upstream, UpstreamConfig

__version__ = "0.1.0"

__all__ = [
    "EventEmitter", "Gate", "GateConfig", "ProxyConfig", "Upstream",
    "Shipper", "UpstreamConfig", "WeaverProxy", "__version__",
]
