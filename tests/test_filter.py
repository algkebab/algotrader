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
# RVOL tests — standard candle-based formula: current bar / avg of prior N bars
# ---------------------------------------------------------------------------

def _make_volume_candles(volumes):
    """Minimal candles with only the volume field populated."""
    return [[0, 0, 0, 0, 100, v] for v in volumes]


def test_compute_rvol_insufficient_candles_returns_zero(f):
    # Fewer than period+2 candles → no baseline → 0.0
    candles = _make_volume_candles([1000] * 10)
    assert f._compute_rvol_from_candles(candles, period=20) == 0.0


def test_compute_rvol_equal_to_average_returns_one(f):
    # candles[-2] (closed bar) == average of prior 20 bars → RVOL = 1.0
    # Layout: 20 baseline + 1 closed current + 1 forming (ignored)
    candles = _make_volume_candles([1000] * 22)
    assert f._compute_rvol_from_candles(candles, period=20) == 1.0


def test_compute_rvol_double_average_returns_two(f):
    # Closed bar = 2× average → RVOL = 2.0; forming bar volume is irrelevant
    baseline = [1000] * 20
    candles = _make_volume_candles(baseline + [2000] + [999])  # 999 = forming bar (ignored)
    assert f._compute_rvol_from_candles(candles, period=20) == 2.0


def test_compute_rvol_below_average_returns_less_than_one(f):
    # Closed bar < average → RVOL < 1.0
    baseline = [1000] * 20
    candles = _make_volume_candles(baseline + [500] + [999])
    assert f._compute_rvol_from_candles(candles, period=20) < 1.0


def test_compute_rvol_zero_current_volume_returns_zero(f):
    baseline = [1000] * 20
    candles = _make_volume_candles(baseline + [0] + [999])
    assert f._compute_rvol_from_candles(candles, period=20) == 0.0


# ---------------------------------------------------------------------------
# Strategy profile tests
# ---------------------------------------------------------------------------

def test_all_strategy_profiles_have_required_keys():
    required = {"min_24h_volume", "rvol_threshold", "rsi_min", "rsi_max", "rsi_1h_max", "min_change"}
    for name, profile in STRATEGY_PROFILES.items():
        assert required == set(profile.keys()), f"Profile {name} missing keys"


def test_momentum_strategies_have_positive_rsi_min():
    # CONSERVATIVE and AGGRESSIVE must reject deeply oversold (counter-trend) entries
    assert STRATEGY_PROFILES["CONSERVATIVE"]["rsi_min"] > 0
    assert STRATEGY_PROFILES["AGGRESSIVE"]["rsi_min"] > 0


def test_reversal_rsi_min_is_zero():
    # REVERSAL has no lower RSI bound — oversold is the target
    assert STRATEGY_PROFILES["REVERSAL"]["rsi_min"] == 0


def test_reversal_rsi_1h_max_confirms_oversold():
    # REVERSAL requires 1h RSI to also be oversold — not just a 15m blip
    assert STRATEGY_PROFILES["REVERSAL"]["rsi_1h_max"] <= 40


def test_momentum_strategies_rsi_1h_max_not_overbought():
    assert STRATEGY_PROFILES["CONSERVATIVE"]["rsi_1h_max"] <= 70
    assert STRATEGY_PROFILES["AGGRESSIVE"]["rsi_1h_max"] <= 80


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


# ---------------------------------------------------------------------------
# Recent change tests (4h momentum proxy)
# ---------------------------------------------------------------------------

def test_recent_change_insufficient_candles_returns_zero(f):
    candles = _make_candles([100] * 10)
    assert f._compute_recent_change(candles, lookback=16) == 0.0


def test_recent_change_flat_market_returns_zero(f):
    candles = _make_candles([100] * 20)
    assert f._compute_recent_change(candles, lookback=16) == 0.0


def test_recent_change_upward_move_is_positive(f):
    # Price rises from 100 to 110 over lookback period
    prices = [100] * 16 + [110, 110]  # 16 baseline + closed bar + forming bar
    candles = _make_candles(prices)
    change = f._compute_recent_change(candles, lookback=16)
    assert change > 0


def test_recent_change_downward_move_is_negative(f):
    prices = [100] * 16 + [90, 90]
    candles = _make_candles(prices)
    change = f._compute_recent_change(candles, lookback=16)
    assert change < 0


def test_recent_change_ignores_forming_bar(f):
    # Forming bar (last candle) price should not affect the result
    prices = [100] * 16 + [110, 999]  # 999 = forming bar, should be ignored
    candles = _make_candles(prices)
    change_with_spike = f._compute_recent_change(candles, lookback=16)
    prices_no_spike = [100] * 16 + [110, 1]  # different forming bar
    candles_no_spike = _make_candles(prices_no_spike)
    change_without_spike = f._compute_recent_change(candles_no_spike, lookback=16)
    assert change_with_spike == change_without_spike


# ---------------------------------------------------------------------------
# Candidate scoring tests
# ---------------------------------------------------------------------------

def test_score_higher_rvol_scores_higher(f):
    base = {'rsi': 55, 'ema_stack_15m': {'alignment': 'BULLISH'}, 'macd_15m': None}
    low_rvol = {**base, 'rvol': 1.2}
    high_rvol = {**base, 'rvol': 3.5}
    assert f._score_candidate(high_rvol, 'CONSERVATIVE') > f._score_candidate(low_rvol, 'CONSERVATIVE')


def test_score_bullish_ema_scores_higher_than_mixed(f):
    base = {'rvol': 2.0, 'rsi': 55, 'macd_15m': None}
    bullish = {**base, 'ema_stack_15m': {'alignment': 'BULLISH'}}
    mixed = {**base, 'ema_stack_15m': {'alignment': 'MIXED'}}
    assert f._score_candidate(bullish, 'CONSERVATIVE') > f._score_candidate(mixed, 'CONSERVATIVE')


def test_score_bounded_0_to_100(f):
    best = {'rvol': 4.0, 'rsi': 55, 'ema_stack_15m': {'alignment': 'BULLISH'},
            'macd_15m': {'histogram': 1.0, 'histogram_prev': 0.5}}
    worst = {'rvol': 0, 'rsi': 85, 'ema_stack_15m': {'alignment': 'BEARISH'}, 'macd_15m': None}
    assert 0 <= f._score_candidate(worst, 'CONSERVATIVE') <= 100
    assert 0 <= f._score_candidate(best, 'CONSERVATIVE') <= 100
