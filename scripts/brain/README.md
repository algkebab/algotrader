# Brain test scripts

Brain reads `filtered_candidates` from Redis and pushes AI verdicts to `signals`. These scripts write proper mock data so you can test Brain without running Scout or Filter.

## Mock data shape

Each candidate must have (same as Filter output):

- `symbol` — e.g. `"BTC/USDT"`
- `last_price` — float
- `rsi` — float (RSI 14)
- `rvol` — float (relative volume)
- `candles` — list of `[timestamp, open, high, low, close, volume]`; at least 5 used in the AI prompt

## Scripts

| Script | Purpose |
|--------|--------|
| `write_mock_filtered_candidates.py` | Write Brain-ready `filtered_candidates` to Redis. |
| `clear_brain_cache.py` | Delete `cache:brain_price:*` so Brain re-analyzes. |

## How to test

**1. Brain will analyze (calls OpenAI):**

```bash
# Write mock candidates and clear cache so Brain doesn’t skip
python scripts/brain/write_mock_filtered_candidates.py --clear-cache

# Run Brain (will read candidates, call GPT, push to signals)
cd services/brain && python main.py
```

**2. Brain will skip (cache path, no API call):**

```bash
# Write mock candidates and set cache = same price → Brain skips
python scripts/brain/write_mock_filtered_candidates.py --set-cache-skip

# Run Brain (will log "Skipping SYMBOL (Price change ... < 0.50%)")
cd services/brain && python main.py
```

**3. Custom symbols/prices:**

```bash
python scripts/brain/write_mock_filtered_candidates.py --symbols "BTC/USDT,SOL/USDT" --clear-cache
```

**4. Force re-analysis after a run:**

```bash
python scripts/brain/clear_brain_cache.py
# Then write new mock data and run Brain again
```

Requires `OPENAI_API_KEY` in `.env` for real AI verdicts; if the API fails, Brain still pushes a WAIT fallback to `signals`.
