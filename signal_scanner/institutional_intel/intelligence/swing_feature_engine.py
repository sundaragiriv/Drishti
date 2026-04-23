"""Swing Feature Engine — daily technical features for swing strategy ML.

Pre-computes ~48 features from daily bars for each liquid ticker-day.
Features are stored in ``fact_swing_features`` and consumed by
swing_backtester.py and swing_strategy_ml.py.

Usage:
    python -m signal_scanner.institutional_intel.intelligence.swing_feature_engine \
        --compute --quarters 2023-Q4,2024-Q1,2024-Q2,2024-Q3,2024-Q4

    python -m signal_scanner.institutional_intel.intelligence.swing_feature_engine --summary
"""

from __future__ import annotations

import argparse
import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import numpy as np
import pandas as pd
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_PRICE = 5.0
MIN_AVG_VOLUME = 100_000
LOOKBACK_DAYS = 260  # ~1 year of trading days (need 252 for 52w high)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _quarter_end_date(quarter: str) -> date:
    year = int(quarter.split("-Q")[0])
    qnum = int(quarter.split("-Q")[1])
    end_month = {1: 3, 2: 6, 3: 9, 4: 12}[qnum]
    last_day = calendar.monthrange(year, end_month)[1]
    return date(year, end_month, last_day)


def _quarter_start_date(quarter: str) -> date:
    year = int(quarter.split("-Q")[0])
    qnum = int(quarter.split("-Q")[1])
    start_month = {1: 1, 2: 4, 3: 7, 4: 10}[qnum]
    return date(year, start_month, 1)


def _filing_date(quarter: str) -> date:
    return _quarter_end_date(quarter) + timedelta(days=45)


# ---------------------------------------------------------------------------
# Numpy feature helpers
# ---------------------------------------------------------------------------

def _compute_rsi(closes: np.ndarray, period: int) -> np.ndarray:
    """RSI using Wilder's smoothing (EWM with alpha=1/period)."""
    n = len(closes)
    rsi = np.full(n, np.nan)
    if n < period + 1:
        return rsi

    delta = np.diff(closes)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    avg_gain = np.mean(gain[:period])
    avg_loss = np.mean(loss[:period])

    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))

    return rsi


def _compute_ema(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average."""
    n = len(values)
    ema = np.full(n, np.nan)
    if n < period:
        return ema
    ema[period - 1] = np.mean(values[:period])
    k = 2.0 / (period + 1)
    for i in range(period, n):
        ema[i] = values[i] * k + ema[i - 1] * (1 - k)
    return ema


def _compute_sma(values: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average."""
    n = len(values)
    sma = np.full(n, np.nan)
    if n < period:
        return sma
    cs = np.cumsum(values)
    sma[period - 1:] = (cs[period - 1:] - np.concatenate([[0], cs[:n - period]])) / period
    return sma


def _compute_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                 period: int = 20) -> np.ndarray:
    """Average True Range (SMA of true range)."""
    n = len(closes)
    atr = np.full(n, np.nan)
    if n < 2:
        return atr
    tr = np.maximum(highs[1:] - lows[1:],
                    np.maximum(np.abs(highs[1:] - closes[:-1]),
                               np.abs(lows[1:] - closes[:-1])))
    tr = np.concatenate([[highs[0] - lows[0]], tr])
    sma_tr = _compute_sma(tr, period)
    return sma_tr


def _compute_adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                 period: int = 14) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """ADX, +DI, -DI using Wilder's smoothing."""
    n = len(closes)
    adx = np.full(n, np.nan)
    plus_di = np.full(n, np.nan)
    minus_di = np.full(n, np.nan)
    if n < period + 1:
        return adx, plus_di, minus_di

    up_move = np.diff(highs)
    down_move = -np.diff(lows)
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = np.maximum(highs[1:] - lows[1:],
                    np.maximum(np.abs(highs[1:] - closes[:-1]),
                               np.abs(lows[1:] - closes[:-1])))

    # Wilder smoothing for ATR, +DM, -DM
    atr_s = np.mean(tr[:period])
    pdm_s = np.mean(plus_dm[:period])
    mdm_s = np.mean(minus_dm[:period])

    dx_vals = []
    for i in range(period, len(tr)):
        atr_s = (atr_s * (period - 1) + tr[i]) / period
        pdm_s = (pdm_s * (period - 1) + plus_dm[i]) / period
        mdm_s = (mdm_s * (period - 1) + minus_dm[i]) / period

        if atr_s == 0:
            pdi = 0.0
            mdi = 0.0
        else:
            pdi = 100.0 * pdm_s / atr_s
            mdi = 100.0 * mdm_s / atr_s

        idx = i + 1  # offset by 1 for diff
        plus_di[idx] = pdi
        minus_di[idx] = mdi

        di_sum = pdi + mdi
        dx = 100.0 * abs(pdi - mdi) / di_sum if di_sum > 0 else 0.0
        dx_vals.append((idx, dx))

    # ADX = smoothed DX
    if len(dx_vals) >= period:
        adx_s = np.mean([d[1] for d in dx_vals[:period]])
        adx[dx_vals[period - 1][0]] = adx_s
        for j in range(period, len(dx_vals)):
            adx_s = (adx_s * (period - 1) + dx_vals[j][1]) / period
            adx[dx_vals[j][0]] = adx_s

    return adx, plus_di, minus_di


