"""Intraday Feature Engine — universal per-ticker-day feature computation.

Pre-computes ~55 features from 1-minute bars for each qualifying ticker-day.
Features are stored in ``fact_intraday_features`` and consumed by any strategy
backtester without re-computation.

Usage:
    python -m signal_scanner.institutional_intel.intelligence.intraday_feature_engine \
        --compute --quarters 2024-Q1,2024-Q2,2024-Q3 --min-conviction 55

    python -m signal_scanner.institutional_intel.intelligence.intraday_feature_engine --summary
"""

from __future__ import annotations

import argparse
import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import duckdb
import numpy as np
import pandas as pd
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_CONVICTION = 55.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _quarter_end_date(quarter: str) -> date:
    year = int(quarter.split("-Q")[0])
    qnum = int(quarter.split("-Q")[1])
    end_month = {1: 3, 2: 6, 3: 9, 4: 12}[qnum]
    last_day = calendar.monthrange(year, end_month)[1]
    return date(year, end_month, last_day)


def _filing_date(quarter: str) -> date:
    return _quarter_end_date(quarter) + timedelta(days=45)


def _next_quarter(quarter: str) -> str:
    year = int(quarter.split("-Q")[0])
    qnum = int(quarter.split("-Q")[1])
    qnum += 1
    if qnum > 4:
        qnum = 1
        year += 1
    return f"{year}-Q{qnum}"


# ---------------------------------------------------------------------------
# DuckDB table
# ---------------------------------------------------------------------------

