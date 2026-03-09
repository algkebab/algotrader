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

HELP_MESSAGE = """🛠 Commands (send exactly as below):

• stop — Pause Filter & Brain (no filtering, no AI). Scout, Executor, Monitor keep running.
• start — Resume Filter & Brain.
• stop wait — Only BUY signals sent; WAIT verdicts are not sent.
• start wait — Send both BUY and WAIT signals again.
• status — Show pipeline (paused/running) and WAIT setting.
• orders — List current open orders from the database.
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
        elif text == "status":
            paused = self.db.get(REDIS_KEY_TRADING_PAUSED)
            suppress_wait = self.db.get(REDIS_KEY_SUPPRESS_WAIT_SIGNALS)
            parts = []
            parts.append("Pipeline: paused (send \"start\" to resume)." if paused else "Pipeline: running.")
            parts.append("WAIT signals: suppressed (only BUY sent). Send \"start wait\" to enable." if suppress_wait else "WAIT signals: sent (BUY + WAIT). Send \"stop wait\" to suppress.")
            await self._safe_reply(update, "📊 Status:\n\n" + "\n".join(parts))
        elif text == "orders":
            await self._reply_orders(update)
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
                    
                    # Prepare keyboard only if AI gives a BUY verdict
                    keyboard = None
                    if verdict == "BUY":
                        kb = [[InlineKeyboardButton(f"🚀 Buy 10 USDT", callback_data=f"buy:{symbol}")]]
                        keyboard = InlineKeyboardMarkup(kb)
                    
                    await self.send_telegram_msg(message, symbol, keyboard)
                    print(f"[{_ts()}] Messenger: Signal alert sent for {symbol} (Verdict: {verdict})")

            except Exception as e:
                print(f"[{_ts()}] Messenger Loop Error: {e}")
            
            await asyncio.sleep(1)

if __name__ == "__main__":
    messenger = Messenger()
    asyncio.run(messenger.run())