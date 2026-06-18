# ML Signal Model — Phase 1

The Brain can score entry candidates with a LightGBM model that predicts the
**probability a trade reaches take-profit before stop-loss** (triple-barrier
label). That probability decides the entry and feeds Kelly position sizing.

## Decision modes

Set via Telegram: `set decision <ml|code|gpt>`

| Mode | Engine | Notes |
|------|--------|-------|
| `ml` | LightGBM probability model | **Recommended.** Auto-falls back to `code` if no trained model is present. |
| `code` | Deterministic confluence rules | Baseline and automatic fallback. Always works. |
| `gpt` | GPT-4o | Deprecated. Kept only for A/B comparison. Not on the default path. |

If `ml` is selected but the model file is missing or `lightgbm` isn't installed,
the Brain logs a warning and uses the `code` engine. **The bot never breaks.**

## Architecture

```
shared/features.py          Look-ahead-safe feature engineering (pure Python).
                            FEATURE_NAMES is the canonical, ordered feature list.
shared/cross_validation.py  Purged K-Fold with embargo (López de Prado).
shared/ml_model.py          Lazy LightGBM wrapper + analysis builder. Graceful
                            degradation; macro/regime hard-gates preserved.
scripts/train_model.py      Offline trainer (triple-barrier labels + purged CV).
```

Train and serve build feature vectors through the **same** `shared.features`
code path — guaranteeing parity.

## Training

Run where `lightgbm`, `numpy`, `scikit-learn`, `ccxt` are installed
(`pip install -r scripts/requirements.txt`):

```bash
python scripts/train_model.py --days 360 --rr 2.5 --horizon 192
```

- `--days`    history window to fetch/label (default 360)
- `--rr`      reward:risk used for triple-barrier labels (default 2.5)
- `--horizon` holding window in 15m bars (192 = 48h, matches the time-stop)

Outputs to `data/models/`:
- `signal_model.txt`        LightGBM booster
- `signal_model.meta.json`  feature names, buy threshold, CV AUC, label config

The trainer reports **purged out-of-fold AUC**. Guard rail: if AUC < 0.52 the
model has no real edge — keep `decision_mode=code` and do not deploy `ml`.

## Deployment

`data/` is gitignored, so model artefacts are not committed. Train on the server
(or train elsewhere and copy `data/models/` over). The Brain picks up the model
automatically on its next cycle; no restart required for scoring, but a restart
is needed if you changed `requirements.txt` (to install lightgbm/numpy).

The model path follows the DB convention: it lives in `<DATABASE_PATH dir>/models/`
when `DATABASE_PATH` is set, else `./data/models/`. Override with `ML_MODEL_DIR`.

## Feature set (v1)

Returns (log, multi-horizon), realised volatility (rolling RMS of log returns),
vol-of-vol, Parkinson volatility, momentum z-scores, RSI/MACD normalised,
EMA-alignment encodings (15m/1h/4h), Bollinger %B & bandwidth, VWAP distance,
order-flow imbalance proxy, volume z-score, relative volume, cross-sectional
momentum rank, and fractionally-differenced log price.

Changing `FEATURE_NAMES` requires retraining; the model validates the feature
list at load time and refuses to serve on mismatch (falls back to code).
