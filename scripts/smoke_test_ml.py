#!/usr/bin/env python3
"""
Offline smoke test for the ML pipeline — NO network required.

Exercises the full path with synthetic candles so structural bugs (feature
vector length, signature drift, model save/load, AUC guard, build_ml_analysis
schema) are caught locally before deploying to the server.

Run:  python scripts/smoke_test_ml.py
Exit code 0 = all checks passed.
"""

import math
import os
import random
import sys
import tempfile

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import features as F
from shared import indicators as ind_lib
from shared import cross_validation as cv

random.seed(42)

_FAILS = []


def check(cond, msg):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {msg}")
    if not cond:
        _FAILS.append(msg)


def synth_candles(n, start_ts, step_ms, p0=100.0, drift=0.0, vol=0.01):
    """Geometric-random-walk OHLCV candles: [ts, o, h, l, c, v]."""
    out = []
    price = p0
    for i in range(n):
        o = price
        ret = random.gauss(drift, vol)
        c = max(0.01, o * math.exp(ret))
        hi = max(o, c) * (1 + abs(random.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(random.gauss(0, vol / 2)))
        v = abs(random.gauss(1000, 300))
        out.append([start_ts + i * step_ms, o, hi, lo, c, v])
        price = c
    return out


def synth_funding(n, start_ts, step_ms=8 * 3600 * 1000):
    return [(start_ts + i * step_ms, random.gauss(0.0001, 0.0002)) for i in range(n)]


def synth_oi(n, start_ts, step_ms=4 * 3600 * 1000):
    base = 1_000_000.0
    out = []
    for i in range(n):
        base *= math.exp(random.gauss(0, 0.02))
        out.append((start_ts + i * step_ms, base))
    return out


def test_features():
    print("\n== features ==")
    c15 = synth_candles(300, 0, 15 * 60 * 1000)
    c1h = synth_candles(300, 0, 60 * 60 * 1000)
    c4h = synth_candles(200, 0, 4 * 60 * 60 * 1000)
    ts = c15[-1][0]
    indicators = ind_lib.compute_all_indicators(c15, c1h, c4h, as_of_ts_ms=ts)
    rsi = ind_lib.compute_rsi(c15)

    pos = F.funding_and_positioning(synth_funding(20, 0), synth_oi(10, 0))
    check(set(pos.keys()) == {"funding_rate", "funding_bias", "oi_change_pct"},
          f"positioning keys = {sorted(pos.keys())}")

    feats = F.build_features(c15, c1h, c4h, rsi=rsi, indicators=indicators,
                             xs_momentum_rank=0.6, positioning=pos)
    check(set(feats.keys()) == set(F.FEATURE_NAMES),
          "build_features keys exactly match FEATURE_NAMES")
    missing = [n for n in F.FEATURE_NAMES if n not in feats]
    extra = [k for k in feats if k not in F.FEATURE_NAMES]
    check(not missing, f"no missing features (missing={missing})")
    check(not extra, f"no extra features (extra={extra})")

    vec = F.features_to_vector(feats)
    check(len(vec) == len(F.FEATURE_NAMES),
          f"vector length {len(vec)} == {len(F.FEATURE_NAMES)} feature names")
    check(all(isinstance(x, float) for x in vec), "all vector elements are float")
    check(all(not math.isnan(x) and not math.isinf(x) for x in vec),
          "no NaN/Inf in vector")

    # Empty / degenerate inputs must not crash and must stay correct length.
    pos_empty = F.funding_and_positioning([], [])
    check(pos_empty == {"funding_rate": 0.0, "funding_bias": 0.0, "oi_change_pct": 0.0},
          "empty positioning returns neutral zeros")
    feats2 = F.build_features(c15, c1h, c4h, rsi=rsi, indicators=indicators)
    check(len(F.features_to_vector(feats2)) == len(F.FEATURE_NAMES),
          "vector length stable when positioning omitted")
    return c15, c1h, c4h


def test_data_tuple_unpacking():
    print("\n== data dict tuple unpacking ==")
    # build_dataset stores a 5-element tuple: (c15, c1h, c4h, funding, oi_hist)
    c15 = synth_candles(50, 0, 15 * 60 * 1000)
    dummy_data = {"BTC/USDT": (c15, [], [], [], []), "ETH/USDT": (c15, [], [], [], [])}
    try:
        for _osym, (_oc15, _, _, _, _) in dummy_data.items():
            pass
        check(True, "5-element tuple unpacking succeeds")
    except ValueError as e:
        check(False, f"tuple unpack failed: {e}")


def test_cross_validation():
    print("\n== purged k-fold ==")
    n = 500
    folds = list(cv.purged_kfold_indices(n, n_splits=5, label_horizon=20,
                                         embargo_pct=0.02))
    check(len(folds) == 5, f"5 folds produced (got {len(folds)})")
    for i, (tr, te) in enumerate(folds):
        check(len(set(tr) & set(te)) == 0, f"fold {i}: train/test disjoint")
        check(len(te) > 0 and len(tr) > 0, f"fold {i}: both sets non-empty")
    all_test = sorted(idx for _, te in folds for idx in te)
    check(all_test == list(range(n)), "every sample is in exactly one test fold")


def test_train_and_serve(c15, c1h, c4h):
    print("\n== train + serve (synthetic) ==")
    import numpy as np
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score

    # Build a learnable synthetic dataset: label depends on a couple of features
    # plus noise, so AUC should land comfortably above 0.52.
    nfeat = len(F.FEATURE_NAMES)
    X, y = [], []
    rng = random.Random(7)
    for _ in range(1500):
        v = [rng.gauss(0, 1) for _ in range(nfeat)]
        signal = 0.9 * v[0] - 0.6 * v[3] + rng.gauss(0, 0.5)
        y.append(1 if signal > 0 else 0)
        X.append(v)
    Xn = np.array(X, dtype=float)
    yn = np.array(y, dtype=int)

    params = {"objective": "binary", "metric": "auc", "num_leaves": 15,
              "max_depth": 4, "min_data_in_leaf": 50, "verbose": -1}
    oof = np.full(len(yn), np.nan)
    for tr, te in cv.purged_kfold_indices(len(yn), n_splits=5, label_horizon=10,
                                          embargo_pct=0.02):
        if not tr or not te:
            continue
        booster = lgb.train(params, lgb.Dataset(Xn[tr], label=yn[tr]),
                            num_boost_round=100)
        oof[te] = booster.predict(Xn[te])
    valid = ~np.isnan(oof)
    auc = roc_auc_score(yn[valid], oof[valid])
    check(auc > 0.52, f"synthetic learnable AUC {auc:.3f} > 0.52 (sanity)")

    # Save a model + meta and exercise MLSignalModel load + guard rail.
    from shared import ml_model as mlmod
    final = lgb.train(params, lgb.Dataset(Xn, label=yn), num_boost_round=100)

    tmp = tempfile.mkdtemp()
    mpath = os.path.join(tmp, "signal_model.txt")
    metapath = os.path.join(tmp, "signal_model.meta.json")
    final.save_model(mpath)

    import json
    # 1) Good model: AUC above guard.
    with open(metapath, "w") as fh:
        json.dump({"feature_names": F.FEATURE_NAMES, "buy_threshold": 0.5,
                   "cv_auc": 0.60}, fh)
    m = mlmod.MLSignalModel(mpath, metapath)
    check(m.is_available(), "model with AUC 0.60 loads and is available")
    p = m.predict_proba([0.0] * len(F.FEATURE_NAMES))
    check(p is not None and 0.0 <= p <= 1.0, f"predict_proba returns prob ({p})")

    # 2) Weak model: AUC below guard must be rejected.
    with open(metapath, "w") as fh:
        json.dump({"feature_names": F.FEATURE_NAMES, "buy_threshold": 0.5,
                   "cv_auc": 0.5129}, fh)
    m2 = mlmod.MLSignalModel(mpath, metapath)
    check(not m2.is_available(), "model with AUC 0.5129 is REJECTED by guard")
    check("AUC" in (m2.load_error or ""), f"guard error mentions AUC ({m2.load_error})")

    # 3) Feature mismatch must be rejected.
    with open(metapath, "w") as fh:
        json.dump({"feature_names": F.FEATURE_NAMES[:-1], "buy_threshold": 0.5,
                   "cv_auc": 0.60}, fh)
    m3 = mlmod.MLSignalModel(mpath, metapath)
    check(not m3.is_available(), "model with feature mismatch is REJECTED")

    return mpath, metapath


def test_build_ml_analysis(c15, c1h, c4h, mpath, metapath):
    print("\n== build_ml_analysis ==")
    import json
    from shared import ml_model as mlmod
    with open(metapath, "w") as fh:
        json.dump({"feature_names": F.FEATURE_NAMES, "buy_threshold": 0.4,
                   "cv_auc": 0.60}, fh)
    model = mlmod.MLSignalModel(mpath, metapath)
    ts = c15[-1][0]
    indicators = ind_lib.compute_all_indicators(c15, c1h, c4h, as_of_ts_ms=ts)
    pos = F.funding_and_positioning(synth_funding(20, 0), synth_oi(10, 0))

    analysis, sig_id, p_win = mlmod.build_ml_analysis(
        model, symbol="BTC/USDT", price=float(c15[-1][4]),
        rsi=55.0, rvol=1.2, candles_15m=c15, candles_1h=c1h, candles_4h=c4h,
        indicators=indicators, strategy="CONSERVATIVE", btc_bias="NEUTRAL",
        regime_ctx={"regime": "BULL_TRENDING", "active_strategies": ["CONSERVATIVE"]},
        xs_momentum_rank=0.6, positioning=pos,
    )
    required = {"verdict", "confidence", "stop_loss_pct", "take_profit_pct",
                "rr_ratio", "reason", "decision_mode", "setup_grade"}
    missing_keys = required - set(analysis.keys())
    check(not missing_keys,
          f"analysis has required schema keys (missing={missing_keys})")
    check(analysis["decision_mode"] == "ml", "decision_mode == 'ml'")
    check(analysis["verdict"] in ("BUY", "WAIT"),
          f"verdict valid ({analysis['verdict']})")
    check(p_win is not None and 0.0 <= p_win <= 1.0, f"p_win in [0,1] ({p_win})")

    # Hard gate: STRONG_BEARISH BTC must block regardless of model.
    a2, _, _ = mlmod.build_ml_analysis(
        model, symbol="BTC/USDT", price=float(c15[-1][4]),
        rsi=55.0, rvol=1.2, candles_15m=c15, candles_1h=c1h, candles_4h=c4h,
        indicators=indicators, strategy="CONSERVATIVE", btc_bias="STRONG_BEARISH",
        regime_ctx=None, xs_momentum_rank=0.6, positioning=pos,
    )
    check(a2["verdict"] == "WAIT", "STRONG_BEARISH BTC forces WAIT (hard gate)")

    # Regime gate: strategy not in active list must block.
    a3, _, _ = mlmod.build_ml_analysis(
        model, symbol="BTC/USDT", price=float(c15[-1][4]),
        rsi=55.0, rvol=1.2, candles_15m=c15, candles_1h=c1h, candles_4h=c4h,
        indicators=indicators, strategy="CONSERVATIVE", btc_bias="NEUTRAL",
        regime_ctx={"regime": "BEAR_TRENDING", "active_strategies": []},
        xs_momentum_rank=0.6, positioning=pos,
    )
    check(a3["verdict"] == "WAIT", "regime block forces WAIT (hard gate)")


def test_vol_pipeline(c15):
    print("\n== forward-vol regression pipeline ==")
    import numpy as np
    import lightgbm as lgb

    # Forward-vol label must be non-negative and need enough future bars.
    fv = F.forward_realized_vol(c15, horizon=16)
    check(fv >= 0.0, f"forward_realized_vol non-negative ({fv:.4f})")
    fv_short = F.forward_realized_vol(c15[:5], horizon=16)
    check(fv_short == 0.0, "forward_realized_vol returns 0.0 on insufficient bars")

    # Learnable synthetic regression: target depends on features + noise.
    nfeat = len(F.FEATURE_NAMES)
    X, y, base = [], [], []
    rng = random.Random(11)
    for _ in range(1500):
        v = [rng.gauss(0, 1) for _ in range(nfeat)]
        target = 0.8 * v[3] + 0.4 * v[5] + rng.gauss(0, 0.5)
        X.append(v)
        y.append(target)
        base.append(v[3])  # a weak baseline correlated with target
    Xn = np.array(X, dtype=float)
    yn = np.array(y, dtype=float)

    params = {"objective": "regression", "metric": "l2", "num_leaves": 15,
              "max_depth": 4, "min_data_in_leaf": 50, "verbose": -1}
    oof = np.full(len(yn), np.nan)
    for tr, te in cv.purged_kfold_indices(len(yn), n_splits=5, label_horizon=10,
                                          embargo_pct=0.02):
        if not tr or not te:
            continue
        bst = lgb.train(params, lgb.Dataset(Xn[tr], label=yn[tr]), num_boost_round=100)
        oof[te] = bst.predict(Xn[te])
    valid = ~np.isnan(oof)
    ss_res = float(np.sum((yn[valid] - oof[valid]) ** 2))
    ss_tot = float(np.sum((yn[valid] - yn[valid].mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot
    check(r2 > 0.3, f"synthetic regression OOF R2 {r2:.3f} > 0.3 (sanity)")


def main():
    print("ML pipeline smoke test (offline, synthetic data)")
    c15, c1h, c4h = test_features()
    test_data_tuple_unpacking()
    test_cross_validation()
    mpath, metapath = test_train_and_serve(c15, c1h, c4h)
    test_build_ml_analysis(c15, c1h, c4h, mpath, metapath)
    test_vol_pipeline(c15)

    print("\n" + "=" * 50)
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} check(s)")
        for f in _FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
