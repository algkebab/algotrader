"""
Trading analytics: closed orders joined with AI signals and OpenAI-powered performance report.
Used by Messenger for the 'analytics' command.
"""
import json
import os

from dotenv import load_dotenv
from openai import OpenAI

from shared import db as shared_db

load_dotenv()


def _period_label(period: str) -> str:
    """Return human-readable period label."""
    return {
        "today": "Today",
        "last": "Last day",
        "yesterday": "Yesterday",
        "week": "Last 7 days",
        "month": "Last 30 days",
        "all": "All time",
    }.get(period, period)


ANALYTICS_SYSTEM_PROMPT = """You are a Senior Quant Researcher and Market Microstructure Expert. Your mission is to perform a rigorous post-trade forensic analysis.

For each trade, you must synthesize the 'AI Signal Reason', the 'Market Context at Entry', and the 'Trade Outcome' to identify structural edges or flaws.

CRITICAL ANALYSIS DIMENSIONS:
1. MARKET REGIME: Analyze if the trade occurred during high-volatility expansion, low-liquidity weekend range, or a trending/mean-reverting environment.
2. TIMING & SESSION: Evaluate the impact of the trading session (Asia, London, NY) and time of day on the trade's success.
3. ADVERSE EXCURSION: Analyze the 'Max Adverse Excursion' (how deep the price went against the trade) vs. the 'Stop Loss' to see if stops are being hunted or are mathematically too tight for the asset's ATR.
4. AI REASONING VALIDITY: Cross-reference the AI's technical justification (e.g., "Volume expansion") with the actual candle data during the trade. Detect 'AI Hallucinations' where the AI saw a pattern that the market immediately invalidated.
5. CORRELATION: Check if multiple losses occurred simultaneously across different assets (systemic market dump vs. idiosyncratic asset failure).

OUTPUT REQUIREMENTS:
- PERFORMANCE METRICS: Win Rate, Profit Factor, and Average Drawdown.
- SYSTEMIC FLAWS: Identify exactly which market conditions (e.g., "Post-pump RSI > 65 on 1H") lead to the most toxic trades.
- 3 ACTIONABLE UPDATES: Provide precise, data-driven changes to filter.py (thresholds) or brain.py (logic) to filter out these specific losing patterns."""


def generate_performance_report(period: str = "today") -> str:
    """
    Fetch closed orders with signals for the period, send summary to OpenAI, return formatted report.
    period: 'today'|'last'|'yesterday'|'week'|'month'|'all'.
    Returns a string suitable for Telegram (Win Rate, Total PnL, Strategic Insights, Recommended tweaks).
    """
    try:
        with shared_db.get_connection() as conn:
            shared_db.init_schema(conn)
            trades = shared_db.get_closed_orders_with_signals(conn, period)
            stats = shared_db.get_closed_orders_stats(conn, period)
    except Exception as e:
        return f"❌ Could not load analytics data: {e}"

    if not trades:
        period_label = _period_label(period)
        return f"📊 Analytics ({period_label})\n\nNo closed trades in this period."

    total_pnl = stats["total_pnl"]
    count = stats["count"]
    wins = stats["count_successful"]
    win_rate_pct = (wins / count * 100) if count else 0
    count_sl = stats["count_sl"]
    count_tp = stats["count_tp"]

    gross_profit = sum(t["pnl_usdt"] for t in trades if (t.get("pnl_usdt") or 0) > 0)
    gross_loss = abs(sum(t["pnl_usdt"] for t in trades if (t.get("pnl_usdt") or 0) < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

    summary = {
        "period": period,
        "total_trades": count,
        "win_rate_pct": round(win_rate_pct, 1),
        "total_pnl_usdt": round(total_pnl, 2),
        "profit_factor": profit_factor,
        "count_stop_loss": count_sl,
        "count_take_profit": count_tp,
        "trades": [
            {
                "symbol": t["symbol"],
                "strategy": t.get("strategy_name") or "—",
                # AI verdict context
                "ai_reason": t.get("ai_reason") or "—",
                "ai_confidence": t.get("ai_confidence"),
                "ai_setup_grade": t.get("ai_setup_grade"),
                # Entry indicators
                "rsi_at_entry": t.get("rsi_at_entry"),
                "rvol_at_entry": t.get("rvol_at_entry"),
                "atr_at_entry": t.get("atr_at_entry"),
                "ema_alignment_15m": t.get("ema_alignment_15m"),
                "ema_alignment_1h": t.get("ema_alignment_1h"),
                "bb_pct_b_15m": t.get("bb_pct_b_15m"),
                "btc_bias_at_entry": t.get("btc_bias_at_entry"),
                # Trade outcome
                "entry_price": t["entry_price"],
                "exit_price": t.get("exit_price"),
                "pnl_usdt": t.get("pnl_usdt"),
                "pnl_percent": t.get("pnl_percent"),
                "hours_held": t.get("hours_held"),
                "close_reason": t.get("close_reason") or "—",
                "mfe_pct": t.get("mfe_pct"),
                "mae_pct": t.get("mae_pct"),
                "opened_at": t.get("opened_at"),
                "closed_at": t.get("closed_at"),
            }
            for t in trades
        ],
    }

    payload_str = json.dumps(summary, indent=2)

    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": ANALYTICS_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Post-trade summary below. Perform the forensic analysis per your instructions.\n\n"
                        "Key fields available per trade:\n"
                        "- ai_setup_grade (A/B/C): the AI's own entry quality assessment\n"
                        "- ai_confidence (0-100): AI certainty at time of BUY signal\n"
                        "- btc_bias_at_entry: macro context (BULLISH_TAILWIND/NEUTRAL/BEARISH_HEADWIND/STRONG_BEARISH)\n"
                        "- mae_pct: max adverse excursion — how far price moved against the trade before close\n"
                        "- mfe_pct: max favorable excursion — the best unrealized gain during the trade\n"
                        "- ema_alignment_15m / ema_alignment_1h: trend alignment at entry\n"
                        "- atr_at_entry: volatility measure used for stop sizing\n"
                        "- hours_held: trade duration\n\n"
                        "Output: (1) PERFORMANCE METRICS — Win Rate, Profit Factor, Avg MAE vs SL width; "
                        "(2) SYSTEMIC FLAWS — cluster losing trades by btc_bias, setup_grade, session, "
                        "ema_alignment, and RSI to identify toxic entry conditions; "
                        "(3) 3 ACTIONABLE UPDATES — precise threshold or logic changes to filter.py or brain.py "
                        "backed by the data patterns you found.\n\n"
                        + payload_str
                    ),
                },
            ],
            timeout=60,
        )
        content = (response.choices[0].message.content or "").strip()
    except Exception as e:
        return (
            f"📊 Analytics ({_period_label(period)})\n\n"
            f"📋 Trades: {count} · Win rate: {win_rate_pct:.0f}% · PnL: {total_pnl:+.2f} USDT\n\n"
            f"❌ AI analysis failed: {e}"
        )

    pf_str = f" · PF: {profit_factor:.2f}" if profit_factor is not None else ""
    header = (
        f"📊 Analytics ({_period_label(period)})\n\n"
        f"📋 Trades: {count} · Win rate: {win_rate_pct:.0f}% · PnL: {total_pnl:+.2f} USDT{pf_str}\n"
        f"🟢 TP: {count_tp} · 🔴 SL: {count_sl}\n\n"
    )
    return header + content
