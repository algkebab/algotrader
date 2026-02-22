import asyncio
import json
import os
import redis
import time
from datetime import datetime

from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler

load_dotenv()

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
            
        # Bot instance for manual sending
        self.bot = Bot(token=self.bot_token)
        print(f"[{_ts()}] Messenger: Bot initialized (chat_id={self.chat_id})")

        # Initialize Telegram Application with longer timeouts (avoids TimedOut from slow networks)
        self.application = (
            Application.builder()
            .token(self.bot_token)
            .connect_timeout(30)
            .read_timeout(30)
            .write_timeout(30)
            .build()
        )
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))

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
        """Helper to send Markdown messages with optional keyboards."""
        try:
            async with self.bot:
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                    disable_web_page_preview=True
                )
        except Exception as e:
            print(f"[{_ts()}] Messenger: Telegram API Error: {e}")

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