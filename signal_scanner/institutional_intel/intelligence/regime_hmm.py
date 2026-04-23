"""Daily Hidden Markov Model for market regime detection.

Uses hmmlearn GaussianHMM with 5 states to classify the market into:
    State 0: Bull Trend      — low vol, positive returns
    State 1: Mean Reversion  — medium vol, range-bound
    State 2: Accumulation    — low vol, flat returns, building
    State 3: Distribution    — rising vol, fading returns
    State 4: Crash/Panic     — high vol, negative returns

Features fed to HMM:
    1. Log returns
    2. Range (high-low)/close  (intraday volatility proxy)
    3. Volume change (pct)

The HMM is fit on a rolling window (default 252 days) and can be refit
periodically.  State labels are assigned post-fit by sorting states on
mean return (ascending), so State 4 is always the worst-return regime
and State 0 is always the best.

Usage (standalone validation):
    python -m signal_scanner.institutional_intel.intelligence.regime_hmm --validate
    python -m signal_scanner.institutional_intel.intelligence.regime_hmm --current

Usage (from code):
    from signal_scanner.institutional_intel.intelligence.regime_hmm import DailyRegimeHMM
    hmm = DailyRegimeHMM()
    hmm.fit_from_db()
    state, probs = hmm.current_regime()
"""

from __future__ import annotations

import argparse
import pickle
import warnings
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM
from loguru import logger

from signal_scanner.institutional_intel.config import safe_duckdb_connect

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

N_STATES = 5
TRAIN_WINDOW = 252          # trading days for fit window
MIN_BARS = 120              # minimum bars to attempt fitting
RANDOM_SEED = 42
N_ITER = 200                # EM iterations
COV_TYPE = "full"           # covariance type

# Canonical regime names (index = canonical label after sorting)
REGIME_NAMES = {
    0: "CRASH",         # lowest mean return
    1: "DISTRIBUTION",  # negative lean
    2: "ACCUMULATION",  # near-zero / flat
    3: "MEAN_REVERSION",# slightly positive
    4: "BULL_TREND",    # highest mean return
}

# Trading rules per regime
REGIME_LONG_ALLOWED = {0: False, 1: False, 2: True, 3: True, 4: True}
REGIME_SHORT_ALLOWED = {0: True, 1: True, 2: False, 3: True, 4: False}
REGIME_ANY_TRADE = {0: False, 1: True, 2: True, 3: True, 4: True}

MODEL_DIR = Path("data/warehouse/models")
MODEL_PATH = MODEL_DIR / "regime_hmm_daily.pkl"


# ---------------------------------------------------------------------------
# Core class
# ---------------------------------------------------------------------------

