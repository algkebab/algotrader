"""Brain service: strategy execution and GPT analysis."""
import asyncio
import json
import os
import time
from datetime import datetime

import redis
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


def _ts():
    return datetime.utcnow().strftime("%H:%M:%S")


class Brain:
    def __init__(self):
        # Redis setup
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        
        # AI setup
        self.client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

    def get_ai_verdict(self, symbol, price, rsi, rvol, candles):
        """
        Sends technical data to GPT for a high-level trading verdict
        """
        # Prepare a concise summary of the last 5 candles for the AI
        candle_summary = [f"Close: {c[4]}, Vol: {c[5]}" for c in candles[-5:]]
        
        prompt = f"""
        Analyze this crypto trade setup for {symbol}:
        - Current Price: {price}
        - RSI (14): {rsi}
        - Relative Volume (RVOL): {rvol}
        - Recent Price Action (Last 5h): {', '.join(candle_summary)}

        Provide a verdict in JSON format:
        {{
            "verdict": "BUY" or "WAIT",
            "reason": "Short technical explanation",
            "confidence": "0-100%"
        }}
        """

        try:
            response = self.client.chat.completions.create(
                model="gpt-4o", # Using the latest model for better reasoning
                messages=[
                    {"role": "system", "content": "You are an expert crypto day-trader. Be conservative and look for high-probability setups."},
                    {"role": "user", "content": prompt}
                ],
                response_format={ "type": "json_object" } # Ensures we get clean JSON
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            print(f"AI Error: {e}")
            return {"verdict": "WAIT", "reason": "AI Analysis failed", "confidence": "0%"}

    def run(self):
        print("🧠 Brain: AI Technical Analyst is online...")
        
        while True:
            # Get candidates from the Filter service
            raw_data = self.db.get('filtered_candidates')
            if not raw_data:
                time.sleep(5)
                continue

            candidates = json.loads(raw_data)
            
            for item in candidates:
                symbol = item['symbol']
                
                # Prevent analyzing the same coin too often
                if self.db.exists(f"analyzed:{symbol}"):
                    continue

                print(f"🔍 Analyzing {symbol} with AI...")
                
                analysis = self.get_ai_verdict(
                    symbol, 
                    item['last_price'], 
                    item['rsi'], 
                    item['rvol'], 
                    item.get('candles', [])
                )

                # Merge AI verdict with market data
                final_signal = {**item, **analysis}
                
                # Push to messenger queue
                self.db.rpush('signals', json.dumps(final_signal))
                
                # Mark as analyzed for 30 minutes
                self.db.set(f"analyzed:{symbol}", "1", ex=1800)

            # Clear candidates after processing
            self.db.delete('filtered_candidates')
            time.sleep(5)

if __name__ == "__main__":
    brain = Brain()
    asyncio.run(brain.run())
