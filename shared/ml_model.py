"""
LightGBM signal model wrapper — calibrated probability of profit.

Design principles:
  * GRACEFUL DEGRADATION. lightgbm/numpy are imported lazily. If the library or
    the trained model file is missing, `MLSignalModel.is_available()` returns
    False and the Brain falls back to the deterministic `code` engine. The bot
    NEVER crashes because ML isn't installed.
  * TRAIN/SERVE PARITY. Features come exclusively from shared.features so the
    exact same code path builds vectors in training and in production.
  * GUARDRAILS PRESERVED. The same hard macro/regime gates that protect the
    code engine are applied to the ML path — the model decides entry QUALITY,
    it cannot override capital-preservation rules.

The model predicts P(trade hits take-profit before stop-loss within the holding
window) — a triple-barrier binary label. That probability:
  (a) decides the verdict (BUY when p >= buy_threshold), and
  (b) feeds directly into Kelly sizing as the win-rate estimate.
"""

import json
import os
import uuid

from shared import features as F
from shared import decision as dec

# Default artefact locations (text Booster + JSON metadata sidecar).
# Derive the models dir from the SAME data directory the DB uses so Docker volume
# mounts stay consistent (DATABASE_PATH=/data/... → model at /data/models/...).
def _default_models_dir() -> str:
    override = os.getenv("ML_MODEL_DIR")
    if override:
        return override
    db_path = os.getenv("DATABASE_PATH")
    if db_path:
        return os.path.join(os.path.dirname(db_path), "models")
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "models"
    )


_DEFAULT_DIR = _default_models_dir()
DEFAULT_MODEL_PATH = os.path.join(_DEFAULT_DIR, "signal_model.txt")
DEFAULT_META_PATH = os.path.join(_DEFAULT_DIR, "signal_model.meta.json")

MIN_AUC = 0.52  # models below this have no meaningful edge over random


class MLSignalModel:
    """Lazy-loaded LightGBM booster producing P(win) for a feature vector."""

    def __init__(self, model_path: str = DEFAULT_MODEL_PATH,
                 meta_path: str = DEFAULT_META_PATH):
        self.model_path = model_path
        self.meta_path = meta_path
        self._booster = None
        self._meta = None
        self._load_error = None
        self._tried = False

    # -- lifecycle ---------------------------------------------------------

    def _try_load(self) -> None:
        """Attempt to load the booster + metadata exactly once."""
        if self._tried:
            return
        self._tried = True
        try:
            if not os.path.exists(self.model_path):
                self._load_error = f"model file not found: {self.model_path}"
                return
            import lightgbm as lgb  # lazy — only needed when ML mode is active
            self._booster = lgb.Booster(model_file=self.model_path)
            if os.path.exists(self.meta_path):
                with open(self.meta_path, "r") as fh:
                    self._meta = json.load(fh)
            else:
                self._meta = {}
            # Validate feature ordering matches what we serve.
            trained_feats = (self._meta or {}).get("feature_names")
            if trained_feats and trained_feats != F.FEATURE_NAMES:
                self._load_error = (
                    "feature_names mismatch between model metadata and "
                    "shared.features.FEATURE_NAMES — retrain required"
                )
                self._booster = None
                return
            # Enforce AUC guard: refuse to serve a model with no real edge.
            cv_auc = float((self._meta or {}).get("cv_auc", 1.0))
            if cv_auc < MIN_AUC:
                self._load_error = (
                    f"CV AUC {cv_auc:.4f} < {MIN_AUC} — model has no edge; "
                    "retrain or keep decision_mode=code"
                )
                self._booster = None
        except Exception as e:  # ImportError, corrupt file, etc.
            self._load_error = f"{type(e).__name__}: {e}"
            self._booster = None

    def is_available(self) -> bool:
        self._try_load()
        return self._booster is not None

    @property
    def load_error(self):
        self._try_load()
        return self._load_error

    @property
    def buy_threshold(self) -> float:
        self._try_load()
        return float((self._meta or {}).get("buy_threshold", 0.5))

    @property
    def metadata(self) -> dict:
        self._try_load()
        return dict(self._meta or {})

    # -- inference ---------------------------------------------------------

    def predict_proba(self, feature_vector: list) -> float:
        """Return P(win) in [0,1] for one ordered feature vector, or None if
        the model is unavailable."""
        if not self.is_available():
            return None
        try:
            import numpy as np
            x = np.array([feature_vector], dtype=float)
            p = float(self._booster.predict(x)[0])
            return max(0.0, min(1.0, p))
        except Exception:
            return None


