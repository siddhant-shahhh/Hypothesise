"""Risk arithmetic — reinforcement, decay, corroboration, trust."""

from .model import (
    ALPHA,
    BETA,
    GAMMA,
    Signal,
    Weight,
    confidence_from_observations,
    corroborate,
    decay,
    effective_risk,
    hours_between,
    reinforce,
    trust_after_clean,
    trust_after_signal,
)

__all__ = [
    "ALPHA", "BETA", "GAMMA", "Signal", "Weight", "confidence_from_observations",
    "corroborate", "decay", "effective_risk", "hours_between", "reinforce",
    "trust_after_clean", "trust_after_signal",
]
