#!/usr/bin/env python3
"""
Offline trainer for the LightGBM signal model.

Pipeline:
  1. Fetch historical OHLCV (15m/1h/4h) for a basket of liquid symbols from
     Binance public API (same source as the backtester).
  2. Walk each symbol in 4h steps. At every step build the SAME feature vector
     the live Brain builds (shared.features) using only PAST bars — no leakage.
  3. Label each sample with the TRIPLE-BARRIER method: simulate an entry at the
     next bar open with an ATR-based SL and R:R take-profit; label 1 if TP is hit
     before SL within the holding window, else 0.
  4. Train LightGBM with PURGED K-FOLD cross-validation (embargo) and report
     out-of-fold AUC. Pick the probability threshold that maximises expected
     value at the configured R:R.
  5. Persist booster + metadata sidecar to data/models/.

Run on a machine with lightgbm/numpy installed (the live container or a dev box):
    python scripts/train_model.py --days 360 --rr 2.5 --horizon 192

Requires: ccxt, numpy, lightgbm, scikit-learn (for AUC).
"""

import argparse
import json
import os
import sys
import time
import urllib.request as _urllib
from datetime import datetime, timezone

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import features as F
from shared import indicators as ind_lib
from shared import cross_validation as cv

# Step interval: 4h in 15m bars (matches backtester walk-forward cadence).
STEP_BARS_15M = 16

DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT",
]


def _binance_get(path, params):
    """Binance Futures public REST GET. Returns parsed JSON or []."""
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"https://fapi.binance.com{path}?{qs}"
    try:
        with _urllib.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  Binance API error {path}: {e}")
        return []


def _fetch_funding_history(sym_raw, since_ms):
    """Fetch full funding-rate history. Returns [(ts_ms, rate), ...] ascending."""
    out, cur = [], since_ms
    while True:
        batch = _binance_get("/fapi/v1/fundingRate",
                             {"symbol": sym_raw, "startTime": cur, "limit": 1000})
        if not batch:
            break
        for r in batch:
            out.append((int(r["fundingTime"]), float(r["fundingRate"])))
        if len(batch) < 1000:
            break
        cur = int(batch[-1]["fundingTime"]) + 1
        time.sleep(0.1)
    return sorted(out, key=lambda x: x[0])


def _fetch_oi_history(sym_raw, since_ms):
    """Fetch 4h open-interest history. Returns [(ts_ms, oi), ...] ascending."""
    out, cur = [], since_ms
    while True:
        batch = _binance_get("/futures/data/openInterestHist",
                             {"symbol": sym_raw, "period": "4h",
                              "startTime": cur, "limit": 500})
        if not batch:
            break
        for r in batch:
            out.append((int(r["timestamp"]), float(r["sumOpenInterest"])))
        if len(batch) < 500:
            break
        cur = int(batch[-1]["timestamp"]) + 1
        time.sleep(0.1)
    return sorted(out, key=lambda x: x[0])


def _fetch_basis_history(sym_raw, since_ms):
    """Fetch 4h perp-basis history. Returns [(ts_ms, basis_rate), ...] ascending."""
    out, cur = [], since_ms
    while True:
        batch = _binance_get("/futures/data/basis",
                             {"symbol": sym_raw, "contractType": "PERPETUAL",
                              "period": "4h", "startTime": cur, "limit": 500})
        if not batch:
            break
        for r in batch:
            out.append((int(r["timestamp"]), float(r["basisRate"])))
        if len(batch) < 500:
            break
        cur = int(batch[-1]["timestamp"]) + 1
        time.sleep(0.1)
    return sorted(out, key=lambda x: x[0])


def _fetch_ohlcv_full(exchange, symbol, timeframe, since_ms, limit=1000):
    out = []
    cur = since_ms
    while True:
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=cur, limit=limit)
        except Exception as e:
            print(f"  fetch error {symbol}/{timeframe}: {e}")
            break
        if not batch:
            break
        out.extend(batch)
        if len(batch) < limit:
            break
        cur = batch[-1][0] + 1
        time.sleep(0.15)
    seen, uniq = set(), []
    for c in out:
        if c[0] not in seen:
            seen.add(c[0])
            uniq.append(c)
    return sorted(uniq, key=lambda x: x[0])


def _triple_barrier_label(future_bars, entry_price, sl_pct, tp_pct, horizon_bars):
    """Return 1 if TP hit before SL within horizon, 0 otherwise.

    future_bars: 15m bars AFTER entry (entry is at future_bars[0] open).
    """
    sl_price = entry_price * (1 - sl_pct / 100.0)
    tp_price = entry_price * (1 + tp_pct / 100.0)
    for bar in future_bars[:horizon_bars]:
        high, low = float(bar[2]), float(bar[3])
        hit_sl = low <= sl_price
        hit_tp = high >= tp_price
        if hit_sl and hit_tp:
            return 0  # conservative: assume SL first when both in same bar
        if hit_sl:
            return 0
        if hit_tp:
            return 1
    return 0  # neither barrier => treat as non-win (time-stop)


