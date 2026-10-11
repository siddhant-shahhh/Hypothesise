"""Hypothesise detection engine.

Zero I/O, zero framework dependencies (ADR-005). Everything here is pure:
data in, decision out. That keeps it testable, and keeps it separable.
"""

from .classify import classify_action, classify_resource
from .events import (
    Action,
    Decision,
    Event,
    Outcome,
    Resource,
    ResourceClass,
    Status,
    Tool,
    Verdict,
    digest_args,
    new_id,
    utcnow,
)

__version__ = "0.1.0a2"

__all__ = [
    "Action", "Decision", "Event", "Outcome", "Resource", "ResourceClass",
    "Status", "Tool", "Verdict", "classify_action", "classify_resource",
    "digest_args", "new_id", "utcnow", "__version__",
]
