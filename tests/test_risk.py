"""Risk arithmetic tests — the stigmergy mechanic.

These pin the properties the design depends on: saturation, decay to a floor
and never below it, distinct-reporter corroboration, and asymmetric trust.
"""

import math

import pytest
from hypothesise_core.events import ResourceClass
from hypothesise_core.graph.model import floor_for, lambda_for
from hypothesise_core.risk.model import (
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


def test_reinforce_saturates():
    """Risk approaches 1.0 but never reaches or exceeds it."""
    r = 0.0
    for _ in range(200):
        r = reinforce(r, Weight.CRITICAL)
    assert r < 1.0
    assert r > 0.99


def test_reinforce_diminishing_returns():
    """The tenth signal must move risk far less than the first."""
    first = reinforce(0.0, Weight.HIGH) - 0.0
    high = 0.9
    tenth = reinforce(high, Weight.HIGH) - high
    assert first > tenth * 5


def test_decay_never_below_floor():
    floor = floor_for(ResourceClass.SECRET)
    r = decay(0.95, floor, hours=10_000, lam=lambda_for(ResourceClass.SECRET))
    assert r == pytest.approx(floor, abs=1e-6)
    assert r >= floor


def test_decay_is_monotonic():
    floor, lam = 0.1, 0.02
    vals = [decay(0.9, floor, h, lam) for h in (0, 1, 10, 100, 1000)]
    assert vals == sorted(vals, reverse=True)


def test_secrets_decay_slower_than_source():
    """A flagged credentials store must stay hot longer than a flagged .py file."""
    after_24h_secret = decay(0.9, 0.3, 24, lambda_for(ResourceClass.SECRET))
    after_24h_source = decay(0.9, 0.0, 24, lambda_for(ResourceClass.SOURCE_CODE))
    assert after_24h_secret > after_24h_source


def test_corroboration_rewards_distinct_reporters():
    """Three different agents > one agent three times. This is the ant-colony term."""
    one = corroborate(Weight.MEDIUM, distinct_reporters=1)
    three = corroborate(Weight.MEDIUM, distinct_reporters=3)
    assert one == pytest.approx(float(Weight.MEDIUM))
    assert three > one
    assert three == pytest.approx(float(Weight.MEDIUM) * (1 + math.log(3)))


def test_corroboration_floor_at_one_reporter():
    assert corroborate(0.5, 0) == pytest.approx(0.5)


def test_confidence_rises_and_plateaus():
    vals = [confidence_from_observations(n) for n in range(0, 200)]
    assert vals == sorted(vals)            # monotonic
    assert vals[0] == 0.0                  # no observations, no confidence
    assert vals[-1] < 1.0                  # never certain
    gains = [b - a for a, b in zip(vals, vals[1:], strict=False)]
    assert gains == sorted(gains, reverse=True)   # diminishing


def test_single_observation_has_low_confidence():
    """The regression this formula exists for.

    The incremental form saturated to 1.0 on the first observation, which let
    one sighting of a brand-new node carry full weight into the score.
    """
    assert confidence_from_observations(1) < 0.15
    assert confidence_from_observations(10) == pytest.approx(0.5)


def test_effective_risk_damped_by_low_confidence():
    """A brand-new node with one bad observation must not start blocking traffic."""
    assert effective_risk(0.9, 0.1) == pytest.approx(0.09)
    assert effective_risk(0.9, 1.0) == pytest.approx(0.9)


def test_trust_is_asymmetric():
    """Cheap to lose, expensive to regain — the correct shape for security."""
    after_hit = trust_after_signal(1.0, Weight.CRITICAL)
    assert after_hit == pytest.approx(0.75)

    loss = 1.0 - after_hit
    recovered_1h = trust_after_clean(after_hit, hours=1)
    gain_1h = recovered_1h - after_hit
    assert gain_1h < loss / 10          # one clean hour recovers <10% of the loss

    recovered_24h = trust_after_clean(after_hit, hours=24)
    assert recovered_24h < 1.0
    assert recovered_24h > recovered_1h


def test_trust_clamped():
    assert trust_after_signal(0.1, Weight.CRITICAL) == 0.0
    assert trust_after_clean(1.0, hours=1000) == 1.0


def test_hours_between():
    a = "2026-09-19T10:00:00+00:00"
    b = "2026-09-19T22:00:00+00:00"
    assert hours_between(a, b) == pytest.approx(12.0)
    assert hours_between(b, a) == 0.0
    assert hours_between("garbage", b) == 0.0
