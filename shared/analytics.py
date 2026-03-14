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


ANALYTICS_SYSTEM_PROMPT = (
    "You are a Senior Quant Researcher. Your mission is to perform a post-trade analysis. "
    "Compare the AI's original 'reason' for entry with the actual market outcome. "
    "Identify systematic flaws (e.g., entering during high RSI, tight stops, or fake breakouts). "
    "Provide 3 actionable changes to the bot's configuration to improve the Profit Factor."
)


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

    summary = {
        "period": period,
        "total_trades": count,
        "win_rate_pct": round(win_rate_pct, 1),
        "total_pnl_usdt": round(total_pnl, 2),
        "count_stop_loss": count_sl,
        "count_take_profit": count_tp,
        "trades": [
            {
                "symbol": t["symbol"],
                "strategy": t.get("strategy_name") or "—",
                "ai_reason": (t.get("ai_reason") or "—")[:300],
                "rsi_at_entry": t.get("rsi_at_entry"),
                "rvol_at_entry": t.get("rvol_at_entry"),
                "entry_price": t["entry_price"],
                "exit_price": t.get("exit_price"),
                "pnl_usdt": t.get("pnl_usdt"),
                "pnl_percent": t.get("pnl_percent"),
                "close_reason": t.get("close_reason") or "—",
                "opened_at": t.get("opened_at"),
                "closed_at": t.get("closed_at"),
                "mfe_pct": t.get("mfe_pct"),
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
                        "Post-trade summary below. Identify the Top 3 Profit Killers and suggest "
                        "specific code/parameter adjustments for filter.py and brain.py. "
                        "Reply in clear sections: Strategic Insights, then Recommended Tweaks (bulleted).\n\n"
                        + payload_str
                    ),
                },
            ],
        )
        content = (response.choices[0].message.content or "").strip()
    except Exception as e:
        return (
            f"📊 Analytics ({_period_label(period)})\n\n"
            f"📋 Trades: {count} · Win rate: {win_rate_pct:.0f}% · PnL: {total_pnl:+.2f} USDT\n\n"
            f"❌ AI analysis failed: {e}"
        )

    header = (
        f"📊 Analytics ({_period_label(period)})\n\n"
        f"📋 Trades: {count} · Win rate: {win_rate_pct:.0f}% · PnL: {total_pnl:+.2f} USDT\n"
        f"🟢 TP: {count_tp} · 🔴 SL: {count_sl}\n\n"
    )
    return header + content
