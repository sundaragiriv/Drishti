"""ML-driven exit monitor for open Director-cluster positions.

Each open position is scored daily:
  - Time stop: exit if days_held >= 30
  - Stop loss: exit if current_close <= entry - 2.0 x atr14 (also held by IBKR bracket)
  - Target:    exit if current_close >= entry + 2.0 x stop_dist (also held by IBKR bracket)
  - ML exit:   exit if p(exit-better) > 0.55 from the ml_exit_model.pkl
  - Regime:    exit if SPY (or composite) drops below its 200-day SMA

For positions executed via IBKR brackets, the stop / target are managed
server-side by IBKR; this exiter handles ML + Regime + Time exits.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger

from signal_scanner.institutional_intel.config import safe_duckdb_connect

ML_HOLD = 30
ML_STOP_X = 2.0
ML_TGT_R = 2.0
ML_EXIT_THRESHOLD = 0.55
MODEL_PATH = Path(r"e:\Quant-Bridge\research\artifacts\insider_strategy\ml_exit_model.pkl")


_FEATURE_COLS = [
    "days_elapsed", "days_until_ruled_exit", "curr_return_pct",
    "mfe_pct", "mae_pct", "atr_pct_of_entry",
    "n_directors", "n_insiders", "n_officers", "log_total_value",
]


def _load_model():
    """Lazy-load the ML exit model trained by research/insider_strategy_backtest.py."""
    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"ML exit model not found at {MODEL_PATH}. "
            "Run: python -m research.insider_strategy_backtest --train-ml"
        )
    import joblib
    return joblib.load(MODEL_PATH)


def _load_path_since_entry(ticker: str, entry_date: str, as_of: date
                            ) -> Optional[pd.DataFrame]:
    """Pull daily bars from entry through as_of for one ticker. Returns None
    if insufficient bars."""
    con = safe_duckdb_connect(read_only=True)
    if con is None:
        return None
    try:
        rows = con.execute(f"""
            SELECT trade_date, open, high, low, close
            FROM fact_daily_prices
            WHERE ticker='{ticker}'
              AND trade_date BETWEEN DATE '{entry_date}' AND DATE '{as_of}'
            ORDER BY trade_date
        """).fetchall()
    finally:
        try:
            con.close()
        except Exception:
            pass
    if len(rows) < 2:
        return None
    return pd.DataFrame(rows, columns=["trade_date", "open", "high", "low", "close"])


def evaluate_position(position: Dict, as_of: date,
                       model=None, regime_ok: bool = True
                       ) -> Tuple[bool, str, Optional[float]]:
    """For one open position, decide whether to exit today.

    Returns (should_exit, reason, exit_price). exit_price is the close to use
    if reason in {ML, TIME, REGIME}. For STOP / TARGET, IBKR fills handle it.
    """
    entry_price = float(position["entry_price"])
    entry_date = str(position["entry_date"])
    atr14 = float(position["atr14"] or 0.02 * entry_price)
    stop_atr = float(position.get("stop_atr_mult", ML_STOP_X))
    tgt_r = float(position.get("target_r_mult", ML_TGT_R))
    time_stop = int(position.get("time_stop_days", ML_HOLD))

    stop_dist = stop_atr * atr14
    stop_px = entry_price - stop_dist
    target_px = entry_price + tgt_r * stop_dist

    path = _load_path_since_entry(position["ticker"], entry_date, as_of)
    if path is None or len(path) < 2:
        return (False, "INSUFFICIENT_DATA", None)

    today_close = float(path["close"].iloc[-1])
    days_elapsed = len(path) - 1  # entry was bar 0
    days_until_ruled_exit = max(0, time_stop - days_elapsed)

    # Highs/lows from day 1..today
    sub = path.iloc[1:]
    if len(sub) == 0:
        return (False, "INSUFFICIENT_DATA", None)

    # Stop / target (these are normally executed by IBKR brackets; this is a
    # safety check for SIM mode or to catch if brackets failed).
    if sub["low"].min() <= stop_px:
        return (True, "STOP", float(stop_px))
    if sub["high"].max() >= target_px:
        return (True, "TARGET", float(target_px))

    # Time stop
    if days_elapsed >= time_stop:
        return (True, "TIME", today_close)

    # Regime exit
    if not regime_ok:
        return (True, "REGIME", today_close)

    # ML exit — score today's state
    if model is None:
        try:
            model = _load_model()
        except FileNotFoundError as e:
            logger.warning(f"[INSIDER-EXIT] {e}")
            return (False, "NO_MODEL", None)

    running_max = max(entry_price, float(sub["high"].max()))
    running_min = min(entry_price, float(sub["low"].min()))
    curr_ret_pct = (today_close / entry_price - 1.0) * 100
    mfe_pct = (running_max / entry_price - 1.0) * 100
    mae_pct = (running_min / entry_price - 1.0) * 100
    atr_pct = (atr14 / entry_price) * 100

    feat = pd.DataFrame([{
        "days_elapsed": days_elapsed,
        "days_until_ruled_exit": days_until_ruled_exit,
        "curr_return_pct": curr_ret_pct,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "atr_pct_of_entry": atr_pct,
        "n_directors": int(position["n_directors"]),
        "n_insiders": int(position["n_insiders"]),
        "n_officers": int(position.get("n_officers", 0)),
        "log_total_value": float(np.log1p(float(position.get("total_value", 0)))),
    }])
    p_exit_better = float(model.predict_proba(feat[_FEATURE_COLS])[0, 1])
    if p_exit_better > ML_EXIT_THRESHOLD:
        return (True, "ML", today_close)

    return (False, "HOLD", today_close)
