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
        """Main loop: monitors Redis and sends alerts"""
        print("📱 Messenger: Service is up and monitoring Redis...")
        
        while True:
            try:
                # Get filtered candidates from the Filter service
                data = self.db.get('filtered_candidates')
                
                if data:
                    candidates = json.loads(data)
                    current_time = time.time()
                    
                    for asset in candidates:
                        symbol = asset['symbol']
                        
                        # Check if we already alerted about this symbol recently
                        if symbol not in self.sent_alerts:
                            message = (
                                f"🚀 *New Trading Signal Found!*\n\n"
                                f"💎 *Asset:* `{symbol}`\n"
                                f"📈 *24h Change:* `{asset['change_24h']}%`\n"
                                f"💰 *Price:* `${asset['last_price']}`\n"
                                f"📊 *24h Volume:* `${asset['volume_24h']:,.0f}`\n\n"
                                f"🔗 [View on Binance](https://www.binance.com/en/trade/{symbol.replace('/', '_')})"
                            )
                            
                            await self.send_telegram_msg(message)
                            self.sent_alerts[symbol] = current_time
                            print(f"📩 Alert sent for {symbol}")

                self.clean_old_alerts()
                
            except Exception as e:
                print(f"❌ Messenger Loop Error: {e}")
            
            # Polling interval
            await asyncio.sleep(10)

if __name__ == "__main__":
    messenger = Messenger()
    asyncio.run(messenger.run())