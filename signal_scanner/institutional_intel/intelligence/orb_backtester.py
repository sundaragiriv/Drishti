"""ORB Edge Backtester — 15-minute Opening Range Breakout strategy validation.

Tests the ORB strategy on historically qualifying tickers (filtered by the
intelligence pipeline) across multiple R:R ratios (1:2, 1:3, 1:4) and a
trailing-stop variant.

Reads 1-minute bars from ``fact_intraday_bars`` (loaded by intraday_loader)
and intelligence context from ``intelligence_scores``.

Usage:
    # Run backtest for specific quarters
    python -m signal_scanner.institutional_intel.intelligence.orb_backtester \
        --quarters 2024-Q3,2024-Q4,2025-Q1 --min-conviction 55

    # Print analysis summary
    python -m signal_scanner.institutional_intel.intelligence.orb_backtester --summary

    # Both in one go
    python -m signal_scanner.institutional_intel.intelligence.orb_backtester \
        --quarters 2024-Q3,2024-Q4,2025-Q1 --run --summary
"""

from __future__ import annotations

import argparse
import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import pandas as pd
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OR_MINUTES = 15           # Opening range window (9:30 → 9:45)
BREAKOUT_WINDOW_MIN = 45  # Scan for breakout 9:45 → 10:30
VOLUME_MULT = 1.5         # Minimum volume ratio vs 20-day avg
GAP_MAX_PCT = 5.0         # Skip if gap > 5%
MIN_CONVICTION = 55.0     # Default minimum conviction


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
        CREATE TABLE IF NOT EXISTS orb_backtest_results (
            ticker            TEXT NOT NULL,
            trade_date        DATE NOT NULL,
            report_quarter    TEXT,
            conviction_score  DOUBLE,
            accum_phase       TEXT,
            swing_signal      TEXT,
            expected_value    DOUBLE,
            squeeze_score     DOUBLE,
            sector            TEXT,
            tier1_count       INTEGER,
            insider_cluster   BOOLEAN,
            or_high           DOUBLE,
            or_low            DOUBLE,
            or_range          DOUBLE,
            or_volume         BIGINT,
            avg_or_volume     DOUBLE,
            volume_ratio      DOUBLE,
            gap_pct           DOUBLE,
            prev_close        DOUBLE,
            triggered         BOOLEAN,
            trigger_time      TIMESTAMP,
            entry_price       DOUBLE,
            stop_price        DOUBLE,
            hit_1r            BOOLEAN DEFAULT FALSE,
            hit_2r            BOOLEAN DEFAULT FALSE,
            hit_3r            BOOLEAN DEFAULT FALSE,
            hit_4r            BOOLEAN DEFAULT FALSE,
            hit_stop          BOOLEAN DEFAULT FALSE,
            time_to_1r_min    INTEGER,
            time_to_2r_min    INTEGER,
            time_to_3r_min    INTEGER,
            time_to_4r_min    INTEGER,
            time_to_stop_min  INTEGER,
            max_favorable_r   DOUBLE,
            max_adverse_r     DOUBLE,
            trail_exit_price  DOUBLE,
            trail_exit_r      DOUBLE,
            eod_price         DOUBLE,
            eod_r             DOUBLE,
            computed_at       TIMESTAMP,
            PRIMARY KEY (ticker, trade_date)
        )
    """)


# ---------------------------------------------------------------------------
# Intelligence context loading
# ---------------------------------------------------------------------------

def _load_qualifying_tickers(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
    min_conviction: float,
) -> pd.DataFrame:
    """Load qualifying tickers with their intelligence context."""
    return conn.execute("""
        SELECT
            i.ticker,
            i.conviction_score,
            i.accum_phase,
            i.swing_signal,
            i.expected_value,
            i.squeeze_score,
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


# ---------------------------------------------------------------------------
# Volume baseline computation
# ---------------------------------------------------------------------------