class DailyRegimeHMM:
    """Daily market regime detector using Gaussian HMM."""

    def __init__(self, n_states: int = N_STATES, train_window: int = TRAIN_WINDOW):
        self.n_states = n_states
        self.train_window = train_window
        self._model: Optional[GaussianHMM] = None
        self._label_map: Optional[Dict[int, int]] = None  # raw state → canonical
        self._fit_date: Optional[date] = None
        self._ticker: str = ""

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    @staticmethod
    def load_daily_bars(
        ticker: str = "SPY",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        fallback_tickers: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """Load OHLCV daily bars from DuckDB.

        If the primary ticker doesn't have recent data, falls back to
        computing a composite from fallback_tickers.
        """
        conn = safe_duckdb_connect(read_only=True)
        if not conn:
            raise RuntimeError("Cannot connect to DuckDB")

        try:
            where = [f"ticker = '{ticker}'"]
            if start_date:
                where.append(f"trade_date >= '{start_date}'")
            if end_date:
                where.append(f"trade_date <= '{end_date}'")

            sql = f"""
                SELECT trade_date, open, high, low, close, volume
                FROM fact_daily_prices
                WHERE {' AND '.join(where)}
                ORDER BY trade_date
            """
            df = conn.execute(sql).fetchdf()

            if len(df) < MIN_BARS and fallback_tickers:
                logger.info(
                    "HMM: {} has only {} bars, building composite from {}",
                    ticker, len(df), fallback_tickers,
                )
                df = DailyRegimeHMM._build_composite(
                    conn, fallback_tickers, start_date, end_date
                )

            return df
        finally:
            conn.close()

    @staticmethod
    def _build_composite(
        conn, tickers: List[str],
        start_date: Optional[str], end_date: Optional[str],
    ) -> pd.DataFrame:
        """Build equal-weight composite OHLCV from multiple tickers."""
        ticker_list = ",".join(f"'{t}'" for t in tickers)
        where = [f"ticker IN ({ticker_list})"]
        if start_date:
            where.append(f"trade_date >= '{start_date}'")
        if end_date:
            where.append(f"trade_date <= '{end_date}'")

        sql = f"""
            SELECT trade_date,
                   AVG(open) as open, AVG(high) as high,
                   AVG(low) as low, AVG(close) as close,
                   SUM(volume) as volume
            FROM fact_daily_prices
            WHERE {' AND '.join(where)}
            GROUP BY trade_date
            HAVING COUNT(DISTINCT ticker) >= {max(1, len(tickers) // 2)}
            ORDER BY trade_date
        """
        return conn.execute(sql).fetchdf()

    # ------------------------------------------------------------------
    # Feature engineering
    # ------------------------------------------------------------------

    @staticmethod
    def compute_features(df: pd.DataFrame) -> np.ndarray:
        """Compute HMM observation features from OHLCV bars.

        Returns (N, 3) array: [log_return, range_pct, volume_change]
        """
        close = df["close"].values.astype(float)
        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        volume = df["volume"].values.astype(float)

        # Log returns
        log_ret = np.diff(np.log(close))

        # Range / close (volatility proxy)
        range_pct = (high[1:] - low[1:]) / close[1:]

        # Volume pct change
        vol_chg = np.diff(volume) / (volume[:-1] + 1e-10)
        vol_chg = np.clip(vol_chg, -5.0, 5.0)  # cap extreme outliers

        features = np.column_stack([log_ret, range_pct, vol_chg])

        # Replace any NaN/inf
        features = np.nan_to_num(features, nan=0.0, posinf=0.0, neginf=0.0)
        return features

    # ------------------------------------------------------------------
    # Model fitting
    # ------------------------------------------------------------------

    def fit(self, features: np.ndarray, ticker: str = "SPY") -> None:
        """Fit the GaussianHMM on feature matrix."""
        if len(features) < MIN_BARS:
            raise ValueError(
                f"Need >= {MIN_BARS} bars, got {len(features)}"
            )

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")

            # Try full covariance first; fall back to diagonal if it fails
            model = None
            for cov in (COV_TYPE, "diag"):
                try:
                    m = GaussianHMM(
                        n_components=self.n_states,
                        covariance_type=cov,
                        n_iter=N_ITER,
                        random_state=RANDOM_SEED,
                        verbose=False,
                    )
                    m.fit(features)
                    model = m
                    break
                except ValueError:
                    if cov == "diag":
                        raise
                    logger.debug("HMM: full covariance failed, trying diag")
            if model is None:
                raise RuntimeError("HMM fit failed with all covariance types")

        # Sort states by mean return (first feature = log returns)
        # so that state 0 = lowest return (crash), state 4 = highest (bull)
        mean_returns = model.means_[:, 0]
        sorted_indices = np.argsort(mean_returns)
        self._label_map = {int(sorted_indices[i]): i for i in range(self.n_states)}

        self._model = model
        self._ticker = ticker
        self._fit_date = date.today()

        # Log state characteristics
        for raw_idx in range(self.n_states):
            canonical = self._label_map[raw_idx]
            name = REGIME_NAMES.get(canonical, f"STATE_{canonical}")
            mean_ret = model.means_[raw_idx, 0] * 100
            mean_range = model.means_[raw_idx, 1] * 100
            logger.info(
                "HMM State {} ({}): mean_ret={:+.3f}%/day, mean_range={:.3f}%",
                canonical, name, mean_ret, mean_range,
            )

    def fit_from_db(
        self,
        ticker: str = "SPY",
        end_date: Optional[str] = None,
        fallback_tickers: Optional[List[str]] = None,
    ) -> None:
        """Load data from DB and fit the model."""
        if fallback_tickers is None:
            fallback_tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"]

        end = end_date or str(date.today())
        start = str(date.fromisoformat(end) - timedelta(days=int(self.train_window * 1.6)))

        df = self.load_daily_bars(
            ticker=ticker,
            start_date=start,
            end_date=end,
            fallback_tickers=fallback_tickers,
        )
        if len(df) < MIN_BARS:
            raise ValueError(
                f"Insufficient data for {ticker}: {len(df)} bars "
                f"(need {MIN_BARS})"
            )

        features = self.compute_features(df)

        # Use only last train_window bars for fitting
        if len(features) > self.train_window:
            features = features[-self.train_window:]

        self.fit(features, ticker=ticker)

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, features: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Predict canonical regime labels and state probabilities.

        Returns:
            labels: (N,) array of canonical state labels (0-4)
            probs:  (N, n_states) array of state probabilities (canonical order)
        """
        if self._model is None:
            raise RuntimeError("Model not fitted — call fit() or load() first")

        raw_states = self._model.predict(features)
        raw_probs = self._model.predict_proba(features)

        # Map raw to canonical
        labels = np.array([self._label_map[int(s)] for s in raw_states])

        # Reorder probability columns to canonical order
        inv_map = {v: k for k, v in self._label_map.items()}
        canonical_probs = np.zeros_like(raw_probs)
        for canonical_idx in range(self.n_states):
            raw_idx = inv_map[canonical_idx]
            canonical_probs[:, canonical_idx] = raw_probs[:, raw_idx]

        return labels, canonical_probs

    def current_regime(self) -> Tuple[int, np.ndarray, str]:
        """Get the current (latest bar) regime state.

        Returns (canonical_label, probability_vector, regime_name).
        Loads fresh data from DB if model is fitted.
        """
        if self._model is None:
            raise RuntimeError("Model not fitted")

        # Load recent data for prediction
        end = str(date.today())
        start = str(date.today() - timedelta(days=60))
        fallback = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"]

        df = self.load_daily_bars(
            ticker=self._ticker,
            start_date=start,
            end_date=end,
            fallback_tickers=fallback,
        )
        features = self.compute_features(df)

        labels, probs = self.predict(features)
        current_label = int(labels[-1])
        current_probs = probs[-1]
        name = REGIME_NAMES.get(current_label, f"STATE_{current_label}")

        return current_label, current_probs, name

    # ------------------------------------------------------------------
    # Trading gates
    # ------------------------------------------------------------------

    def is_long_allowed(self, state: Optional[int] = None) -> bool:
        """Check if LONG entries are allowed in the given regime."""
        if state is None:
            state, _, _ = self.current_regime()
        return REGIME_LONG_ALLOWED.get(state, False)

    def is_short_allowed(self, state: Optional[int] = None) -> bool:
        """Check if SHORT entries are allowed in the given regime."""
        if state is None:
            state, _, _ = self.current_regime()
        return REGIME_SHORT_ALLOWED.get(state, False)

    def is_any_trade_allowed(self, state: Optional[int] = None) -> bool:
        """Check if any trading is allowed (False only for CRASH)."""
        if state is None:
            state, _, _ = self.current_regime()
        return REGIME_ANY_TRADE.get(state, False)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Optional[Path] = None) -> None:
        """Save fitted model to disk."""
        path = path or MODEL_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": self._model,
            "label_map": self._label_map,
            "fit_date": self._fit_date,
            "ticker": self._ticker,
            "n_states": self.n_states,
            "train_window": self.train_window,
        }
        with open(path, "wb") as f:
            pickle.dump(payload, f)
        logger.info("HMM model saved to {}", path)

    def load(self, path: Optional[Path] = None) -> None:
        """Load a previously fitted model."""
        path = path or MODEL_PATH
        if not path.exists():
            raise FileNotFoundError(f"No saved model at {path}")
        with open(path, "rb") as f:
            payload = pickle.load(f)
        self._model = payload["model"]
        self._label_map = payload["label_map"]
        self._fit_date = payload["fit_date"]
        self._ticker = payload["ticker"]
        self.n_states = payload["n_states"]
        self.train_window = payload["train_window"]
        logger.info(
            "HMM model loaded from {} (fitted {} on {})",
            path, self._fit_date, self._ticker,
        )


# ---------------------------------------------------------------------------
# Walk-forward validation
# ---------------------------------------------------------------------------

def walk_forward_validate(
    ticker: str = "SPY",
    train_days: int = 252,
    test_days: int = 63,
    fallback_tickers: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Run walk-forward validation of the HMM regime model.

    Trains on `train_days`, tests on next `test_days`, slides forward.
    Returns a DataFrame with columns:
        trade_date, close, log_return, regime, regime_name,
        regime_prob, strategy_return (regime-filtered)
    """
    if fallback_tickers is None:
        fallback_tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"]

    # Load full history
    df = DailyRegimeHMM.load_daily_bars(
        ticker=ticker,
        fallback_tickers=fallback_tickers,
    )
    if len(df) < train_days + test_days:
        raise ValueError(
            f"Need >= {train_days + test_days} bars, got {len(df)}"
        )

    features = DailyRegimeHMM.compute_features(df)
    dates = df["trade_date"].values[1:]  # features are diff-based, lose 1 bar
    closes = df["close"].values[1:]
    log_rets = features[:, 0]

    results = []
    step = 0
    n_folds = 0

    while step + train_days + test_days <= len(features):
        train_feat = features[step : step + train_days]
        test_feat = features[step + train_days : step + train_days + test_days]
        test_dates = dates[step + train_days : step + train_days + test_days]
        test_closes = closes[step + train_days : step + train_days + test_days]
        test_rets = log_rets[step + train_days : step + train_days + test_days]

        # Fit on train window
        hmm = DailyRegimeHMM(n_states=N_STATES, train_window=train_days)
        try:
            hmm.fit(train_feat, ticker=ticker)
        except Exception as e:
            logger.warning("HMM fit failed at step {}: {}", step, e)
            step += test_days
            continue

        # Predict on test window
        labels, probs = hmm.predict(test_feat)

        for i in range(len(test_dates)):
            regime = int(labels[i])
            name = REGIME_NAMES.get(regime, f"STATE_{regime}")
            regime_prob = float(probs[i, regime])
            ret = float(test_rets[i])

            # Strategy: hold long in states 2,3,4 (accumulation, mean-rev, bull)
            # Short in states 0,1 (crash, distribution) — simplified as flat
            if REGIME_LONG_ALLOWED.get(regime, False):
                strat_ret = ret  # long
            elif REGIME_SHORT_ALLOWED.get(regime, False) and not REGIME_LONG_ALLOWED.get(regime, False):
                strat_ret = 0.0  # flat (conservative — don't short in backtest)
            else:
                strat_ret = 0.0  # crash = cash

            results.append({
                "trade_date": test_dates[i],
                "close": float(test_closes[i]),
                "log_return": ret,
                "regime": regime,
                "regime_name": name,
                "regime_prob": regime_prob,
                "strategy_return": strat_ret,
            })

        step += test_days
        n_folds += 1

    logger.info("Walk-forward complete: {} folds, {} out-of-sample bars", n_folds, len(results))
    return pd.DataFrame(results)


def print_validation_report(results: pd.DataFrame) -> Dict:
    """Print and return walk-forward validation metrics."""
    if results.empty:
        print("No results to report.")
        return {}

    total_bars = len(results)
    bh_cum = np.exp(results["log_return"].sum()) - 1
    strat_cum = np.exp(results["strategy_return"].sum()) - 1

    # Annualized metrics
    years = total_bars / 252
    bh_annual = (1 + bh_cum) ** (1 / max(years, 0.1)) - 1
    strat_annual = (1 + strat_cum) ** (1 / max(years, 0.1)) - 1

    # Sharpe (daily)
    bh_sharpe = (results["log_return"].mean() / results["log_return"].std()) * np.sqrt(252) if results["log_return"].std() > 0 else 0
    strat_sharpe = (results["strategy_return"].mean() / results["strategy_return"].std()) * np.sqrt(252) if results["strategy_return"].std() > 0 else 0

    # Max drawdown
    bh_equity = np.exp(results["log_return"].cumsum())
    bh_peak = np.maximum.accumulate(bh_equity)
    bh_dd = ((bh_equity - bh_peak) / bh_peak).min()

    strat_equity = np.exp(results["strategy_return"].cumsum())
    strat_peak = np.maximum.accumulate(strat_equity)
    strat_dd = ((strat_equity - strat_peak) / strat_peak).min()

    # Regime distribution
    regime_counts = results["regime"].value_counts().sort_index()

    # Win rate by regime (positive return days)
    regime_wr = {}
    for regime in sorted(results["regime"].unique()):
        subset = results[results["regime"] == regime]
        wr = (subset["log_return"] > 0).mean() * 100
        avg_ret = subset["log_return"].mean() * 100
        regime_wr[regime] = {"wr": wr, "avg_ret": avg_ret, "count": len(subset)}

    # State 0 (crash) drawdown capture
    crash_bars = results[results["regime"] == 0]
    crash_loss = crash_bars["log_return"].sum() if len(crash_bars) > 0 else 0
    total_neg = results[results["log_return"] < 0]["log_return"].sum()
    crash_capture = (crash_loss / total_neg * 100) if total_neg < 0 else 0

    print("\n" + "=" * 70)
    print("  HMM REGIME MODEL — WALK-FORWARD VALIDATION REPORT")
    print("=" * 70)
    print(f"\n  Out-of-sample bars: {total_bars} ({years:.1f} years)")
    print(f"  Walk-forward folds: {total_bars // 63}")

    print(f"\n  {'Metric':<30} {'Buy & Hold':>12} {'HMM Strategy':>12}")
    print(f"  {'-'*30} {'-'*12} {'-'*12}")
    print(f"  {'Cumulative Return':<30} {bh_cum:>11.1%} {strat_cum:>11.1%}")
    print(f"  {'Annualized Return':<30} {bh_annual:>11.1%} {strat_annual:>11.1%}")
    print(f"  {'Sharpe Ratio':<30} {bh_sharpe:>12.2f} {strat_sharpe:>12.2f}")
    print(f"  {'Max Drawdown':<30} {bh_dd:>11.1%} {strat_dd:>11.1%}")

    print(f"\n  Regime Distribution (out-of-sample):")
    for regime in sorted(regime_wr.keys()):
        info = regime_wr[regime]
        name = REGIME_NAMES.get(regime, f"STATE_{regime}")
        pct = info["count"] / total_bars * 100
        print(
            f"    State {regime} ({name:15s}): "
            f"{info['count']:5d} bars ({pct:4.1f}%)  "
            f"WR={info['wr']:5.1f}%  AvgRet={info['avg_ret']:+.3f}%/day"
        )

    print(f"\n  Crash State (0) Loss Capture: {crash_capture:.1f}% of total negative returns")
    print(f"  Avoided by strategy: {-crash_loss*100:.1f}% cumulative loss")

    print("=" * 70)

    return {
        "total_bars": total_bars,
        "years": years,
        "bh_return": bh_cum,
        "strat_return": strat_cum,
        "bh_sharpe": bh_sharpe,
        "strat_sharpe": strat_sharpe,
        "bh_max_dd": bh_dd,
        "strat_max_dd": strat_dd,
        "crash_capture_pct": crash_capture,
        "regime_stats": regime_wr,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Daily HMM Regime Detector")
    parser.add_argument("--validate", action="store_true", help="Run walk-forward validation")
    parser.add_argument("--current", action="store_true", help="Show current regime")
    parser.add_argument("--fit-save", action="store_true", help="Fit on latest data and save model")
    parser.add_argument(
        "--ticker", default="SPY",
        help="Primary ticker for regime detection (default: SPY)",
    )
    parser.add_argument(
        "--fallback", default="AAPL,MSFT,NVDA,GOOGL,AMZN",
        help="Comma-separated fallback tickers for composite",
    )
    args = parser.parse_args()
    fallback = [t.strip() for t in args.fallback.split(",")]

    if args.validate:
        print(f"Running walk-forward validation on {args.ticker}...")
        results = walk_forward_validate(
            ticker=args.ticker,
            fallback_tickers=fallback,
        )
        metrics = print_validation_report(results)

        # Save validation results
        out_path = Path("data/warehouse/models/regime_hmm_validation.csv")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        results.to_csv(out_path, index=False)
        print(f"\nValidation results saved to {out_path}")

    elif args.current:
        hmm = DailyRegimeHMM()
        if MODEL_PATH.exists():
            hmm.load()
        else:
            print("No saved model — fitting from DB...")
            hmm.fit_from_db(ticker=args.ticker, fallback_tickers=fallback)
            hmm.save()

        state, probs, name = hmm.current_regime()
        print(f"\nCurrent Market Regime: State {state} — {name}")
        print(f"Probabilities:")
        for i in range(hmm.n_states):
            rname = REGIME_NAMES.get(i, f"STATE_{i}")
            bar = "#" * int(probs[i] * 40)
            print(f"  {i} {rname:15s} {probs[i]:6.1%}  {bar}")
        print(f"\nLONG allowed: {hmm.is_long_allowed(state)}")
        print(f"SHORT allowed: {hmm.is_short_allowed(state)}")
        print(f"Any trade allowed: {hmm.is_any_trade_allowed(state)}")

    elif args.fit_save:
        hmm = DailyRegimeHMM()
        hmm.fit_from_db(ticker=args.ticker, fallback_tickers=fallback)
        hmm.save()
        state, probs, name = hmm.current_regime()
        print(f"\nModel fitted and saved. Current regime: {name} (State {state})")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