def _ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fact_intraday_features (
            ticker                  TEXT NOT NULL,
            trade_date              DATE NOT NULL,

            -- Price Structure
            prev_close              DOUBLE,
            gap_pct                 DOUBLE,
            open_930                DOUBLE,
            or_high                 DOUBLE,
            or_low                  DOUBLE,
            or_range                DOUBLE,
            day_high                DOUBLE,
            day_low                 DOUBLE,
            day_range               DOUBLE,
            atr_20d                 DOUBLE,

            -- VWAP Features
            vwap_at_1000            DOUBLE,
            vwap_at_1030            DOUBLE,
            vwap_at_1100            DOUBLE,
            price_vs_vwap_1000      DOUBLE,
            price_vs_vwap_1030      DOUBLE,
            price_vs_vwap_1100      DOUBLE,
            max_vwap_dev_above      DOUBLE,
            max_vwap_dev_below      DOUBLE,
            vwap_cross_count        INTEGER,

            -- Volume Profile
            or_volume               BIGINT,
            avg_or_volume_20d       DOUBLE,
            volume_ratio            DOUBLE,
            total_rth_volume        BIGINT,
            first_30min_vol_pct     DOUBLE,
            rel_volume_1000         DOUBLE,

            -- Momentum
            ret_5min_0945           DOUBLE,
            ret_15min_1000          DOUBLE,
            ret_30min_1000          DOUBLE,
            ret_vs_spy_1000         DOUBLE,
            ret_vs_spy_1030         DOUBLE,
            new_hod_count           INTEGER,
            new_hod_with_volume     INTEGER,

            -- RSI (14-period on 5-min bars)
            rsi_14_at_1000          DOUBLE,
            rsi_14_at_1030          DOUBLE,
            rsi_14_at_1100          DOUBLE,
            rsi_14_min              DOUBLE,
            rsi_14_max              DOUBLE,

            -- Volatility
            intraday_volatility     DOUBLE,
            or_range_vs_atr         DOUBLE,
            first_30min_range_pct   DOUBLE,
            consolidation_bars      INTEGER,

            -- Breakout/Breakdown Flags
            or_breakout             BOOLEAN DEFAULT FALSE,
            or_breakout_time        TIMESTAMP,
            or_breakdown            BOOLEAN DEFAULT FALSE,
            or_breakdown_time       TIMESTAMP,
            first_pullback_to_or    BOOLEAN DEFAULT FALSE,
            first_pullback_time     TIMESTAMP,

            -- 5-min Candlestick Context (4)
            candle_hammer_count_5m      INTEGER DEFAULT 0,
            candle_engulf_bull_count_5m INTEGER DEFAULT 0,
            candle_doji_count_5m        INTEGER DEFAULT 0,
            candle_reversal_near_vwap   BOOLEAN DEFAULT FALSE,

            -- Volume Spike Features (4)
            volume_spike_count_5m       INTEGER DEFAULT 0,
            volume_spike_near_vwap      BOOLEAN DEFAULT FALSE,
            max_bar_volume_ratio_5m     DOUBLE,
            volume_climax_reversal      BOOLEAN DEFAULT FALSE,

            -- EOD
            eod_close               DOUBLE,
            eod_vs_open_pct         DOUBLE,
            eod_vs_vwap_pct         DOUBLE,
            eod_close_near_hod      BOOLEAN DEFAULT FALSE,
            eod_close_near_lod      BOOLEAN DEFAULT FALSE,

            -- Intelligence Context
            report_quarter          TEXT,
            conviction_score        DOUBLE,
            accum_phase             TEXT,
            swing_signal            TEXT,
            expected_value          DOUBLE,
            squeeze_score           DOUBLE,
            short_squeeze_score     DOUBLE,
            days_to_cover           DOUBLE,
            insider_cluster         BOOLEAN DEFAULT FALSE,
            tier1_count             INTEGER DEFAULT 0,
            sector                  TEXT,

            computed_at             TIMESTAMP,
            PRIMARY KEY (ticker, trade_date)
        )
    """)


# ---------------------------------------------------------------------------
# SQL pre-computation helpers
# ---------------------------------------------------------------------------

def _load_qualifying_tickers(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
    min_conviction: float,
) -> pd.DataFrame:
    return conn.execute("""
        SELECT
            i.ticker,
            i.conviction_score,
            i.accum_phase,
            i.swing_signal,
            i.expected_value,
            i.squeeze_score,
            i.short_squeeze_score,
            i.days_to_cover,
            i.tier1_manager_count AS tier1_count,
            i.insider_cluster_detected AS insider_cluster,
            (SELECT d.sector FROM dim_issuer d
             WHERE d.ticker = i.ticker LIMIT 1) AS sector
        FROM intelligence_scores i
        WHERE i.report_quarter = ?
          AND i.conviction_score >= ?
          AND i.accum_phase IN ('ACTIVE_ACCUM', 'LATE_ACCUM', 'EARLY_ACCUM')
          AND i.swing_signal IN ('BUY', 'WATCH')
    """, [quarter, min_conviction]).fetchdf()


def _load_prev_closes(
    conn: duckdb.DuckDBPyConnection,
    tickers: List[str],
    from_date: date,
    to_date: date,
) -> Dict[Tuple[str, date], float]:
    if not tickers:
        return {}
    placeholders = ",".join(["?" for _ in tickers])
    df = conn.execute(f"""
        SELECT ticker, trade_date, close,
               LAG(close) OVER (PARTITION BY ticker ORDER BY trade_date) AS prev_close
        FROM fact_daily_prices
        WHERE ticker IN ({placeholders})
          AND trade_date >= CAST(? AS DATE) - INTERVAL '30 DAY'
          AND trade_date <= CAST(? AS DATE)
        ORDER BY ticker, trade_date
    """, [*tickers, from_date.isoformat(), to_date.isoformat()]).fetchdf()

    result = {}
    for _, row in df.iterrows():
        if row["prev_close"] is not None and pd.notna(row["prev_close"]):
            td = pd.Timestamp(row["trade_date"]).date()
            result[(str(row["ticker"]), td)] = float(row["prev_close"])
    return result


def _load_atr_20d(
    conn: duckdb.DuckDBPyConnection,
    tickers: List[str],
    from_date: date,
    to_date: date,
) -> Dict[Tuple[str, date], float]:
    if not tickers:
        return {}
    placeholders = ",".join(["?" for _ in tickers])
    df = conn.execute(f"""
        WITH daily AS (
            SELECT ticker, trade_date, high, low, close,
                   LAG(close) OVER (PARTITION BY ticker ORDER BY trade_date) AS pc
            FROM fact_daily_prices
            WHERE ticker IN ({placeholders})
              AND trade_date >= CAST(? AS DATE) - INTERVAL '60 DAY'
              AND trade_date <= CAST(? AS DATE)
        ),
        tr AS (
            SELECT ticker, trade_date,
                   GREATEST(high - low,
                            ABS(high - COALESCE(pc, close)),
                            ABS(low - COALESCE(pc, close))) AS true_range
            FROM daily
            WHERE high IS NOT NULL AND low IS NOT NULL
        )
        SELECT ticker, trade_date,
               AVG(true_range) OVER (
                   PARTITION BY ticker ORDER BY trade_date
                   ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
               ) AS atr_20
        FROM tr
        WHERE trade_date >= CAST(? AS DATE) AND trade_date <= CAST(? AS DATE)
    """, [*tickers, from_date.isoformat(), to_date.isoformat(),
          from_date.isoformat(), to_date.isoformat()]).fetchdf()

    result = {}
    for _, row in df.iterrows():
        if row["atr_20"] is not None and pd.notna(row["atr_20"]):
            td = pd.Timestamp(row["trade_date"]).date()
            result[(str(row["ticker"]), td)] = float(row["atr_20"])
    return result


def _compute_avg_or_volumes(
    conn: duckdb.DuckDBPyConnection,
    tickers: List[str],
) -> Dict[Tuple[str, date], float]:
    if not tickers:
        return {}
    placeholders = ",".join(["?" for _ in tickers])
    daily_or = conn.execute(f"""
        SELECT ticker, CAST(bar_time AS DATE) AS td, SUM(volume) AS or_vol
        FROM fact_intraday_bars
        WHERE ticker IN ({placeholders})
          AND EXTRACT(HOUR FROM bar_time) = 9
          AND EXTRACT(MINUTE FROM bar_time) >= 30
          AND EXTRACT(MINUTE FROM bar_time) < 45
        GROUP BY ticker, CAST(bar_time AS DATE)
        ORDER BY ticker, td
    """, tickers).fetchdf()

    if daily_or.empty:
        return {}

    result = {}
    for ticker, group in daily_or.groupby("ticker"):
        group = group.sort_values("td")
        volumes = group["or_vol"].values
        dates = group["td"].values
        for idx in range(len(dates)):
            start = max(0, idx - 20)
            window = volumes[start:idx]
            avg_vol = float(window.mean()) if len(window) >= 3 else float(volumes[idx])
            td = pd.Timestamp(dates[idx]).date()
            result[(str(ticker), td)] = avg_vol
    return result


def _load_spy_bars(
    conn: duckdb.DuckDBPyConnection,
    from_date: date,
    to_date: date,
) -> Dict[date, pd.DataFrame]:
    """Load SPY 1-min bars, grouped by date. Returns empty dict if no data."""
    try:
        spy_df = conn.execute("""
            SELECT bar_time, open, high, low, close, volume
            FROM fact_intraday_bars
            WHERE ticker = 'SPY'
              AND CAST(bar_time AS DATE) >= ?
              AND CAST(bar_time AS DATE) <= ?
            ORDER BY bar_time
        """, [from_date, to_date]).fetchdf()
    except Exception:
        return {}

    if spy_df.empty:
        return {}

    spy_df["_date"] = pd.to_datetime(spy_df["bar_time"]).dt.date
    result = {}
    for d, group in spy_df.groupby("_date"):
        result[d] = group.drop(columns=["_date"]).reset_index(drop=True)
    return result


def _load_spy_daily_returns(
    conn: duckdb.DuckDBPyConnection,
    from_date: date,
    to_date: date,
) -> Dict[date, float]:
    """Fallback: use daily open-to-close return for SPY."""
    df = conn.execute("""
        SELECT trade_date, open, close
        FROM fact_daily_prices
        WHERE ticker = 'SPY'
          AND trade_date >= ? AND trade_date <= ?
          AND open > 0
    """, [from_date, to_date]).fetchdf()

    result = {}
    for _, row in df.iterrows():
        td = pd.Timestamp(row["trade_date"]).date()
        result[td] = (float(row["close"]) - float(row["open"])) / float(row["open"]) * 100
    return result


# ---------------------------------------------------------------------------
# Core computation helpers (numpy-vectorized)
# ---------------------------------------------------------------------------

def _compute_running_vwap(highs: np.ndarray, lows: np.ndarray,
                          closes: np.ndarray, volumes: np.ndarray) -> np.ndarray:
    """Running VWAP = cumsum(typical_price * volume) / cumsum(volume)."""
    tp = (highs + lows + closes) / 3.0
    vol = volumes.astype(np.float64)
    cum_tp_vol = np.cumsum(tp * vol)
    cum_vol = np.cumsum(vol)
    cum_vol = np.where(cum_vol == 0, 1.0, cum_vol)
    return cum_tp_vol / cum_vol


def _resample_5min(bars: pd.DataFrame) -> pd.DataFrame:
    """Resample 1-min bars to 5-min OHLCV."""
    bars = bars.copy()
    bars["bar_time"] = pd.to_datetime(bars["bar_time"])
    bars = bars.set_index("bar_time")
    resampled = bars.resample("5min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["close"])
    return resampled.reset_index()


def _compute_rsi_14(closes: np.ndarray) -> np.ndarray:
    """RSI-14 using Wilder's smoothing. Returns array same length as input."""
    if len(closes) < 2:
        return np.full(len(closes), 50.0)

    delta = np.diff(closes, prepend=closes[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    avg_gain = pd.Series(gain).ewm(alpha=1.0 / 14, min_periods=14, adjust=False).mean().values
    avg_loss = pd.Series(loss).ewm(alpha=1.0 / 14, min_periods=14, adjust=False).mean().values

    avg_loss = np.where(avg_loss == 0, 1e-10, avg_loss)
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


# ---------------------------------------------------------------------------
# 5-min candlestick + volume patterns
# ---------------------------------------------------------------------------

def _detect_5min_candle_volume_features(
    bars_5m: pd.DataFrame,
    running_vwap: np.ndarray,
    bars_1m: pd.DataFrame,
) -> Dict[str, Any]:
    """Detect candlestick patterns and volume anomalies on 5-min bars.

    Focuses on the entry window (9:45-11:00) for VWAP_MR relevance.
    Returns dict of feature values.
    """
    feat: Dict[str, Any] = {
        "candle_hammer_count_5m": 0,
        "candle_engulf_bull_count_5m": 0,
        "candle_doji_count_5m": 0,
        "candle_reversal_near_vwap": False,
        "volume_spike_count_5m": 0,
        "volume_spike_near_vwap": False,
        "max_bar_volume_ratio_5m": None,
        "volume_climax_reversal": False,
    }

    if len(bars_5m) < 3:
        return feat

    times_5m = pd.to_datetime(bars_5m["bar_time"])
    hours_5m = times_5m.dt.hour
    mins_5m = times_5m.dt.minute

    # Entry window: 9:45-11:00 (hour 9 min>=45, or hour 10, or hour 11 min=0)
    window_mask = (
        ((hours_5m == 9) & (mins_5m >= 45)) |
        (hours_5m == 10) |
        ((hours_5m == 11) & (mins_5m == 0))
    )
    w_idx = np.where(window_mask.values)[0]
    if len(w_idx) < 2:
        return feat

    opens_5m = bars_5m["open"].values.astype(np.float64)
    highs_5m = bars_5m["high"].values.astype(np.float64)
    lows_5m = bars_5m["low"].values.astype(np.float64)
    closes_5m = bars_5m["close"].values.astype(np.float64)
    volumes_5m = bars_5m["volume"].values.astype(np.float64)

    body = closes_5m - opens_5m
    abs_body = np.abs(body)
    bar_range = highs_5m - lows_5m
    upper_wick = highs_5m - np.maximum(opens_5m, closes_5m)
    lower_wick = np.minimum(opens_5m, closes_5m) - lows_5m

    # Patterns (full array, then filter to window)
    hammer = (lower_wick >= 2 * abs_body) & (upper_wick < abs_body) & (bar_range > 0)
    doji = (abs_body < 0.1 * bar_range) & (bar_range > 0)

    # Bullish engulfing
    engulf_bull = np.zeros(len(opens_5m), dtype=bool)
    engulf_bull[1:] = ((body[1:] > 0) & (body[:-1] < 0) &
                       (opens_5m[1:] <= closes_5m[:-1]) & (closes_5m[1:] >= opens_5m[:-1]))

    # Count patterns in entry window
    feat["candle_hammer_count_5m"] = int(hammer[w_idx].sum())
    feat["candle_engulf_bull_count_5m"] = int(engulf_bull[w_idx].sum())
    feat["candle_doji_count_5m"] = int(doji[w_idx].sum())

    # VWAP proximity check — map 5-min bars to running VWAP from 1-min bars
    # Get VWAP at each 5-min bar's time
    bars_1m_times = pd.to_datetime(bars_1m["bar_time"])
    for idx in w_idx:
        bar_time_5m = times_5m.iloc[idx]
        # Find the 1-min VWAP at this 5-min bar's end
        vwap_mask = bars_1m_times <= bar_time_5m
        vwap_idx = np.where(vwap_mask.values)[0]
        if len(vwap_idx) == 0:
            continue
        vwap_val = running_vwap[vwap_idx[-1]]
        if vwap_val <= 0:
            continue

        bar_mid = (highs_5m[idx] + lows_5m[idx]) / 2
        near_vwap = abs(bar_mid - vwap_val) / vwap_val < 0.003  # within 0.3%

        if near_vwap and (hammer[idx] or engulf_bull[idx]):
            feat["candle_reversal_near_vwap"] = True

    # Volume spike features (on 5-min bars in entry window)
    avg_vol_5m = float(volumes_5m.mean()) if len(volumes_5m) > 0 else 1.0
    if avg_vol_5m > 0:
        vol_ratios = volumes_5m / avg_vol_5m

        # Max bar volume ratio (full day)
        feat["max_bar_volume_ratio_5m"] = round(float(vol_ratios.max()), 2)

        # Spikes in entry window (>3x average)
        spike_mask = vol_ratios[w_idx] > 3.0
        feat["volume_spike_count_5m"] = int(spike_mask.sum())

        # Volume spike near VWAP
        for i, idx in enumerate(w_idx):
            if vol_ratios[idx] > 3.0:
                bar_time_5m = times_5m.iloc[idx]
                vwap_mask = bars_1m_times <= bar_time_5m
                vwap_idx_arr = np.where(vwap_mask.values)[0]
                if len(vwap_idx_arr) > 0:
                    vwap_val = running_vwap[vwap_idx_arr[-1]]
                    if vwap_val > 0:
                        bar_mid = (highs_5m[idx] + lows_5m[idx]) / 2
                        if abs(bar_mid - vwap_val) / vwap_val < 0.003:
                            feat["volume_spike_near_vwap"] = True

        # Volume climax reversal: >5x volume bar followed by reversal (green bar)
        for idx in w_idx:
            if idx + 1 < len(volumes_5m) and vol_ratios[idx] > 5.0:
                if body[idx] < 0 and body[idx + 1] > 0:  # red climax then green
                    feat["volume_climax_reversal"] = True
                    break

    return feat


# ---------------------------------------------------------------------------
# Per-day feature computation
# ---------------------------------------------------------------------------

def _compute_day_features(
    bars: pd.DataFrame,
    prev_close: Optional[float],
    atr_20d: Optional[float],
    avg_or_vol: float,
    spy_ret_at_1000: Optional[float],
    spy_ret_at_1030: Optional[float],
) -> Dict[str, Any]:
    """Compute all features for one ticker on one day.

    Args:
        bars: 1-min bars for this day, columns [bar_time, open, high, low, close, volume].
        prev_close: Previous day's close.
        atr_20d: 20-day ATR.
        avg_or_vol: 20-day average opening range volume.
        spy_ret_at_1000: SPY return open→10:00 (%).
        spy_ret_at_1030: SPY return open→10:30 (%).

    Returns dict with all feature values.
    """
    feat: Dict[str, Any] = {}

    if bars.empty or len(bars) < 20:
        return feat

    # Filter to RTH only (9:30-15:59)
    bar_times = pd.to_datetime(bars["bar_time"])
    rth_mask = (
        ((bar_times.dt.hour == 9) & (bar_times.dt.minute >= 30)) |
        ((bar_times.dt.hour >= 10) & (bar_times.dt.hour < 16))
    )
    bars = bars[rth_mask.values].reset_index(drop=True)
    if len(bars) < 20:
        return feat

    bar_times = pd.to_datetime(bars["bar_time"])
    hours = bar_times.dt.hour
    minutes = bar_times.dt.minute

    highs = bars["high"].values.astype(np.float64)
    lows = bars["low"].values.astype(np.float64)
    closes = bars["close"].values.astype(np.float64)
    volumes = bars["volume"].values.astype(np.float64)

    # --- Price Structure ---
    feat["prev_close"] = prev_close
    feat["atr_20d"] = atr_20d
    feat["day_high"] = float(highs.max())
    feat["day_low"] = float(lows.min())
    feat["day_range"] = feat["day_high"] - feat["day_low"]

    # Opening range (9:30-9:44)
    or_mask = (hours == 9) & (minutes >= 30) & (minutes < 45)
    or_bars_idx = np.where(or_mask)[0]

    if len(or_bars_idx) < 5:
        return feat

    feat["open_930"] = float(bars.iloc[or_bars_idx[0]]["open"])
    feat["or_high"] = float(highs[or_bars_idx].max())
    feat["or_low"] = float(lows[or_bars_idx].min())
    feat["or_range"] = feat["or_high"] - feat["or_low"]
    feat["or_volume"] = int(volumes[or_bars_idx].sum())

    if prev_close and prev_close > 0:
        feat["gap_pct"] = (feat["open_930"] - prev_close) / prev_close * 100
    else:
        feat["gap_pct"] = 0.0

    feat["avg_or_volume_20d"] = avg_or_vol
    feat["volume_ratio"] = feat["or_volume"] / avg_or_vol if avg_or_vol > 0 else 1.0

    if atr_20d and atr_20d > 0:
        feat["or_range_vs_atr"] = feat["or_range"] / atr_20d
    else:
        feat["or_range_vs_atr"] = None

    # --- VWAP ---
    running_vwap = _compute_running_vwap(highs, lows, closes, volumes)

    def _vwap_at(h: int, m: int):
        mask = (hours == h) & (minutes == m)
        idx = np.where(mask)[0]
        if len(idx) > 0:
            return float(running_vwap[idx[-1]])
        # Fallback: closest bar before target time
        target_mask = (hours < h) | ((hours == h) & (minutes <= m))
        idx2 = np.where(target_mask)[0]
        return float(running_vwap[idx2[-1]]) if len(idx2) > 0 else None

    def _price_at(h: int, m: int):
        mask = (hours == h) & (minutes == m)
        idx = np.where(mask)[0]
        if len(idx) > 0:
            return float(closes[idx[-1]])
        target_mask = (hours < h) | ((hours == h) & (minutes <= m))
        idx2 = np.where(target_mask)[0]
        return float(closes[idx2[-1]]) if len(idx2) > 0 else None

    feat["vwap_at_1000"] = _vwap_at(10, 0)
    feat["vwap_at_1030"] = _vwap_at(10, 30)
    feat["vwap_at_1100"] = _vwap_at(11, 0)

    for label, h, m in [("1000", 10, 0), ("1030", 10, 30), ("1100", 11, 0)]:
        vw = feat.get(f"vwap_at_{label}")
        pr = _price_at(h, m)
        if vw and vw > 0 and pr is not None:
            feat[f"price_vs_vwap_{label}"] = (pr - vw) / vw * 100
        else:
            feat[f"price_vs_vwap_{label}"] = None

    # Max VWAP deviations
    if len(running_vwap) > 0:
        safe_vwap = np.where(running_vwap == 0, 1e-10, running_vwap)
        dev_pct = (closes - running_vwap) / safe_vwap * 100
        feat["max_vwap_dev_above"] = float(dev_pct.max())
        feat["max_vwap_dev_below"] = float(dev_pct.min())

        # VWAP cross count
        above = closes > running_vwap
        crosses = np.diff(above.astype(int))
        feat["vwap_cross_count"] = int(np.count_nonzero(crosses))
    else:
        feat["max_vwap_dev_above"] = None
        feat["max_vwap_dev_below"] = None
        feat["vwap_cross_count"] = 0

    # --- Volume Profile ---
    feat["total_rth_volume"] = int(volumes.sum())

    first_30_mask = (hours == 9) & (minutes >= 30) | (hours == 10) & (minutes < 0)
    # Fix: first 30 min = 9:30-9:59
    first_30_mask = (hours == 9) & (minutes >= 30)
    first_30_vol = float(volumes[first_30_mask].sum())
    total_vol = float(volumes.sum())
    feat["first_30min_vol_pct"] = (first_30_vol / total_vol * 100) if total_vol > 0 else 0.0

    # Relative volume at 10:00
    cum_vol_1000_mask = (hours < 10) | ((hours == 10) & (minutes == 0))
    cum_vol_1000 = float(volumes[cum_vol_1000_mask].sum())
    feat["rel_volume_1000"] = cum_vol_1000 / avg_or_vol if avg_or_vol > 0 else 1.0

    # --- Momentum ---
    price_930 = feat["open_930"]
    price_0945 = _price_at(9, 44)  # close of 9:44 bar
    price_1000 = _price_at(10, 0)
    price_1030 = _price_at(10, 30)

    # 5-min return ending at 9:45
    price_0940 = _price_at(9, 39)
    if price_0940 and price_0945 and price_0940 > 0:
        feat["ret_5min_0945"] = (price_0945 - price_0940) / price_0940 * 100
    else:
        feat["ret_5min_0945"] = None

    # 15-min return ending at 10:00 (from 9:45)
    if price_0945 and price_1000 and price_0945 > 0:
        feat["ret_15min_1000"] = (price_1000 - price_0945) / price_0945 * 100
    else:
        feat["ret_15min_1000"] = None

    # 30-min return ending at 10:00 (from 9:30)
    if price_930 and price_1000 and price_930 > 0:
        feat["ret_30min_1000"] = (price_1000 - price_930) / price_930 * 100
    else:
        feat["ret_30min_1000"] = None

    # Relative strength vs SPY
    stock_ret_1000 = feat.get("ret_30min_1000")
    if stock_ret_1000 is not None and spy_ret_at_1000 is not None:
        feat["ret_vs_spy_1000"] = stock_ret_1000 - spy_ret_at_1000
    else:
        feat["ret_vs_spy_1000"] = None

    stock_ret_1030 = None
    if price_930 and price_1030 and price_930 > 0:
        stock_ret_1030 = (price_1030 - price_930) / price_930 * 100
    if stock_ret_1030 is not None and spy_ret_at_1030 is not None:
        feat["ret_vs_spy_1030"] = stock_ret_1030 - spy_ret_at_1030
    else:
        feat["ret_vs_spy_1030"] = None

    # New HOD count after 9:45
    post_or_mask = ~or_mask
    post_or_idx = np.where(post_or_mask)[0]
    running_high = feat["or_high"]
    hod_count = 0
    hod_with_vol = 0
    avg_bar_vol = float(volumes.mean()) if len(volumes) > 0 else 1.0

    for idx in post_or_idx:
        if highs[idx] > running_high:
            running_high = highs[idx]
            hod_count += 1
            if volumes[idx] > 1.5 * avg_bar_vol:
                hod_with_vol += 1

    feat["new_hod_count"] = hod_count
    feat["new_hod_with_volume"] = hod_with_vol

    # --- RSI on 5-min bars ---
    bars_5m = _resample_5min(bars)
    if len(bars_5m) >= 14:
        rsi_vals = _compute_rsi_14(bars_5m["close"].values)
        bars_5m_times = pd.to_datetime(bars_5m["bar_time"])
        bars_5m_hours = bars_5m_times.dt.hour
        bars_5m_minutes = bars_5m_times.dt.minute

        def _rsi_at(h: int, m: int):
            mask = (bars_5m_hours <= h) & ((bars_5m_hours < h) | (bars_5m_minutes <= m))
            idx = np.where(mask.values)[0]
            return float(rsi_vals[idx[-1]]) if len(idx) > 0 else None

        feat["rsi_14_at_1000"] = _rsi_at(10, 0)
        feat["rsi_14_at_1030"] = _rsi_at(10, 30)
        feat["rsi_14_at_1100"] = _rsi_at(11, 0)
        feat["rsi_14_min"] = float(np.nanmin(rsi_vals))
        feat["rsi_14_max"] = float(np.nanmax(rsi_vals))
    else:
        feat["rsi_14_at_1000"] = None
        feat["rsi_14_at_1030"] = None
        feat["rsi_14_at_1100"] = None
        feat["rsi_14_min"] = None
        feat["rsi_14_max"] = None

    # --- Volatility ---
    if len(bars_5m) >= 3:
        rets_5m = np.diff(bars_5m["close"].values) / np.where(
            bars_5m["close"].values[:-1] == 0, 1e-10, bars_5m["close"].values[:-1]
        ) * 100
        feat["intraday_volatility"] = float(np.std(rets_5m))
    else:
        feat["intraday_volatility"] = None

    # First 30-min range
    first_30_highs = highs[np.where((hours == 9) & (minutes >= 30))[0]]
    first_30_lows = lows[np.where((hours == 9) & (minutes >= 30))[0]]
    if len(first_30_highs) > 0 and prev_close and prev_close > 0:
        r30 = float(first_30_highs.max()) - float(first_30_lows.min())
        feat["first_30min_range_pct"] = r30 / prev_close * 100
    else:
        feat["first_30min_range_pct"] = None

    # Consolidation bars — consecutive bars with range < 0.1% before first breakout
    consol = 0
    if len(post_or_idx) > 0 and feat["or_high"] > 0:
        for idx in post_or_idx:
            bar_range = highs[idx] - lows[idx]
            if bar_range / feat["or_high"] < 0.001:
                consol += 1
            else:
                break
    feat["consolidation_bars"] = consol

    # --- 5-min Candlestick + Volume Patterns ---
    candle_vol = _detect_5min_candle_volume_features(bars_5m, running_vwap, bars)
    feat.update(candle_vol)

    # --- Breakout / Breakdown ---
    feat["or_breakout"] = False
    feat["or_breakout_time"] = None
    feat["or_breakdown"] = False
    feat["or_breakdown_time"] = None
    feat["first_pullback_to_or"] = False
    feat["first_pullback_time"] = None

    breakout_detected = False
    for idx in post_or_idx:
        if not feat["or_breakout"] and highs[idx] > feat["or_high"]:
            feat["or_breakout"] = True
            feat["or_breakout_time"] = bars.iloc[idx]["bar_time"]
            breakout_detected = True
        if not feat["or_breakdown"] and lows[idx] < feat["or_low"]:
            feat["or_breakdown"] = True
            feat["or_breakdown_time"] = bars.iloc[idx]["bar_time"]
        if breakout_detected and not feat["first_pullback_to_or"]:
            if lows[idx] <= feat["or_high"] * 1.002:
                feat["first_pullback_to_or"] = True
                feat["first_pullback_time"] = bars.iloc[idx]["bar_time"]

    # --- EOD ---
    eod_close = float(closes[-1])
    feat["eod_close"] = eod_close

    if feat["open_930"] and feat["open_930"] > 0:
        feat["eod_vs_open_pct"] = (eod_close - feat["open_930"]) / feat["open_930"] * 100
    else:
        feat["eod_vs_open_pct"] = None

    final_vwap = float(running_vwap[-1]) if len(running_vwap) > 0 else None
    if final_vwap and final_vwap > 0:
        feat["eod_vs_vwap_pct"] = (eod_close - final_vwap) / final_vwap * 100
    else:
        feat["eod_vs_vwap_pct"] = None

    # Close near HOD/LOD (within 10% of day range from high/low)
    if feat["day_range"] > 0:
        dist_from_high = feat["day_high"] - eod_close
        dist_from_low = eod_close - feat["day_low"]
        feat["eod_close_near_hod"] = dist_from_high <= 0.10 * feat["day_range"]
        feat["eod_close_near_lod"] = dist_from_low <= 0.10 * feat["day_range"]
    else:
        feat["eod_close_near_hod"] = False
        feat["eod_close_near_lod"] = False

    return feat


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def compute_intraday_features(
    conn: duckdb.DuckDBPyConnection,
    quarters: List[str],
    min_conviction: float = MIN_CONVICTION,
) -> int:
    """Compute intraday features for all qualifying ticker-days.

    Returns total feature rows created.
    """
    _ensure_tables(conn)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total_rows = 0

    for quarter in quarters:
        logger.info("=== Feature computation: quarter={} ===", quarter)

        # 1. Qualifying tickers
        intel_df = _load_qualifying_tickers(conn, quarter, min_conviction)
        if intel_df.empty:
            logger.warning("No qualifying tickers for {}", quarter)
            continue

        tickers = intel_df["ticker"].tolist()
        logger.info("{} qualifying tickers for {}", len(tickers), quarter)

        # 2. Trading window
        start_date = _filing_date(quarter)
        end_date = _filing_date(_next_quarter(quarter))
        logger.info("Trading window: {} -> {}", start_date, end_date)

        # 3. Pre-compute baselines
        logger.info("Loading prev closes...")
        prev_close_map = _load_prev_closes(conn, tickers, start_date, end_date)

        logger.info("Loading ATR-20d...")
        atr_map = _load_atr_20d(conn, tickers, start_date, end_date)

        logger.info("Computing OR volume baselines...")
        or_vol_map = _compute_avg_or_volumes(conn, tickers)

        # 4. SPY data
        spy_bars_map = _load_spy_bars(conn, start_date, end_date)
        spy_daily_ret = _load_spy_daily_returns(conn, start_date, end_date)

        # Pre-compute SPY returns at checkpoints from intraday bars if available
        spy_ret_1000: Dict[date, float] = {}
        spy_ret_1030: Dict[date, float] = {}
        for d, spy_bars in spy_bars_map.items():
            spy_times = pd.to_datetime(spy_bars["bar_time"])
            spy_opens = spy_bars["open"].values
            spy_closes = spy_bars["close"].values
            spy_h = spy_times.dt.hour
            spy_m = spy_times.dt.minute

            open_930_idx = np.where((spy_h == 9) & (spy_m == 30))[0]
            if len(open_930_idx) == 0:
                continue
            spy_open = float(spy_opens[open_930_idx[0]])
            if spy_open <= 0:
                continue

            for target_h, target_m, target_dict in [(10, 0, spy_ret_1000), (10, 30, spy_ret_1030)]:
                mask = (spy_h <= target_h) & ((spy_h < target_h) | (spy_m <= target_m))
                idx = np.where(mask.values)[0]
                if len(idx) > 0:
                    target_dict[d] = (float(spy_closes[idx[-1]]) - spy_open) / spy_open * 100

        # If no SPY intraday, use daily returns as rough proxy
        if not spy_ret_1000:
            for d, ret in spy_daily_ret.items():
                spy_ret_1000[d] = ret * 0.5  # rough: half of daily by 10:00
                spy_ret_1030[d] = ret * 0.6

        # 5. Intelligence lookup
        intel_lookup = {}
        for _, row in intel_df.iterrows():
            intel_lookup[str(row["ticker"])] = row.to_dict()

        # 6. Get trading days per ticker
        placeholders = ",".join(["?" for _ in tickers])
        trading_days_df = conn.execute(f"""
            SELECT DISTINCT ticker, CAST(bar_time AS DATE) AS td
            FROM fact_intraday_bars
            WHERE ticker IN ({placeholders})
              AND CAST(bar_time AS DATE) >= ?
              AND CAST(bar_time AS DATE) <= ?
            ORDER BY ticker, td
        """, [*tickers, start_date, end_date]).fetchdf()

        if trading_days_df.empty:
            logger.warning("No intraday bars for {} in window", quarter)
            continue

        ticker_days = defaultdict(list)
        for _, row in trading_days_df.iterrows():
            td = pd.Timestamp(row["td"]).date()
            ticker_days[str(row["ticker"])].append(td)

        # 7. Compute features per ticker
        features_batch = []
        tickers_with_days = [t for t in tickers if t in ticker_days]
        n_tickers = len(tickers_with_days)

        for t_idx, ticker in enumerate(tickers_with_days):
            days = ticker_days[ticker]
            intel = intel_lookup.get(ticker, {})

            if (t_idx + 1) % 50 == 0 or t_idx == 0:
                logger.info(
                    "  Computing [{}/{}] {} ({} days)...",
                    t_idx + 1, n_tickers, ticker, len(days),
                )

            # Batch-load all bars
            all_bars = conn.execute("""
                SELECT bar_time, open, high, low, close, volume
                FROM fact_intraday_bars
                WHERE ticker = ?
                  AND CAST(bar_time AS DATE) >= ?
                  AND CAST(bar_time AS DATE) <= ?
                ORDER BY bar_time
            """, [ticker, start_date, end_date]).fetchdf()

            if all_bars.empty:
                continue

            all_bars["_date"] = pd.to_datetime(all_bars["bar_time"]).dt.date

            for td in days:
                bars_df = all_bars[all_bars["_date"] == td].drop(columns=["_date"])

                prev_c = prev_close_map.get((ticker, td))
                if prev_c is None:
                    continue

                atr = atr_map.get((ticker, td))
                avg_or = or_vol_map.get((ticker, td), 0.0)
                spy_r1000 = spy_ret_1000.get(td)
                spy_r1030 = spy_ret_1030.get(td)

                feat = _compute_day_features(
                    bars_df, prev_c, atr, avg_or, spy_r1000, spy_r1030,
                )

                if not feat:
                    continue

                # Add intelligence context
                feat["ticker"] = ticker
                feat["trade_date"] = td
                feat["report_quarter"] = quarter
                feat["conviction_score"] = float(intel.get("conviction_score") or 0)
                feat["accum_phase"] = str(intel.get("accum_phase") or "")
                feat["swing_signal"] = str(intel.get("swing_signal") or "")
                feat["expected_value"] = float(intel.get("expected_value") or 0)
                feat["squeeze_score"] = float(intel.get("squeeze_score") or 0)
                feat["short_squeeze_score"] = float(intel.get("short_squeeze_score") or 0)
                feat["days_to_cover"] = float(intel.get("days_to_cover") or 0)
                feat["insider_cluster"] = bool(intel.get("insider_cluster"))
                feat["tier1_count"] = int(intel.get("tier1_count") or 0)
                feat["sector"] = str(intel.get("sector") or "")
                feat["computed_at"] = now_iso

                features_batch.append(feat)

        if not features_batch:
            logger.info("No features computed for {}", quarter)
            continue

        # Bulk insert
        df_feat = pd.DataFrame(features_batch)

        # Ensure all expected columns exist in the DataFrame
        expected_cols = [
            "ticker", "trade_date", "prev_close", "gap_pct", "open_930",
            "or_high", "or_low", "or_range", "day_high", "day_low", "day_range",
            "atr_20d", "vwap_at_1000", "vwap_at_1030", "vwap_at_1100",
            "price_vs_vwap_1000", "price_vs_vwap_1030", "price_vs_vwap_1100",
            "max_vwap_dev_above", "max_vwap_dev_below", "vwap_cross_count",
            "or_volume", "avg_or_volume_20d", "volume_ratio", "total_rth_volume",
            "first_30min_vol_pct", "rel_volume_1000",
            "ret_5min_0945", "ret_15min_1000", "ret_30min_1000",
            "ret_vs_spy_1000", "ret_vs_spy_1030",
            "new_hod_count", "new_hod_with_volume",
            "rsi_14_at_1000", "rsi_14_at_1030", "rsi_14_at_1100",
            "rsi_14_min", "rsi_14_max",
            "intraday_volatility", "or_range_vs_atr", "first_30min_range_pct",
            "consolidation_bars",
            "or_breakout", "or_breakout_time", "or_breakdown", "or_breakdown_time",
            "first_pullback_to_or", "first_pullback_time",
            "candle_hammer_count_5m", "candle_engulf_bull_count_5m",
            "candle_doji_count_5m", "candle_reversal_near_vwap",
            "volume_spike_count_5m", "volume_spike_near_vwap",
            "max_bar_volume_ratio_5m", "volume_climax_reversal",
            "eod_close", "eod_vs_open_pct", "eod_vs_vwap_pct",
            "eod_close_near_hod", "eod_close_near_lod",
            "report_quarter", "conviction_score", "accum_phase", "swing_signal",
            "expected_value", "squeeze_score", "short_squeeze_score",
            "days_to_cover", "insider_cluster", "tier1_count", "sector",
            "computed_at",
        ]
        for col in expected_cols:
            if col not in df_feat.columns:
                df_feat[col] = None
        df_feat = df_feat[expected_cols]

        conn.execute("DELETE FROM fact_intraday_features WHERE report_quarter = ?", [quarter])
        conn.register("_feat_temp", df_feat)
        conn.execute("""
            INSERT OR REPLACE INTO fact_intraday_features
            SELECT * FROM _feat_temp
        """)
        conn.unregister("_feat_temp")

        logger.info(
            "Quarter {}: {} feature rows computed",
            quarter, len(df_feat),
        )
        total_rows += len(df_feat)

    logger.info("Feature computation complete: {} total rows", total_rows)
    return total_rows


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_feature_summary(conn: duckdb.DuckDBPyConnection, quarters: Optional[List[str]] = None) -> None:
    _ensure_tables(conn)

    where = ""
    params: list = []
    if quarters:
        placeholders = ",".join(["?" for _ in quarters])
        where = f"WHERE report_quarter IN ({placeholders})"
        params = list(quarters)

    overview = conn.execute(f"""
        SELECT
            COUNT(*) AS total,
            COUNT(DISTINCT ticker) AS tickers,
            COUNT(DISTINCT trade_date) AS dates,
            AVG(volume_ratio) AS avg_vol_ratio,
            AVG(or_range_vs_atr) AS avg_or_atr,
            AVG(vwap_cross_count) AS avg_vwap_crosses,
            AVG(rsi_14_at_1000) AS avg_rsi_1000,
            SUM(CASE WHEN or_breakout THEN 1 ELSE 0 END) AS breakouts,
            SUM(CASE WHEN or_breakdown THEN 1 ELSE 0 END) AS breakdowns,
            AVG(intraday_volatility) AS avg_vol
        FROM fact_intraday_features {where}
    """, params).fetchone()

    if not overview or overview[0] == 0:
        print("No feature data found.")
        return

    total, tickers, dates, avg_vr, avg_or_atr, avg_vx, avg_rsi, bo, bd, avg_iv = overview

    print("\n" + "=" * 70)
    print("INTRADAY FEATURE SUMMARY")
    print("=" * 70)
    print(f"  Ticker-days:        {total:,}")
    print(f"  Unique tickers:     {tickers}")
    print(f"  Unique dates:       {dates}")
    print(f"\n  Avg volume ratio:   {avg_vr:.2f}")
    print(f"  Avg OR/ATR ratio:   {avg_or_atr:.3f}" if avg_or_atr else "  Avg OR/ATR ratio:   N/A")
    print(f"  Avg VWAP crosses:   {avg_vx:.1f}")
    print(f"  Avg RSI at 10:00:   {avg_rsi:.1f}" if avg_rsi else "  Avg RSI at 10:00:   N/A")
    print(f"  Avg intraday vol:   {avg_iv:.3f}%" if avg_iv else "  Avg intraday vol:   N/A")
    print(f"\n  OR breakouts:       {bo} ({100*bo/total:.1f}%)")
    print(f"  OR breakdowns:      {bd} ({100*bd/total:.1f}%)")

    # Null rate for key columns
    null_check = conn.execute(f"""
        SELECT
            SUM(CASE WHEN vwap_at_1000 IS NULL THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS vwap_null,
            SUM(CASE WHEN rsi_14_at_1000 IS NULL THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS rsi_null,
            SUM(CASE WHEN ret_vs_spy_1000 IS NULL THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS spy_null,
            SUM(CASE WHEN atr_20d IS NULL THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS atr_null
        FROM fact_intraday_features {where}
    """, params).fetchone()

    print(f"\n  Null rates:")
    print(f"    VWAP at 10:00:    {null_check[0]:.1f}%")
    print(f"    RSI at 10:00:     {null_check[1]:.1f}%")
    print(f"    Ret vs SPY 10:00: {null_check[2]:.1f}%")
    print(f"    ATR-20d:          {null_check[3]:.1f}%")
    print("=" * 70)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Intraday Feature Engine — compute universal features per ticker-day"
    )
    parser.add_argument("--compute", action="store_true", help="Run feature computation")
    parser.add_argument(
        "--quarters", type=str, default=None,
        help="Comma-separated quarters (e.g. 2024-Q1,2024-Q2,2024-Q3)",
    )
    parser.add_argument(
        "--min-conviction", type=float, default=MIN_CONVICTION,
        help=f"Minimum conviction score (default: {MIN_CONVICTION})",
    )
    parser.add_argument("--summary", action="store_true", help="Print feature summary")
    parser.add_argument("--drop-table", action="store_true",
                        help="Drop fact_intraday_features before computing (for schema changes)")
    args = parser.parse_args()

    conn = duckdb.connect(str(WAREHOUSE_PATH))

    try:
        quarters = None
        if args.quarters:
            quarters = [q.strip() for q in args.quarters.split(",")]

        if args.drop_table:
            logger.info("Dropping fact_intraday_features table for schema migration...")
            conn.execute("DROP TABLE IF EXISTS fact_intraday_features")
            logger.info("Table dropped. Will be recreated on next compute.")

        if args.compute:
            if not quarters:
                parser.error("--compute requires --quarters")
            compute_intraday_features(conn, quarters, args.min_conviction)

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
