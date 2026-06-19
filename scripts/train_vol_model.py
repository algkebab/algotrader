#!/usr/bin/env python3
"""
Offline trainer for the forward-VOLATILITY model (LightGBM regression).

Direction is unpredictable at 15m/4h from OHLCV (proven: AUC ~0.51). Volatility
is NOT — vol clustering is one of the most robust effects in markets. This model
predicts realised volatility over the next `horizon` bars and is meant to drive
RISK SCALING (position size, SL/TP width), not entry direction.

THE HONEST BAR: it is not enough to "predict vol" — current volatility already
predicts future volatility through persistence. The model must beat that NAIVE
BASELINE (use trailing realised vol as the forecast). We report both and only
flag the model useful if it beats baseline by a meaningful margin.

Pipeline mirrors train_model.py (same fetch + feature path) but:
  - label  = log(forward realised vol over next `horizon` bars)  [regression]
  - metric = out-of-fold R^2 and Pearson r, vs the trailing-vol baseline

Run (where ccxt/numpy/lightgbm/scikit-learn are installed):
    python scripts/train_vol_model.py --days 360 --horizon 96 --out-dir /data/models
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Reuse the proven fetch + walk helpers from the directional trainer.
from train_model import (  # noqa: E402
    STEP_BARS_15M, DEFAULT_SYMBOLS, _mom16,
    _fetch_ohlcv_full, _fetch_funding_history, _fetch_oi_history,
)
from shared import features as F  # noqa: E402
from shared import indicators as ind_lib  # noqa: E402
from shared import cross_validation as cv  # noqa: E402

# Model is "useful" only if OOF R^2 beats the naive baseline by at least this.
MIN_R2_GAIN = 0.02


def build_dataset(days, horizon_bars, symbols):
    import ccxt
    exchange = ccxt.binance({"enableRateLimit": True,
                             "options": {"defaultType": "future"}})
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_cut = now_ms - days * 24 * 3600 * 1000

    data = {}
    for sym in symbols:
        print(f"Fetching {sym} ...")
        c15 = _fetch_ohlcv_full(exchange, sym, "15m", now_ms - (days + 2) * 86400000)
        c1h = _fetch_ohlcv_full(exchange, sym, "1h", now_ms - (days + 7) * 86400000)
        c4h = _fetch_ohlcv_full(exchange, sym, "4h", now_ms - (days + 30) * 86400000)
        if not (c15 and len(c15) > 200):
            continue
        sym_raw = sym.replace("/", "")
        since = now_ms - (days + 2) * 86400000
        funding = _fetch_funding_history(sym_raw, since)
        oi_hist = _fetch_oi_history(sym_raw, since)
        data[sym] = (c15, c1h, c4h, funding, oi_hist)

    X, y, baseline, ts_index = [], [], [], []
    for sym, (c15, c1h, c4h, funding, oi_hist) in data.items():
        print(f"Labelling {sym} ({len(c15)} bars) ...")
        start_idx = next((i for i, c in enumerate(c15) if c[0] >= start_cut), 150)
        start_idx = max(start_idx, 150)
        for step in range(start_idx, len(c15) - STEP_BARS_15M, STEP_BARS_15M):
            ts = c15[step][0]
            w15 = c15[max(0, step - 200):step + 1]
            w1h = [c for c in c1h if c[0] <= ts][-200:]
            w4h = [c for c in c4h if c[0] <= ts][-120:]
            if len(w15) < 60:
                continue

            # Forward realised vol over the next `horizon` bars (the label).
            fwd_vol = F.forward_realized_vol(c15[step:], horizon_bars)
            if fwd_vol <= 0:
                continue

            indicators = ind_lib.compute_all_indicators(w15, w1h, w4h, as_of_ts_ms=ts)
            rsi = ind_lib.compute_rsi(w15)

            my_mom = _mom16(w15)
            peers = []
            for osym, (oc15, _, _, _, _) in data.items():
                if osym == sym:
                    continue
                oslice = [c for c in oc15 if c[0] <= ts]
                if len(oslice) >= 17:
                    peers.append(_mom16(oslice))
            xs_rank = F.cross_sectional_rank(my_mom, peers) if peers else 0.5

            positioning = F.funding_and_positioning(
                [r for r in funding if r[0] <= ts],
                [r for r in oi_hist if r[0] <= ts],
            )

            feats = F.build_features(w15, w1h, w4h, rsi=rsi,
                                     indicators=indicators, xs_momentum_rank=xs_rank,
                                     positioning=positioning)
            vec = F.features_to_vector(feats)

            # Naive baseline forecast: trailing realised vol predicts forward vol.
            trailing_vol = feats["rvol_20"]
            if trailing_vol <= 0:
                continue

            X.append(vec)
            y.append(math.log(fwd_vol))            # regress in log space
            baseline.append(math.log(trailing_vol))
            ts_index.append(ts)

    order = sorted(range(len(ts_index)), key=lambda i: ts_index[i])
    X = [X[i] for i in order]
    y = [y[i] for i in order]
    baseline = [baseline[i] for i in order]
    return X, y, baseline


def _r2(actual, pred):
    import numpy as np
    a = np.asarray(actual, dtype=float)
    p = np.asarray(pred, dtype=float)
    ss_res = float(np.sum((a - p) ** 2))
    ss_tot = float(np.sum((a - a.mean()) ** 2))
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")


def _pearson(a, b):
    import numpy as np
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def train(X, y, baseline, horizon_bars, n_splits=5, embargo_pct=0.02):
    import numpy as np
    import lightgbm as lgb

    Xn = np.array(X, dtype=float)
    yn = np.array(y, dtype=float)
    bn = np.array(baseline, dtype=float)
    n = len(yn)
    print(f"\nDataset: {n} samples (regression on log forward-vol)")
    if n < 500:
        print("WARNING: small dataset (<500). Results will be noisy.")

    params = {
        "objective": "regression",
        "metric": "l2",
        "learning_rate": 0.03,
        "num_leaves": 15,
        "max_depth": 4,
        "min_data_in_leaf": 50,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "lambda_l1": 0.5,
        "lambda_l2": 0.5,
        "verbose": -1,
    }

    oof = np.full(n, np.nan)
    for fold, (tr, te) in enumerate(cv.purged_kfold_indices(
            n, n_splits=n_splits, label_horizon=horizon_bars, embargo_pct=embargo_pct)):
        if not tr or not te:
            continue
        dtrain = lgb.Dataset(Xn[tr], label=yn[tr])
        booster = lgb.train(params, dtrain, num_boost_round=400)
        oof[te] = booster.predict(Xn[te])
        fr2 = _r2(yn[te], oof[te])
        print(f"  fold {fold}: train={len(tr)} test={len(te)} R2={fr2:.4f}")

    valid = ~np.isnan(oof)
    model_r2 = _r2(yn[valid], oof[valid])
    base_r2 = _r2(yn[valid], bn[valid])
    model_r = _pearson(yn[valid], oof[valid])
    base_r = _pearson(yn[valid], bn[valid])

    print("\nForward-vol prediction (out-of-fold):")
    print(f"  MODEL    R2={model_r2:.4f}  Pearson r={model_r:.4f}")
    print(f"  BASELINE R2={base_r2:.4f}  Pearson r={base_r:.4f}  "
          f"(trailing rvol_20)")
    gain = model_r2 - base_r2
    print(f"  GAIN over baseline: {gain:+.4f} R2")

    useful = gain >= MIN_R2_GAIN and model_r2 > 0
    if useful:
        print(f"  ✅ Model beats baseline by >= {MIN_R2_GAIN} R2 — "
              f"useful for risk scaling.")
    else:
        print(f"  ⚠️  Model does NOT beat baseline by {MIN_R2_GAIN} R2 — "
              f"just use trailing vol (ATR); do not deploy vol model.")

    final = lgb.train(params, lgb.Dataset(Xn, label=yn), num_boost_round=400)
    return final, model_r2, base_r2, model_r, base_r, useful


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=360)
    ap.add_argument("--horizon", type=int, default=96,
                    help="forward window in 15m bars to predict vol over (96 = 24h)")
    ap.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    ap.add_argument("--out-dir", default=os.path.join(_ROOT, "data", "models"))
    args = ap.parse_args()

    print(f"Building vol dataset: {args.days}d, horizon={args.horizon} bars")
    X, y, baseline = build_dataset(args.days, args.horizon, args.symbols)
    final, m_r2, b_r2, m_r, b_r, useful = train(X, y, baseline, args.horizon)

    os.makedirs(args.out_dir, exist_ok=True)
    model_path = os.path.join(args.out_dir, "vol_model.txt")
    meta_path = os.path.join(args.out_dir, "vol_model.meta.json")
    final.save_model(model_path)
    meta = {
        "feature_names": F.FEATURE_NAMES,
        "model_r2": m_r2,
        "baseline_r2": b_r2,
        "model_pearson": m_r,
        "baseline_pearson": b_r,
        "beats_baseline": bool(useful),
        "label": "log_forward_realized_vol",
        "horizon_bars": args.horizon,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_samples": len(y),
        "days": args.days,
        "symbols": args.symbols,
    }
    with open(meta_path, "w") as fh:
        json.dump(meta, fh, indent=2)
    print(f"\nSaved model → {model_path}")
    print(f"Saved meta  → {meta_path}")


if __name__ == "__main__":
    main()
