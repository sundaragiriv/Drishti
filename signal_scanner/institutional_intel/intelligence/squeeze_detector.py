"""Squeeze Detector — Short Squeeze & Institutional Squeeze Scoring.

Combines short interest, short volume, dark pool, and institutional intelligence
to detect potential squeeze setups.

Two primary scores:
  1. squeeze_score (0-100): General squeeze pressure from short data alone
  2. short_squeeze_score (0-100): Institutional-confirmed squeeze setup
     (short data + accumulation + insider buying = highest conviction)

Academic basis:
  - Dechow et al. (2001): Short sellers target overvalued stocks; high SI
    with institutional accumulation = smart money disagreement
  - Desai et al. (2002): Stocks with days_to_cover > 5 significantly
    underperform; BUT if institutions are accumulating, the short thesis
    may be wrong — potential squeeze
  - Asquith et al. (2005): Short interest + institutional ownership changes
    are the strongest predictor of abnormal returns

Dimensions for squeeze_score:
  1. Days-to-Cover Signal     (0-25 pts)  DTC > 5 = elevated
  2. Short Volume Trend       (0-25 pts)  Rising short_volume_ratio = pressure
  3. Short Interest Change     (0-25 pts)  SI increasing while price rising = trap
  4. Dark Pool Activity        (0-25 pts)  High dark pool % = institutional hiding

Dimensions for short_squeeze_score (squeeze_score enhanced):
  + Institutional accumulation phase bonus
  + Insider buying confirmation bonus
  + Tier-1 manager presence bonus
  + Conviction score amplifier

Usage:
    python -m signal_scanner.institutional_intel.intelligence.squeeze_detector --score --quarter 2025-Q2
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _safe_int(val, default: int = 0) -> int:
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Add columns to intelligence_scores if needed
# ---------------------------------------------------------------------------

def _ensure_columns(conn: duckdb.DuckDBPyConnection) -> None:
    """Add squeeze columns to intelligence_scores if they don't exist."""
    new_cols = [
        ("squeeze_score", "DOUBLE DEFAULT 0"),
        ("short_squeeze_score", "DOUBLE DEFAULT 0"),
        ("days_to_cover", "DOUBLE"),
        ("short_interest_shares", "BIGINT"),
        ("short_volume_ratio_avg", "DOUBLE"),
        ("short_volume_ratio_trend", "DOUBLE"),
        ("dark_pool_pct_avg", "DOUBLE"),
        ("swing_flow_score", "DOUBLE DEFAULT 0"),
    ]
    for col_name, col_type in new_cols:
        try:
            conn.execute(f"SELECT {col_name} FROM intelligence_scores LIMIT 0")
        except duckdb.BinderException:
            conn.execute(f"ALTER TABLE intelligence_scores ADD COLUMN {col_name} {col_type}")
            logger.debug("Added column intelligence_scores.{}", col_name)


# ---------------------------------------------------------------------------
# Data collection helpers
# ---------------------------------------------------------------------------

def _get_short_interest_snapshot(conn: duckdb.DuckDBPyConnection) -> Dict[str, Dict]:
    """Get latest short interest data per ticker.

    Returns dict of {ticker: {short_interest, days_to_cover, avg_daily_volume,
                              si_change_pct (vs prior settlement)}}.
    """
    try:
        df = conn.execute("""
            WITH ranked AS (
                SELECT ticker, settlement_date, short_interest, days_to_cover, avg_daily_volume,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY settlement_date DESC) as rn
                FROM fact_short_interest
            ),
            latest AS (
                SELECT * FROM ranked WHERE rn = 1
            ),
            prior AS (
                SELECT * FROM ranked WHERE rn = 2
            )
            SELECT
                l.ticker,
                l.short_interest,
                l.days_to_cover,
                l.avg_daily_volume,
                l.settlement_date,
                CASE WHEN p.short_interest > 0
                     THEN (l.short_interest - p.short_interest) * 100.0 / p.short_interest
                     ELSE 0 END AS si_change_pct
            FROM latest l
            LEFT JOIN prior p ON l.ticker = p.ticker
        """).fetchdf()
    except Exception as e:
        logger.warning("Short interest query failed: {}", e)
        return {}

    result = {}
    for _, row in df.iterrows():
        result[str(row["ticker"])] = {
            "short_interest": _safe_int(row.get("short_interest")),
            "days_to_cover": _safe_float(row.get("days_to_cover")),
            "avg_daily_volume": _safe_int(row.get("avg_daily_volume")),
            "settlement_date": str(row.get("settlement_date", "")),
            "si_change_pct": _safe_float(row.get("si_change_pct")),
        }
    return result


