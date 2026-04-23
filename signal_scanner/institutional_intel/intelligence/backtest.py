"""Phase 1.5: Walk-Forward Backtest Engine.

Validates the phase classifier and conviction score against historical data.
Uses only data that was publicly available at entry date (filing_date + 45 days)
to prevent look-ahead bias.

Train: 2020-Q2 → 2023-Q4 (14 quarters)
Validation: 2024-Q1 → 2024-Q3 (3 quarters, out-of-sample)
Holdout: 2024-Q4 → present (never touch until live)

Usage:
    python -m signal_scanner.institutional_intel.intelligence.backtest --run
    python -m signal_scanner.institutional_intel.intelligence.backtest --summary
    python -m signal_scanner.institutional_intel.intelligence.backtest --summary --quarter 2023-Q4
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from typing import List, Optional

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH
from signal_scanner.institutional_intel.intelligence.phase_classifier import run_phase_classification
from signal_scanner.institutional_intel.intelligence.conviction_score import update_conviction_in_intelligence


# ---------------------------------------------------------------------------
# Quarter helpers
# ---------------------------------------------------------------------------

def _quarter_to_filing_date(quarter: str) -> str:
    """13F filing deadline: 45 days after quarter end. Returns YYYY-MM-DD."""
    year = int(quarter.split("-Q")[0])
    qnum = int(quarter.split("-Q")[1])
    # Quarter end months: Q1=Mar, Q2=Jun, Q3=Sep, Q4=Dec
    quarter_end_months = {1: 3, 2: 6, 3: 9, 4: 12}
    month = quarter_end_months[qnum]
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    quarter_end = datetime(year, month, last_day)
    filing_date = quarter_end + timedelta(days=45)
    return filing_date.strftime("%Y-%m-%d")


def _all_quarters_between(start_q: str, end_q: str) -> List[str]:
    """Return list of quarter strings from start to end inclusive."""
    quarters = []
    year = int(start_q.split("-Q")[0])
    qnum = int(start_q.split("-Q")[1])
    end_year = int(end_q.split("-Q")[0])
    end_qnum = int(end_q.split("-Q")[1])

    while (year, qnum) <= (end_year, end_qnum):
        quarters.append(f"{year}-Q{qnum}")
        qnum += 1
        if qnum > 4:
            qnum = 1
            year += 1
    return quarters


# ---------------------------------------------------------------------------
# Forward return computation
# ---------------------------------------------------------------------------

def _compute_forward_return(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    entry_date: str,
    days: int,
) -> Optional[float]:
    """Get price at entry_date and entry_date + days. Return pct change."""
    try:
        # Entry price: nearest trading day on or after entry_date
        entry_row = conn.execute("""
            SELECT close FROM fact_daily_prices
            WHERE ticker = ? AND trade_date >= ?
            ORDER BY trade_date ASC LIMIT 1
        """, [ticker, entry_date]).fetchone()

        if not entry_row or not entry_row[0]:
            return None

        entry_price = float(entry_row[0])

        # Exit price: nearest trading day on or after entry_date + days
        from datetime import datetime, timedelta
        exit_date = (datetime.strptime(entry_date, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
        exit_row = conn.execute("""
            SELECT close FROM fact_daily_prices
            WHERE ticker = ? AND trade_date >= ?
            ORDER BY trade_date ASC LIMIT 1
        """, [ticker, exit_date]).fetchone()

        if not exit_row or not exit_row[0]:
            return None

        exit_price = float(exit_row[0])
        return round((exit_price - entry_price) / entry_price * 100, 4)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------

def run_backtest(
    conn: duckdb.DuckDBPyConnection,
    train_start: str = "2020-Q2",
    train_end: str = "2023-Q4",
    holdout_start: str = "2024-Q4",
) -> int:
    """Run walk-forward backtest over training quarters.

    For each quarter:
      1. Classify phases (using only data available through that quarter)
      2. Compute conviction scores
      3. Record phase + conviction + entry_date (filing_date + 45 days)
      4. Compute forward returns at 30/60/90/180 days
      5. Compute SPY benchmark for same windows
      6. Write to backtest_results
    """
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    quarters = _all_quarters_between(train_start, train_end)
    logger.info("Running backtest over {} quarters: {} to {}", len(quarters), train_start, train_end)

    total_rows = 0

    for quarter in quarters:
        filing_date = _quarter_to_filing_date(quarter)
        logger.info("Backtest quarter={} | entry_date={}", quarter, filing_date)

        # Step 1: Classify phases for this quarter
        try:
            run_phase_classification(conn, quarter)
            update_conviction_in_intelligence(conn, quarter)
        except Exception as e:
            logger.warning("Phase classification failed for {}: {}", quarter, e)
            continue

        # Step 2: Load intelligence scores for this quarter
        scores_df = conn.execute("""
            SELECT
                ticker, accum_phase, conviction_score,
                cascade_stage, insider_cluster_detected,
                tier1_manager_count
            FROM intelligence_scores
            WHERE report_quarter = ?
              AND accum_phase IS NOT NULL
        """, [quarter]).fetchdf()

        if scores_df.empty:
            logger.warning("No intelligence scores for quarter={}", quarter)
            continue

        # Step 3: Compute forward returns
        rows = []
        spy_returns = {}
        for days in [30, 60, 90, 180]:
            spy_ret = _compute_forward_return(conn, "SPY", filing_date, days)
            spy_returns[days] = spy_ret

        processed = 0
        for _, r in scores_df.iterrows():
            ticker = str(r["ticker"])

            entry_price_row = conn.execute("""
                SELECT close FROM fact_daily_prices
                WHERE ticker = ? AND trade_date >= ?
                ORDER BY trade_date ASC LIMIT 1
            """, [ticker, filing_date]).fetchone()

            entry_price = float(entry_price_row[0]) if entry_price_row and entry_price_row[0] else None

            ret_30  = _compute_forward_return(conn, ticker, filing_date, 30)
            ret_60  = _compute_forward_return(conn, ticker, filing_date, 60)
            ret_90  = _compute_forward_return(conn, ticker, filing_date, 90)
            ret_180 = _compute_forward_return(conn, ticker, filing_date, 180)

            rows.append((
                ticker, quarter, filing_date, entry_price,
                str(r["accum_phase"]), float(r["conviction_score"] or 0),
                int(r["cascade_stage"] or 0),
                bool(r["insider_cluster_detected"]),
                int(r["tier1_manager_count"] or 0) > 0,
                ret_30, ret_60, ret_90, ret_180,
                spy_returns.get(30), spy_returns.get(60),
                spy_returns.get(90), spy_returns.get(180),
                (ret_30 - spy_returns[30]) if ret_30 is not None and spy_returns.get(30) is not None else None,
                (ret_60 - spy_returns[60]) if ret_60 is not None and spy_returns.get(60) is not None else None,
                (ret_90 - spy_returns[90]) if ret_90 is not None and spy_returns.get(90) is not None else None,
                (ret_180 - spy_returns[180]) if ret_180 is not None and spy_returns.get(180) is not None else None,
                int(r.get("expected_impact_quarters", 3) if "expected_impact_quarters" in r.index else 3),
                None,  # actual_peak_quarter (computed post-hoc)
                now_iso,
            ))
            processed += 1

        if rows:
            conn.executemany("""
                INSERT OR REPLACE INTO backtest_results (
                    ticker, signal_quarter, entry_date, entry_price,
                    accum_phase, conviction_score, cascade_stage,
                    insider_confirmed, tier1_present,
                    return_30d, return_60d, return_90d, return_180d,
                    spy_return_30d, spy_return_60d, spy_return_90d, spy_return_180d,
                    alpha_30d, alpha_60d, alpha_90d, alpha_180d,
                    estimated_lag_quarters, actual_peak_quarter, computed_at
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
            """, rows)
            total_rows += len(rows)
            logger.info("Backtest quarter={}: {} tickers processed", quarter, processed)

    logger.info("Backtest complete: {} total results across {} quarters", total_rows, len(quarters))
    return total_rows


# ---------------------------------------------------------------------------
# Summary reporting
# ---------------------------------------------------------------------------

def print_backtest_summary(conn: duckdb.DuckDBPyConnection) -> None:
    """Print win rates, alpha, and hit rates by segment."""
    total = conn.execute("SELECT COUNT(*) FROM backtest_results").fetchone()[0]
    if total == 0:
        logger.warning("No backtest results found. Run --run first.")
        return

    print(f"\n{'='*70}")
    print(f"  BACKTEST SUMMARY  ({total:,} signal instances)")
    print(f"{'='*70}")

    # Win rate by phase (90-day alpha)
    print("\n>> WIN RATE BY PHASE (alpha_90d > 0 = win vs SPY):")
    phase_df = conn.execute("""
        SELECT
            accum_phase,
            COUNT(*) AS total,
            SUM(CASE WHEN alpha_90d > 0 THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(alpha_90d), 2) AS avg_alpha_90d,
            ROUND(AVG(return_90d), 2) AS avg_return_90d
        FROM backtest_results
        WHERE return_90d IS NOT NULL AND alpha_90d IS NOT NULL
        GROUP BY accum_phase
        ORDER BY avg_alpha_90d DESC
    """).fetchdf()
    for _, r in phase_df.iterrows():
        win_rate = round(r["wins"] / r["total"] * 100, 1) if r["total"] > 0 else 0
        print(f"  {str(r['accum_phase']):15s}  n={int(r['total']):5d}  "
              f"WinRate={win_rate:5.1f}%  AvgAlpha={r['avg_alpha_90d']:+.1f}%  "
              f"AvgReturn={r['avg_return_90d']:+.1f}%")

    # Win rate by conviction bucket
    print("\n>> WIN RATE BY CONVICTION SCORE BUCKET (alpha_90d):")
    bucket_df = conn.execute("""
        SELECT
            CASE
                WHEN conviction_score < 40 THEN '0-40 Low'
                WHEN conviction_score < 60 THEN '40-60 Moderate'
                WHEN conviction_score < 80 THEN '60-80 High'
                ELSE '80-100 Very High'
            END AS bucket,
            COUNT(*) AS total,
            SUM(CASE WHEN alpha_90d > 0 THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(alpha_90d), 2) AS avg_alpha_90d
        FROM backtest_results
        WHERE return_90d IS NOT NULL AND alpha_90d IS NOT NULL
        GROUP BY bucket
        ORDER BY MIN(conviction_score)
    """).fetchdf()
    for _, r in bucket_df.iterrows():
        win_rate = round(r["wins"] / r["total"] * 100, 1) if r["total"] > 0 else 0
        print(f"  {str(r['bucket']):20s}  n={int(r['total']):5d}  "
              f"WinRate={win_rate:5.1f}%  AvgAlpha={r['avg_alpha_90d']:+.1f}%")

    # Insider confirmation effect
    print("\n>> INSIDER CONFIRMATION IMPACT:")
    ins_df = conn.execute("""
        SELECT
            insider_confirmed,
            COUNT(*) AS total,
            SUM(CASE WHEN alpha_90d > 0 THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(alpha_90d), 2) AS avg_alpha_90d
        FROM backtest_results
        WHERE return_90d IS NOT NULL AND alpha_90d IS NOT NULL
        GROUP BY insider_confirmed
    """).fetchdf()
    for _, r in ins_df.iterrows():
        label = "With Cluster Insider" if r["insider_confirmed"] else "No Insider Signal"
        win_rate = round(r["wins"] / r["total"] * 100, 1) if r["total"] > 0 else 0
        print(f"  {label:25s}  n={int(r['total']):5d}  "
              f"WinRate={win_rate:5.1f}%  AvgAlpha={r['avg_alpha_90d']:+.1f}%")

    # Tier-1 manager effect
    print("\n>> TIER-1 MANAGER PRESENCE IMPACT:")
    tier_df = conn.execute("""
        SELECT
            tier1_present,
            COUNT(*) AS total,
            SUM(CASE WHEN alpha_90d > 0 THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(alpha_90d), 2) AS avg_alpha_90d
        FROM backtest_results
        WHERE return_90d IS NOT NULL AND alpha_90d IS NOT NULL
        GROUP BY tier1_present
    """).fetchdf()
    for _, r in tier_df.iterrows():
        label = "Tier-1 Manager Present" if r["tier1_present"] else "No Tier-1 Manager"
        win_rate = round(r["wins"] / r["total"] * 100, 1) if r["total"] > 0 else 0
        print(f"  {label:25s}  n={int(r['total']):5d}  "
              f"WinRate={win_rate:5.1f}%  AvgAlpha={r['avg_alpha_90d']:+.1f}%")

    # Overall system alpha vs SPY
    print("\n>> OVERALL SYSTEM PERFORMANCE vs SPY:")
    overall_df = conn.execute("""
        SELECT
            ROUND(AVG(alpha_30d), 2) AS avg_alpha_30d,
            ROUND(AVG(alpha_60d), 2) AS avg_alpha_60d,
            ROUND(AVG(alpha_90d), 2) AS avg_alpha_90d,
            ROUND(AVG(alpha_180d), 2) AS avg_alpha_180d,
            SUM(CASE WHEN alpha_90d > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS win_rate_90d
        FROM backtest_results
        WHERE alpha_90d IS NOT NULL
    """).fetchone()
    if overall_df:
        print(f"  30d Alpha: {overall_df[0]:+.2f}%  |  60d Alpha: {overall_df[1]:+.2f}%  |"
              f"  90d Alpha: {overall_df[2]:+.2f}%  |  180d Alpha: {overall_df[3]:+.2f}%")
        print(f"  Overall Win Rate (90d vs SPY): {overall_df[4]:.1f}%")

    print(f"\n{'='*70}\n")


# ---------------------------------------------------------------------------
# Golden-Cross Filter: high conviction + tier-1 + insider cluster
# ---------------------------------------------------------------------------

def backtest_high_conviction_filter(conn: duckdb.DuckDBPyConnection) -> None:
    """Analyze the win rate and alpha for the golden-cross subset:
    conviction_score > 70 AND tier1_present = True AND insider_confirmed = True.

    If this subset achieves > 65% win rate at 90d, it becomes the live trading gate.
    """
    total = conn.execute("SELECT COUNT(*) FROM backtest_results").fetchone()[0]
    if total == 0:
        logger.warning("No backtest results. Run --run first.")
        return

    print(f"\n{'='*70}")
    print("  GOLDEN-CROSS FILTER ANALYSIS")
    print("  (conviction > 70 + Tier-1 Present + Insider Cluster Confirmed)")
    print(f"{'='*70}")

    # Full population baseline
    baseline = conn.execute("""
        SELECT
            COUNT(*) AS n,
            SUM(CASE WHEN alpha_90d > 0 THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(alpha_90d), 2) AS avg_alpha,
            ROUND(AVG(return_90d), 2) AS avg_return
        FROM backtest_results
        WHERE alpha_90d IS NOT NULL
    """).fetchone()
    base_wr = round(baseline[1] / baseline[0] * 100, 1) if baseline[0] else 0
    print(f"\nBaseline (all signals):")
    print(f"  n={baseline[0]:,}  WinRate={base_wr:.1f}%  "
          f"AvgAlpha={baseline[2]:+.2f}%  AvgReturn={baseline[3]:+.2f}%")

    # Golden-cross subset
    gc = conn.execute("""
        SELECT
            COUNT(*) AS n,
            SUM(CASE WHEN alpha_90d > 0 THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(alpha_90d), 2) AS avg_alpha,
            ROUND(AVG(return_90d), 2) AS avg_return,
            ROUND(AVG(return_30d), 2) AS avg_return_30d,
            ROUND(AVG(return_60d), 2) AS avg_return_60d,
            ROUND(AVG(return_180d), 2) AS avg_return_180d
        FROM backtest_results
        WHERE alpha_90d IS NOT NULL
          AND conviction_score > 70
          AND tier1_present = TRUE
          AND insider_confirmed = TRUE
    """).fetchone()

    if not gc or gc[0] == 0:
        print("\nGolden-cross: no data (check conviction_score, tier1_present, insider_confirmed)")
        return

    gc_wr = round(gc[1] / gc[0] * 100, 1)
    lift = gc_wr - base_wr
    print(f"\nGolden-Cross (conviction>70 + Tier-1 + Insider):")
    print(f"  n={gc[0]:,}  WinRate={gc_wr:.1f}%  AvgAlpha={gc[2]:+.2f}%")
    print(f"  AvgReturn  30d={gc[4]:+.2f}%  60d={gc[5]:+.2f}%  "
          f"90d={gc[3]:+.2f}%  180d={gc[6]:+.2f}%")
    print(f"  Win rate lift vs baseline: {lift:+.1f}pp")

    verdict = (
        "ADOPT as live trading gate — 65%+ win rate achieved!"
        if gc_wr >= 65
        else f"Below 65% threshold ({gc_wr:.1f}%) — do not promote to live gate yet"
    )
    print(f"\n  Verdict: {verdict}")

    # Breakdown by conviction bucket within golden-cross
    print("\n  Win Rate Within Golden-Cross by Conviction Bucket:")
    breakdown = conn.execute("""
        SELECT
            CASE
                WHEN conviction_score < 80 THEN '70-80'
                WHEN conviction_score < 90 THEN '80-90'
                ELSE '90-100'
            END AS bucket,
            COUNT(*) AS n,
            SUM(CASE WHEN alpha_90d > 0 THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(alpha_90d), 2) AS avg_alpha
        FROM backtest_results
        WHERE alpha_90d IS NOT NULL
          AND conviction_score > 70
          AND tier1_present = TRUE
          AND insider_confirmed = TRUE
        GROUP BY bucket
        ORDER BY MIN(conviction_score)
    """).fetchdf()
    for _, r in breakdown.iterrows():
        wr = round(r["wins"] / r["n"] * 100, 1) if r["n"] > 0 else 0
        print(f"    {str(r['bucket']):10s}  n={int(r['n']):4d}  WinRate={wr:.1f}%  "
              f"AvgAlpha={r['avg_alpha']:+.2f}%")

    print(f"\n{'='*70}\n")


# ---------------------------------------------------------------------------
# Conviction weight calibration from backtest results
# ---------------------------------------------------------------------------

def calibrate_weights_from_backtest(conn: duckdb.DuckDBPyConnection) -> None:
    """Analyze which conviction dimensions best predict alpha_90d > 0.

    Runs a simple per-dimension correlation and logistic regression to suggest
    adjusted weights. Does NOT auto-apply — shows for review only.
    """
    total = conn.execute("SELECT COUNT(*) FROM backtest_results").fetchone()[0]
    if total == 0:
        logger.warning("No backtest results. Run --run first.")
        return

    try:
        import numpy as np
        from scipy.stats import pointbiserialr
    except ImportError:
        logger.warning("scipy not available — install for weight calibration: pip install scipy")
        return

    # Pull available numeric dimensions from backtest_results
    # (conviction_breakdown JSON not stored, so use proxy columns)
    df = conn.execute("""
        SELECT
            CASE WHEN alpha_90d > 0 THEN 1 ELSE 0 END AS target,
            conviction_score,
            cascade_stage,
            CASE WHEN insider_confirmed THEN 1 ELSE 0 END AS insider_flag,
            CASE WHEN tier1_present    THEN 1 ELSE 0 END AS tier1_flag,
            alpha_90d
        FROM backtest_results
        WHERE alpha_90d IS NOT NULL AND conviction_score IS NOT NULL
    """).fetchdf()

    if df.empty:
        print("No data for calibration.")
        return

    CURRENT_WEIGHTS = {
        "institutional_depth": 0.25,
        "cascade_quality":     0.20,
        "manager_quality":     0.15,
        "insider_alignment":   0.20,
        "sector_tailwind":     0.10,
        "lag_opportunity":     0.10,
    }

    print(f"\n{'='*70}")
    print("  CONVICTION WEIGHT CALIBRATION (point-biserial correlations)")
    print(f"{'='*70}")
    print("\nCurrent weights:")
    for k, v in CURRENT_WEIGHTS.items():
        print(f"  {k:25s}  {v:.2f}")

    print("\nProxy dimension correlations with alpha_90d > 0:")
    proxies = {
        "conviction_score":  ("overall",              df["conviction_score"]),
        "cascade_stage":     ("cascade_quality",      df["cascade_stage"]),
        "insider_flag":      ("insider_alignment",    df["insider_flag"]),
        "tier1_flag":        ("manager_quality",      df["tier1_flag"]),
    }
    correlations: dict[str, float] = {}
    for col, (dim, series) in proxies.items():
        try:
            r, p = pointbiserialr(series, df["target"])
            correlations[dim] = abs(r)
            sig = "**" if p < 0.01 else ("*" if p < 0.05 else "  ")
            print(f"  {col:20s}  r={r:+.3f}  p={p:.3f} {sig}  → dimension: {dim}")
        except Exception:
            pass

    # Suggest proportionally adjusted weights (preserve sum = 1.0)
    if correlations:
        total_corr = sum(correlations.values())
        if total_corr > 0:
            # Dimensions not measured keep their current weight fraction
            measured_dims = set(correlations.keys())
            unmeasured_current = sum(v for k, v in CURRENT_WEIGHTS.items()
                                     if k not in measured_dims)
            measured_current_total = 1.0 - unmeasured_current
            if measured_current_total > 0:
                print("\nSuggested weight adjustments (proportional to correlation strength):")
                print("  NOTE: Review before applying — correlations from proxy features only")
                for dim, corr in sorted(correlations.items(), key=lambda x: x[1], reverse=True):
                    current_w = CURRENT_WEIGHTS.get(dim, 0)
                    suggested_w = round((corr / total_corr) * measured_current_total, 3)
                    delta = suggested_w - current_w
                    print(f"  {dim:25s}  current={current_w:.2f}  suggested={suggested_w:.2f}  "
                          f"delta={delta:+.3f}")

    print(f"\n{'='*70}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Institutional Intelligence Backtest Engine")
    parser.add_argument("--run",       action="store_true", help="Run the backtest")
    parser.add_argument("--summary",   action="store_true", help="Print backtest summary")
    parser.add_argument("--filter",    action="store_true", help="Golden-cross filter analysis")
    parser.add_argument("--calibrate", action="store_true", help="Conviction weight calibration")
    parser.add_argument("--train-start", default="2020-Q2")
    parser.add_argument("--train-end",   default="2023-Q4")
    args = parser.parse_args()

    conn = duckdb.connect(str(WAREHOUSE_PATH))
    try:
        if args.run:
            n = run_backtest(conn, train_start=args.train_start, train_end=args.train_end)
            print(f"Backtest complete: {n:,} results written to backtest_results table")
        if args.summary:
            print_backtest_summary(conn)
        if args.filter:
            backtest_high_conviction_filter(conn)
        if args.calibrate:
            calibrate_weights_from_backtest(conn)
        if not any([args.run, args.summary, args.filter, args.calibrate]):
            parser.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
