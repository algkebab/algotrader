import asyncio
import json
import os
import sys
import redis
import time
from datetime import datetime

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

# Keys we never delete on "clear redis" (system settings)
REDIS_SYSTEM_KEYS = frozenset({
    REDIS_KEY_TRADING_PAUSED,
    REDIS_KEY_SUPPRESS_WAIT_SIGNALS,
    REDIS_KEY_AUTOPILOT,
    REDIS_KEY_MUTED,
    REDIS_KEY_PAPERTRADING,
})

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

HELP_MESSAGE = """🛠 Commands (send exactly as below):

• stop — Pause Filter & Brain (no filtering, no AI). Scout, Executor, Monitor keep running.
• start — Resume Filter & Brain.
• autopilot on — Auto-place orders on BUY verdicts; no Buy button on signals; also resumes pipeline if paused.
• autopilot off — Stop auto orders; Buy button is shown again on BUY signals.
• stop wait — Only BUY signals sent; WAIT verdicts are not sent.
• start wait — Send both BUY and WAIT signals again.
• mute — Stop sending all alerts and notifications to Telegram (platform keeps running).
• unmute — Resume sending alerts and notifications.
• clear redis — Clear all Redis data (queues, cache). Keeps system settings (stop/start, autopilot, mute, etc.).
• papertrading on — No real orders; only write to DB; Monitor uses DB. (Default.)
• papertrading off — Live trading: real orders on exchange; Monitor uses Redis.
• status — Show pipeline (paused/running), WAIT setting, autopilot, mute, and paper trading.
• orders — List current open orders from the database.
• balance — Current USDT balance from DB; shows change since last check.
• help — Show this message."""

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
            await self._safe_reply(update, f"❌ Could not read orders: {e}")
            return
        if not rows:
            await self._safe_reply(update, "📋 No open orders.")
            return
        lines = [f"📋 Open orders ({len(rows)})\n"]
        for i, o in enumerate(rows, 1):
            opened = (o.get("opened_at") or "")[:19].replace("T", " ")
            lines.append(
                f"{i}. {o['symbol']} | Entry: {o['entry_price']} | Qty: {o['quantity']} | "
                f"TP: {o['tp_price']} | SL: {o['sl_price']} | {opened}"
            )
        await self._safe_reply(update, "\n".join(lines))

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
        msg = f"💰 **Balance (USDT)**\n\n`{current:.2f}` USDT\n\n{diff_line}"
        try:
            await update.message.reply_text(msg, parse_mode="Markdown")
        except TimedOut as e:
            print(f"[{_ts()}] Messenger: reply_text timed out (balance state already updated): {e}")

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
                "⏸️ Trading pipeline paused.\n\nFilter and Brain stopped (no filtering, no AI). "
                "Scout, Executor, Monitor still running. Send \"start\" to resume.",
            )
        elif text == "start":
            self.db.delete(REDIS_KEY_TRADING_PAUSED)
            print(f"[{_ts()}] Messenger: Pipeline RESUMED (Filter & Brain running)")
            await self._safe_reply(update, "▶️ Trading pipeline resumed. Filter and Brain are running.")
        elif text == "stop wait":
            self.db.set(REDIS_KEY_SUPPRESS_WAIT_SIGNALS, "1")
            print(f"[{_ts()}] Messenger: WAIT signals disabled (only BUY alerts will be sent)")
            await self._safe_reply(
                update,
                "🔇 WAIT verdicts disabled.\n\nOnly BUY signals will be sent to Telegram. "
                "WAIT verdicts are skipped. Send \"start wait\" to send WAIT signals again.",
            )
        elif text == "start wait":
            self.db.delete(REDIS_KEY_SUPPRESS_WAIT_SIGNALS)
            print(f"[{_ts()}] Messenger: WAIT signals enabled (BUY and WAIT alerts sent)")
            await self._safe_reply(
                update,
                "🔔 WAIT verdicts enabled.\n\nBoth BUY and WAIT signals will be sent to Telegram.",
            )
        elif text == "mute":
            self.db.set(REDIS_KEY_MUTED, "1")
            print(f"[{_ts()}] Messenger: Telegram muted (no alerts/notifications sent)")
            await self._safe_reply(
                update,
                "🔇 Muted.\n\nNo alerts or notifications will be sent to Telegram until you send \"unmute\". "
                "Platform keeps running (signals, autopilot, executor unchanged).",
            )
        elif text == "unmute":
            self.db.delete(REDIS_KEY_MUTED)
            print(f"[{_ts()}] Messenger: Telegram unmuted (alerts/notifications enabled)")
            await self._safe_reply(update, "🔔 Unmuted. Alerts and notifications are enabled again.")
        elif text == "clear redis":
            try:
                n = self._clear_redis_data()
                print(f"[{_ts()}] Messenger: Redis data cleared ({n} keys deleted), system settings kept")
                await self._safe_reply(update, f"🧹 Redis cleared ({n} keys deleted). System settings (stop/start, autopilot, mute, etc.) kept.")
            except Exception as e:
                print(f"[{_ts()}] Messenger: clear redis failed: {e}")
                await self._safe_reply(update, f"❌ Clear Redis failed: {e}")
        elif text == "papertrading on":
            self.db.set(REDIS_KEY_PAPERTRADING, "1")
            print(f"[{_ts()}] Messenger: Paper trading ON (no exchange orders, DB only)")
            await self._safe_reply(
                update,
                "📄 Paper trading ON.\n\nNo real orders on the exchange. Orders are written to the database only. "
                "Monitor uses DB open orders. Send \"papertrading off\" for live trading.",
            )
        elif text == "papertrading off":
            self.db.set(REDIS_KEY_PAPERTRADING, "0")
            print(f"[{_ts()}] Messenger: Paper trading OFF (live trading)")
            await self._safe_reply(
                update,
                "🔴 Paper trading OFF.\n\nLive trading: real orders on the exchange; Monitor uses Redis active_trades.",
            )
        elif text == "status":
            paused = self.db.get(REDIS_KEY_TRADING_PAUSED)
            suppress_wait = self.db.get(REDIS_KEY_SUPPRESS_WAIT_SIGNALS)
            autopilot = self.db.get(REDIS_KEY_AUTOPILOT)
            muted = self.db.get(REDIS_KEY_MUTED)
            paper_val = self.db.get(REDIS_KEY_PAPERTRADING)
            paper_on = paper_val != "0"
            parts = []
            parts.append("Pipeline: paused (send \"start\" to resume)." if paused else "Pipeline: running.")
            parts.append("Autopilot: ON (auto orders on BUY, no button)." if autopilot else "Autopilot: OFF (Buy button on signals).")
            parts.append("WAIT signals: suppressed (only BUY sent). Send \"start wait\" to enable." if suppress_wait else "WAIT signals: sent (BUY + WAIT). Send \"stop wait\" to suppress.")
            parts.append("Telegram: muted (no alerts/notifications). Send \"unmute\" to enable." if muted else "Telegram: unmuted (alerts/notifications sent).")
            parts.append("Paper trading: ON (DB only, no exchange)." if paper_on else "Paper trading: OFF (live).")
            await self._safe_reply(update, "📊 Status:\n\n" + "\n".join(parts))
        elif text == "autopilot on":
            self.db.set(REDIS_KEY_AUTOPILOT, "1")
            self.db.delete(REDIS_KEY_TRADING_PAUSED)
            print(f"[{_ts()}] Messenger: Autopilot ON (pipeline resumed if was paused)")
            await self._safe_reply(
                update,
                "🤖 Autopilot ON.\n\nBUY verdicts will place orders automatically (10 USDT). "
                "No Buy button on signals. Pipeline resumed if it was paused.",
            )
        elif text == "autopilot off":
            self.db.delete(REDIS_KEY_AUTOPILOT)
            print(f"[{_ts()}] Messenger: Autopilot OFF (Buy button restored on signals)")
            await self._safe_reply(
                update,
                "🛑 Autopilot OFF.\n\nNo automatic orders. Buy button is shown again on BUY signals.",
            )
        elif text == "orders":
            await self._reply_orders(update)
        elif text == "balance":
            await self._reply_balance(update)
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
                    text=f"{query.message.text}\n\n⏳ **Command sent to Executor...**",
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

                if note['type'] == 'trade_closed':
                    d = note['data']
                    msg = (
                        f"🏁 **TRADE CLOSED**\n"
                        f"Asset: {d['symbol']}\n"
                        f"Result: `{d['pnl_usdt']} USDT` ({d['pnl_percent']}%)\n"
                        f"Reason: {d['reason']}"
                    )
                    await self.send_telegram_msg(msg)
                
                if note['type'] == 'trade_confirmed':
                    d = note['data']
                    msg = (
                        f"✅ **TRADE EXECUTED**\n\n"
                        f"💰 Entry: `{d['entry']}`\n"
                        f"🎯 Take Profit: `{d['tp']}`\n"
                        f"🛑 Stop Loss: `{d['sl']}`\n"
                        f"📊 Status: `Active` (Orders placed on exchange)"
                    )
                    await self.send_telegram_msg(msg)

                # Handling Trade Closing (Exit)
                elif note['type'] == 'trade_closed':
                    d = note['data']
                    # Choose emoji based on profit or loss
                    result_emoji = "💰" if d['pnl_usdt'] >= 0 else "📉"
                    pnl_sign = "+" if d['pnl_usdt'] >= 0 else ""
                    
                    msg = (
                        f"{result_emoji} **TRADE CLOSED**\n\n"
                        f"Symbol: #{d['symbol'].replace('/', '')}\n"
                        f"Reason: {d['reason']}\n\n"
                        f"💵 PnL USDT: `{pnl_sign}{d['pnl_usdt']} USDT`\n"
                        f"📈 PnL %: `{pnl_sign}{d['pnl_percent']}%`\n\n"
                        f"📥 Entry: `{d['entry']}`\n"
                        f"📤 Exit: `{d['exit']}`"
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
                        f"{emoji} **SIGNAL: {symbol}**\n\n"
                        f"🤖 **AI Verdict:** `{verdict}` ({data.get('confidence', 'N/A')})\n"
                        f"📝 **Reason:** _{data.get('reason', 'N/A')}_\n\n"
                        f"📊 **Technical Stats:**\n"
                        f"• Price: `${data.get('last_price')}`\n"
                        f"• RSI: `{data.get('rsi')}`\n"
                        f"• RVOL: `{data.get('rvol')}x`\n\n"
                        f"🔗 [TradingView]({tv_url}) | [Binance]({binance_url})"
                    )

                    # Autopilot: no Buy button when on; BUY verdict triggers automatic order
                    autopilot_on = self.db.get(REDIS_KEY_AUTOPILOT)
                    keyboard = None
                    if verdict == "BUY" and not autopilot_on:
                        kb = [[InlineKeyboardButton(f"🚀 Buy 10 USDT", callback_data=f"buy:{symbol}")]]
                        keyboard = InlineKeyboardMarkup(kb)

                    await self.send_telegram_msg(message, symbol, keyboard)

                    if verdict == "BUY" and autopilot_on:
                        command = {"symbol": symbol, "amount": AUTOPILOT_ORDER_AMOUNT_USDT}
                        self.db.rpush("trade_commands", json.dumps(command))
                        await self.send_telegram_msg(
                            f"🤖 **Automatic order placed** for {symbol} ({AUTOPILOT_ORDER_AMOUNT_USDT} USDT). "
                            "Executor will confirm shortly."
                        )
                        print(f"[{_ts()}] Messenger: Autopilot order pushed for {symbol}")

                    print(f"[{_ts()}] Messenger: Signal alert sent for {symbol} (Verdict: {verdict})")

            except Exception as e:
                print(f"[{_ts()}] Messenger Loop Error: {e}")
            
            await asyncio.sleep(1)

if __name__ == "__main__":
    messenger = Messenger()
    asyncio.run(messenger.run())