def build_dataset(days, rr, horizon_bars, symbols):
    import ccxt
    exchange = ccxt.binance({"enableRateLimit": True,
                             "options": {"defaultType": "future"}})
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_cut = now_ms - days * 24 * 3600 * 1000

    # Pre-fetch all symbols.
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
        basis_hist = _fetch_basis_history(sym_raw, since)
        data[sym] = (c15, c1h, c4h, funding, oi_hist, basis_hist)

    X, y, ts_index = [], [], []
    # For cross-sectional rank we need each symbol's 16-bar momentum at each ts.
    for sym, (c15, c1h, c4h, funding, oi_hist, basis_hist) in data.items():
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

            indicators = ind_lib.compute_all_indicators(w15, w1h, w4h, as_of_ts_ms=ts)
            rsi = ind_lib.compute_rsi(w15)
            price = float(w15[-1][4])

            # Cross-sectional momentum rank vs basket at this timestamp.
            my_mom = _mom16(w15)
            peers = []
            for osym, (oc15, _, _, _, _, _) in data.items():
                if osym == sym:
                    continue
                oslice = [c for c in oc15 if c[0] <= ts]
                if len(oslice) >= 17:
                    peers.append(_mom16(oslice))
            xs_rank = F.cross_sectional_rank(my_mom, peers) if peers else 0.5

            # Positioning features (look-ahead-safe: only data at or before ts).
            positioning = F.funding_and_positioning(
                [r for r in funding if r[0] <= ts],
                [r for r in oi_hist if r[0] <= ts],
                [r for r in basis_hist if r[0] <= ts],
            )

            feats = F.build_features(w15, w1h, w4h, rsi=rsi,
                                     indicators=indicators, xs_momentum_rank=xs_rank,
                                     positioning=positioning)
            vec = F.features_to_vector(feats)

            # Triple-barrier label using ATR-based SL and R:R TP.
            atr = indicators.get("atr")
            sl_pct = max((atr / price * 100 * 1.5), 1.2) if (atr and price > 0) else 1.5
            tp_pct = sl_pct * rr
            entry = float(c15[step + 1][1]) if step + 1 < len(c15) else price
            label = _triple_barrier_label(
                c15[step + 1:], entry, sl_pct, tp_pct, horizon_bars)

            X.append(vec)
            y.append(label)
            ts_index.append(ts)

    # Sort by timestamp so purged CV sees chronological order.
    order = sorted(range(len(ts_index)), key=lambda i: ts_index[i])
    X = [X[i] for i in order]
    y = [y[i] for i in order]
    return X, y


def _mom16(candles):
    if len(candles) < 17:
        return 0.0
    a, b = float(candles[-17][4]), float(candles[-1][4])
    return (b - a) / a if a > 0 else 0.0


def train(X, y, horizon_bars, rr, n_splits=5, embargo_pct=0.02):
    import numpy as np
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score

    Xn = np.array(X, dtype=float)
    yn = np.array(y, dtype=int)
    n = len(yn)
    print(f"\nDataset: {n} samples, positive rate = {yn.mean():.3f}")
    if n < 500:
        print("WARNING: small dataset (<500). Results will be noisy.")

    params = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.03,
        "num_leaves": 15,          # shallow — guard against overfitting tabular data
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
        booster = lgb.train(params, dtrain, num_boost_round=300)
        oof[te] = booster.predict(Xn[te])
        try:
            auc = roc_auc_score(yn[te], oof[te])
            print(f"  fold {fold}: train={len(tr)} test={len(te)} AUC={auc:.4f}")
        except ValueError:
            print(f"  fold {fold}: AUC undefined (single class in test)")

    valid = ~np.isnan(oof)
    cv_auc = roc_auc_score(yn[valid], oof[valid]) if valid.sum() > 0 else float("nan")
    print(f"\nPurged out-of-fold AUC: {cv_auc:.4f}")

    # Choose buy threshold maximising expected value at this R:R on OOF preds.
    best_thr, best_ev = 0.5, -1e9
    for thr in [x / 100 for x in range(30, 75)]:
        picked = valid & (oof >= thr)
        if picked.sum() < 20:
            continue
        wr = yn[picked].mean()
        ev = wr * rr - (1 - wr)  # per-trade EV in R units
        if ev > best_ev:
            best_ev, best_thr = ev, thr
    print(f"Selected buy_threshold={best_thr:.2f} (OOF EV={best_ev:+.3f}R, "
          f"trades={int((valid & (oof >= best_thr)).sum())})")

    # Final fit on ALL data for production.
    final = lgb.train(params, lgb.Dataset(Xn, label=yn), num_boost_round=300)
    return final, cv_auc, best_thr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=360)
    ap.add_argument("--rr", type=float, default=2.5, help="reward:risk for labels")
    ap.add_argument("--horizon", type=int, default=192,
                    help="holding window in 15m bars (192 = 48h)")
    ap.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    ap.add_argument("--out-dir", default=os.path.join(_ROOT, "data", "models"))
    args = ap.parse_args()

    print(f"Building dataset: {args.days}d, R:R={args.rr}, horizon={args.horizon} bars")
    X, y = build_dataset(args.days, args.rr, args.horizon, args.symbols)
    booster, cv_auc, thr = train(X, y, args.horizon, args.rr)

    os.makedirs(args.out_dir, exist_ok=True)
    model_path = os.path.join(args.out_dir, "signal_model.txt")
    meta_path = os.path.join(args.out_dir, "signal_model.meta.json")
    booster.save_model(model_path)
    meta = {
        "feature_names": F.FEATURE_NAMES,
        "buy_threshold": thr,
        "cv_auc": cv_auc,
        "label_horizon_bars": args.horizon,
        "label_rr": args.rr,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_samples": len(y),
        "positive_rate": sum(y) / len(y) if y else 0.0,
        "days": args.days,
        "symbols": args.symbols,
    }
    with open(meta_path, "w") as fh:
        json.dump(meta, fh, indent=2)
    print(f"\nSaved model → {model_path}")
    print(f"Saved meta  → {meta_path}")
    if cv_auc < 0.52:
        print("\n⚠️  CV AUC < 0.52 — the model has little/no edge over random. "
              "Do NOT deploy to ml mode; keep decision_mode=code.")


if __name__ == "__main__":
    main()
