import redis
import json
import os
import asyncio
import time
from telegram import Bot
from dotenv import load_dotenv

# Load credentials from .env
load_dotenv()

class Messenger:
    def __init__(self):
        # Redis setup: connects to 'redis' container in Docker or 'localhost' locally
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        
        # Telegram setup
        self.bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID')
        
        if not self.bot_token or not self.chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not found in environment variables")
            
        self.bot = Bot(token=self.bot_token)
        
        # Memory to avoid duplicate alerts for the same symbol in a short period
        self.sent_alerts = {} 
        self.alert_expiry = 3600 # Do not repeat the same asset for 1 hour

    async def send_telegram_msg(self, text):
        """Sends a formatted Markdown message to Telegram"""
        try:
            async with self.bot:
                await self.bot.send_message(chat_id=self.chat_id, text=text, parse_mode='Markdown')
        except Exception as e:
            print(f"❌ Telegram API Error: {e}")

    def clean_old_alerts(self):
        """Removes expired alerts from local memory"""
        current_time = time.time()
        self.sent_alerts = {
            symbol: timestamp for symbol, timestamp in self.sent_alerts.items() 
            if current_time - timestamp < self.alert_expiry
        }

    async def run(self):
        """Main loop: monitors Redis and sends alerts with AI and Charts"""
        print("📱 Messenger: Service is up and monitoring 'ai_signals'...")
        
        while True:
            try:
                data = self.db.get('ai_signals')
                
                if data:
                    signals = json.loads(data)
                    for signal in signals:
                        symbol = signal['symbol']
                        
                        # Check in Redis: have we already sent this?
                        alert_key = f"sent_alert:{symbol}"
                        if self.db.exists(alert_key):
                            continue  # Skip if we already sent this symbol recently

                        # Build chart/trade links
                        clean_symbol = symbol.replace('/', '')
                        tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{clean_symbol}"
                        binance_url = f"https://www.binance.com/en/trade/{symbol.replace('/', '_')}"

                        rvol = signal.get('rvol', 'N/A')
                        ai_opinion = signal.get('ai_analysis', 'No AI analysis provided.')

                        message = (
                            f"🚀 *VOLUMETRIC SPIKE: {symbol}*\n\n"
                            f"📈 *24h Change:* `{signal.get('change_24h')}%`\n"
                            f"🔥 *RVOL:* `{rvol}x` (Relative Volume)\n"
                            f"💰 *Price:* `${signal.get('last_price', '0')}`\n\n"
                            f"🤖 *AI Opinion:* \n_{ai_opinion}_\n\n"
                            f"📊 [TradingView]({tv_url}) | 🔗 [Binance]({binance_url})"
                        )
                        
                        await self.send_telegram_msg(message)
                        
                        # Remember in Redis for 1 hour (3600 seconds)
                        self.db.set(alert_key, "sent", ex=3600)
                        
                        print(f"📩 Smart alert sent for {symbol}")

                    # Clear the signal so other instances (if any) don't send duplicates
                    self.db.delete('ai_signals')

            except Exception as e:
                print(f"❌ Messenger Loop Error: {e}")
            
            await asyncio.sleep(10)

if __name__ == "__main__":
    messenger = Messenger()
    asyncio.run(messenger.run())