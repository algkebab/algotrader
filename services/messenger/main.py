import asyncio
import json
import math
import os
import sys
import redis
import time
from datetime import datetime, timezone, timedelta

import ccxt
from dotenv import load_dotenv

# Allow importing shared.db (project root or /app in Docker)
_this_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_this_dir, "..", "..")) if os.path.basename(_this_dir) == "messenger" else _this_dir
if _root not in sys.path:
    sys.path.insert(0, _root)

from shared import analytics as shared_analytics
from shared import config as shared_config
from shared import db as shared_db
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TimedOut
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

load_dotenv()

# Longer timeouts for Telegram API (default is 5s; avoids TimedOut on slow networks)
TELEGRAM_READ_TIMEOUT = 30.0
TELEGRAM_WRITE_TIMEOUT = 30.0
TELEGRAM_CONNECT_TIMEOUT = 30.0
TELEGRAM_POOL_TIMEOUT = 15.0
# Pool size > 1 so polling + reply_text + send_telegram_msg can run without Pool timeout
TELEGRAM_CONNECTION_POOL_SIZE = 8
SEND_MESSAGE_RETRIES = 2
SEND_MESSAGE_RETRY_DELAY = 2.0

# System settings are stored in DB (shared/db.py settings table), not Redis.
# Keys we never delete on "clear redis" (empty: no system keys in Redis)
REDIS_SYSTEM_KEYS = frozenset()

# Allowed values for "strategy <name>"
STRATEGY_VALUES = frozenset({"conservative", "aggressive", "reversal"})
# Allowed values for "stats <value>"
STATS_VALUES = frozenset({"today", "yesterday", "week", "month", "all"})
# Allowed values for "analytics <period>"
ANALYTICS_VALUES = frozenset({"last", "today", "week", "month"})

# Data keys and patterns to clear on "clear redis"
REDIS_DATA_KEYS = [
    "market_data",
    "filtered_candidates",
    "signals",
    "trade_commands",
    "active_trades",
    "notifications",
]
REDIS_DATA_PATTERNS = ["analyzed:*", "last_vol:*", "cache:brain_price:*"]

def _normalize_symbol(s: str) -> str:
    """Normalize to BASE/USDT (e.g. BTCUSDT or btc/usdt -> BTC/USDT)."""
    s = (s or "").strip().upper()
    if not s:
        return s
    if "/" in s:
        return s
    if s.endswith("USDT"):
        return s[:-4] + "/USDT"
    return s + "/USDT"

HELP_MESSAGE = f"""🛠 *Algotrader — Commands*

📌 *Pipeline*
⏸️ • *stop* — Pause Filter & Brain (no filtering, no AI). Scout, Executor, Monitor keep running.
▶️ • *start* — Resume Filter & Brain.

🤖 *Autopilot*
🟢 • *autopilot on* — Auto-place orders on BUY (max set by orders set max). No Buy button. Resumes pipeline if paused.
🔴 • *autopilot off* — Stop auto orders. Buy button shown again on BUY signals.

🧹 *Data*
🗑️ • *clear redis* — Clear queues and cache. Keeps system settings.

📊 *Info & Trading*
📈 • *status* — Pipeline, autopilot, WAIT signals.
📢 • *signal wait on* — Send Telegram notifications for WAIT verdicts.
📢 • *signal wait off* — Do not notify for WAIT verdicts (default).
📋 • *orders* — List open orders from DB.
🔒 • *orders close* <symbol> — Manually close open order (e.g. orders close BTC/USDT). Updates balance.
🔢 • *orders set max* <number> — Max open orders for autopilot (e.g. orders set max 15). Default {shared_config.MAX_OPEN_ORDERS_DEFAULT}. Min {shared_config.MAX_OPEN_ORDERS_MIN}, max {shared_config.MAX_OPEN_ORDERS_MAX}.
💰 • *balance* — Wallet, today PnL, and change since last check.
💵 • *set balance* <amount> — Set USDT in DB (e.g. set balance 100.50).
📊 • *set symbols* <number> — Top N symbols by volume (e.g. set symbols {shared_config.MAX_SYMBOLS_DEFAULT}). Min {shared_config.MAX_SYMBOLS_MIN}, max {shared_config.MAX_SYMBOLS_MAX}. Default {shared_config.MAX_SYMBOLS_DEFAULT}.
🛡️ • *strategy* <name> — Strategy: conservative, aggressive, reversal. Default: CONSERVATIVE.
🕒 • *set timezone* <offset> — Local timezone offset vs UTC in hours (e.g. set timezone +2, set timezone -5, set timezone 5.5). Affects timestamps in bot messages only.
📊 • *stats* <value> — Closed orders stats: today, yesterday, week, month, all. Default: today.
📊 • *analytics* <period> — AI performance report: last, today, week, month. Win rate, PnL, insights, recommended tweaks.
❓ • *help* — This message."""

def _ts():
    """Returns current UTC timestamp for logging."""
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