def _get_short_volume_stats(conn: duckdb.DuckDBPyConnection, lookback_days: int = 30) -> Dict[str, Dict]:
    """Get short volume statistics over lookback period.

    Returns {ticker: {avg_ratio, latest_ratio, ratio_trend, spike_count}}.
    ratio_trend: positive = short volume increasing, negative = decreasing.
    """
    try:
        df = conn.execute("""
            WITH recent AS (
                SELECT ticker, trade_date, short_volume_ratio,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) as rn
                FROM fact_short_volume
                WHERE trade_date >= CURRENT_DATE - INTERVAL '30' DAY
                  AND total_volume > 10000
            )
            SELECT
                ticker,
                AVG(short_volume_ratio) AS avg_ratio,
                MAX(CASE WHEN rn = 1 THEN short_volume_ratio END) AS latest_ratio,
                -- Trend: avg of last 5 days vs avg of prior 15 days
                AVG(CASE WHEN rn <= 5 THEN short_volume_ratio END) -
                    AVG(CASE WHEN rn BETWEEN 6 AND 20 THEN short_volume_ratio END) AS ratio_trend,
                -- Spike count: days where ratio > 50%
                SUM(CASE WHEN short_volume_ratio > 50 THEN 1 ELSE 0 END) AS spike_count,
                COUNT(*) AS sample_days
            FROM recent
            GROUP BY ticker
            HAVING COUNT(*) >= 3
        """).fetchdf()
    except Exception as e:
        logger.warning("Short volume query failed: {}", e)
        return {}

    result = {}
    for _, row in df.iterrows():
        result[str(row["ticker"])] = {
            "avg_ratio": _safe_float(row.get("avg_ratio")),
            "latest_ratio": _safe_float(row.get("latest_ratio")),
            "ratio_trend": _safe_float(row.get("ratio_trend")),
            "spike_count": _safe_int(row.get("spike_count")),
            "sample_days": _safe_int(row.get("sample_days")),
        }
    return result


def _get_dark_pool_stats(conn: duckdb.DuckDBPyConnection, lookback_days: int = 30) -> Dict[str, Dict]:
    """Get dark pool volume statistics.

    Returns {ticker: {avg_dp_pct, latest_dp_pct, dp_trend}}.
    """
    try:
        df = conn.execute("""
            WITH recent AS (
                SELECT ticker, trade_date, dark_pool_pct,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) as rn
                FROM fact_dark_pool_daily
                WHERE trade_date >= CURRENT_DATE - INTERVAL '30' DAY
                  AND total_volume > 0
            )
            SELECT
                ticker,
                AVG(dark_pool_pct) AS avg_dp_pct,
                MAX(CASE WHEN rn = 1 THEN dark_pool_pct END) AS latest_dp_pct,
                AVG(CASE WHEN rn <= 5 THEN dark_pool_pct END) -
                    AVG(CASE WHEN rn BETWEEN 6 AND 20 THEN dark_pool_pct END) AS dp_trend,
                COUNT(*) AS sample_days
            FROM recent
            GROUP BY ticker
            HAVING COUNT(*) >= 3
        """).fetchdf()
    except Exception as e:
        logger.warning("Dark pool query failed: {}", e)
        return {}

    result = {}
    for _, row in df.iterrows():
        result[str(row["ticker"])] = {
            "avg_dp_pct": _safe_float(row.get("avg_dp_pct")),
            "latest_dp_pct": _safe_float(row.get("latest_dp_pct")),
            "dp_trend": _safe_float(row.get("dp_trend")),
            "sample_days": _safe_int(row.get("sample_days")),
        }
    return result


# ---------------------------------------------------------------------------
# Squeeze score computation
# ---------------------------------------------------------------------------

def _compute_dtc_signal(days_to_cover: float) -> float:
    """Days-to-cover component (0-25 pts).

    DTC > 5: elevated squeeze risk (Desai et al. 2002)
    DTC > 10: extreme squeeze risk
    """
    if days_to_cover <= 1:
        return 0.0
    elif days_to_cover <= 2:
        return 5.0
    elif days_to_cover <= 3:
        return 10.0
    elif days_to_cover <= 5:
        return 15.0
    elif days_to_cover <= 8:
        return 20.0
    else:
        return 25.0


