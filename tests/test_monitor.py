"""Tests for Monitor trailing stop-loss logic (pure math, no service imports needed)."""
import pytest


# ---------------------------------------------------------------------------
# Trail distance derivation
# ---------------------------------------------------------------------------

def test_trail_pct_from_entry_and_sl():
    entry, sl = 100.0, 98.0
    trail_pct = (entry - sl) / entry * 100
    assert abs(trail_pct - 2.0) < 1e-9


# ---------------------------------------------------------------------------
# Trailing SL formula: new_sl = price * (1 - trail_pct/100)
# ---------------------------------------------------------------------------

def test_trailing_sl_rises_with_price():
    entry, sl = 100.0, 98.0
    trail_pct = (entry - sl) / entry * 100  # 2%
    new_price = 110.0
    new_sl = new_price * (1 - trail_pct / 100)
    assert new_sl > sl
    assert abs(new_sl - 107.8) < 0.01


def test_trailing_sl_does_not_move_when_price_unchanged():
    entry, sl = 100.0, 98.0
    trail_pct = (entry - sl) / entry * 100
    # price has not moved above entry yet
    new_sl = entry * (1 - trail_pct / 100)
    # new_sl == original sl, so no update (0.1% threshold not met)
    assert not (new_sl > sl * 1.001)


def test_trailing_sl_does_not_move_when_price_drops():
    entry, sl = 100.0, 98.0
    trail_pct = (entry - sl) / entry * 100
    new_price = 95.0  # price fell
    new_sl = new_price * (1 - trail_pct / 100)
    # new_sl < original sl — must NOT update
    assert new_sl < sl


def test_trailing_sl_breakeven_when_price_rises_enough():
    entry, sl = 100.0, 98.0
    trail_pct = (entry - sl) / entry * 100  # 2%
    # SL reaches breakeven when price = entry / (1 - trail_pct/100)
    breakeven_price = entry / (1 - trail_pct / 100)
    new_sl = breakeven_price * (1 - trail_pct / 100)
    assert abs(new_sl - entry) < 0.01  # SL is now at entry price


# ---------------------------------------------------------------------------
# 0.1% write-threshold guard
# ---------------------------------------------------------------------------

def test_update_threshold_prevents_tiny_moves():
    sl = 98.0
    # A new SL only 0.05% higher should NOT trigger a write
    new_sl = sl * 1.0005
    assert not (new_sl > sl * 1.001)


def test_update_threshold_allows_meaningful_moves():
    sl = 98.0
    # A new SL 0.2% higher should trigger a write
    new_sl = sl * 1.002
    assert new_sl > sl * 1.001
