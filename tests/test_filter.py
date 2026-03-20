"""Tests for Filter service: RSI calculation, RVOL computation, and strategy profiles."""
import os
from unittest import mock

import pytest

# conftest.py adds services/filter to sys.path
with mock.patch.dict(os.environ, {"REDIS_HOST": "localhost"}):
    with mock.patch("redis.Redis"):
        from main import Filter, STRATEGY_PROFILES, STRATEGY_DEFAULT


@pytest.fixture
def f():
    """Filter instance with Redis mocked out (no real connection needed)."""
    with mock.patch("redis.Redis"):
        return Filter()


# ---------------------------------------------------------------------------
# RSI tests
# ---------------------------------------------------------------------------

def _make_candles(closes):
    """Wrap a list of close prices into minimal candle tuples [ts, o, h, l, close, vol]."""
    return [[0, 0, 0, 0, c, 0] for c in closes]


def test_rsi_returns_neutral_when_insufficient_data(f):
    candles = _make_candles([100] * 10)  # only 10 candles, need 15+
    assert f.calculate_rsi(candles) == 50


def test_rsi_returns_100_when_all_gains(f):
    # Steadily rising prices → no losses → RSI = 100
    closes = list(range(1, 30))
    candles = _make_candles(closes)
    assert f.calculate_rsi(candles) == 100


def test_rsi_returns_low_when_all_losses(f):
    # Steadily falling prices → no gains → avg_gain = 0 → RSI approaches 0
    closes = list(range(30, 1, -1))
    candles = _make_candles(closes)
    rsi = f.calculate_rsi(candles)
    assert rsi == 0 or rsi < 5  # avg_loss > 0, avg_gain = 0 → RS = 0 → RSI = 0


def test_rsi_midrange_for_alternating_prices(f):
    # Alternating up/down by same amount → roughly 50
    closes = [100 + (5 if i % 2 == 0 else -5) for i in range(30)]
    candles = _make_candles(closes)
    rsi = f.calculate_rsi(candles)
    assert 30 < rsi < 70


def test_rsi_bounded(f):
    closes = [100 + i * 2 for i in range(30)]
    candles = _make_candles(closes)
    rsi = f.calculate_rsi(candles)
    assert 0 <= rsi <= 100


# ---------------------------------------------------------------------------
# RVOL tests (pure computation, no Redis)
# ---------------------------------------------------------------------------

def test_compute_rvol_no_baseline_returns_zero(f):
    assert f._compute_rvol(1_000_000, None) == 0.0


def test_compute_rvol_one_avg_minute_of_volume(f):
    # If volume grew by exactly one avg-minute worth, RVOL = 1.0
    daily_vol = 1_440_000
    avg_per_min = daily_vol / 1440  # = 1000
    prev_vol = daily_vol - avg_per_min
    rvol = f._compute_rvol(daily_vol, prev_vol)
    assert abs(rvol - 1.0) < 1e-9


def test_compute_rvol_negative_when_volume_shrinks(f):
    # Shrinking volume → negative RVOL → fails threshold → no false signals
    rvol = f._compute_rvol(900_000, 1_000_000)
    assert rvol < 0


def test_compute_rvol_zero_volume_returns_zero(f):
    assert f._compute_rvol(0.0, 0.0) == 0.0


# ---------------------------------------------------------------------------
# Strategy profile tests
# ---------------------------------------------------------------------------

def test_all_strategy_profiles_have_required_keys():
    required = {"min_24h_volume", "rvol_threshold", "rsi_max", "min_change"}
    for name, profile in STRATEGY_PROFILES.items():
        assert required == set(profile.keys()), f"Profile {name} missing keys"


def test_default_strategy_exists():
    assert STRATEGY_DEFAULT in STRATEGY_PROFILES


def test_reversal_has_negative_min_change():
    # REVERSAL looks for price drops, so min_change must be negative
    assert STRATEGY_PROFILES["REVERSAL"]["min_change"] < 0


def test_reversal_rsi_max_is_oversold():
    # REVERSAL looks for RSI < 30 (oversold), not overbought
    assert STRATEGY_PROFILES["REVERSAL"]["rsi_max"] <= 30


def test_conservative_has_stricter_volume_than_aggressive():
    assert STRATEGY_PROFILES["CONSERVATIVE"]["min_24h_volume"] > STRATEGY_PROFILES["AGGRESSIVE"]["min_24h_volume"]