def _compute_short_volume_signal(sv_stats: Dict) -> float:
    """Short volume trend component (0-25 pts).

    Rising short volume ratio + high absolute ratio = selling pressure
    that can reverse into squeeze.
    """
    avg_ratio = sv_stats.get("avg_ratio", 0)
    ratio_trend = sv_stats.get("ratio_trend", 0)
    spike_count = sv_stats.get("spike_count", 0)

    score = 0.0

    # Base: high average short volume ratio
    if avg_ratio > 50:
        score += 10.0
    elif avg_ratio > 40:
        score += 6.0
    elif avg_ratio > 30:
        score += 3.0

    # Trend: increasing short volume = building pressure
    if ratio_trend > 5:
        score += 8.0
    elif ratio_trend > 2:
        score += 5.0
    elif ratio_trend > 0:
        score += 2.0

    # Spikes: multiple days over 50% = persistent pressure
    if spike_count >= 5:
        score += 7.0
    elif spike_count >= 3:
        score += 4.0
    elif spike_count >= 1:
        score += 2.0

    return min(25.0, score)


def _compute_si_change_signal(si_change_pct: float, days_to_cover: float) -> float:
    """Short interest change component (0-25 pts).

    SI increasing while stock not dropping = shorts piling in on wrong side.
    Combined with high DTC = trap setup.
    """
    score = 0.0

    # SI increasing
    if si_change_pct > 20:
        score += 12.0
    elif si_change_pct > 10:
        score += 8.0
    elif si_change_pct > 5:
        score += 5.0
    elif si_change_pct > 0:
        score += 2.0
    # SI decreasing (short covering) — still contributes if DTC high
    elif si_change_pct < -10:
        score += 3.0  # covering rally potential

    # Amplifier: SI increase + high DTC = dangerous for shorts
    if si_change_pct > 10 and days_to_cover > 5:
        score += 8.0
    elif si_change_pct > 5 and days_to_cover > 3:
        score += 5.0

    return min(25.0, score)


def _compute_dark_pool_signal(dp_stats: Dict) -> float:
    """Dark pool activity component (0-25 pts).

    High dark pool % = institutional block trades hidden from lit markets.
    Rising dark pool activity can signal accumulation before squeeze.
    """
    avg_dp_pct = dp_stats.get("avg_dp_pct", 0)
    dp_trend = dp_stats.get("dp_trend", 0)

    score = 0.0

    # High absolute dark pool percentage
    if avg_dp_pct > 50:
        score += 12.0
    elif avg_dp_pct > 40:
        score += 8.0
    elif avg_dp_pct > 30:
        score += 5.0
    elif avg_dp_pct > 20:
        score += 2.0

    # Increasing dark pool activity
    if dp_trend > 5:
        score += 10.0
    elif dp_trend > 2:
        score += 6.0
    elif dp_trend > 0:
        score += 3.0

    return min(25.0, score)