def _compute_or_volumes(
    conn: duckdb.DuckDBPyConnection,
    tickers: List[str],
) -> Dict[Tuple[str, date], float]:
    """Compute per-ticker per-day opening-range volume and 20-day rolling average.

    Returns dict of (ticker, trade_date) → avg_or_volume (20-day rolling).
    """
    if not tickers:
        return {}

    placeholders = ",".join(["?" for _ in tickers])

    # Daily OR volume: sum of volume for bars between 9:30 and 9:44
    daily_or = conn.execute(f"""
        SELECT
            ticker,
            CAST(bar_time AS DATE) AS td,
            SUM(volume) AS or_vol
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

    # Compute 20-day rolling average per ticker
    result = {}
    for ticker, group in daily_or.groupby("ticker"):
        group = group.sort_values("td")
        volumes = group["or_vol"].values
        dates = group["td"].values

        for idx in range(len(dates)):
            # Look back up to 20 trading days (excluding current)
            start = max(0, idx - 20)
            window = volumes[start:idx]
            if len(window) >= 3:  # need at least 3 days for meaningful avg
                avg_vol = float(window.mean())
            else:
                avg_vol = float(volumes[idx])  # fallback to current day

            td = pd.Timestamp(dates[idx]).date()
            result[(str(ticker), td)] = avg_vol

    return result


# ---------------------------------------------------------------------------
# Previous close lookup
# ---------------------------------------------------------------------------

def _load_prev_closes(
    conn: duckdb.DuckDBPyConnection,
    tickers: List[str],
    from_date: date,
    to_date: date,
) -> Dict[Tuple[str, date], float]:
    """Load previous trading day's close for each ticker×date.

    Returns dict of (ticker, trade_date) → prev_close.
    """
    if not tickers:
        return {}

    placeholders = ",".join(["?" for _ in tickers])

    from_str = from_date.isoformat()
    to_str = to_date.isoformat()

    df = conn.execute(f"""
        SELECT ticker, trade_date, close,
               LAG(close) OVER (PARTITION BY ticker ORDER BY trade_date) AS prev_close
        FROM fact_daily_prices
        WHERE ticker IN ({placeholders})
          AND trade_date >= CAST(? AS DATE) - INTERVAL '30 DAY'
          AND trade_date <= CAST(? AS DATE)
        ORDER BY ticker, trade_date
    """, [*tickers, from_str, to_str]).fetchdf()

    result = {}
    for _, row in df.iterrows():
        if row["prev_close"] is not None and pd.notna(row["prev_close"]):
            td = pd.Timestamp(row["trade_date"]).date()  # always convert to date
            result[(str(row["ticker"]), td)] = float(row["prev_close"])

    return result


# ---------------------------------------------------------------------------
# Core ORB simulation
# ---------------------------------------------------------------------------

def _simulate_orb_day(
    bars: pd.DataFrame,
    prev_close: float,
    avg_or_vol: float,
    volume_mult: float,
    gap_max_pct: float,
) -> Dict[str, Any]:
    """Simulate ORB strategy on one ticker for one day.

    Args:
        bars: DataFrame with columns [bar_time, open, high, low, close, volume]
              sorted by bar_time, filtered to RTH (9:30-16:00).
        prev_close: Previous day's closing price.
        avg_or_vol: 20-day rolling average of opening-range volume.
        volume_mult: Minimum volume ratio for trigger.
        gap_max_pct: Maximum gap percentage to allow.

    Returns dict with all result fields.
    """
    result: Dict[str, Any] = {
        "or_high": None, "or_low": None, "or_range": None,
        "or_volume": None, "avg_or_volume": avg_or_vol,
        "volume_ratio": None, "gap_pct": None, "prev_close": prev_close,
        "triggered": False, "trigger_time": None,
        "entry_price": None, "stop_price": None,
        "hit_1r": False, "hit_2r": False, "hit_3r": False, "hit_4r": False,
        "hit_stop": False,
        "time_to_1r_min": None, "time_to_2r_min": None,
        "time_to_3r_min": None, "time_to_4r_min": None,
        "time_to_stop_min": None,
        "max_favorable_r": 0.0, "max_adverse_r": 0.0,
        "trail_exit_price": None, "trail_exit_r": None,
        "eod_price": None, "eod_r": None,
    }

    if bars.empty or len(bars) < 20:
        return result

    # Extract bar_time components for filtering
    bar_times = pd.to_datetime(bars["bar_time"])
    hours = bar_times.dt.hour
    minutes = bar_times.dt.minute

    # Opening range: 9:30-9:44 (first 15 bars)
    or_mask = (hours == 9) & (minutes >= 30) & (minutes < 45)
    or_bars = bars[or_mask]

    if or_bars.empty or len(or_bars) < 5:
        return result

    or_high = float(or_bars["high"].max())
    or_low = float(or_bars["low"].min())
    or_range = or_high - or_low
    or_volume = int(or_bars["volume"].sum())

    result["or_high"] = or_high
    result["or_low"] = or_low
    result["or_range"] = or_range
    result["or_volume"] = or_volume

    # Gap filter
    first_open = float(or_bars.iloc[0]["open"])
    if prev_close > 0:
        gap_pct = (first_open - prev_close) / prev_close * 100
    else:
        gap_pct = 0.0
    result["gap_pct"] = gap_pct

    if abs(gap_pct) > gap_max_pct:
        return result

    # Volume filter
    if avg_or_vol > 0:
        vol_ratio = or_volume / avg_or_vol
    else:
        vol_ratio = 1.0
    result["volume_ratio"] = vol_ratio

    if vol_ratio < volume_mult:
        return result

    # Range too tight — skip if range < 0.1% of price (noise)
    if or_range <= 0 or (or_range / or_high) < 0.001:
        return result

    # Breakout scan: 9:45 to 10:30
    breakout_start_min = 45  # 9:45
    breakout_end_hour = 10
    breakout_end_min = 30

    breakout_mask = (
        ((hours == 9) & (minutes >= breakout_start_min)) |
        ((hours == 10) & (minutes <= breakout_end_min))
    )
    breakout_bars = bars[breakout_mask]

    trigger_time = None
    for _, bar in breakout_bars.iterrows():
        if float(bar["high"]) > or_high:
            trigger_time = bar["bar_time"]
            break

    if trigger_time is None:
        return result

    # Triggered!
    entry_price = or_high  # limit order at breakout level
    stop_price = or_low
    result["triggered"] = True
    result["trigger_time"] = trigger_time
    result["entry_price"] = entry_price
    result["stop_price"] = stop_price

    # R targets
    targets = {
        1: entry_price + 1 * or_range,
        2: entry_price + 2 * or_range,
        3: entry_price + 3 * or_range,
        4: entry_price + 4 * or_range,
    }

    # Outcome tracking: from trigger bar to EOD
    trigger_dt = pd.Timestamp(trigger_time)
    post_trigger_mask = bar_times >= trigger_dt
    tracking_bars = bars[post_trigger_mask]

    max_fav = 0.0
    max_adv = 0.0
    stopped = False
    trail_active = False
    trail_exited = False

    for _, bar in tracking_bars.iterrows():
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_time_val = bar["bar_time"]
        mins_from_entry = int((pd.Timestamp(bar_time_val) - trigger_dt).total_seconds() / 60)

        # R values for this bar
        if or_range > 0:
            r_high = (bar_high - entry_price) / or_range
            r_low = (bar_low - entry_price) / or_range
        else:
            continue

        max_fav = max(max_fav, r_high)
        max_adv = min(max_adv, r_low)

        # FIRST-ONE-WINS: check stop before targets
        if not stopped and bar_low <= stop_price:
            stopped = True
            result["hit_stop"] = True
            result["time_to_stop_min"] = mins_from_entry
            # Stop hit — no more target tracking
            break

        # Target tracking
        for n in [1, 2, 3, 4]:
            key = f"hit_{n}r"
            if not result[key] and bar_high >= targets[n]:
                result[key] = True
                result[f"time_to_{n}r_min"] = mins_from_entry

        # Trailing stop variant: after 1R hit, trail stop to breakeven
        if result["hit_1r"] and not trail_active:
            trail_active = True

        if trail_active and not trail_exited:
            if bar_low <= entry_price:
                trail_exited = True
                result["trail_exit_price"] = entry_price
                result["trail_exit_r"] = 0.0

    result["max_favorable_r"] = round(max_fav, 2)
    result["max_adverse_r"] = round(max_adv, 2)

    # Trailing stop: if still open at EOD, exit at last bar close
    if trail_active and not trail_exited and not stopped:
        if not tracking_bars.empty:
            last_close = float(tracking_bars.iloc[-1]["close"])
            if or_range > 0:
                result["trail_exit_price"] = last_close
                result["trail_exit_r"] = round((last_close - entry_price) / or_range, 2)

    # EOD price
    if not bars.empty:
        eod_close = float(bars.iloc[-1]["close"])
        result["eod_price"] = eod_close
        if or_range > 0 and entry_price:
            result["eod_r"] = round((eod_close - entry_price) / or_range, 2)

    return result


# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------

def run_orb_backtest(
    conn: duckdb.DuckDBPyConnection,
    quarters: List[str],
    min_conviction: float = MIN_CONVICTION,
    volume_mult: float = VOLUME_MULT,
    or_minutes: int = OR_MINUTES,
    gap_max_pct: float = GAP_MAX_PCT,
) -> int:
    """Run ORB backtest across multiple quarters.

    Returns total number of result rows created.
    """
    _ensure_tables(conn)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total_results = 0

    for quarter in quarters:
        logger.info("=== ORB Backtest: quarter={} ===", quarter)

        # 1. Load qualifying tickers
        intel_df = _load_qualifying_tickers(conn, quarter, min_conviction)
        if intel_df.empty:
            logger.warning("No qualifying tickers for {}", quarter)
            continue

        tickers = intel_df["ticker"].tolist()
        logger.info("{} qualifying tickers for {}", len(tickers), quarter)

        # 2. Trading day window
        start_date = _filing_date(quarter)
        end_date = _filing_date(_next_quarter(quarter))
        logger.info("Trading window: {} → {}", start_date, end_date)

        # 3. Pre-compute OR volumes
        logger.info("Computing opening-range volume baselines...")
        or_vol_map = _compute_or_volumes(conn, tickers)

        # 4. Previous closes
        logger.info("Loading previous closes...")
        prev_close_map = _load_prev_closes(conn, tickers, start_date, end_date)

        # 5. Build intel lookup
        intel_lookup = {}
        for _, row in intel_df.iterrows():
            intel_lookup[str(row["ticker"])] = row.to_dict()

        # 6. Get available trading days from intraday bars
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
            logger.warning("No intraday bars available for {} in window", quarter)
            continue

        # Group by ticker
        ticker_days = defaultdict(list)
        for _, row in trading_days_df.iterrows():
            td = pd.Timestamp(row["td"]).date()  # always convert to date
            ticker_days[str(row["ticker"])].append(td)

        # 7. Simulate — load ALL bars per ticker at once (1 query per ticker)
        results_batch = []
        tickers_with_days = [t for t in tickers if t in ticker_days]
        n_tickers_sim = len(tickers_with_days)

        for t_idx, ticker in enumerate(tickers_with_days):
            days = ticker_days[ticker]
            intel = intel_lookup.get(ticker, {})

            if (t_idx + 1) % 50 == 0 or t_idx == 0:
                logger.info(
                    "  Simulating [{}/{}] {} ({} days)...",
                    t_idx + 1, n_tickers_sim, ticker, len(days),
                )

            # Batch-load all bars for this ticker in the window
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

            # Split by date in-memory
            all_bars["_date"] = pd.to_datetime(all_bars["bar_time"]).dt.date

            for td in days:
                bars_df = all_bars[all_bars["_date"] == td].drop(columns=["_date"])

                prev_c = prev_close_map.get((ticker, td))
                if prev_c is None:
                    continue

                avg_or = or_vol_map.get((ticker, td), 0.0)

                sim = _simulate_orb_day(
                    bars_df, prev_c, avg_or,
                    volume_mult=volume_mult,
                    gap_max_pct=gap_max_pct,
                )

                results_batch.append({
                    "ticker": ticker,
                    "trade_date": td,
                    "report_quarter": quarter,
                    "conviction_score": float(intel.get("conviction_score") or 0),
                    "accum_phase": str(intel.get("accum_phase") or ""),
                    "swing_signal": str(intel.get("swing_signal") or ""),
                    "expected_value": float(intel.get("expected_value") or 0),
                    "squeeze_score": float(intel.get("squeeze_score") or 0),
                    "sector": str(intel.get("sector") or ""),
                    "tier1_count": int(intel.get("tier1_count") or 0),
                    "insider_cluster": bool(intel.get("insider_cluster")),
                    **sim,
                    "computed_at": now_iso,
                })

        if not results_batch:
            logger.info("No results for {}", quarter)
            continue

        # Bulk insert
        df_results = pd.DataFrame(results_batch)
        conn.execute("DELETE FROM orb_backtest_results WHERE report_quarter = ?", [quarter])
        conn.register("_orb_temp", df_results)
        conn.execute("""
            INSERT OR REPLACE INTO orb_backtest_results
            SELECT * FROM _orb_temp
        """)
        conn.unregister("_orb_temp")

        triggered = df_results["triggered"].sum()
        logger.info(
            "Quarter {}: {} ticker-days, {} triggered ({:.1f}%)",
            quarter, len(df_results), triggered,
            100 * triggered / max(1, len(df_results)),
        )
        total_results += len(df_results)

    logger.info("ORB backtest complete: {} total result rows", total_results)
    return total_results


# ---------------------------------------------------------------------------
# Analysis & reporting
# ---------------------------------------------------------------------------

def print_orb_summary(conn: duckdb.DuckDBPyConnection, quarters: Optional[List[str]] = None) -> None:
    """Print comprehensive ORB backtest analysis."""
    _ensure_tables(conn)

    where = ""
    params: list = []
    if quarters:
        placeholders = ",".join(["?" for _ in quarters])
        where = f"WHERE report_quarter IN ({placeholders})"
        params = list(quarters)

    # Overview
    overview = conn.execute(f"""
        SELECT
            COUNT(*) AS total_days,
            SUM(CASE WHEN triggered THEN 1 ELSE 0 END) AS triggered,
            SUM(CASE WHEN triggered AND hit_1r THEN 1 ELSE 0 END) AS hit_1r,
            SUM(CASE WHEN triggered AND hit_2r THEN 1 ELSE 0 END) AS hit_2r,
            SUM(CASE WHEN triggered AND hit_3r THEN 1 ELSE 0 END) AS hit_3r,
            SUM(CASE WHEN triggered AND hit_4r THEN 1 ELSE 0 END) AS hit_4r,
            SUM(CASE WHEN triggered AND hit_stop THEN 1 ELSE 0 END) AS stopped,
            COUNT(DISTINCT ticker) AS tickers
        FROM orb_backtest_results
        {where}
    """, params).fetchone()

    total, triggered, h1, h2, h3, h4, stopped, n_tickers = overview

    if total == 0:
        print("No backtest results found. Run --quarters first.")
        return

    print("\n" + "=" * 90)
    print("ORB EDGE BACKTEST RESULTS")
    print("=" * 90)

    print(f"\n1. OVERVIEW")
    print(f"   Unique tickers:        {n_tickers}")
    print(f"   Total ticker-days:     {total}")
    print(f"   Triggered (breakout):  {triggered} ({100 * triggered / max(1, total):.1f}%)")
    print(f"   Not triggered:         {total - triggered}")

    if triggered == 0:
        print("\n   No triggered trades to analyze.")
        print("=" * 90)
        return

    # Win rate by R target
    print(f"\n2. WIN RATE BY R:R TARGET (n={triggered} triggered trades)")
    print(f"   {'Target':<16} {'Win Rate':>8} {'Stopped':>8} {'EV/Trade':>10}")
    print(f"   {'-' * 50}")

    for n, label in [(1, "1:1 (1R)"), (2, "1:2 (2R)"), (3, "1:3 (3R)"), (4, "1:4 (4R)")]:
        wr = 100 * (h1 if n == 1 else h2 if n == 2 else h3 if n == 3 else h4) / max(1, triggered)
        hits = h1 if n == 1 else h2 if n == 2 else h3 if n == 3 else h4
        losses = stopped  # trades that hit stop
        # EV = p(win) * nR - p(loss) * 1R
        # p(win) = hits/triggered, but some trades neither hit target nor stop (EOD close)
        # For clean EV: only count trades that resolved (hit target OR hit stop)
        resolved = hits + stopped
        if resolved > 0:
            wr_clean = 100 * hits / resolved
            ev = (hits / resolved) * n - (stopped / resolved) * 1.0
        else:
            wr_clean = 0.0
            ev = 0.0

        print(f"   {label:<16} {wr_clean:>7.1f}% {100 * stopped / max(1, triggered):>7.1f}% {ev:>+9.2f}R")

    # Trailing stop analysis
    trail_data = conn.execute(f"""
        SELECT
            AVG(trail_exit_r) AS avg_trail_r,
            COUNT(CASE WHEN trail_exit_r > 0 THEN 1 END) AS trail_winners,
            COUNT(CASE WHEN trail_exit_r IS NOT NULL THEN 1 END) AS trail_total,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY trail_exit_r) AS median_trail_r
        FROM orb_backtest_results
        {where}
        {"AND" if where else "WHERE"} triggered = TRUE AND hit_1r = TRUE
    """, params).fetchone()

    if trail_data and trail_data[2] and trail_data[2] > 0:
        print(f"\n   Trailing Stop (BE after 1R):  avg {trail_data[0]:+.2f}R  "
              f"median {trail_data[3]:+.2f}R  (n={trail_data[2]})")

    # Max favorable excursion distribution
    mfe_data = conn.execute(f"""
        SELECT
            PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY max_favorable_r) AS p25,
            PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY max_favorable_r) AS p50,
            PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY max_favorable_r) AS p75,
            PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY max_favorable_r) AS p90,
            AVG(max_favorable_r) AS avg_mfe
        FROM orb_backtest_results
        {where}
        {"AND" if where else "WHERE"} triggered = TRUE
    """, params).fetchone()

    if mfe_data:
        print(f"\n   Max Favorable Excursion:  P25={mfe_data[0]:+.1f}R  P50={mfe_data[1]:+.1f}R  "
              f"P75={mfe_data[2]:+.1f}R  P90={mfe_data[3]:+.1f}R  avg={mfe_data[4]:+.1f}R")

    # R-distribution: where do winners die?
    print(f"\n3. R-DISTRIBUTION (where do winners peak?)")
    r_dist = conn.execute(f"""
        SELECT
            SUM(CASE WHEN hit_1r AND NOT hit_2r THEN 1 ELSE 0 END) AS r1_only,
            SUM(CASE WHEN hit_2r AND NOT hit_3r THEN 1 ELSE 0 END) AS r2_only,
            SUM(CASE WHEN hit_3r AND NOT hit_4r THEN 1 ELSE 0 END) AS r3_only,
            SUM(CASE WHEN hit_4r THEN 1 ELSE 0 END) AS r4_plus,
            SUM(CASE WHEN NOT hit_1r AND hit_stop THEN 1 ELSE 0 END) AS stopped_before_1r,
            SUM(CASE WHEN NOT hit_1r AND NOT hit_stop THEN 1 ELSE 0 END) AS no_resolve
        FROM orb_backtest_results
        {where}
        {"AND" if where else "WHERE"} triggered = TRUE
    """, params).fetchone()

    if r_dist:
        t = max(1, triggered)
        print(f"   Stopped before 1R:    {r_dist[4]:>5} ({100 * r_dist[4] / t:>5.1f}%)")
        print(f"   Reached 1R not 2R:    {r_dist[0]:>5} ({100 * r_dist[0] / t:>5.1f}%)")
        print(f"   Reached 2R not 3R:    {r_dist[1]:>5} ({100 * r_dist[1] / t:>5.1f}%)")
        print(f"   Reached 3R not 4R:    {r_dist[2]:>5} ({100 * r_dist[2] / t:>5.1f}%)")
        print(f"   Reached 4R+:          {r_dist[3]:>5} ({100 * r_dist[3] / t:>5.1f}%)")
        print(f"   No resolution (EOD):  {r_dist[5]:>5} ({100 * r_dist[5] / t:>5.1f}%)")

    # By sector
    print(f"\n4. WIN RATE BY SECTOR (at 1:3 R:R)")
    sector_data = conn.execute(f"""
        SELECT
            sector,
            COUNT(*) AS n,
            SUM(CASE WHEN hit_3r THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN hit_stop THEN 1 ELSE 0 END) AS losses,
            AVG(max_favorable_r) AS avg_mfe
        FROM orb_backtest_results
        {where}
        {"AND" if where else "WHERE"} triggered = TRUE
        GROUP BY sector
        HAVING COUNT(*) >= 5
        ORDER BY SUM(CASE WHEN hit_3r THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(*), 0) DESC
    """, params).fetchall()

    if sector_data:
        print(f"   {'Sector':<25} {'N':>5} {'WinRate':>8} {'StopRate':>9} {'AvgMFE':>8}")
        print(f"   {'-' * 58}")
        for row in sector_data:
            sector = row[0] or "Unknown"
            n = row[1]
            wr = 100 * row[2] / max(1, n)
            sr = 100 * row[3] / max(1, n)
            print(f"   {sector:<25} {n:>5} {wr:>7.1f}% {sr:>8.1f}% {row[4]:>+7.1f}R")

    # By conviction bucket
    print(f"\n5. WIN RATE BY CONVICTION BUCKET (at 1:3 R:R)")
    conv_data = conn.execute(f"""
        SELECT
            CASE
                WHEN conviction_score >= 85 THEN '85-100'
                WHEN conviction_score >= 75 THEN '75-85'
                WHEN conviction_score >= 65 THEN '65-75'
                ELSE '55-65'
            END AS bucket,
            COUNT(*) AS n,
            SUM(CASE WHEN hit_3r THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN hit_stop THEN 1 ELSE 0 END) AS losses,
            AVG(max_favorable_r) AS avg_mfe
        FROM orb_backtest_results
        {where}
        {"AND" if where else "WHERE"} triggered = TRUE
        GROUP BY bucket
        ORDER BY bucket
    """, params).fetchall()

    if conv_data:
        print(f"   {'Conviction':<12} {'N':>5} {'WinRate':>8} {'StopRate':>9} {'AvgMFE':>8}")
        print(f"   {'-' * 46}")
        for row in conv_data:
            n = row[1]
            wr = 100 * row[2] / max(1, n)
            sr = 100 * row[3] / max(1, n)
            print(f"   {row[0]:<12} {n:>5} {wr:>7.1f}% {sr:>8.1f}% {row[4]:>+7.1f}R")

    # By phase
    print(f"\n6. WIN RATE BY PHASE (at 1:3 R:R)")
    phase_data = conn.execute(f"""
        SELECT
            accum_phase,
            COUNT(*) AS n,
            SUM(CASE WHEN hit_3r THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN hit_stop THEN 1 ELSE 0 END) AS losses,
            AVG(max_favorable_r) AS avg_mfe
        FROM orb_backtest_results
        {where}
        {"AND" if where else "WHERE"} triggered = TRUE
        GROUP BY accum_phase
        HAVING COUNT(*) >= 5
        ORDER BY SUM(CASE WHEN hit_3r THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(*), 0) DESC
    """, params).fetchall()

    if phase_data:
        print(f"   {'Phase':<16} {'N':>5} {'WinRate':>8} {'StopRate':>9} {'AvgMFE':>8}")
        print(f"   {'-' * 50}")
        for row in phase_data:
            n = row[1]
            wr = 100 * row[2] / max(1, n)
            sr = 100 * row[3] / max(1, n)
            print(f"   {row[0]:<16} {n:>5} {wr:>7.1f}% {sr:>8.1f}% {row[4]:>+7.1f}R")

    # By squeeze score
    print(f"\n7. SQUEEZE SCORE IMPACT (at 1:3 R:R)")
    squeeze_data = conn.execute(f"""
        SELECT
            CASE
                WHEN squeeze_score >= 60 THEN 'High (60+)'
                WHEN squeeze_score >= 30 THEN 'Medium (30-60)'
                ELSE 'Low (<30)'
            END AS bucket,
            COUNT(*) AS n,
            SUM(CASE WHEN hit_3r THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN hit_stop THEN 1 ELSE 0 END) AS losses,
            AVG(max_favorable_r) AS avg_mfe
        FROM orb_backtest_results
        {where}
        {"AND" if where else "WHERE"} triggered = TRUE AND squeeze_score > 0
        GROUP BY bucket
        ORDER BY bucket DESC
    """, params).fetchall()

    if squeeze_data:
        print(f"   {'Squeeze':<16} {'N':>5} {'WinRate':>8} {'StopRate':>9} {'AvgMFE':>8}")
        print(f"   {'-' * 50}")
        for row in squeeze_data:
            n = row[1]
            wr = 100 * row[2] / max(1, n)
            sr = 100 * row[3] / max(1, n)
            print(f"   {row[0]:<16} {n:>5} {wr:>7.1f}% {sr:>8.1f}% {row[4]:>+7.1f}R")

    # Best combo finder
    print(f"\n8. BEST COMBINATIONS (at 1:3 R:R, min 10 trades)")
    combo_data = conn.execute(f"""
        SELECT
            accum_phase,
            CASE
                WHEN conviction_score >= 75 THEN '75+'
                WHEN conviction_score >= 65 THEN '65-75'
                ELSE '55-65'
            END AS conv_bucket,
            sector,
            COUNT(*) AS n,
            SUM(CASE WHEN hit_3r THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN hit_stop THEN 1 ELSE 0 END) AS losses,
            AVG(max_favorable_r) AS avg_mfe
        FROM orb_backtest_results
        {where}
        {"AND" if where else "WHERE"} triggered = TRUE
        GROUP BY accum_phase, conv_bucket, sector
        HAVING COUNT(*) >= 10
        ORDER BY SUM(CASE WHEN hit_3r THEN 1 ELSE 0 END) * 1.0 / COUNT(*) DESC
        LIMIT 15
    """, params).fetchall()

    if combo_data:
        print(f"   {'Phase':<14} {'Conv':<8} {'Sector':<18} {'N':>4} {'WR':>7} {'MFE':>7}")
        print(f"   {'-' * 62}")
        for row in combo_data:
            n = row[3]
            wr = 100 * row[4] / max(1, n)
            print(f"   {row[0]:<14} {row[1]:<8} {(row[2] or '?'):<18} {n:>4} {wr:>6.1f}% {row[6]:>+6.1f}R")

    # Time-of-day analysis
    print(f"\n9. BREAKOUT TIMING")
    time_data = conn.execute(f"""
        SELECT
            EXTRACT(HOUR FROM trigger_time) AS hr,
            EXTRACT(MINUTE FROM trigger_time) AS mn,
            COUNT(*) AS n,
            SUM(CASE WHEN hit_3r THEN 1 ELSE 0 END) AS wins
        FROM orb_backtest_results
        {where}
        {"AND" if where else "WHERE"} triggered = TRUE AND trigger_time IS NOT NULL
        GROUP BY hr, mn
        ORDER BY hr, mn
    """, params).fetchall()

    if time_data:
        # Aggregate into 5-minute buckets
        buckets: Dict[str, list] = {}
        for hr, mn, n, w in time_data:
            bucket_mn = int(mn) // 5 * 5
            key = f"{int(hr):02d}:{bucket_mn:02d}"
            if key not in buckets:
                buckets[key] = [0, 0]
            buckets[key][0] += n
            buckets[key][1] += w

        print(f"   {'Time':<8} {'Triggers':>8} {'3R WinRate':>10}")
        print(f"   {'-' * 30}")
        for time_key, (n, w) in sorted(buckets.items()):
            if n >= 3:
                wr = 100 * w / n
                print(f"   {time_key:<8} {n:>8} {wr:>9.1f}%")

    print("\n" + "=" * 90)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="ORB Edge Backtester — validate 15-min ORB + intelligence filter"
    )
    parser.add_argument(
        "--quarters", type=str, default=None,
        help="Comma-separated quarters to backtest (e.g. 2024-Q3,2024-Q4,2025-Q1)",
    )
    parser.add_argument("--run", action="store_true", help="Run the backtest")
    parser.add_argument("--summary", action="store_true", help="Print analysis summary")
    parser.add_argument(
        "--min-conviction", type=float, default=MIN_CONVICTION,
        help=f"Minimum conviction score (default: {MIN_CONVICTION})",
    )
    parser.add_argument(
        "--volume-mult", type=float, default=VOLUME_MULT,
        help=f"Volume multiplier for trigger (default: {VOLUME_MULT})",
    )
    parser.add_argument(
        "--gap-max", type=float, default=GAP_MAX_PCT,
        help=f"Max gap %% to allow (default: {GAP_MAX_PCT})",
    )
    args = parser.parse_args()

    conn = duckdb.connect(str(WAREHOUSE_PATH))

    try:
        quarters = None
        if args.quarters:
            quarters = [q.strip() for q in args.quarters.split(",")]

        if args.run or (quarters and not args.summary):
            if not quarters:
                parser.error("--run requires --quarters")
            run_orb_backtest(
                conn, quarters,
                min_conviction=args.min_conviction,
                volume_mult=args.volume_mult,
                gap_max_pct=args.gap_max,
            )

        if args.summary:
            print_orb_summary(conn, quarters)

        if not args.run and not args.summary and not quarters:
            parser.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    from signal_scanner.utils.logger import setup_logger
    setup_logger()
    main()
