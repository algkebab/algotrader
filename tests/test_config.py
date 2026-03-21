"""Tests for shared/config.py — verify bounds, types, and internal consistency."""
from shared import config


def test_max_open_orders_default_within_bounds():
    assert config.MAX_OPEN_ORDERS_MIN <= config.MAX_OPEN_ORDERS_DEFAULT <= config.MAX_OPEN_ORDERS_MAX


def test_max_symbols_default_within_bounds():
    assert config.MAX_SYMBOLS_MIN <= config.MAX_SYMBOLS_DEFAULT <= config.MAX_SYMBOLS_MAX


def test_risk_guard_values_positive():
    assert config.RISK_GUARD_MAX_SL > 0
    assert config.RISK_GUARD_MIN_RR >= 1.0  # R:R below 1 would be irrational


def test_leverage_positive():
    assert config.LEVERAGE >= 1


def test_fees_non_negative():
    assert config.BINANCE_TAKER_FEE >= 0
    assert config.ENTRY_SLIPPAGE >= 0
    assert config.HOURLY_MARGIN_INTEREST_RATE >= 0


def test_position_risk_pct_range():
    # Must be a fraction between 0 and 1 (exclusive)
    assert 0 < config.POSITION_RISK_PCT < 1


def test_liquidation_threshold_consistent_with_leverage():
    # For N× leverage, liquidation happens at ~100/N% drop.
    # LIQUIDATION_THRESHOLD_PCT should be close to that value.
    expected = 100.0 / config.LEVERAGE
    assert abs(config.LIQUIDATION_THRESHOLD_PCT - expected) < 5  # allow ±5pp tolerance


def test_sl_and_tp_percent_are_multipliers():
    # SL_PERCENT < 1 (price drops below entry), TP_PERCENT > 1 (price rises above entry)
    assert config.SL_PERCENT < 1.0
    assert config.TP_PERCENT > 1.0


def test_price_change_threshold_positive():
    assert config.PRICE_CHANGE_THRESHOLD > 0
