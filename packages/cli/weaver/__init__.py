"""Weaver — behavioural security layer for AI agents.

One package, one command. `weaver --help` is the entry point for everything.

The internal split into weaver_core / weaver_store / weaver_proxy / weaver_api
stays (ADR-005: the engine imports nothing, and stays separable), but users
install and learn one thing (ADR-027).
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
