"""Brain service: strategy execution and GPT analysis."""
import asyncio
import json
import os
from datetime import datetime

import redis
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()


def _ts():
    return datetime.utcnow().strftime("%H:%M:%S")


class Brain:
    def __init__(self):
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        self.db.ping()
        print(f"[{_ts()}] Brain: Connected to Redis at {redis_host}:6379")
        self.client = AsyncOpenAI(api_key=os.getenv('OPENAI_API_KEY'))
        print(f"[{_ts()}] Brain: OpenAI client initialized (model: gpt-4o)")
        self.analyzed_symbols = set()

    async def analyze_with_ai(self, asset_data):
        """Sends asset data to GPT-4o for a quick trading opinion"""
        symbol = asset_data['symbol']
        change = asset_data['change_24h']
        vol = asset_data['volume_24h']
        price = asset_data['last_price']

        prompt = (
            f"As a crypto analyst, look at this: {symbol} is up {change}% in 24h. "
            f"Current price: ${price}. 24h Volume: ${vol:,.0f}. "
            f"Give me a 1-sentence summary: Is this a pump-and-dump or a solid move? "
            f"Keep it professional and concise."
        )

        try:
            response = await self.client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=60,
            )
            content = response.choices[0].message.content
            print(f"[{_ts()}] Brain: GPT response for {symbol} ({len(content)} chars)")
            return content
        except Exception as e:
            print(f"[{_ts()}] Brain: OpenAI error for {symbol}: {e}")
            return f"AI Analysis unavailable: {str(e)}"

    async def run(self):
        print(f"[{_ts()}] Brain: AI analyst online, polling filtered_candidates every 5s...")
        cycle = 0
        while True:
            cycle += 1
            data = self.db.get('filtered_candidates')
            if not data:
                if cycle == 1 or cycle % 12 == 0:
                    print(f"[{_ts()}] Brain: No filtered_candidates in Redis (cycle #{cycle})")
                await asyncio.sleep(5)
                continue

            candidates = json.loads(data)
            new_count = sum(1 for a in candidates if a['symbol'] not in self.analyzed_symbols)
            if new_count == 0:
                if cycle == 1 or cycle % 12 == 0:
                    print(f"[{_ts()}] Brain: {len(candidates)} candidates, all already analyzed (cycle #{cycle})")
                await asyncio.sleep(5)
                continue

            print(f"[{_ts()}] Brain: {len(candidates)} candidates, {new_count} new to analyze (cycle #{cycle})")
            for asset in candidates:
                symbol = asset['symbol']
                if symbol in self.analyzed_symbols:
                    continue
                print(f"[{_ts()}] Brain: Analyzing {symbol} (change={asset.get('change_24h')}%, vol=${asset.get('volume_24h', 0):,.0f})...")
                ai_opinion = await self.analyze_with_ai(asset)
                asset['ai_analysis'] = ai_opinion
                self.db.set('ai_signals', json.dumps([asset]))
                self.analyzed_symbols.add(symbol)
                print(f"[{_ts()}] Brain: Wrote ai_signals for {symbol} to Redis")

            await asyncio.sleep(5)

if __name__ == "__main__":
    brain = Brain()
    asyncio.run(brain.run())