class Messenger:
    def __init__(self):
        # Redis setup: connects to 'redis' container in Docker or 'localhost' locally
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        self.db.ping()
        print(f"[{_ts()}] Messenger: Connected to Redis at {redis_host}:6379")
        
        # Telegram setup
        self.bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID')
        
        if not self.bot_token or not self.chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not found in environment variables")

        # Two separate HTTPXRequest instances: Application must not share with Bot.
        # "async with self.bot" in send_telegram_msg calls request.shutdown() on exit, which would
        # break polling if we shared one request → "This HTTPXRequest is not initialized!".
        _req_opts = dict(
            connection_pool_size=TELEGRAM_CONNECTION_POOL_SIZE,
            connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
            read_timeout=TELEGRAM_READ_TIMEOUT,
            write_timeout=TELEGRAM_WRITE_TIMEOUT,
            pool_timeout=TELEGRAM_POOL_TIMEOUT,
        )
        self._request_app = HTTPXRequest(**_req_opts)
        self._request_bot = HTTPXRequest(**_req_opts)
        self.bot = Bot(token=self.bot_token, request=self._request_bot)
        print(f"[{_ts()}] Messenger: Bot initialized (chat_id={self.chat_id}, timeouts={TELEGRAM_READ_TIMEOUT}s)")

        # Exchange for current price (orders command); public API only
        self.exchange = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})

        self.application = (
            Application.builder()
            .token(self.bot_token)
            .request(self._request_app)
            .get_updates_request(self._request_app)
            .build()
        )
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))

    def _is_allowed_chat(self, chat_id):
        """Only the configured chat can send stop/start/status."""
        return str(chat_id) == str(self.chat_id)

    def _get_setting(self, key: str, default: str | None = None) -> str | None:
        """Read system setting from DB (not Redis)."""
        return shared_db.get_setting_value(key, default)

    def _set_setting(self, key: str, value: str) -> None:
        """Write system setting to DB (not Redis)."""
        with shared_db.get_connection() as conn:
            shared_db.init_schema(conn)
            shared_db.set_setting(conn, key, value)

    async def _safe_reply(self, update, text: str) -> None:
        """Reply to the user; log and continue on timeout so handler doesn't crash."""
        try:
            await update.message.reply_text(text)
        except TimedOut as e:
            print(f"[{_ts()}] Messenger: reply_text timed out (state already updated): {e}")

    async def _reply_orders(self, update) -> None:
        """Reply with list of current (open) orders from SQLite."""
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                rows = shared_db.get_open_orders(conn)
        except Exception as e:
            await self._safe_reply(update, f"❌ Could not read orders\n\n{e}")
            return
        if not rows:
            await self._safe_reply(update, "📋 Open orders\n\nNo open orders.")
            return
        lines = [f"📋 Open orders ({len(rows)})", ""]
        for i, o in enumerate(rows, 1):
            opened = self._format_iso_to_user_time(o.get("opened_at") or "")
            symbol = o["symbol"]
            entry = float(o["entry_price"])
            qty = float(o["quantity"])
            try:
                ticker = await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
                now_price = ticker.get("last")
                now_f = float(now_price) if now_price is not None else None
                now_str = f"{now_f:.4g}" if now_f is not None else "—"
                if now_f is not None:
                    unrealized = (now_f - entry) * qty
                    pnl_sign = "+" if unrealized >= 0 else ""
                    pnl_emoji = "📈" if unrealized >= 0 else "📉"
                    pnl_line = f"\n   {pnl_emoji} Unrealized PnL: {pnl_sign}{unrealized:.2f} USDT"
                else:
                    pnl_line = ""
            except Exception:
                now_str = "—"
                pnl_line = ""
            lines.append(
                f"{i}. {symbol}\n"
                f"   💵 Entry: {o['entry_price']} · Qty: {o['quantity']}\n"
                f"   📈 Now: {now_str}\n"
                f"   🎯 TP: {o['tp_price']} · 🛑 SL: {o['sl_price']}"
                f"{pnl_line}\n"
                f"   📅 {opened}"
            )
            if i < len(rows):
                lines.append("")
        await self._safe_reply(update, "\n".join(lines))

    async def _handle_orders_close(self, update, text: str) -> None:
        """Close open order for symbol and update balance (manual close). Usage: orders close <symbol>."""
        rest = text[len("orders close "):].strip()
        if not rest:
            await self._safe_reply(
                update,
                "📋 Orders close\n\nUsage: orders close <symbol>\nExample: orders close BTC/USDT",
            )
            return
        symbol = _normalize_symbol(rest)
        if not symbol or symbol == "/USDT":
            await self._safe_reply(update, "❌ Invalid symbol. Use e.g. BTC/USDT or BTCUSDT.")
            return
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                row = shared_db.get_open_order_for_symbol(conn, symbol)
            if not row:
                await self._safe_reply(update, f"❌ No open order for {symbol}.")
                return
            try:
                ticker = await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
                close_price = float(ticker.get("last", 0))
            except Exception as e:
                await self._safe_reply(update, f"❌ Could not get price for {symbol}\n\n{e}")
                return
            entry_price = float(row["entry_price"])
            qty = float(row["quantity"])
            order_id = row["id"]
            notional_usdt = float(row["amount_usdt"])
            gross_pnl_usdt = (close_price - entry_price) * qty
            gross_pnl_percent = ((close_price / entry_price) - 1) * 100
            reason = "Manual close"
            margin_usdt = notional_usdt / shared_config.LEVERAGE

            # Fees and interest for manual close
            exit_notional = qty * close_price
            exit_fee_usd = exit_notional * shared_config.BINANCE_SPOT_FEE
            borrowed = row.get("borrowed_amount")
            if borrowed is not None:
                try:
                    borrowed = float(borrowed)
                except (TypeError, ValueError):
                    borrowed = None
            if borrowed is None:
                borrowed = max(0.0, notional_usdt - margin_usdt)
            rate = row.get("hourly_interest_rate")
            if rate is not None:
                try:
                    rate = float(rate)
                except (TypeError, ValueError):
                    rate = shared_config.HOURLY_MARGIN_INTEREST_RATE
            else:
                rate = shared_config.HOURLY_MARGIN_INTEREST_RATE
            try:
                opened_at = datetime.fromisoformat(row["opened_at"].replace("Z", "+00:00"))
                hours_held = max(1, math.ceil((datetime.now(timezone.utc) - opened_at).total_seconds() / 3600.0))
            except Exception:
                hours_held = 1
            margin_interest_paid = borrowed * rate * hours_held
            net_pnl_usdt = gross_pnl_usdt - exit_fee_usd - margin_interest_paid
            net_pnl_pct = (net_pnl_usdt / margin_usdt * 100) if margin_usdt else 0.0

            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                shared_db.update_order_closed(
                    conn, order_id,
                    pnl_usdt=round(net_pnl_usdt, 2),
                    pnl_percent=round(net_pnl_pct, 2),
                    close_reason=reason,
                    exit_fee_usd=float(exit_fee_usd),
                    margin_interest_paid=float(margin_interest_paid),
                    net_pnl_pct=round(net_pnl_pct, 2),
                )
                bal = shared_db.get_balance(conn, "USDT")
                shared_db.set_balance(conn, "USDT", bal + margin_usdt + net_pnl_usdt)

            self.db.rpush("notifications", json.dumps({
                "type": "trade_closed",
                "data": {
                    "symbol": symbol,
                    "entry": entry_price,
                    "exit": close_price,
                    "pnl_usdt": round(net_pnl_usdt, 2),
                    "pnl_percent": round(net_pnl_pct, 2),
                    "reason": reason,
                },
            }))
            print(f"[{_ts()}] Messenger: Manual close {symbol} at {close_price}. PnL: {net_pnl_usdt:.2f} USDT")
            await self._safe_reply(
                update,
                f"✅ Order closed\n\n{symbol}\n"
                f"💵 PnL: {net_pnl_usdt:+.2f} USDT ({net_pnl_pct:+.2f}%)\n"
                f"Exit: {close_price}",
            )
        except Exception as e:
            print(f"[{_ts()}] Messenger: orders close failed: {e}")
            await self._safe_reply(update, f"❌ Close failed\n\n{e}")

    def _get_open_order_count(self) -> int:
        """Return number of open orders from DB (for autopilot cap)."""
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                return len(shared_db.get_open_orders(conn))
        except Exception:
            return 0

    def _get_max_open_orders(self) -> int:
        """Return max simultaneous open orders (from DB, default shared_config.MAX_OPEN_ORDERS_DEFAULT)."""
        val = self._get_setting(shared_config.SYSTEM_KEY_MAX_OPEN_ORDERS)
        if val is None or not str(val).isdigit():
            return shared_config.MAX_OPEN_ORDERS_DEFAULT
        n = int(val)
        return max(shared_config.MAX_OPEN_ORDERS_MIN, min(shared_config.MAX_OPEN_ORDERS_MAX, n))

    async def _handle_orders_set_max(self, update, text: str) -> None:
        """Set max simultaneous open orders. Usage: orders set max <number> (e.g. orders set max 15)."""
        rest = text[len("orders set max "):].strip()
        if not rest:
            await self._safe_reply(
                update,
                f"📋 Orders set max\n\nUsage: orders set max <number>\n"
                f"Min {shared_config.MAX_OPEN_ORDERS_MIN}, max {shared_config.MAX_OPEN_ORDERS_MAX}. Default {shared_config.MAX_OPEN_ORDERS_DEFAULT}.\n"
                "Example: orders set max 15",
            )
            return
        try:
            n = int(rest)
        except ValueError:
            await self._safe_reply(
                update,
                f"❌ Invalid number. Use an integer between {shared_config.MAX_OPEN_ORDERS_MIN} and {shared_config.MAX_OPEN_ORDERS_MAX}.",
            )
            return
        if n < shared_config.MAX_OPEN_ORDERS_MIN or n > shared_config.MAX_OPEN_ORDERS_MAX:
            await self._safe_reply(
                update,
                f"❌ Out of range. Use {shared_config.MAX_OPEN_ORDERS_MIN}–{shared_config.MAX_OPEN_ORDERS_MAX}.",
            )
            return
        self._set_setting(shared_config.SYSTEM_KEY_MAX_OPEN_ORDERS, str(n))
        print(f"[{_ts()}] Messenger: max_open_orders set to {n}")
        await self._safe_reply(
            update,
            f"✅ Max open orders updated\n\n📋 Autopilot will allow up to {n} simultaneous open orders.",
        )

    def _get_timezone_offset_minutes(self) -> int:
        """Return timezone offset in minutes from UTC (from Redis, default 0)."""
        val = self._get_setting(shared_config.SYSTEM_KEY_TIMEZONE_OFFSET_MIN)
        if val is None:
            return 0
        try:
            return int(val)
        except (ValueError, TypeError):
            return 0

    def _format_user_time(self, dt_utc: datetime) -> str:
        """Format a UTC datetime into user's configured timezone."""
        offset_min = self._get_timezone_offset_minutes()
        local_dt = dt_utc + timedelta(minutes=offset_min)
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")

    def _format_iso_to_user_time(self, iso_str: str) -> str:
        """Parse UTC ISO timestamp string and return formatted in user's timezone. Returns '—' if empty/invalid."""
        if not (iso_str and iso_str.strip()):
            return "—"
        try:
            dt = datetime.fromisoformat(iso_str.strip().replace("Z", "+00:00"))
            return self._format_user_time(dt)
        except Exception:
            return (iso_str[:19].replace("T", " ") if len(iso_str) >= 19 else iso_str) or "—"

    async def _reply_balance(self, update) -> None:
        """Reply with current USDT balance, today's PnL, day PnL delta, and unrealized PnL per open order."""
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                current = shared_db.get_balance(conn, "USDT")
                today_pnl = shared_db.get_today_closed_pnl(conn)
                open_orders = shared_db.get_open_orders(conn)
        except Exception as e:
            await self._safe_reply(update, f"❌ Could not read balance: {e}")
            return
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()
        last_pnl_str = self._get_setting(shared_config.SYSTEM_KEY_BALANCE_LAST_DAY_PNL)
        last_check = self._get_setting(shared_config.SYSTEM_KEY_BALANCE_LAST_CHECK)
        if last_pnl_str is not None and last_check is not None:
            try:
                last_pnl = float(last_pnl_str)
                delta_pnl = today_pnl - last_pnl
                if delta_pnl > 0:
                    change_emoji = "📈"
                    change_text = f"+{delta_pnl:.2f} USDT since last check"
                elif delta_pnl < 0:
                    change_emoji = "📉"
                    change_text = f"{delta_pnl:.2f} USDT since last check"
                else:
                    change_emoji = "➡️"
                    change_text = "No change since last check"
                last_str = self._format_iso_to_user_time(last_check)
                diff_line = f"{change_emoji} {change_text}\n🕐 Last check: {last_str}"
            except (ValueError, TypeError):
                diff_line = "🕐 Last check: —"
        else:
            diff_line = "🆕 First check — no previous day PnL to compare."
        self._set_setting(shared_config.SYSTEM_KEY_BALANCE_LAST_DAY_PNL, f"{today_pnl:.2f}")
        self._set_setting(shared_config.SYSTEM_KEY_BALANCE_LAST_CHECK, now_iso)
        pnl_sign = "+" if today_pnl >= 0 else ""
        pnl_emoji = "📈" if today_pnl >= 0 else "📉" if today_pnl < 0 else "➡️"
        parts = [
            f"💰 Balance",
            f"\n\n",
            "",
            f"💵 Wallet: {current:.2f} USDT",
            f"{pnl_emoji} Today PnL: {pnl_sign}{today_pnl:.2f} USDT",
            "",
            diff_line,
        ]
        total_unrealized = 0.0
        if open_orders:
            parts.append("")
            parts.append("📋 Open positions (unrealized PnL)")
            for row in open_orders:
                symbol = row["symbol"]
                entry = float(row["entry_price"])
                qty = float(row["quantity"])
                try:
                    ticker = await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
                    price = float(ticker.get("last", 0))
                    unrealized = (price - entry) * qty
                    total_unrealized += unrealized
                    sign = "+" if unrealized >= 0 else ""
                    em = "📈" if unrealized >= 0 else "📉"
                    parts.append(f"  {em} {symbol}: {sign}{unrealized:.2f} USDT")
                except Exception:
                    parts.append(f"  ➡️ {symbol}: —")
            if open_orders and total_unrealized != 0:
                sign = "+" if total_unrealized >= 0 else ""
                parts.append(f"  Total unrealized: {sign}{total_unrealized:.2f} USDT")
        msg = "\n".join(parts)
        await self._safe_reply(update, msg)

    async def _handle_set_balance(self, update, text: str) -> None:
        """Set USDT balance in DB. Usage: set balance <amount> (e.g. set balance 100.50)."""
        rest = text[len("set balance "):].strip()
        if not rest:
            await self._safe_reply(
                update,
                "💵 Set balance\n\nUsage: set balance <amount>\nExample: set balance 100.50",
            )
            return
        try:
            amount = float(rest)
        except ValueError:
            await self._safe_reply(
                update,
                "❌ Invalid amount\n\nUse a number, e.g. set balance 100.50",
            )
            return
        if amount < 0:
            await self._safe_reply(update, "❌ Amount must be ≥ 0.")
            return
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                shared_db.set_balance(conn, "USDT", amount)
            print(f"[{_ts()}] Messenger: Balance set to {amount:.2f} USDT")
            await self._safe_reply(update, f"✅ Balance updated\n\n💰 USDT: {amount:.2f}")
        except Exception as e:
            await self._safe_reply(update, f"❌ Could not set balance\n\n{e}")

    async def _handle_set_symbols(self, update, text: str) -> None:
        """Set number of top symbols by volume Scout fetches. Usage: set symbols <number> (5–200)."""
        rest = text[len("set symbols "):].strip()
        if not rest:
            await self._safe_reply(
                update,
                f"📈 Set symbols\n\nUsage: set symbols <number>\n"
                f"Min {shared_config.MAX_SYMBOLS_MIN}, max {shared_config.MAX_SYMBOLS_MAX}. Example: set symbols 50",
            )
            return
        try:
            n = int(rest)
        except ValueError:
            await self._safe_reply(update, f"❌ Invalid number. Use an integer between {shared_config.MAX_SYMBOLS_MIN} and {shared_config.MAX_SYMBOLS_MAX}.")
            return
        if n < shared_config.MAX_SYMBOLS_MIN or n > shared_config.MAX_SYMBOLS_MAX:
            await self._safe_reply(
                update,
                f"❌ Out of range. Use {shared_config.MAX_SYMBOLS_MIN}–{shared_config.MAX_SYMBOLS_MAX}.",
            )
            return
        self._set_setting(shared_config.SYSTEM_KEY_MAX_SYMBOLS, str(n))
        print(f"[{_ts()}] Messenger: max_symbols set to {n}")
        await self._safe_reply(update, f"✅ Symbols updated\n\n📈 Scout will fetch top {n} symbols by volume.")

    async def _handle_strategy(self, update, text: str) -> None:
        """Set strategy. Usage: strategy <name>. Values: conservative, aggressive, reversal."""
        rest = text[len("strategy "):].strip().lower() if text.startswith("strategy ") else ""
        if not rest:
            await self._safe_reply(
                update,
                "🎯 Strategy\n\nUsage: strategy <name>\n"
                "Values: conservative, aggressive, reversal.\n"
                "Default: CONSERVATIVE.",
            )
            return
        if rest not in STRATEGY_VALUES:
            await self._safe_reply(
                update,
                f"❌ Invalid strategy. Use one of: conservative, aggressive, reversal.",
            )
            return
        name_upper = rest.upper()
        self._set_setting(shared_config.SYSTEM_KEY_STRATEGY, name_upper)
        print(f"[{_ts()}] Messenger: strategy set to {name_upper}")
        await self._safe_reply(update, f"🎯 Strategy changed to {name_upper}. Filters updated.")

    async def _handle_set_timezone(self, update, text: str) -> None:
        """Set timezone offset in hours from UTC. Usage: set timezone <offset>, e.g. +2, -5, 1.5."""
        rest = text[len("set timezone "):].strip()
        if not rest:
            offset_min = self._get_timezone_offset_minutes()
            hours = offset_min / 60
            await self._safe_reply(
                update,
                "🕒 Set timezone\n\n"
                f"Current offset: UTC{hours:+.1f} hours\n\n"
                "Usage: set timezone <offset>\n"
                "Examples:\n"
                "  set timezone 0     → UTC\n"
                "  set timezone +2    → UTC+2\n"
                "  set timezone -5    → UTC-5\n"
                "  set timezone 5.5   → UTC+5:30\n",
            )
            return
        try:
            hours = float(rest.replace("UTC", "").replace("utc", ""))
        except ValueError:
            await self._safe_reply(
                update,
                "❌ Invalid offset\n\nUse a number of hours, e.g. 0, +2, -5, 5.5",
            )
            return
        # Clamp to reasonable world timezones
        if hours < -12 or hours > 14:
            await self._safe_reply(update, "❌ Out of range. Use between -12 and +14 hours.")
            return
        offset_min = int(hours * 60)
        self._set_setting(shared_config.SYSTEM_KEY_TIMEZONE_OFFSET_MIN, str(offset_min))
        print(f"[{_ts()}] Messenger: timezone offset set to {offset_min} minutes")
        await self._safe_reply(
            update,
            f"✅ Timezone updated\n\n🕒 New offset: UTC{hours:+.1f}",
        )

    async def _handle_stats(self, update, text: str) -> None:
        """Reply with closed orders statistics. Usage: stats [today|yesterday|week|month|all]. Default: today."""
        rest = text[len("stats"):].strip().lower() if text.startswith("stats") else ""
        period = rest if rest in STATS_VALUES else "today"
        period_label = {"today": "Today", "yesterday": "Yesterday", "week": "Last 7 days", "month": "Last 30 days", "all": "All time"}[period]
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                s = shared_db.get_closed_orders_stats(conn, period)
        except Exception as e:
            await self._safe_reply(update, f"❌ Could not read stats: {e}")
            return
        if s["count"] == 0:
            await self._safe_reply(
                update,
                f"📊 Stats ({period_label})\n"
                f"\n"
                f"No closed orders in this period.",
            )
            return
        total_pnl = s["total_pnl"]
        pnl_sign = "+" if total_pnl >= 0 else ""
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"
        success_pct = (s["count_successful"] / s["count"] * 100) if s["count"] else 0
        parts = [
            f"📊 Stats ({period_label})",
            "\n",
            f"📋 Closed orders: {s['count']}",
            f"{pnl_emoji} Total PnL: {pnl_sign}{total_pnl:.2f} USDT",
            f"✅ Successful (PnL > 0): {s['count_successful']} ({success_pct:.0f}%)",
            "",
            "Closed by:",
            f"  🟢 Take profit: {s['count_tp']}",
            f"  🔴 Stop loss: {s['count_sl']}",
            f"  ✋ Manual: {s['count_manual']}",
        ]
        await self._safe_reply(update, "\n".join(parts))

    async def _handle_analytics(self, update, text: str) -> None:
        """Reply with AI-generated performance report. Usage: analytics [last|today|week|month]. Default: today."""
        rest = text[len("analytics"):].strip().lower() if text.startswith("analytics") else ""
        period = rest if rest in ANALYTICS_VALUES else "today"
        period_label = {"last": "Last day", "today": "Today", "week": "Weekly", "month": "Monthly"}[period]
        await self._safe_reply(
            update,
            f"📊 Generating AI {period_label} Report… please wait.",
        )
        try:
            report = await asyncio.to_thread(shared_analytics.generate_performance_report, period)
        except Exception as e:
            print(f"[{_ts()}] Messenger: analytics failed: {e}")
            await self._safe_reply(update, f"❌ Analytics failed\n\n{e}")
            return
        await self._safe_reply(update, report)

    def _clear_redis_data(self) -> int:
        """Delete all Redis data keys and pattern keys; never touch REDIS_SYSTEM_KEYS. Returns count deleted."""
        deleted = 0
        for key in REDIS_DATA_KEYS:
            deleted += self.db.delete(key)
        for pattern in REDIS_DATA_PATTERNS:
            for key in self.db.scan_iter(match=pattern):
                if key not in REDIS_SYSTEM_KEYS:
                    deleted += self.db.delete(key)
        return deleted

    async def handle_text(self, update, context):
        """Handle stop / start / status commands from Telegram."""
        if not self._is_allowed_chat(update.effective_chat.id):
            return
        text = (update.message.text or "").strip().lower()
        if text == "stop":
            self._set_setting(shared_config.SYSTEM_KEY_TRADING_PAUSED, "1")
            print(f"[{_ts()}] Messenger: Pipeline PAUSED (Filter & Brain stopped)")
            await self._safe_reply(
                update,
                "⏸️ Pipeline paused\n\n"
                "Filter & Brain stopped (no filtering, no AI).\n"
                "Scout, Executor, Monitor still running.\n\n"
                "👉 Send \"start\" to resume.",
            )
        elif text == "start":
            self._set_setting(shared_config.SYSTEM_KEY_TRADING_PAUSED, "0")
            print(f"[{_ts()}] Messenger: Pipeline RESUMED (Filter & Brain running)")
            await self._safe_reply(update, "▶️ Pipeline resumed\n\nFilter and Brain are running.")
        elif text == "clear redis":
            try:
                n = self._clear_redis_data()
                print(f"[{_ts()}] Messenger: Redis data cleared ({n} keys deleted)")
                await self._safe_reply(
                    update,
                    f"🧹 Redis cleared\n\n{n} keys deleted.",
                )
            except Exception as e:
                print(f"[{_ts()}] Messenger: clear redis failed: {e}")
                await self._safe_reply(update, f"❌ Clear Redis failed\n\n{e}")
        elif text == "status":
            try:
                paused = self._get_setting(shared_config.SYSTEM_KEY_TRADING_PAUSED)
                autopilot = self._get_setting(shared_config.SYSTEM_KEY_AUTOPILOT)
                symbols_val = self._get_setting(shared_config.SYSTEM_KEY_MAX_SYMBOLS)
                symbols_n = int(symbols_val) if symbols_val and str(symbols_val).isdigit() else shared_config.MAX_SYMBOLS_DEFAULT
                lines = [
                    "📊 Status",
                    "",
                    "📌 Pipeline: " + ("⏸️ paused (send \"start\" to resume)" if paused == "1" else "▶️ running"),
                    "🤖 Autopilot: " + ("ON (auto orders, no button)" if autopilot == "1" else "OFF (Buy button on signals)"),
                    f"📈 Symbols: top {symbols_n} by volume",
                    "⚙️ Model: 3x leverage · 0.1% taker fee · 0.001%/h margin interest",
                ]
                strategy_val = (self._get_setting(shared_config.SYSTEM_KEY_STRATEGY) or "CONSERVATIVE").strip().upper()
                if strategy_val not in {"CONSERVATIVE", "AGGRESSIVE", "REVERSAL"}:
                    strategy_val = "CONSERVATIVE"
                lines.append(f"🎯 Strategy: {strategy_val}")
                max_open = self._get_max_open_orders()
                open_count = self._get_open_order_count()
                lines.append(f"📋 Open orders: {open_count} / {max_open}")
                signal_wait = self._get_setting(shared_config.SYSTEM_KEY_SIGNAL_WAIT)
                lines.append("📢 WAIT signals: " + ("ON" if signal_wait == "1" else "OFF"))
                await self._safe_reply(update, "\n".join(lines))
            except Exception as e:
                print(f"[{_ts()}] Messenger: status failed: {e}")
                await self._safe_reply(update, f"❌ Status failed\n\n{e}")
        elif text == "autopilot on":
            self._set_setting(shared_config.SYSTEM_KEY_AUTOPILOT, "1")
            self._set_setting(shared_config.SYSTEM_KEY_TRADING_PAUSED, "0")
            print(f"[{_ts()}] Messenger: Autopilot ON (pipeline resumed if was paused)")
            await self._safe_reply(
                update,
                "🤖 Autopilot on\n\n"
                "BUY verdicts → auto orders. No Buy button.\n"
                "Pipeline resumed if it was paused.",
            )
        elif text == "autopilot off":
            self._set_setting(shared_config.SYSTEM_KEY_AUTOPILOT, "0")
            print(f"[{_ts()}] Messenger: Autopilot OFF (Buy button restored on signals)")
            await self._safe_reply(
                update,
                "🛑 Autopilot off\n\n"
                "No auto orders. Buy button shown again on BUY signals.",
            )
        elif text == "orders":
            await self._reply_orders(update)
        elif text.startswith("orders close "):
            await self._handle_orders_close(update, text)
        elif text.startswith("orders set max "):
            await self._handle_orders_set_max(update, text)
        elif text == "strategy":
            await self._handle_strategy(update, "strategy ")
        elif text.startswith("strategy "):
            await self._handle_strategy(update, text)
        elif text == "balance":
            await self._reply_balance(update)
        elif text.startswith("set balance "):
            await self._handle_set_balance(update, text)
        elif text.startswith("set symbols "):
            await self._handle_set_symbols(update, text)
        elif text.startswith("set timezone "):
            await self._handle_set_timezone(update, text)
        elif text == "signal wait on":
            self._set_setting(shared_config.SYSTEM_KEY_SIGNAL_WAIT, "1")
            print(f"[{_ts()}] Messenger: WAIT verdict notifications ON")
            await self._safe_reply(
                update,
                "📢 WAIT signals ON\n\nYou will receive Telegram notifications for WAIT verdicts.",
            )
        elif text == "signal wait off":
            self._set_setting(shared_config.SYSTEM_KEY_SIGNAL_WAIT, "0")
            print(f"[{_ts()}] Messenger: WAIT verdict notifications OFF")
            await self._safe_reply(
                update,
                "📢 WAIT signals OFF\n\nNo notifications for WAIT verdicts. BUY signals unchanged.",
            )
        elif text == "signal wait":
            signal_wait = self._get_setting(shared_config.SYSTEM_KEY_SIGNAL_WAIT)
            await self._safe_reply(
                update,
                "📢 WAIT signals: " + ("ON" if signal_wait == "1" else "OFF") + "\n\nUse *signal wait on* or *signal wait off* to change.",
            )
        elif text == "stats" or text.startswith("stats "):
            await self._handle_stats(update, text)
        elif text == "analytics" or text.startswith("analytics "):
            await self._handle_analytics(update, text)
        elif text == "help":
            await self._safe_reply(update, HELP_MESSAGE)

    async def handle_callback(self, update, context):
        """Handles button clicks (Buy commands) from Telegram UI."""
        query = update.callback_query
        print(f"[{_ts()}] Messenger: Callback received, data={query.data!r}")
        try:
            await query.answer()
        except Exception as e:
            print(f"[{_ts()}] Messenger: query.answer() failed (continuing): {e}")

        if query.data.startswith("buy:"):
            # Format: buy:SYMBOL:SIGNAL_ID[:stop_loss_pct:take_profit_pct]
            parts = query.data.split(":")
            symbol = parts[1]
            signal_id = parts[2] if len(parts) > 2 and parts[2] else None
            stop_loss_pct = float(parts[3]) if len(parts) > 3 and parts[3] else None
            take_profit_pct = float(parts[4]) if len(parts) > 4 and parts[4] else None
            strategy_name = (self._get_setting(shared_config.SYSTEM_KEY_STRATEGY) or "CONSERVATIVE").strip().upper()
            if strategy_name not in {"CONSERVATIVE", "AGGRESSIVE", "REVERSAL"}:
                strategy_name = "CONSERVATIVE"
            command = {
                "symbol": symbol,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "strategy_name": strategy_name,
                "signal_id": signal_id,
            }
            self.db.rpush("trade_commands", json.dumps(command))
            
            print(f"[{_ts()}] Messenger: Pushed trade_command to Redis: {symbol}")
            try:
                await query.edit_message_text(
                    text=f"{query.message.text}\n\n✅ _Command sent to Executor_",
                    parse_mode="Markdown",
                )
            except Exception as e:
                print(f"[{_ts()}] Messenger: edit_message_text error: {e}")

    async def send_telegram_msg(self, text, symbol=None, keyboard=None):
        """Helper to send Markdown messages with optional keyboards. Retries on timeout."""
        last_error = None
        for attempt in range(SEND_MESSAGE_RETRIES + 1):
            try:
                async with self.bot:
                    await self.bot.send_message(
                        chat_id=self.chat_id,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=keyboard,
                        disable_web_page_preview=True,
                    )
                return
            except TimedOut as e:
                last_error = e
                if attempt < SEND_MESSAGE_RETRIES:
                    print(f"[{_ts()}] Messenger: Telegram timeout (attempt {attempt + 1}), retrying in {SEND_MESSAGE_RETRY_DELAY}s...")
                    await asyncio.sleep(SEND_MESSAGE_RETRY_DELAY)
            except Exception as e:
                print(f"[{_ts()}] Messenger: Telegram API Error: {e}")
                return
        if last_error:
            print(f"[{_ts()}] Messenger: Telegram API Error (timed out after {SEND_MESSAGE_RETRIES + 1} attempts): {last_error}")

    async def listen_for_notifications(self):
        """Background task: Listens for trade execution results from Executor."""
        print(f"[{_ts()}] Messenger: Notification listener started (waiting for Executor updates)")
        while True:
            # BLPOP blocks until a notification appears in the queue
            notification = self.db.blpop('notifications', timeout=5)
            if notification:
                _, payload = notification
                note = json.loads(payload)

                if note['type'] == 'trade_confirmed':
                    d = note['data']
                    msg = (
                        f"✅ *Trade opened*\n\n"
                        f"📌 #{d['symbol'].replace('/', '')}\n\n"
                        f"💰 Entry: `{d['entry']}`\n"
                        f"🎯 Take profit: `{d['tp']}`\n"
                        f"🛑 Stop loss: `{d['sl']}`\n\n"
                        f"_Active — orders placed_"
                    )
                    await self.send_telegram_msg(msg)

                elif note['type'] == 'trade_skipped':
                    d = note.get('data', {})
                    symbol = d.get('symbol', '—')
                    reason = d.get('reason', 'Already have open order for this symbol')
                    msg = (
                        f"⏸️ *Order skipped*\n\n"
                        f"📌 {symbol}\n\n"
                        f"{reason}"
                    )
                    await self.send_telegram_msg(msg)

                elif note['type'] == 'trade_closed':
                    d = note['data']
                    result_emoji = "💰" if d['pnl_usdt'] >= 0 else "📉"
                    pnl_sign = "+" if d['pnl_usdt'] >= 0 else ""
                    lines = [
                        f"{result_emoji} *Trade closed*",
                        "",
                        f"📌 #{d['symbol'].replace('/', '')}",
                        f"📋 {d['reason']}",
                        "",
                        f"💵 Net PnL: `{pnl_sign}{d['pnl_usdt']}` USDT",
                        f"📈 PnL: `{pnl_sign}{d['pnl_percent']}`%",
                    ]
                    # Optional extended audit fields
                    gross_pnl_usdt = d.get("gross_pnl_usdt")
                    if gross_pnl_usdt is not None:
                        gross_sign = "+" if gross_pnl_usdt >= 0 else ""
                        lines.append(f"💰 Gross PnL: `{gross_sign}{gross_pnl_usdt}` USDT")
                    gross_pnl_percent = d.get("gross_pnl_percent")
                    if gross_pnl_percent is not None:
                        gsign = "+" if gross_pnl_percent >= 0 else ""
                        lines.append(f"📊 Gross PnL: `{gsign}{gross_pnl_percent}`%")
                    exit_fee = d.get("exit_fee_usd")
                    interest = d.get("margin_interest_paid")
                    if exit_fee is not None or interest is not None:
                        fee_str = f"{exit_fee:.4f}" if isinstance(exit_fee, (int, float)) else exit_fee
                        int_str = f"{interest:.4f}" if isinstance(interest, (int, float)) else interest
                        lines.append(f"💸 Fees: entry (inc.), exit `{fee_str}` USDT")
                        lines.append(f"🏦 Margin interest: `{int_str}` USDT")
                    net_roe = d.get("net_pnl_pct")
                    if net_roe is not None:
                        roe_sign = "+" if net_roe >= 0 else ""
                        lines.append(f"📈 ROE (on margin): `{roe_sign}{net_roe}`%")
                    hours_held = d.get("hours_held")
                    if hours_held is not None:
                        lines.append(f"⏱️ Time in trade: `{hours_held}` h")
                    strategy_name = d.get("strategy_name")
                    if strategy_name:
                        lines.append(f"🎯 Strategy: `{strategy_name}`")
                    lines.extend(
                        [
                            "",
                            f"📥 Entry: `{d.get('entry', '—')}`",
                            f"📤 Exit: `{d.get('exit', '—')}`",
                        ]
                    )
                    msg = "\n".join(lines)
                    await self.send_telegram_msg(msg)

                elif note['type'] == 'risk_guard_adjustment':
                    d = note.get('data', {})
                    symbol = d.get('symbol', '—')
                    orig_sl = d.get('original_stop_loss_pct')
                    new_sl = d.get('adjusted_stop_loss_pct')
                    orig_tp = d.get('original_take_profit_pct')
                    new_tp = d.get('adjusted_take_profit_pct')
                    rr = d.get('min_rr_ratio')
                    max_sl = d.get('max_allowed_sl')
                    lines = [
                        "🛡️ *RiskGuard adjustment*",
                        "",
                        f"📌 {symbol}",
                        f"🛑 SL: `{orig_sl}` → `{new_sl}` (max {max_sl}%)",
                        f"🎯 TP: `{orig_tp}` → `{new_tp}` (RR≥{rr}x)",
                    ]
                    await self.send_telegram_msg("\n".join(lines))
            
            await asyncio.sleep(1)

    async def run(self):
        """Main loop: Monitors signals from Brain and handles UI."""
        # Start the Telegram Application for callback listening
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()
        
        # Start background listener for Executor notifications
        asyncio.create_task(self.listen_for_notifications())
        
        print(f"[{_ts()}] Messenger: Monitoring 'signals' from Brain every 5s...")

        while True:
            try:
                # Run blocking blpop in thread so event loop can process Telegram callbacks
                signal_data = await asyncio.to_thread(self.db.blpop, "signals", 10)
                
                if signal_data:
                    _, payload = signal_data
                    data = json.loads(payload)
                    
                    symbol = data['symbol']
                    verdict = data.get('verdict', 'WAIT')

                    # Skip Telegram notification for WAIT verdicts unless "signal wait on"
                    if verdict == "WAIT" and self._get_setting(shared_config.SYSTEM_KEY_SIGNAL_WAIT) != "1":
                        print(f"[{_ts()}] Messenger: WAIT signal for {symbol} (notifications off, skipped)")
                        continue

                    # When at max open orders, don't show BUY signals (no slot to trade); consume and skip
                    if verdict == "BUY":
                        open_count = self._get_open_order_count()
                        max_open = self._get_max_open_orders()
                        if open_count >= max_open:
                            print(f"[{_ts()}] Messenger: Dropping BUY signal for {symbol} (max open orders: {open_count}/{max_open})")
                            continue

                    # Formatting external links
                    clean_symbol = symbol.replace('/', '')
                    tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{clean_symbol}"
                    binance_url = f"https://www.binance.com/en/trade/{symbol.replace('/', '_')}"

                    emoji = "🚀" if verdict == "BUY" else "⚠️"

                    stop_loss_pct = data.get("stop_loss_pct")
                    take_profit_pct = data.get("take_profit_pct")
                    high_24h = data.get("high_24h")
                    low_24h = data.get("low_24h")
                    signal_id = data.get("signal_id")

                    # Strategy for context
                    strategy_val = (self._get_setting(shared_config.SYSTEM_KEY_STRATEGY) or "CONSERVATIVE").strip().upper()
                    if strategy_val not in {"CONSERVATIVE", "AGGRESSIVE", "REVERSAL"}:
                        strategy_val = "CONSERVATIVE"

                    # Format TP/SL line
                    sl_str = None
                    tp_str = None
                    try:
                        if stop_loss_pct is not None:
                            sl_str = f"{float(stop_loss_pct):.2f}%"
                        if take_profit_pct is not None:
                            tp_str = f"{float(take_profit_pct):.2f}%"
                    except (TypeError, ValueError):
                        pass

                    tp_sl_line = ""
                    if sl_str or tp_str:
                        tp_sl_line = f"• SL/TP: `{sl_str or 'N/A'}` / `{tp_str or 'N/A'}`\n"

                    range_line = ""
                    if high_24h is not None or low_24h is not None:
                        range_line = f"• 24h range: `{low_24h or 'N/A'}` – `{high_24h or 'N/A'}`\n"

                    message = (
                        f"{emoji} *Signal: {symbol}*\n\n"
                        f"🤖 Verdict: `{verdict}` ({data.get('confidence', 'N/A')})\n"
                        f"📝 _{data.get('reason', 'N/A')}_\n\n"
                        f"📊 Stats\n"
                        f"• Price: `${data.get('last_price')}`\n"
                        f"• RSI: `{data.get('rsi')}` · RVOL: `{data.get('rvol')}x`\n"
                        f"{tp_sl_line}"
                        f"{range_line}"
                        f"• Strategy: `{strategy_val}`\n\n"
                        f"🔗 [TradingView]({tv_url}) · [Binance]({binance_url})"
                    )

                    # Autopilot: no Buy button when on; BUY verdict triggers automatic order
                    autopilot_on = self._get_setting(shared_config.SYSTEM_KEY_AUTOPILOT)
                    keyboard = None
                    if verdict == "BUY" and autopilot_on != "1":
                        if stop_loss_pct is not None and take_profit_pct is not None:
                            callback = f"buy:{symbol}:{signal_id}:{stop_loss_pct}:{take_profit_pct}"
                        else:
                            callback = f"buy:{symbol}:{signal_id}"
                        kb = [[InlineKeyboardButton("🚀 Buy", callback_data=callback)]]
                        keyboard = InlineKeyboardMarkup(kb)

                    await self.send_telegram_msg(message, symbol, keyboard)

                    if verdict == "BUY" and autopilot_on == "1":
                        open_count = self._get_open_order_count()
                        max_open = self._get_max_open_orders()
                        if open_count >= max_open:
                            await self.send_telegram_msg(
                                f"⏸️ *Autopilot skipped*\n\n"
                                f"📌 {symbol}\n\n"
                                f"Max {max_open} open orders reached ({open_count}).\n"
                                f"Close a position or wait for TP/SL to free a slot."
                            )
                            print(f"[{_ts()}] Messenger: Autopilot skipped for {symbol} (open orders: {open_count})")
                        else:
                            # Do not send order if we already have an open position for this symbol
                            try:
                                with shared_db.get_connection() as conn:
                                    shared_db.init_schema(conn)
                                    if shared_db.get_open_order_id_for_symbol(conn, symbol) is not None:
                                        await self.send_telegram_msg(
                                            f"⏸️ *Autopilot skipped*\n\n"
                                            f"📌 {symbol}\n\n"
                                            f"Already have an open order for this symbol."
                                        )
                                        print(f"[{_ts()}] Messenger: Autopilot skipped for {symbol} (already have open order)")
                                        # skip pushing command below
                                    else:
                                        strategy_name = (self._get_setting(shared_config.SYSTEM_KEY_STRATEGY) or "CONSERVATIVE").strip().upper()
                                        if strategy_name not in {"CONSERVATIVE", "AGGRESSIVE", "REVERSAL"}:
                                            strategy_name = "CONSERVATIVE"
                                        command = {
                                            "symbol": symbol,
                                            "stop_loss_pct": stop_loss_pct,
                                            "take_profit_pct": take_profit_pct,
                                            "strategy_name": strategy_name,
                                            "signal_id": signal_id,
                                        }
                                        self.db.rpush("trade_commands", json.dumps(command))
                                        sl_info = f"{float(stop_loss_pct):.2f}%" if isinstance(stop_loss_pct, (int, float)) else str(stop_loss_pct)
                                        tp_info = f"{float(take_profit_pct):.2f}%" if isinstance(take_profit_pct, (int, float)) else str(take_profit_pct)
                                        await self.send_telegram_msg(
                                            f"🤖 *Autopilot order sent*\n\n"
                                            f"📌 {symbol}\n"
                                            f"🎯 Strategy: `{strategy_name}` · SL/TP: `{sl_info}` / `{tp_info}`\n\n"
                                            f"_Executor will confirm shortly._"
                                        )
                                        print(f"[{_ts()}] Messenger: Autopilot order pushed for {symbol}")
                            except Exception as e:
                                print(f"[{_ts()}] Messenger: DB check for open order failed: {e}")
                                strategy_name = (self._get_setting(shared_config.SYSTEM_KEY_STRATEGY) or "CONSERVATIVE").strip().upper()
                                if strategy_name not in {"CONSERVATIVE", "AGGRESSIVE", "REVERSAL"}:
                                    strategy_name = "CONSERVATIVE"
                                command = {"symbol": symbol, "strategy_name": strategy_name, "signal_id": signal_id}
                                self.db.rpush("trade_commands", json.dumps(command))
                                await self.send_telegram_msg(
                                    f"🤖 *Autopilot order sent*\n\n"
                                    f"📌 {symbol}\n"
                                    f"🎯 Strategy: `{strategy_name}`\n\n"
                                    f"_Executor will confirm shortly._"
                                )
                                print(f"[{_ts()}] Messenger: Autopilot order pushed for {symbol}")

                    print(f"[{_ts()}] Messenger: Signal alert sent for {symbol} (Verdict: {verdict})")

            except Exception as e:
                print(f"[{_ts()}] Messenger Loop Error: {e}")
            
            await asyncio.sleep(1)

if __name__ == "__main__":
    messenger = Messenger()
    asyncio.run(messenger.run())