def build_ml_analysis(
    model: MLSignalModel,
    symbol: str,
    price: float,
    rsi,
    rvol,
    candles_15m: list,
    candles_1h: list,
    candles_4h: list = None,
    indicators: dict = None,
    strategy: str = "CONSERVATIVE",
    btc_bias: str = "NEUTRAL",
    regime_ctx: dict = None,
    xs_momentum_rank: float = 0.5,
    positioning: dict = None,
) -> tuple:
    """Produce an analysis dict (same schema as decision.make_decision) using the
    ML probability for the verdict. Returns (analysis, signal_id, p_win).

    Returns p_win=None when the model could not score (caller should fall back).
    Hard macro/regime gates are enforced here so ML cannot bypass them.
    """
    signal_id = str(uuid.uuid4())
    ind = indicators or {}
    strat = strategy.upper()
    profile = dec.STRATEGY_PROFILES.get(strat, dec.STRATEGY_PROFILES["CONSERVATIVE"])

    # ---- Hard gates (identical to the code engine) -----------------------
    block, block_reason = dec._btc_bias_gate(btc_bias)
    regime_block = False
    regime_reason = ""
    if regime_ctx:
        allowed = regime_ctx.get("active_strategies", [])
        if allowed and strat not in allowed:
            regime_block = True
            regime_reason = f"{strat} not active in {regime_ctx.get('regime')} regime"

    # ---- Descriptive context (reuse code-engine detectors for UI parity) -
    ema_1h = ind.get("ema_stack_1h")
    trend_1h = dec._detect_trend_1h(candles_1h, ema_1h)
    setup_15m = dec._detect_setup_15m(candles_15m, rsi, ind)
    candle_patterns = dec._detect_candle_patterns(candles_15m)
    vol_verdict = dec._volume_verdict(candles_15m, rvol)

    # ---- Risk levels (reuse proven ATR-based structural SL/TP) -----------
    sl_pct = dec._compute_sl_pct(ind, price, strat)
    tp_pct = round(sl_pct * profile["min_rr"], 2)
    rr = round(tp_pct / sl_pct, 2) if sl_pct > 0 else 0.0

    def _wait(reason, conf=0):
        return {
            "trend_1h": trend_1h, "setup_15m": setup_15m,
            "candle_patterns": candle_patterns, "volume_verdict": vol_verdict,
            "confluence_signals": [], "conflicting_signals": [reason],
            "setup_grade": "C", "verdict": "WAIT",
            "stop_loss_pct": 0.0, "take_profit_pct": 0.0, "rr_ratio": 0.0,
            "confidence": conf, "reason": reason, "decision_mode": "ml",
        }, signal_id, None

    if block:
        return _wait(f"BTC macro block: {block_reason}")
    if regime_block:
        return _wait(f"Regime block: {regime_reason}")

    # ---- Feature vector + probability ------------------------------------
    feats = F.build_features(
        candles_15m, candles_1h, candles_4h or [],
        rsi=rsi, indicators=ind, xs_momentum_rank=xs_momentum_rank,
        positioning=positioning,
    )
    vector = F.features_to_vector(feats)
    p_win = model.predict_proba(vector)
    if p_win is None:
        # signal the caller to fall back to the code engine
        return _wait("ML model unavailable — fall back to code engine")

    threshold = model.buy_threshold
    confidence = int(round(p_win * 100))
    confidence = max(0, min(95, confidence))

    # Grade for UI: tiers above the buy threshold.
    if p_win >= threshold + 0.10:
        grade = "A"
    elif p_win >= threshold:
        grade = "B"
    else:
        grade = "C"

    if p_win >= threshold:
        verdict = "BUY"
        reason = (
            f"ML P(win)={p_win:.2f} >= {threshold:.2f}. {trend_1h} 1h, "
            f"{setup_15m} 15m. SL {sl_pct:.2f}% / TP {tp_pct:.2f}% (R:R {rr:.2f})."
        )
    else:
        verdict = "WAIT"
        reason = f"ML P(win)={p_win:.2f} < {threshold:.2f} buy threshold."

    confluence = [f"ML P(profit) {p_win:.1%}"] if verdict == "BUY" else []
    analysis = {
        "trend_1h": trend_1h,
        "setup_15m": setup_15m,
        "candle_patterns": candle_patterns,
        "volume_verdict": vol_verdict,
        "confluence_signals": confluence,
        "conflicting_signals": [] if verdict == "BUY" else [reason],
        "setup_grade": grade,
        "verdict": verdict,
        "stop_loss_pct": sl_pct if verdict == "BUY" else 0.0,
        "take_profit_pct": tp_pct if verdict == "BUY" else 0.0,
        "rr_ratio": rr if verdict == "BUY" else 0.0,
        "confidence": confidence,
        "reason": reason,
        "decision_mode": "ml",
        "ml_p_win": round(p_win, 4),
    }
    return analysis, signal_id, p_win
