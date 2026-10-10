"""Hypothesise — behavioural security layer for AI agents.

One package, one command. `hypothesise --help` is the entry point for everything.

The internal split into hypothesise_core / hypothesise_store / hypothesise_proxy / hypothesise_api
stays (ADR-005: the engine imports nothing, and stays separable), but users
install and learn one thing (ADR-027).
"""

__version__ = "0.1.0a1"
__all__ = ["__version__"]
