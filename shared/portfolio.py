"""Portfolio-level risk: Kelly sizing, correlation guard, VaR, alpha vs benchmark."""

import math
from typing import Optional

# Sector buckets for correlation guard
SECTOR_MAP = {
    'BTC/USDT': 'L1', 'ETH/USDT': 'L1', 'SOL/USDT': 'L1', 'ADA/USDT': 'L1',
    'AVAX/USDT': 'L1', 'DOT/USDT': 'L1', 'ATOM/USDT': 'L1', 'NEAR/USDT': 'L1',
    'APT/USDT': 'L1', 'SUI/USDT': 'L1', 'TON/USDT': 'L1', 'TRX/USDT': 'L1',
    'MATIC/USDT': 'L2', 'ARB/USDT': 'L2', 'OP/USDT': 'L2', 'STRK/USDT': 'L2',
    'UNI/USDT': 'DEFI', 'AAVE/USDT': 'DEFI', 'CRV/USDT': 'DEFI',
    'LINK/USDT': 'DEFI', 'MKR/USDT': 'DEFI', 'COMP/USDT': 'DEFI',
    'BNB/USDT': 'CEX', 'OKB/USDT': 'CEX',
    'DOGE/USDT': 'MEME', 'SHIB/USDT': 'MEME', 'PEPE/USDT': 'MEME', 'FLOKI/USDT': 'MEME',
}
MAX_PER_SECTOR = 2


def get_sector(symbol: str) -> str:
    return SECTOR_MAP.get(symbol, 'ALT')


def check_correlation_guard(open_orders: list, new_symbol: str) -> tuple:
    """Returns (allowed: bool, reason: str)."""
    new_sector = get_sector(new_symbol)
    count = sum(1 for o in open_orders if get_sector(o['symbol']) == new_sector)
    if count >= MAX_PER_SECTOR:
        return False, f"{count}/{MAX_PER_SECTOR} positions already in {new_sector} sector"
    return True, ""


def kelly_position_size(
    win_rate: Optional[float],
    avg_win_pct: Optional[float],
    avg_loss_pct: Optional[float],
    base_risk_pct: float,
    confidence: int,
    current_drawdown_pct: float,
    regime_multiplier: float = 1.0,
) -> float:
    """
    Fractional Kelly position sizing (25% Kelly).
    Returns risk_pct to use (bounded 0.2% – 2.5%).

    When no history: starts at 50% of base risk (cautious debut).
    Kelly formula: f = (W*b - (1-W)) / b  where b = avg_win / avg_loss
    Uses 25% fractional Kelly for safety.
    Scales by: confidence, current drawdown, regime multiplier.
    """
    # Confidence multiplier: 50 conf = 0.75x, 95 conf = ~1.19x
    confidence_mult = 0.5 + (min(confidence, 95) / 95) * 0.75

    # Drawdown multiplier: reduce size as losses accumulate
    if current_drawdown_pct >= 15:
        dd_mult = 0.25
    elif current_drawdown_pct >= 10:
        dd_mult = 0.50
    elif current_drawdown_pct >= 5:
        dd_mult = 0.75
    else:
        dd_mult = 1.0

    # No history yet — start cautious
    if win_rate is None or avg_win_pct is None or avg_loss_pct is None:
        return max(0.002, min(0.025, base_risk_pct * 0.5 * confidence_mult * dd_mult * regime_multiplier))

    if avg_loss_pct <= 0 or win_rate <= 0:
        return max(0.002, min(0.025, base_risk_pct * 0.3 * dd_mult * regime_multiplier))

    b = avg_win_pct / avg_loss_pct
    kelly_f = (win_rate * b - (1 - win_rate)) / b
    kelly_f = max(0.0, min(kelly_f, 1.0))
    fractional = kelly_f * 0.25  # 25% of full Kelly

    # Map fractional Kelly to a position risk pct (scale from 30% to 200% of base)
    # When kelly_f=0 (no edge) → 0.3x base; when kelly_f=0.25 (max) → 1.5x base
    scale = 0.3 + (fractional / 0.25) * 1.2
    final = base_risk_pct * scale * confidence_mult * dd_mult * regime_multiplier

    return max(0.002, min(0.025, final))


def compute_portfolio_var(open_orders: list, atr_by_symbol: dict, price_by_symbol: dict) -> float:
    """
    95% 1-day VaR estimate. VaR = Σ(notional × atr_pct × 1.65).
    atr_by_symbol: {symbol: atr_value}
    price_by_symbol: {symbol: current_price}
    Returns dollar VaR.
    """
    total_var = 0.0
    for order in open_orders:
        sym = order['symbol']
        notional = float(order.get('amount_usdt', 0))
        atr = atr_by_symbol.get(sym)
        price = price_by_symbol.get(sym) or float(order.get('entry_price', 1))
        atr_pct = (atr / price) if (atr and price > 0) else 0.02
        total_var += notional * atr_pct * 1.65
    return round(total_var, 2)


def compute_alpha(strategy_return_pct: float, benchmark_return_pct: float) -> float:
    """Alpha = strategy return - benchmark (BTC buy-and-hold) return over same period."""
    return round(strategy_return_pct - benchmark_return_pct, 2)


def compute_peak_drawdown(equity_curve: list) -> float:
    """
    Max drawdown from equity curve (list of float values, e.g. daily balance snapshots).
    Returns max drawdown as a positive percentage.
    """
    if not equity_curve or len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 2)


def compute_sharpe(returns: list, risk_free_rate: float = 0.0) -> Optional[float]:
    """
    Annualised Sharpe ratio from a list of periodic return percentages.
    Assumes returns are daily. Returns None if fewer than 5 data points.
    """
    if not returns or len(returns) < 5:
        return None
    n = len(returns)
    mean_r = sum(returns) / n
    variance = sum((r - mean_r) ** 2 for r in returns) / n
    std = math.sqrt(variance)
    if std == 0:
        return None
    daily_sharpe = (mean_r - risk_free_rate) / std
    return round(daily_sharpe * math.sqrt(365), 2)