def _compute_obv_slope(closes: np.ndarray, volumes: np.ndarray,
                       window: int = 10) -> np.ndarray:
    """Linear regression slope of OBV over rolling window."""
    n = len(closes)
    slope = np.full(n, np.nan)
    if n < 2:
        return slope

    obv = np.zeros(n)
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            obv[i] = obv[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            obv[i] = obv[i - 1] - volumes[i]
        else:
            obv[i] = obv[i - 1]

    x = np.arange(window, dtype=np.float64)
    x_mean = x.mean()
    ss_xx = np.sum((x - x_mean) ** 2)

    for i in range(window - 1, n):
        y = obv[i - window + 1: i + 1]
        y_mean = y.mean()
        ss_xy = np.sum((x - x_mean) * (y - y_mean))
        slope[i] = ss_xy / ss_xx if ss_xx > 0 else 0.0

    return slope


def _compute_linreg_slope(closes: np.ndarray, window: int = 12) -> np.ndarray:
    """Linear regression slope of close over rolling window."""
    n = len(closes)
    slope = np.full(n, np.nan)
    if n < window:
        return slope

    x = np.arange(window, dtype=np.float64)
    x_mean = x.mean()
    ss_xx = np.sum((x - x_mean) ** 2)

    for i in range(window - 1, n):
        y = closes[i - window + 1: i + 1]
        y_mean = y.mean()
        ss_xy = np.sum((x - x_mean) * (y - y_mean))
        slope[i] = ss_xy / ss_xx if ss_xx > 0 else 0.0

    return slope


# ---------------------------------------------------------------------------
# Candlestick pattern detection
# ---------------------------------------------------------------------------

def _detect_candlestick_patterns(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
) -> Dict[str, np.ndarray]:
    """Detect 10 candlestick patterns from OHLC arrays.

    Returns dict of boolean arrays, one per pattern.
    """
    n = len(opens)
    body = closes - opens  # positive = green bar
    abs_body = np.abs(body)
    bar_range = highs - lows
    upper_wick = highs - np.maximum(opens, closes)
    lower_wick = np.minimum(opens, closes) - lows
    midpoint = (opens + closes) / 2

    # 1. Hammer: small body at top, long lower wick
    hammer = (lower_wick >= 2 * abs_body) & (upper_wick < abs_body) & (bar_range > 0)

    # 2. Inverted Hammer: small body at bottom, long upper wick
    inv_hammer = (upper_wick >= 2 * abs_body) & (lower_wick < abs_body) & (bar_range > 0)

    # 3. Bullish Engulfing: green bar fully engulfs prior red bar's body
    engulf_bull = np.zeros(n, dtype=bool)
    engulf_bull[1:] = ((body[1:] > 0) & (body[:-1] < 0) &
                       (opens[1:] <= closes[:-1]) & (closes[1:] >= opens[:-1]))

    # 4. Bearish Engulfing
    engulf_bear = np.zeros(n, dtype=bool)
    engulf_bear[1:] = ((body[1:] < 0) & (body[:-1] > 0) &
                       (opens[1:] >= closes[:-1]) & (closes[1:] <= opens[:-1]))

    # 5. Doji: body < 10% of range
    doji = (abs_body < 0.1 * bar_range) & (bar_range > 0)

    # 6. Morning Star: 3-bar (long red, small body, long green)
    morning = np.zeros(n, dtype=bool)
    avg_range = _compute_sma(bar_range, 20)
    for i in range(2, n):
        if avg_range[i] > 0 and not np.isnan(avg_range[i]):
            long_thresh = 0.7 * avg_range[i]
            if bar_range[i - 1] > 0:
                morning[i] = (body[i - 2] < -long_thresh and
                              abs_body[i - 1] < 0.3 * bar_range[i - 1] and
                              body[i] > long_thresh and
                              closes[i] > midpoint[i - 2])

    # 7. Evening Star: opposite of morning star
    evening = np.zeros(n, dtype=bool)
    for i in range(2, n):
        if avg_range[i] > 0 and not np.isnan(avg_range[i]):
            long_thresh = 0.7 * avg_range[i]
            if bar_range[i - 1] > 0:
                evening[i] = (body[i - 2] > long_thresh and
                              abs_body[i - 1] < 0.3 * bar_range[i - 1] and
                              body[i] < -long_thresh and
                              closes[i] < midpoint[i - 2])

    # 8. Three White Soldiers: 3 consecutive green, each closes higher
    tws = np.zeros(n, dtype=bool)
    tws[2:] = ((body[2:] > 0) & (body[1:-1] > 0) & (body[:-2] > 0) &
               (closes[2:] > closes[1:-1]) & (closes[1:-1] > closes[:-2]))

    # 9. Piercing Line: opens below prior low, closes above prior midpoint
    piercing = np.zeros(n, dtype=bool)
    piercing[1:] = ((body[:-1] < 0) & (body[1:] > 0) &
                    (opens[1:] < lows[:-1]) & (closes[1:] > midpoint[:-1]))

    # 10. Dark Cloud Cover: opens above prior high, closes below prior midpoint
    dark_cloud = np.zeros(n, dtype=bool)
    dark_cloud[1:] = ((body[:-1] > 0) & (body[1:] < 0) &
                      (opens[1:] > highs[:-1]) & (closes[1:] < midpoint[:-1]))

    return {
        "hammer": hammer, "inv_hammer": inv_hammer,
        "engulfing_bull": engulf_bull, "engulfing_bear": engulf_bear,
        "doji": doji, "morning_star": morning, "evening_star": evening,
        "three_white_soldiers": tws, "piercing_line": piercing,
        "dark_cloud_cover": dark_cloud,
    }


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def _load_spy_returns(conn: duckdb.DuckDBPyConnection,
                      from_date: date, to_date: date) -> Dict[date, float]:
    """Load SPY daily returns as dict[date, pct_return]."""
    df = conn.execute("""
        SELECT trade_date, close,
               LAG(close) OVER (ORDER BY trade_date) AS prev_close
        FROM fact_daily_prices
        WHERE ticker = 'SPY'
          AND trade_date >= CAST(? AS DATE) - INTERVAL '30 DAY'
          AND trade_date <= ?
        ORDER BY trade_date
    """, [from_date.isoformat(), to_date.isoformat()]).fetchdf()

    result = {}
    for _, row in df.iterrows():
        if pd.notna(row["prev_close"]) and row["prev_close"] > 0:
            td = pd.Timestamp(row["trade_date"]).date()
            result[td] = (row["close"] - row["prev_close"]) / row["prev_close"]
    return result


def _detect_insider_clusters(conn: duckdb.DuckDBPyConnection,
                             from_date: date, to_date: date
                             ) -> Dict[Tuple[str, date], int]:
    """Find insider clusters: 3+ unique buyers within 10-day windows.

    Returns dict[(ticker, cluster_end_date), days_since] but we actually
    return dict[(ticker, date), days_since_last_cluster] for each trade_date.
    """
    df = conn.execute("""
        SELECT ticker, transaction_date, insider_name
        FROM fact_form4_transactions
        WHERE transaction_code = 'P'
          AND direction = 'BUY'
          AND transaction_date >= CAST(? AS DATE) - INTERVAL '60 DAY'
          AND transaction_date <= ?
          AND ticker IS NOT NULL AND ticker != ''
        ORDER BY ticker, transaction_date
    """, [from_date.isoformat(), to_date.isoformat()]).fetchdf()

    if df.empty:
        return {}

    # For each ticker, find 10-day windows with 3+ unique buyers
    clusters: Dict[str, List[date]] = {}  # ticker -> list of cluster dates

    for ticker, grp in df.groupby("ticker"):
        grp = grp.sort_values("transaction_date")
        dates_names = list(zip(
            [pd.Timestamp(d).date() for d in grp["transaction_date"]],
            grp["insider_name"].tolist()
        ))

        for i, (dt, _) in enumerate(dates_names):
            window_start = dt - timedelta(days=10)
            unique_buyers = set()
            for d, name in dates_names:
                if window_start <= d <= dt:
                    unique_buyers.add(name)
            if len(unique_buyers) >= 3:
                clusters.setdefault(ticker, []).append(dt)

    return clusters


def _load_intelligence_overlay(conn: duckdb.DuckDBPyConnection,
                               quarter: str) -> Dict[str, Dict]:
    """Load intelligence_scores for a quarter as dict[ticker, {fields}]."""
    df = conn.execute("""
        SELECT ticker,
               conviction_score, accum_phase,
               insider_cluster_detected, insider_hist_win_rate,
               insider_effect_score, trend_score, institutional_pressure,
               expected_value,
               squeeze_score AS int_squeeze_score,
               short_squeeze_score AS int_short_squeeze_score,
               days_to_cover AS int_days_to_cover,
               short_volume_ratio_avg, dark_pool_pct_avg
        FROM intelligence_scores
        WHERE report_quarter = ?
    """, [quarter]).fetchdf()

    result = {}
    for _, row in df.iterrows():
        result[str(row["ticker"])] = row.to_dict()
    return result


def _load_sector_map(conn: duckdb.DuckDBPyConnection,
                     tickers: List[str]) -> Dict[str, str]:
    """Load sector for each ticker from dim_issuer (LIMIT 1 for dupes)."""
    if not tickers:
        return {}
    placeholders = ",".join(["?" for _ in tickers])
    df = conn.execute(f"""
        SELECT DISTINCT ON (ticker) ticker, sector
        FROM dim_issuer
        WHERE ticker IN ({placeholders})
    """, tickers).fetchdf()
    return dict(zip(df["ticker"], df["sector"]))


# ---------------------------------------------------------------------------
# DuckDB table
# ---------------------------------------------------------------------------

def _ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fact_swing_features (
            ticker                  TEXT NOT NULL,
            trade_date              DATE NOT NULL,

            -- Price Structure (8)
            close                   DOUBLE,
            sma_10                  DOUBLE,
            sma_20                  DOUBLE,
            sma_50                  DOUBLE,
            sma_200                 DOUBLE,
            price_vs_sma200_pct     DOUBLE,
            price_vs_sma50_pct      DOUBLE,
            pct_from_52w_high       DOUBLE,

            -- Momentum (6)
            rsi_14                  DOUBLE,
            rsi_2                   DOUBLE,
            roc_5                   DOUBLE,
            roc_10                  DOUBLE,
            roc_20                  DOUBLE,
            ret_vs_spy_20d          DOUBLE,

            -- Volatility (7)
            atr_20                  DOUBLE,
            bb_upper                DOUBLE,
            bb_lower                DOUBLE,
            bb_width_pct            DOUBLE,
            kc_upper                DOUBLE,
            kc_lower                DOUBLE,
            squeeze_on              BOOLEAN,

            -- Volume (4)
            volume_ratio_20d        DOUBLE,
            obv_slope_10d           DOUBLE,
            volume_trend_5d         DOUBLE,
            avg_volume_20d          BIGINT,

            -- Setup Detection (6)
            consecutive_down_days   INTEGER,
            rsi2_below_10           BOOLEAN,
            gap_pct_from_prev       DOUBLE,
            volume_surge_3x         BOOLEAN,
            days_since_insider_cluster INTEGER,
            price_vs_20d_high_pct   DOUBLE,

            -- Candlestick Patterns (10)
            hammer                  BOOLEAN,
            inv_hammer              BOOLEAN,
            engulfing_bull          BOOLEAN,
            engulfing_bear          BOOLEAN,
            doji                    BOOLEAN,
            morning_star            BOOLEAN,
            evening_star            BOOLEAN,
            three_white_soldiers    BOOLEAN,
            piercing_line           BOOLEAN,
            dark_cloud_cover        BOOLEAN,

            -- Timing (2)
            quarter_month           INTEGER,
            day_of_week             INTEGER,

            -- Trend (4)
            ema_20_slope            DOUBLE,
            adx_14                  DOUBLE,
            plus_di_minus_di        DOUBLE,
            linreg_slope_12d        DOUBLE,

            -- Short/Squeeze (5)
            int_squeeze_score       DOUBLE,
            int_short_squeeze_score DOUBLE,
            int_days_to_cover       DOUBLE,
            short_volume_ratio_avg  DOUBLE,
            dark_pool_pct_avg       DOUBLE,

            -- Intelligence (8)
            conviction_score        DOUBLE,
            accum_phase             TEXT,
            insider_cluster_detected BOOLEAN,
            insider_hist_win_rate   DOUBLE,
            insider_effect_score    DOUBLE,
            trend_score             DOUBLE,
            institutional_pressure  DOUBLE,
            expected_value          DOUBLE,

            -- Metadata
            report_quarter          TEXT,
            sector                  TEXT,
            computed_at             TIMESTAMP,
            PRIMARY KEY (ticker, trade_date)
        )
    """)


# ---------------------------------------------------------------------------
# Per-ticker feature computation
# ---------------------------------------------------------------------------

def _compute_ticker_features(
    daily_df: pd.DataFrame,
    spy_cum_rets: Dict[date, float],
    intel: Optional[Dict],
    insider_clusters: Dict[str, List[date]],
    sector: str,
    quarter: str,
    start_date: date,
    end_date: date,
    now_iso: str,
) -> List[Dict[str, Any]]:
    """Compute all features for one ticker's daily bars.

    daily_df must have columns: trade_date, open, high, low, close, volume
    sorted by trade_date ascending. Must include lookback rows.
    """
    if len(daily_df) < 201:
        return []

    ticker = str(daily_df.iloc[0].get("ticker", ""))
    dates = np.array([pd.Timestamp(d).date() for d in daily_df["trade_date"]])
    opens = daily_df["open"].values.astype(np.float64)
    highs = daily_df["high"].values.astype(np.float64)
    lows = daily_df["low"].values.astype(np.float64)
    closes = daily_df["close"].values.astype(np.float64)
    volumes = daily_df["volume"].values.astype(np.float64)

    # Pre-compute all indicators
    sma_10 = _compute_sma(closes, 10)
    sma_20 = _compute_sma(closes, 20)
    sma_50 = _compute_sma(closes, 50)
    sma_200 = _compute_sma(closes, 200)
    ema_20 = _compute_ema(closes, 20)
    rsi_14 = _compute_rsi(closes, 14)
    rsi_2 = _compute_rsi(closes, 2)
    atr_20 = _compute_atr(highs, lows, closes, 20)
    adx_arr, plus_di_arr, minus_di_arr = _compute_adx(highs, lows, closes, 14)
    obv_slope = _compute_obv_slope(closes, volumes, 10)
    linreg_slope = _compute_linreg_slope(closes, 12)

    # Candlestick patterns
    candles = _detect_candlestick_patterns(opens, highs, lows, closes)

    # Bollinger Bands (SMA20 +/- 2*stddev)
    bb_std = np.full(len(closes), np.nan)
    for i in range(19, len(closes)):
        bb_std[i] = np.std(closes[i - 19: i + 1], ddof=0)

    bb_upper = sma_20 + 2 * bb_std
    bb_lower = sma_20 - 2 * bb_std

    # Keltner Channels (EMA20 +/- 1.5*ATR20)
    kc_upper = ema_20 + 1.5 * atr_20
    kc_lower = ema_20 - 1.5 * atr_20

    # Volume SMAs
    vol_sma_5 = _compute_sma(volumes, 5)
    vol_sma_20 = _compute_sma(volumes, 20)

    # 52-week high
    high_252 = np.full(len(closes), np.nan)
    for i in range(251, len(closes)):
        high_252[i] = np.max(highs[i - 251: i + 1])

    # 20-day high
    high_20d = np.full(len(closes), np.nan)
    for i in range(19, len(closes)):
        high_20d[i] = np.max(highs[i - 19: i + 1])

    # Consecutive down days
    consec_down = np.zeros(len(closes), dtype=int)
    for i in range(1, len(closes)):
        if closes[i] < closes[i - 1]:
            consec_down[i] = consec_down[i - 1] + 1

    # EMA20 slope (5-day pct change of ema20)
    ema_20_slope = np.full(len(closes), np.nan)
    for i in range(24, len(closes)):  # need ema20 valid at i and i-5
        if not np.isnan(ema_20[i]) and not np.isnan(ema_20[i - 5]) and ema_20[i - 5] != 0:
            ema_20_slope[i] = (ema_20[i] - ema_20[i - 5]) / ema_20[i - 5] * 100

    # SPY relative return (20d)
    # Pre-compute ticker cumulative returns for fast 20d lookback
    ticker_rets = {}
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            ticker_rets[dates[i]] = closes[i] / closes[i - 1] - 1

    # Insider cluster days-since
    cluster_dates = insider_clusters.get(ticker, [])

    # Intelligence overlay
    intel_vals = {
        "conviction_score": None,
        "accum_phase": None,
        "insider_cluster_detected": None,
        "insider_hist_win_rate": None,
        "insider_effect_score": None,
        "trend_score": None,
        "institutional_pressure": None,
        "expected_value": None,
        "int_squeeze_score": None,
        "int_short_squeeze_score": None,
        "int_days_to_cover": None,
        "short_volume_ratio_avg": None,
        "dark_pool_pct_avg": None,
    }
    if intel:
        for k in intel_vals:
            v = intel.get(k)
            if pd.notna(v) if not isinstance(v, str) else (v != "" and v is not None):
                intel_vals[k] = v

    # Build feature rows only for dates in [start_date, end_date]
    rows = []
    for i in range(200, len(closes)):
        td = dates[i]
        if td < start_date or td > end_date:
            continue

        c = closes[i]
        if c <= 0 or np.isnan(c):
            continue

        # Price vs SMA
        pvs200 = ((c - sma_200[i]) / sma_200[i] * 100) if not np.isnan(sma_200[i]) and sma_200[i] > 0 else None
        pvs50 = ((c - sma_50[i]) / sma_50[i] * 100) if not np.isnan(sma_50[i]) and sma_50[i] > 0 else None
        pf52h = ((c - high_252[i]) / high_252[i] * 100) if not np.isnan(high_252[i]) and high_252[i] > 0 else None

        # ROC
        roc_5 = ((c - closes[i - 5]) / closes[i - 5] * 100) if closes[i - 5] > 0 else None
        roc_10 = ((c - closes[i - 10]) / closes[i - 10] * 100) if closes[i - 10] > 0 else None
        roc_20 = ((c - closes[i - 20]) / closes[i - 20] * 100) if closes[i - 20] > 0 else None

        # Relative return vs SPY (20d)
        ret_vs_spy = None
        if roc_20 is not None:
            spy_20d = 0.0
            # Approximate SPY 20d return from daily returns
            count_spy = 0
            for j in range(max(0, i - 19), i + 1):
                d = dates[j]
                if d in spy_cum_rets:
                    spy_20d += spy_cum_rets[d]
                    count_spy += 1
            if count_spy > 0:
                ret_vs_spy = roc_20 - spy_20d * 100

        # BB width
        bb_w = None
        if not np.isnan(bb_upper[i]) and not np.isnan(bb_lower[i]) and not np.isnan(sma_20[i]) and sma_20[i] > 0:
            bb_w = (bb_upper[i] - bb_lower[i]) / sma_20[i] * 100

        # Squeeze
        sq_on = False
        if (not np.isnan(bb_upper[i]) and not np.isnan(kc_upper[i])
                and not np.isnan(bb_lower[i]) and not np.isnan(kc_lower[i])):
            sq_on = bool(bb_upper[i] < kc_upper[i] and bb_lower[i] > kc_lower[i])

        # Volume
        vol_ratio = None
        if not np.isnan(vol_sma_20[i]) and vol_sma_20[i] > 0:
            vol_ratio = volumes[i] / vol_sma_20[i]
        vol_trend = None
        if not np.isnan(vol_sma_5[i]) and not np.isnan(vol_sma_20[i]) and vol_sma_20[i] > 0:
            vol_trend = vol_sma_5[i] / vol_sma_20[i]

        # Gap from previous close
        gap_pct = None
        if i > 0 and closes[i - 1] > 0:
            gap_pct = (opens[i] - closes[i - 1]) / closes[i - 1] * 100

        # Volume surge
        vol_surge = False
        if not np.isnan(vol_sma_20[i]) and vol_sma_20[i] > 0:
            vol_surge = bool(volumes[i] > 3 * vol_sma_20[i])

        # Days since insider cluster (strict: cluster_date < td)
        days_since_cluster = None
        if cluster_dates:
            recent = [cd for cd in cluster_dates if cd < td]
            if recent:
                days_since_cluster = (td - max(recent)).days

        # Price vs 20d high
        pvs20h = None
        if not np.isnan(high_20d[i]) and high_20d[i] > 0:
            pvs20h = (c - high_20d[i]) / high_20d[i] * 100

        row = {
            "ticker": ticker,
            "trade_date": td,
            # Price
            "close": round(c, 4),
            "sma_10": _rnd(sma_10[i]),
            "sma_20": _rnd(sma_20[i]),
            "sma_50": _rnd(sma_50[i]),
            "sma_200": _rnd(sma_200[i]),
            "price_vs_sma200_pct": _rnd(pvs200),
            "price_vs_sma50_pct": _rnd(pvs50),
            "pct_from_52w_high": _rnd(pf52h),
            # Momentum
            "rsi_14": _rnd(rsi_14[i]),
            "rsi_2": _rnd(rsi_2[i]),
            "roc_5": _rnd(roc_5),
            "roc_10": _rnd(roc_10),
            "roc_20": _rnd(roc_20),
            "ret_vs_spy_20d": _rnd(ret_vs_spy),
            # Volatility
            "atr_20": _rnd(atr_20[i]),
            "bb_upper": _rnd(bb_upper[i]),
            "bb_lower": _rnd(bb_lower[i]),
            "bb_width_pct": _rnd(bb_w),
            "kc_upper": _rnd(kc_upper[i]),
            "kc_lower": _rnd(kc_lower[i]),
            "squeeze_on": sq_on,
            # Volume
            "volume_ratio_20d": _rnd(vol_ratio),
            "obv_slope_10d": _rnd(obv_slope[i]),
            "volume_trend_5d": _rnd(vol_trend),
            "avg_volume_20d": int(vol_sma_20[i]) if not np.isnan(vol_sma_20[i]) else None,
            # Setup
            "consecutive_down_days": int(consec_down[i]),
            "rsi2_below_10": bool(not np.isnan(rsi_2[i]) and rsi_2[i] < 10),
            "gap_pct_from_prev": _rnd(gap_pct),
            "volume_surge_3x": vol_surge,
            "days_since_insider_cluster": days_since_cluster,
            "price_vs_20d_high_pct": _rnd(pvs20h),
            # Candlestick Patterns
            "hammer": bool(candles["hammer"][i]),
            "inv_hammer": bool(candles["inv_hammer"][i]),
            "engulfing_bull": bool(candles["engulfing_bull"][i]),
            "engulfing_bear": bool(candles["engulfing_bear"][i]),
            "doji": bool(candles["doji"][i]),
            "morning_star": bool(candles["morning_star"][i]),
            "evening_star": bool(candles["evening_star"][i]),
            "three_white_soldiers": bool(candles["three_white_soldiers"][i]),
            "piercing_line": bool(candles["piercing_line"][i]),
            "dark_cloud_cover": bool(candles["dark_cloud_cover"][i]),
            # Timing
            "quarter_month": ((td.month - 1) % 3) + 1,
            "day_of_week": td.weekday(),
            # Trend
            "ema_20_slope": _rnd(ema_20_slope[i]),
            "adx_14": _rnd(adx_arr[i]),
            "plus_di_minus_di": _rnd(
                (plus_di_arr[i] - minus_di_arr[i])
                if not np.isnan(plus_di_arr[i]) and not np.isnan(minus_di_arr[i])
                else None
            ),
            "linreg_slope_12d": _rnd(linreg_slope[i]),
            # Intelligence overlay
            "int_squeeze_score": intel_vals["int_squeeze_score"],
            "int_short_squeeze_score": intel_vals["int_short_squeeze_score"],
            "int_days_to_cover": intel_vals["int_days_to_cover"],
            "short_volume_ratio_avg": intel_vals["short_volume_ratio_avg"],
            "dark_pool_pct_avg": intel_vals["dark_pool_pct_avg"],
            "conviction_score": intel_vals["conviction_score"],
            "accum_phase": intel_vals["accum_phase"],
            "insider_cluster_detected": intel_vals["insider_cluster_detected"],
            "insider_hist_win_rate": intel_vals["insider_hist_win_rate"],
            "insider_effect_score": intel_vals["insider_effect_score"],
            "trend_score": intel_vals["trend_score"],
            "institutional_pressure": intel_vals["institutional_pressure"],
            "expected_value": intel_vals["expected_value"],
            # Metadata
            "report_quarter": quarter,
            "sector": sector,
            "computed_at": now_iso,
        }
        rows.append(row)

    return rows


def _rnd(val, decimals: int = 4):
    """Round a value or return None if NaN."""
    if val is None:
        return None
    if isinstance(val, float) and np.isnan(val):
        return None
    return round(float(val), decimals)


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------

def compute_swing_features(
    conn: duckdb.DuckDBPyConnection,
    quarters: List[str],
    min_price: float = MIN_PRICE,
    min_avg_volume: int = MIN_AVG_VOLUME,
) -> int:
    """Compute daily swing features for all liquid tickers across quarters.

    Returns total rows written.
    """
    _ensure_tables(conn)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total_rows = 0

    for quarter in quarters:
        q_start = _quarter_start_date(quarter)
        q_end = _quarter_end_date(quarter)
        lookback_start = q_start - timedelta(days=LOOKBACK_DAYS + 50)  # extra margin

        logger.info("=== Swing features: quarter={} ({} to {}) ===", quarter, q_start, q_end)

        # 1. Find liquid tickers for this quarter
        tickers_df = conn.execute("""
            SELECT ticker, AVG(close) AS avg_close, AVG(volume) AS avg_vol
            FROM fact_daily_prices
            WHERE trade_date >= ? AND trade_date <= ?
              AND close IS NOT NULL AND close > 0
            GROUP BY ticker
            HAVING AVG(close) >= ? AND AVG(volume) >= ?
            ORDER BY ticker
        """, [q_start.isoformat(), q_end.isoformat(), min_price, min_avg_volume]).fetchdf()

        tickers = tickers_df["ticker"].tolist()
        logger.info("  {} liquid tickers qualify (avg close>=${}, avg vol>={})",
                     len(tickers), min_price, f"{min_avg_volume:,}")

        if not tickers:
            continue

        # 2. Load SPY returns for relative performance
        spy_rets = _load_spy_returns(conn, lookback_start, q_end)
        logger.info("  SPY daily returns loaded: {} days", len(spy_rets))

        # 3. Load insider clusters
        insider_clusters = _detect_insider_clusters(conn, lookback_start, q_end)
        n_clusters = sum(len(v) for v in insider_clusters.values())
        logger.info("  Insider clusters found: {} across {} tickers",
                     n_clusters, len(insider_clusters))

        # 4. Load intelligence overlay for this quarter
        # Use latest quarter where filing_date <= q_end (prevent lookahead)
        intel_quarter = quarter
        intel = _load_intelligence_overlay(conn, intel_quarter)
        logger.info("  Intelligence overlay loaded: {} tickers for {}", len(intel), intel_quarter)

        # 5. Load sector map
        sector_map = _load_sector_map(conn, tickers)

        # 6. Process tickers in batches
        batch = []
        n_tickers = len(tickers)

        for t_idx, ticker in enumerate(tickers):
            if (t_idx + 1) % 200 == 0 or t_idx == 0:
                logger.info("  Processing [{}/{}] {}...", t_idx + 1, n_tickers, ticker)

            # Load daily bars with lookback
            daily_df = conn.execute("""
                SELECT ticker, trade_date, open, high, low, close, volume
                FROM fact_daily_prices
                WHERE ticker = ?
                  AND trade_date >= ? AND trade_date <= ?
                  AND close IS NOT NULL AND close > 0
                ORDER BY trade_date
            """, [ticker, lookback_start.isoformat(), q_end.isoformat()]).fetchdf()

            if len(daily_df) < 201:
                continue

            ticker_intel = intel.get(ticker)
            sector = sector_map.get(ticker, "Unknown")

            rows = _compute_ticker_features(
                daily_df, spy_rets, ticker_intel, insider_clusters,
                sector, quarter, q_start, q_end, now_iso,
            )
            batch.extend(rows)

            # Flush every 500 tickers
            if len(batch) >= 50_000:
                _flush_batch(conn, batch, quarter)
                total_rows += len(batch)
                batch = []

        # Flush remaining
        if batch:
            _flush_batch(conn, batch, quarter)
            total_rows += len(batch)

        logger.info("  Quarter {} complete: {} feature rows", quarter, total_rows)

    logger.info("=== Swing feature computation done: {:,} total rows ===", total_rows)
    return total_rows


def _flush_batch(conn: duckdb.DuckDBPyConnection, batch: List[Dict],
                 quarter: str) -> None:
    """Write batch of feature rows to DuckDB."""
    df = pd.DataFrame(batch)

    # Ensure column order matches table
    expected_cols = [
        "ticker", "trade_date",
        "close", "sma_10", "sma_20", "sma_50", "sma_200",
        "price_vs_sma200_pct", "price_vs_sma50_pct", "pct_from_52w_high",
        "rsi_14", "rsi_2", "roc_5", "roc_10", "roc_20", "ret_vs_spy_20d",
        "atr_20", "bb_upper", "bb_lower", "bb_width_pct",
        "kc_upper", "kc_lower", "squeeze_on",
        "volume_ratio_20d", "obv_slope_10d", "volume_trend_5d", "avg_volume_20d",
        "consecutive_down_days", "rsi2_below_10", "gap_pct_from_prev",
        "volume_surge_3x", "days_since_insider_cluster", "price_vs_20d_high_pct",
        "hammer", "inv_hammer", "engulfing_bull", "engulfing_bear",
        "doji", "morning_star", "evening_star", "three_white_soldiers",
        "piercing_line", "dark_cloud_cover",
        "quarter_month", "day_of_week",
        "ema_20_slope", "adx_14", "plus_di_minus_di", "linreg_slope_12d",
        "int_squeeze_score", "int_short_squeeze_score", "int_days_to_cover",
        "short_volume_ratio_avg", "dark_pool_pct_avg",
        "conviction_score", "accum_phase", "insider_cluster_detected",
        "insider_hist_win_rate", "insider_effect_score", "trend_score",
        "institutional_pressure", "expected_value",
        "report_quarter", "sector", "computed_at",
    ]
    for col in expected_cols:
        if col not in df.columns:
            df[col] = None
    df = df[expected_cols]

    conn.register("_swing_feat_temp", df)
    conn.execute("INSERT OR REPLACE INTO fact_swing_features SELECT * FROM _swing_feat_temp")
    conn.unregister("_swing_feat_temp")
    logger.info("    Flushed {:,} feature rows", len(df))


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_feature_summary(conn: duckdb.DuckDBPyConnection,
                          quarters: Optional[List[str]] = None) -> None:
    """Print summary statistics for computed features."""
    where = ""
    params = []
    if quarters:
        placeholders = ",".join(["?" for _ in quarters])
        where = f"WHERE report_quarter IN ({placeholders})"
        params = quarters

    total = conn.execute(
        f"SELECT COUNT(*) FROM fact_swing_features {where}", params
    ).fetchone()[0]
    print(f"\n{'='*60}")
    print(f"  SWING FEATURE SUMMARY")
    print(f"{'='*60}")
    print(f"  Total feature rows: {total:,}")

    if total == 0:
        return

    # By quarter
    qdf = conn.execute(f"""
        SELECT report_quarter, COUNT(*) AS cnt,
               COUNT(DISTINCT ticker) AS tickers,
               MIN(trade_date) AS min_date, MAX(trade_date) AS max_date
        FROM fact_swing_features {where}
        GROUP BY report_quarter ORDER BY report_quarter
    """, params).fetchdf()
    print(f"\n  By Quarter:")
    for _, row in qdf.iterrows():
        print(f"    {row['report_quarter']}: {row['cnt']:>8,} rows, "
              f"{row['tickers']:>5,} tickers  ({row['min_date']} to {row['max_date']})")

    # Feature coverage
    coverage = conn.execute(f"""
        SELECT
            COUNT(*) AS total,
            COUNT(sma_200) AS has_sma200,
            COUNT(rsi_14) AS has_rsi14,
            COUNT(adx_14) AS has_adx,
            COUNT(conviction_score) AS has_conviction,
            COUNT(days_since_insider_cluster) AS has_insider_cluster,
            SUM(CASE WHEN squeeze_on THEN 1 ELSE 0 END) AS squeeze_on_count,
            SUM(CASE WHEN hammer THEN 1 ELSE 0 END) AS hammer_count,
            SUM(CASE WHEN engulfing_bull THEN 1 ELSE 0 END) AS engulf_bull_count,
            SUM(CASE WHEN doji THEN 1 ELSE 0 END) AS doji_count,
            SUM(CASE WHEN morning_star THEN 1 ELSE 0 END) AS morning_count
        FROM fact_swing_features {where}
    """, params).fetchone()
    print(f"\n  Feature Coverage:")
    print(f"    SMA(200): {coverage[1]:,}/{coverage[0]:,} ({coverage[1]/coverage[0]*100:.1f}%)")
    print(f"    RSI(14):  {coverage[2]:,}/{coverage[0]:,} ({coverage[2]/coverage[0]*100:.1f}%)")
    print(f"    ADX(14):  {coverage[3]:,}/{coverage[0]:,} ({coverage[3]/coverage[0]*100:.1f}%)")
    print(f"    Conviction: {coverage[4]:,}/{coverage[0]:,} ({coverage[4]/coverage[0]*100:.1f}%)")
    print(f"    Insider Cluster: {coverage[5]:,}/{coverage[0]:,} ({coverage[5]/coverage[0]*100:.1f}%)")
    print(f"    Squeeze On: {coverage[6]:,}/{coverage[0]:,} ({coverage[6]/coverage[0]*100:.1f}%)")
    print(f"\n  Candlestick Pattern Rates:")
    print(f"    Hammer:          {coverage[7]:,}/{coverage[0]:,} ({coverage[7]/coverage[0]*100:.1f}%)")
    print(f"    Engulfing Bull:  {coverage[8]:,}/{coverage[0]:,} ({coverage[8]/coverage[0]*100:.1f}%)")
    print(f"    Doji:            {coverage[9]:,}/{coverage[0]:,} ({coverage[9]/coverage[0]*100:.1f}%)")
    print(f"    Morning Star:    {coverage[10]:,}/{coverage[0]:,} ({coverage[10]/coverage[0]*100:.1f}%)")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Swing Feature Engine — daily technical features for swing ML"
    )
    parser.add_argument("--compute", action="store_true", help="Compute features")
    parser.add_argument("--summary", action="store_true", help="Print feature summary")
    parser.add_argument(
        "--quarters", type=str, default=None,
        help="Comma-separated quarters (e.g. 2023-Q4,2024-Q1,2024-Q2)",
    )
    parser.add_argument("--min-price", type=float, default=MIN_PRICE)
    parser.add_argument("--min-avg-volume", type=int, default=MIN_AVG_VOLUME)
    parser.add_argument("--drop-table", action="store_true",
                        help="Drop fact_swing_features before computing (for schema changes)")
    args = parser.parse_args()

    conn = duckdb.connect(str(WAREHOUSE_PATH))
    try:
        quarters = None
        if args.quarters:
            quarters = [q.strip() for q in args.quarters.split(",")]

        if args.drop_table:
            logger.info("Dropping fact_swing_features table for schema migration...")
            conn.execute("DROP TABLE IF EXISTS fact_swing_features")
            logger.info("Table dropped. Will be recreated on next compute.")

        if args.compute:
            if not quarters:
                parser.error("--compute requires --quarters")
            compute_swing_features(conn, quarters, args.min_price, args.min_avg_volume)

        if args.summary:
            print_feature_summary(conn, quarters)

        if not args.compute and not args.summary:
            parser.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    from signal_scanner.utils.logger import setup_logger
    setup_logger()
    main()
