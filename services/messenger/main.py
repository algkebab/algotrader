import asyncio
import json
import os
import sys
import redis
import time
from datetime import datetime

import ccxt
from dotenv import load_dotenv

# Allow importing shared.db (project root or /app in Docker)
_this_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_this_dir, "..", "..")) if os.path.basename(_this_dir) == "messenger" else _this_dir
if _root not in sys.path:
    sys.path.insert(0, _root)

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

# Redis key: when set, Filter and Brain skip work (pause pipeline)
REDIS_KEY_TRADING_PAUSED = "system:trading_paused"
# Redis key: when set, don't send Telegram alerts for AI verdict WAIT (only BUY signals sent)
REDIS_KEY_SUPPRESS_WAIT_SIGNALS = "system:suppress_wait_signals"
# Redis key: when set, BUY verdicts trigger automatic order (no Buy button on signals)
REDIS_KEY_AUTOPILOT = "system:autopilot"
# Redis key: when set, no alerts/notifications sent to Telegram (platform keeps working)
REDIS_KEY_MUTED = "system:muted"
# Redis key: "1" = paper trading (no exchange, DB only); "0" = live. Default when absent = paper.
REDIS_KEY_PAPERTRADING = "system:papertrading"
# Last balance check (for "balance" command diff)
REDIS_KEY_BALANCE_LAST_USDT = "system:balance_last_usdt"
REDIS_KEY_BALANCE_LAST_CHECK = "system:balance_last_check"
# Number of top symbols by volume Scout fetches from Binance (default 30)
REDIS_KEY_MAX_SYMBOLS = "system:max_symbols"
# AI strategy: conservative, moderate, aggressive, active_day (default conservative)
REDIS_KEY_STRATEGY = "system:strategy"

# Keys we never delete on "clear redis" (system settings)
REDIS_SYSTEM_KEYS = frozenset({
    REDIS_KEY_TRADING_PAUSED,
    REDIS_KEY_SUPPRESS_WAIT_SIGNALS,
    REDIS_KEY_AUTOPILOT,
    REDIS_KEY_MUTED,
    REDIS_KEY_PAPERTRADING,
    REDIS_KEY_MAX_SYMBOLS,
    REDIS_KEY_STRATEGY,
})

# Allowed values for "set strategy"
STRATEGY_VALUES = frozenset({"conservative", "moderate", "aggressive", "active_day"})

# Data keys and patterns to clear on "clear redis" (excludes REDIS_SYSTEM_KEYS)
REDIS_DATA_KEYS = [
    "market_data",
    "filtered_candidates",
    "signals",
    "trade_commands",
    "active_trades",
    "notifications",
    REDIS_KEY_BALANCE_LAST_USDT,
    REDIS_KEY_BALANCE_LAST_CHECK,
]
REDIS_DATA_PATTERNS = ["analyzed:*", "last_vol:*", "cache:brain_price:*"]

AUTOPILOT_ORDER_AMOUNT_USDT = 10
AUTOPILOT_MAX_OPEN_ORDERS = 10
# Bounds for "set symbols <n>"
MAX_SYMBOLS_MIN = 5
MAX_SYMBOLS_MAX = 200
MAX_SYMBOLS_DEFAULT = 30
# Paper leverage for margin on manual close (must match Monitor/Executor)
PAPER_LEVERAGE = 3

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

HELP_MESSAGE = """🛠 *Algotrader — Commands*

📌 *Pipeline*
• *stop* — Pause Filter & Brain (no filtering, no AI). Scout, Executor, Monitor keep running.
• *start* — Resume Filter & Brain.

🤖 *Autopilot*
• *autopilot on* — Auto-place orders on BUY (max 10 open). No Buy button. Resumes pipeline if paused.
• *autopilot off* — Stop auto orders. Buy button shown again on BUY signals.

🔔 *Signals*
• *stop wait* — Only BUY signals sent; WAIT verdicts hidden.
• *start wait* — Send both BUY and WAIT signals.

🔇 *Notifications*
• *mute* — No alerts or notifications sent (platform keeps running).
• *unmute* — Resume alerts and notifications.

📄 *Paper / Live*
• *papertrading on* — No real orders; DB only. (Default.)
• *papertrading off* — Live trading on exchange.

🧹 *Data*
• *clear redis* — Clear queues and cache. Keeps system settings.

📊 *Info*
• *status* — Pipeline, autopilot, mute, paper trading.
• *orders* — List open orders from DB.
• *orders close* <symbol> — Manually close open order for symbol (e.g. orders close BTC/USDT). Updates balance.
• *balance* — Current USDT balance and change since last check.
• *set balance* <amount> — Set USDT in DB (e.g. set balance 100.50).
• *set symbols* <number> — Top N symbols by volume to fetch (e.g. set symbols 50). Min 5, max 200.
• *set strategy* <name> — AI strategy: conservative, moderate, aggressive, active_day. Default: conservative.
• *help* — This message."""