def compute_squeeze_scores(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> List[Dict[str, Any]]:
    """Compute squeeze scores for all tickers in the quarter.

    Returns list of dicts with squeeze-related fields.
    """
    logger.info("Computing squeeze scores for quarter={}", quarter)

    # Load short data snapshots
    si_data = _get_short_interest_snapshot(conn)
    sv_data = _get_short_volume_stats(conn)
    dp_data = _get_dark_pool_stats(conn)

    logger.info(
        "Squeeze data: {} SI tickers, {} SV tickers, {} DP tickers",
        len(si_data), len(sv_data), len(dp_data),
    )

    # Load intelligence data for short_squeeze_score enhancement
    intel_data = {}
    try:
        intel_df = conn.execute("""
            SELECT ticker, accum_phase, conviction_score, insider_cluster_detected,
                   insider_score, tier1_manager_count, insider_effect_score
            FROM intelligence_scores
            WHERE report_quarter = ?
        """, [quarter]).fetchdf()
        for _, row in intel_df.iterrows():
            intel_data[str(row["ticker"])] = row.to_dict()
    except Exception as e:
        logger.warning("Intelligence query for squeeze failed: {}", e)

    # Get all tickers that have at least one data source
    all_tickers = set(si_data.keys()) | set(sv_data.keys()) | set(dp_data.keys())
    # Intersect with intelligence tickers for the quarter
    if intel_data:
        all_tickers = all_tickers & set(intel_data.keys())

    results = []
    for ticker in sorted(all_tickers):
        si = si_data.get(ticker, {})
        sv = sv_data.get(ticker, {})
        dp = dp_data.get(ticker, {})
        intel = intel_data.get(ticker, {})

        dtc = _safe_float(si.get("days_to_cover"))
        si_change = _safe_float(si.get("si_change_pct"))

        # --- Base squeeze_score (short data only) ---
        d1 = _compute_dtc_signal(dtc)
        d2 = _compute_short_volume_signal(sv)
        d3 = _compute_si_change_signal(si_change, dtc)
        d4 = _compute_dark_pool_signal(dp)

        squeeze = round(min(100.0, d1 + d2 + d3 + d4), 1)

        # --- Enhanced short_squeeze_score (multiplicative modifiers) ---
        # Multipliers preserve base score differentiation instead of stacking
        # additive bonuses that push everything to 100.
        phase = str(intel.get("accum_phase", ""))
        phase_mult = (
            1.20 if phase in ("ACTIVE_ACCUM", "LATE_ACCUM")
            else 1.10 if phase == "EARLY_ACCUM"
            else 0.70 if phase in ("DISTRIBUTION", "DECLINE")
            else 1.0
        )

        insider_mult = 1.15 if intel.get("insider_cluster_detected") else 1.0
        insider_score = _safe_float(intel.get("insider_score"))
        if insider_score > 60:
            insider_mult = min(insider_mult + 0.05, 1.20)

        tier1_count = _safe_int(intel.get("tier1_manager_count"))
        tier1_mult = (
            1.10 if tier1_count >= 3
            else 1.05 if tier1_count >= 1
            else 1.0
        )

        conviction = _safe_float(intel.get("conviction_score"))
        conv_mult = (
            1.10 if conviction > 70
            else 1.05 if conviction > 50
            else 1.0
        )

        short_squeeze = round(
            min(100.0, max(0.0, squeeze * phase_mult * insider_mult * tier1_mult * conv_mult)),
            1,
        )

        # --- Short Pressure Score (0-100) ---
        # Measures bearish pressure from short selling + dark pool hiding.
        # High = heavy short/dark-pool activity on this stock.
        # Interpretation depends on side:
        #   LONG ideas: high pressure = squeeze potential (contrarian edge)
        #   SHORT ideas: high pressure = confirmation (shorts agree)
        svr_avg = _safe_float(sv.get("avg_ratio"))
        svr_trend = _safe_float(sv.get("ratio_trend"))
        dp_avg = _safe_float(dp.get("avg_dp_pct"))

        # Short volume ratio: high = more short selling
        # Cross-sectional: avg ~40%, >50% = elevated, >60% = heavy
        svr_signal = min(25.0, max(0.0, (svr_avg - 30) * 0.83)) if svr_avg else 0

        # SVR trend: rising = increasing short pressure
        svr_trend_signal = min(15.0, max(0.0, svr_trend * 1.5)) if svr_trend else 0

        # Dark pool %: high = institutional hiding activity
        # Cross-sectional: avg ~35%, >45% = elevated, >55% = heavy
        dp_signal = min(25.0, max(0.0, (dp_avg - 25) * 1.0)) if dp_avg else 0

        # Squeeze component: reuse base squeeze but cap at 35pts
        squeeze_component = min(35.0, squeeze * 0.35)

        swing_flow = round(
            min(100.0, svr_signal + svr_trend_signal + dp_signal + squeeze_component),
            1,
        )

        results.append({
            "ticker": ticker,
            "squeeze_score": squeeze,
            "short_squeeze_score": short_squeeze,
            "swing_flow_score": swing_flow,
            "days_to_cover": dtc,
            "short_interest_shares": si.get("short_interest"),
            "short_volume_ratio_avg": sv.get("avg_ratio"),
            "short_volume_ratio_trend": sv.get("ratio_trend"),
            "dark_pool_pct_avg": dp.get("avg_dp_pct"),
        })

    logger.info("Squeeze scores computed for {} tickers", len(results))
    return results


def update_squeeze_in_intelligence(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> int:
    """Write squeeze scores into intelligence_scores table."""
    _ensure_columns(conn)

    results = compute_squeeze_scores(conn, quarter)
    if not results:
        return 0

    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    updated = 0

    for r in results:
        try:
            conn.execute("""
                UPDATE intelligence_scores
                SET squeeze_score = ?,
                    short_squeeze_score = ?,
                    swing_flow_score = ?,
                    days_to_cover = ?,
                    short_interest_shares = ?,
                    short_volume_ratio_avg = ?,
                    short_volume_ratio_trend = ?,
                    dark_pool_pct_avg = ?,
                    computed_at = ?
                WHERE ticker = ? AND report_quarter = ?
            """, [
                r["squeeze_score"],
                r["short_squeeze_score"],
                r["swing_flow_score"],
                r["days_to_cover"],
                r["short_interest_shares"],
                r["short_volume_ratio_avg"],
                r["short_volume_ratio_trend"],
                r["dark_pool_pct_avg"],
                now_iso,
                r["ticker"],
                quarter,
            ])
            updated += 1
        except Exception as e:
            logger.debug("Squeeze update failed for {}: {}", r["ticker"], e)

    logger.info("Squeeze scores updated: {}/{} for quarter={}", updated, len(results), quarter)
    return updated


def print_squeeze_summary(conn: duckdb.DuckDBPyConnection, quarter: str) -> None:
    """Print squeeze score summary and top candidates."""
    print("\n" + "=" * 70)
    print(f"SQUEEZE DETECTOR SUMMARY — {quarter}")
    print("=" * 70)

    try:
        # Distribution
        dist = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN squeeze_score >= 50 THEN 1 ELSE 0 END) as high_squeeze,
                SUM(CASE WHEN short_squeeze_score >= 50 THEN 1 ELSE 0 END) as high_short_squeeze,
                AVG(squeeze_score) as avg_squeeze,
                AVG(short_squeeze_score) as avg_short_squeeze
            FROM intelligence_scores
            WHERE report_quarter = ? AND squeeze_score > 0
        """, [quarter]).fetchone()
        if dist:
            print(f"\nTickers with squeeze data: {dist[0]}")
            print(f"High squeeze (>=50): {dist[1]}")
            print(f"High institutional squeeze (>=50): {dist[2]}")
            print(f"Avg squeeze: {dist[3]:.1f} | Avg inst squeeze: {dist[4]:.1f}")

        # Top squeeze candidates
        top = conn.execute("""
            SELECT ticker, squeeze_score, short_squeeze_score,
                   days_to_cover, accum_phase, conviction_score,
                   insider_cluster_detected
            FROM intelligence_scores
            WHERE report_quarter = ? AND short_squeeze_score > 0
            ORDER BY short_squeeze_score DESC
            LIMIT 20
        """, [quarter]).fetchall()
        if top:
            print(f"\nTop 20 Short Squeeze Candidates:")
            print(f"  {'Ticker':>6s}  {'Sqz':>5s}  {'InstSqz':>7s}  {'DTC':>5s}  {'Phase':>14s}  {'Conv':>5s}  {'Insider':>7s}")
            print(f"  {'-'*6}  {'-'*5}  {'-'*7}  {'-'*5}  {'-'*14}  {'-'*5}  {'-'*7}")
            for r in top:
                insider = "CLUSTER" if r[6] else ""
                dtc_str = f"{r[3]:.1f}" if r[3] else "N/A"
                conv_str = f"{r[5]:.0f}" if r[5] else "N/A"
                print(f"  {r[0]:>6s}  {r[1]:5.1f}  {r[2]:7.1f}  {dtc_str:>5s}  {str(r[4] or ''):>14s}  {conv_str:>5s}  {insider:>7s}")

        # Dangerous setups: high squeeze + institutional accumulation + insider buying
        golden = conn.execute("""
            SELECT ticker, short_squeeze_score, days_to_cover,
                   conviction_score, accum_phase
            FROM intelligence_scores
            WHERE report_quarter = ?
              AND short_squeeze_score >= 60
              AND accum_phase IN ('ACTIVE_ACCUM', 'LATE_ACCUM')
              AND conviction_score >= 50
            ORDER BY short_squeeze_score DESC
            LIMIT 10
        """, [quarter]).fetchall()
        if golden:
            print(f"\nGOLDEN SQUEEZE SETUPS (Inst Squeeze >= 60 + Active Accum + Conv >= 50):")
            for r in golden:
                print(f"  {r[0]:>6s}  Score: {r[1]:.0f}  DTC: {r[2]:.1f}  Conv: {r[3]:.0f}  Phase: {r[4]}")
        else:
            print(f"\nNo golden squeeze setups found (need more short data)")

    except Exception as e:
        print(f"  Error: {e}")

    print("=" * 70)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Squeeze Detector — compute squeeze scores from short data")
    p.add_argument("--score", action="store_true", help="Compute squeeze scores")
    p.add_argument("--summary", action="store_true", help="Print squeeze summary")
    p.add_argument("--quarter", default="", help="Target quarter (default: latest)")
    return p.parse_args()


def main() -> None:
    from signal_scanner.institutional_intel.warehouse.db import init_warehouse
    init_warehouse()

    args = parse_args()
    conn = duckdb.connect(str(WAREHOUSE_PATH))

    try:
        quarter = args.quarter
        if not quarter:
            from signal_scanner.institutional_intel.config import get_active_quarter
            quarter = get_active_quarter(conn) or "2025-Q3"

        if args.score:
            updated = update_squeeze_in_intelligence(conn, quarter)
            print(f"Squeeze scores updated for {updated} tickers in {quarter}")

        if args.summary or args.score:
            print_squeeze_summary(conn, quarter)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