def _ts():
    """Returns current UTC timestamp for logging."""
    return datetime.utcnow().strftime("%H:%M:%S")

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
            opened = (o.get("opened_at") or "")[:19].replace("T", " ")
            symbol = o["symbol"]
            try:
                ticker = await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
                now_price = ticker.get("last")
                now_str = f"{float(now_price):.4g}" if now_price is not None else "—"
            except Exception:
                now_str = "—"
            lines.append(
                f"{i}. {symbol}\n"
                f"   💵 Entry: {o['entry_price']} · Qty: {o['quantity']}\n"
                f"   📈 Now: {now_str}\n"
                f"   🎯 TP: {o['tp_price']} · 🛑 SL: {o['sl_price']}\n"
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
            pnl_usdt = (close_price - entry_price) * qty
            pnl_percent = ((close_price / entry_price) - 1) * 100
            reason = "Manual close"
            paper = self.db.get("system:papertrading") != "0"
            margin_usdt = float(row["amount_usdt"]) / PAPER_LEVERAGE if paper else 0.0

            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                shared_db.update_order_closed(
                    conn, order_id,
                    pnl_usdt=round(pnl_usdt, 2),
                    pnl_percent=round(pnl_percent, 2),
                    close_reason=reason,
                )
                bal = shared_db.get_balance(conn, "USDT")
                shared_db.set_balance(conn, "USDT", bal + pnl_usdt + margin_usdt)

            if not paper:
                self.db.hdel("active_trades", symbol)

            self.db.rpush("notifications", json.dumps({
                "type": "trade_closed",
                "data": {
                    "symbol": symbol,
                    "entry": entry_price,
                    "exit": close_price,
                    "pnl_usdt": round(pnl_usdt, 2),
                    "pnl_percent": round(pnl_percent, 2),
                    "reason": reason,
                },
            }))
            print(f"[{_ts()}] Messenger: Manual close {symbol} at {close_price}. PnL: {pnl_usdt:.2f} USDT")
            await self._safe_reply(
                update,
                f"✅ Order closed\n\n{symbol}\n"
                f"💵 PnL: {pnl_usdt:+.2f} USDT ({pnl_percent:+.2f}%)\n"
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

    async def _reply_balance(self, update) -> None:
        """Reply with current USDT balance from DB and delta since last check (stored in Redis)."""
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                current = shared_db.get_balance(conn, "USDT")
        except Exception as e:
            await self._safe_reply(update, f"❌ Could not read balance: {e}")
            return
        now_iso = datetime.utcnow().isoformat() + "Z"
        now_short = (now_iso[:19].replace("T", " ") + " UTC")
        last_str = self.db.get(REDIS_KEY_BALANCE_LAST_USDT)
        last_check = self.db.get(REDIS_KEY_BALANCE_LAST_CHECK)
        if last_str is not None and last_check is not None:
            try:
                last = float(last_str)
                delta = current - last
                if delta > 0:
                    change_emoji = "📈"
                    change_text = f"+{delta:.2f} USDT since last check"
                elif delta < 0:
                    change_emoji = "📉"
                    change_text = f"{delta:.2f} USDT since last check"
                else:
                    change_emoji = "➡️"
                    change_text = "No change since last check"
                diff_line = f"{change_emoji} {change_text}\nLast check: {last_check[:19].replace('T', ' ')} UTC"
            except (ValueError, TypeError):
                diff_line = f"Last check: —"
        else:
            diff_line = "First check — no previous balance to compare."
        self.db.set(REDIS_KEY_BALANCE_LAST_USDT, f"{current:.2f}")
        self.db.set(REDIS_KEY_BALANCE_LAST_CHECK, now_iso)
        msg = (
            f"💰 *Balance (USDT)*\n\n"
            f"`{current:.2f}` USDT\n\n"
            f"_{diff_line}_"
        )
        try:
            await update.message.reply_text(msg, parse_mode="Markdown")
        except TimedOut as e:
            print(f"[{_ts()}] Messenger: reply_text timed out (balance state already updated): {e}")

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
                f"Min {MAX_SYMBOLS_MIN}, max {MAX_SYMBOLS_MAX}. Example: set symbols 50",
            )
            return
        try:
            n = int(rest)
        except ValueError:
            await self._safe_reply(update, f"❌ Invalid number. Use an integer between {MAX_SYMBOLS_MIN} and {MAX_SYMBOLS_MAX}.")
            return
        if n < MAX_SYMBOLS_MIN or n > MAX_SYMBOLS_MAX:
            await self._safe_reply(
                update,
                f"❌ Out of range. Use {MAX_SYMBOLS_MIN}–{MAX_SYMBOLS_MAX}.",
            )
            return
        self.db.set(REDIS_KEY_MAX_SYMBOLS, str(n))
        print(f"[{_ts()}] Messenger: max_symbols set to {n}")
        await self._safe_reply(update, f"✅ Symbols updated\n\n📈 Scout will fetch top {n} symbols by volume.")

    async def _handle_set_strategy(self, update, text: str) -> None:
        """Set AI strategy. Usage: set strategy <name>. Values: conservative, moderate, aggressive, active_day."""
        rest = text[len("set strategy "):].strip().lower()
        if not rest:
            await self._safe_reply(
                update,
                "🎯 Set strategy\n\nUsage: set strategy <name>\n"
                "Values: conservative, moderate, aggressive, active_day.\n"
                "Default: conservative.",
            )
            return
        if rest not in STRATEGY_VALUES:
            await self._safe_reply(
                update,
                f"❌ Invalid strategy. Use one of: conservative, moderate, aggressive, active_day.",
            )
            return
        self.db.set(REDIS_KEY_STRATEGY, rest)
        print(f"[{_ts()}] Messenger: strategy set to {rest}")
        await self._safe_reply(update, f"✅ Strategy updated\n\n🎯 AI strategy: {rest}")

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
            self.db.set(REDIS_KEY_TRADING_PAUSED, "1")
            print(f"[{_ts()}] Messenger: Pipeline PAUSED (Filter & Brain stopped)")
            await self._safe_reply(
                update,
                "⏸️ Pipeline paused\n\n"
                "Filter & Brain stopped (no filtering, no AI).\n"
                "Scout, Executor, Monitor still running.\n\n"
                "👉 Send \"start\" to resume.",
            )
        elif text == "start":
            self.db.delete(REDIS_KEY_TRADING_PAUSED)
            print(f"[{_ts()}] Messenger: Pipeline RESUMED (Filter & Brain running)")
            await self._safe_reply(update, "▶️ Pipeline resumed\n\nFilter and Brain are running.")
        elif text == "stop wait":
            self.db.set(REDIS_KEY_SUPPRESS_WAIT_SIGNALS, "1")
            print(f"[{_ts()}] Messenger: WAIT signals disabled (only BUY alerts will be sent)")
            await self._safe_reply(
                update,
                "🔇 WAIT verdicts off\n\n"
                "Only BUY signals will be sent.\n"
                "👉 Send \"start wait\" to send WAIT again.",
            )
        elif text == "start wait":
            self.db.delete(REDIS_KEY_SUPPRESS_WAIT_SIGNALS)
            print(f"[{_ts()}] Messenger: WAIT signals enabled (BUY and WAIT alerts sent)")
            await self._safe_reply(
                update,
                "🔔 WAIT verdicts on\n\nBoth BUY and WAIT signals will be sent.",
            )
        elif text == "mute":
            self.db.set(REDIS_KEY_MUTED, "1")
            print(f"[{_ts()}] Messenger: Telegram muted (no alerts/notifications sent)")
            await self._safe_reply(
                update,
                "🔇 Muted\n\n"
                "No alerts or notifications until you send \"unmute\".\n"
                "Platform keeps running (signals, autopilot, executor unchanged).",
            )
        elif text == "unmute":
            self.db.delete(REDIS_KEY_MUTED)
            print(f"[{_ts()}] Messenger: Telegram unmuted (alerts/notifications enabled)")
            await self._safe_reply(update, "🔔 Unmuted\n\nAlerts and notifications are on again.")
        elif text == "clear redis":
            try:
                n = self._clear_redis_data()
                print(f"[{_ts()}] Messenger: Redis data cleared ({n} keys deleted), system settings kept")
                await self._safe_reply(
                    update,
                    f"🧹 Redis cleared\n\n{n} keys deleted.\n"
                    "System settings (stop/start, autopilot, mute, paper) kept.",
                )
            except Exception as e:
                print(f"[{_ts()}] Messenger: clear redis failed: {e}")
                await self._safe_reply(update, f"❌ Clear Redis failed\n\n{e}")
        elif text == "papertrading on":
            self.db.set(REDIS_KEY_PAPERTRADING, "1")
            print(f"[{_ts()}] Messenger: Paper trading ON (no exchange orders, DB only)")
            await self._safe_reply(
                update,
                "📄 Paper trading on\n\n"
                "No real orders on exchange. Orders written to DB only.\n"
                "Monitor uses DB. 👉 Send \"papertrading off\" for live.",
            )
        elif text == "papertrading off":
            self.db.set(REDIS_KEY_PAPERTRADING, "0")
            print(f"[{_ts()}] Messenger: Paper trading OFF (live trading)")
            await self._safe_reply(
                update,
                "🔴 Paper trading off\n\n"
                "Live trading: real orders on exchange. Monitor uses Redis.",
            )
        elif text == "status":
            paused = self.db.get(REDIS_KEY_TRADING_PAUSED)
            suppress_wait = self.db.get(REDIS_KEY_SUPPRESS_WAIT_SIGNALS)
            autopilot = self.db.get(REDIS_KEY_AUTOPILOT)
            muted = self.db.get(REDIS_KEY_MUTED)
            paper_val = self.db.get(REDIS_KEY_PAPERTRADING)
            paper_on = paper_val != "0"
            symbols_val = self.db.get(REDIS_KEY_MAX_SYMBOLS)
            symbols_n = int(symbols_val) if symbols_val and str(symbols_val).isdigit() else MAX_SYMBOLS_DEFAULT
            lines = [
                "📊 Status",
                "",
                "📌 Pipeline: " + ("⏸️ paused (send \"start\" to resume)" if paused else "▶️ running"),
                "🤖 Autopilot: " + ("ON (auto orders, no button)" if autopilot else "OFF (Buy button on signals)"),
                "🔔 WAIT signals: " + ("suppressed (only BUY)" if suppress_wait else "on (BUY + WAIT)"),
                "📱 Telegram: " + ("🔇 muted" if muted else "🔔 unmuted"),
                "📄 Paper: " + ("ON (DB only)" if paper_on else "OFF (live)"),
                f"📈 Symbols: top {symbols_n} by volume",
            ]
            strategy_val = (self.db.get(REDIS_KEY_STRATEGY) or "conservative").strip().lower()
            if strategy_val not in STRATEGY_VALUES:
                strategy_val = "conservative"
            lines.append(f"🎯 Strategy: {strategy_val}")
            await self._safe_reply(update, "\n".join(lines))
        elif text == "autopilot on":
            self.db.set(REDIS_KEY_AUTOPILOT, "1")
            self.db.delete(REDIS_KEY_TRADING_PAUSED)
            print(f"[{_ts()}] Messenger: Autopilot ON (pipeline resumed if was paused)")
            await self._safe_reply(
                update,
                "🤖 Autopilot on\n\n"
                "BUY verdicts → auto orders (10 USDT). No Buy button.\n"
                "Pipeline resumed if it was paused.",
            )
        elif text == "autopilot off":
            self.db.delete(REDIS_KEY_AUTOPILOT)
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
        elif text == "balance":
            await self._reply_balance(update)
        elif text.startswith("set balance "):
            await self._handle_set_balance(update, text)
        elif text.startswith("set symbols "):
            await self._handle_set_symbols(update, text)
        elif text.startswith("set strategy "):
            await self._handle_set_strategy(update, text)
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
            symbol = query.data.split(":")[1]
            # Payload for the Executor service
            command = {"symbol": symbol, "amount": 10}
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
        """Helper to send Markdown messages with optional keyboards. Retries on timeout.
        When system:muted is set, no message is sent (platform keeps working)."""
        if self.db.get(REDIS_KEY_MUTED):
            return
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

                elif note['type'] == 'trade_closed':
                    d = note['data']
                    result_emoji = "💰" if d['pnl_usdt'] >= 0 else "📉"
                    pnl_sign = "+" if d['pnl_usdt'] >= 0 else ""
                    msg = (
                        f"{result_emoji} *Trade closed*\n\n"
                        f"📌 #{d['symbol'].replace('/', '')}\n"
                        f"📋 {d['reason']}\n\n"
                        f"💵 PnL: `{pnl_sign}{d['pnl_usdt']}` USDT\n"
                        f"📈 PnL: `{pnl_sign}{d['pnl_percent']}`%\n\n"
                        f"📥 Entry: `{d.get('entry', '—')}`\n"
                        f"📤 Exit: `{d.get('exit', '—')}`"
                    )
                    await self.send_telegram_msg(msg)
            
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
                    
                    # Formatting external links
                    clean_symbol = symbol.replace('/', '')
                    tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{clean_symbol}"
                    binance_url = f"https://www.binance.com/en/trade/{symbol.replace('/', '_')}"

                    # AI Verdict handling
                    verdict = data.get('verdict', 'WAIT')
                    if verdict == "WAIT" and self.db.get(REDIS_KEY_SUPPRESS_WAIT_SIGNALS):
                        print(f"[{_ts()}] Messenger: Skipped WAIT signal for {symbol} (suppress enabled)")
                        continue

                    emoji = "🚀" if verdict == "BUY" else "⚠️"
                    
                    message = (
                        f"{emoji} *Signal: {symbol}*\n\n"
                        f"🤖 Verdict: `{verdict}` ({data.get('confidence', 'N/A')})\n"
                        f"📝 _{data.get('reason', 'N/A')}_\n\n"
                        f"📊 Stats\n"
                        f"• Price: `${data.get('last_price')}`\n"
                        f"• RSI: `{data.get('rsi')}` · RVOL: `{data.get('rvol')}x`\n\n"
                        f"🔗 [TradingView]({tv_url}) · [Binance]({binance_url})"
                    )

                    # Autopilot: no Buy button when on; BUY verdict triggers automatic order
                    autopilot_on = self.db.get(REDIS_KEY_AUTOPILOT)
                    keyboard = None
                    if verdict == "BUY" and not autopilot_on:
                        kb = [[InlineKeyboardButton(f"🚀 Buy 10 USDT", callback_data=f"buy:{symbol}")]]
                        keyboard = InlineKeyboardMarkup(kb)

                    await self.send_telegram_msg(message, symbol, keyboard)

                    if verdict == "BUY" and autopilot_on:
                        open_count = self._get_open_order_count()
                        if open_count >= AUTOPILOT_MAX_OPEN_ORDERS:
                            await self.send_telegram_msg(
                                f"⏸️ *Autopilot skipped*\n\n"
                                f"📌 {symbol}\n\n"
                                f"Max {AUTOPILOT_MAX_OPEN_ORDERS} open orders reached ({open_count}).\n"
                                f"Close a position or wait for TP/SL to free a slot."
                            )
                            print(f"[{_ts()}] Messenger: Autopilot skipped for {symbol} (open orders: {open_count})")
                        else:
                            command = {"symbol": symbol, "amount": AUTOPILOT_ORDER_AMOUNT_USDT}
                            self.db.rpush("trade_commands", json.dumps(command))
                            await self.send_telegram_msg(
                                f"🤖 *Autopilot order sent*\n\n"
                                f"📌 {symbol} · {AUTOPILOT_ORDER_AMOUNT_USDT} USDT\n\n"
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