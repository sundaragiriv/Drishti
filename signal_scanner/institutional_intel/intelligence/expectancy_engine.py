"""Expectancy Calibration Engine — Connect scores to actual outcomes.

Answers: "If you act on a signal at this score level, what's your expected return?"

Uses backtest_results to compute calibrated win rates and expected value (EV)
for each (signal_type, score_bucket) combination. This creates the feedback loop
between our intelligence scores and real market outcomes.

EV = win_rate * avg_win - (1 - win_rate) * avg_loss

Tables:
    expectancy_calibration — per signal_type × score_bucket statistics
    intelligence_scores.expected_value — per-ticker calibrated EV from lookup

Usage:
    python -m signal_scanner.institutional_intel.intelligence.expectancy_engine --calibrate
    python -m signal_scanner.institutional_intel.intelligence.expectancy_engine --apply --quarter 2025-Q3
    python -m signal_scanner.institutional_intel.intelligence.expectancy_engine --summary
"""

from __future__ import annotations

import argparse
from datetime import datetime

import duckdb
from loguru import logger


SCORE_BUCKETS = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)]
RETURN_HORIZON = "return_90d"  # Primary evaluation horizon
ALPHA_HORIZON = "alpha_90d"

# Signal types mapped to how they're identified in backtest_results
SIGNAL_TYPES = {
    "SWING_BUY":     {"phase_filter": "accum_phase IN ('ACTIVE_ACCUM', 'LATE_ACCUM', 'EARLY_ACCUM')"},
    "LONGTERM_BUY":  {"phase_filter": "accum_phase IN ('ACTIVE_ACCUM', 'LATE_ACCUM')"},
    "SQUEEZE":       {"phase_filter": "1=1"},  # Uses squeeze_score buckets
    "ALL":           {"phase_filter": "1=1"},   # Overall calibration
}


def _ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Create expectancy_calibration table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expectancy_calibration (
            signal_type       TEXT NOT NULL,
            score_bucket      TEXT NOT NULL,
            sample_count      INTEGER DEFAULT 0,
            win_rate          DOUBLE,
            avg_win_pct       DOUBLE,
            avg_loss_pct      DOUBLE,
            expected_value    DOUBLE,
            median_return     DOUBLE,
            alpha_win_rate    DOUBLE,
            avg_alpha         DOUBLE,
            computed_at       TIMESTAMP NOT NULL,
            PRIMARY KEY (signal_type, score_bucket)
        )
    """)

    # Add expected_value column to intelligence_scores
    try:
        conn.execute(
            "ALTER TABLE intelligence_scores ADD COLUMN expected_value DOUBLE DEFAULT 0"
        )
    except Exception:
        pass  # Column already exists


def calibrate_expectancy(conn: duckdb.DuckDBPyConnection) -> int:
    """Compute expectancy statistics from backtest_results.

    Groups by conviction_score buckets and signal types to build
    a lookup table of calibrated win rates and expected values.

    Returns number of calibration rows created.
    """
    logger.info("Calibrating expectancy from backtest_results...")
    _ensure_tables(conn)

    total_bt = conn.execute("SELECT COUNT(*) FROM backtest_results").fetchone()[0]
    if total_bt == 0:
        logger.warning("No backtest results available for calibration")
        return 0

    conn.execute("DELETE FROM expectancy_calibration")
    now = datetime.utcnow().isoformat(timespec="seconds")
    rows_created = 0

    for sig_type, config in SIGNAL_TYPES.items():
        phase_filter = config["phase_filter"]

        for lo, hi in SCORE_BUCKETS:
            bucket_label = f"{lo}-{hi}"

            result = conn.execute(f"""
                SELECT
                    COUNT(*) AS n,
                    -- Win rate (return > 0)
                    COUNT(CASE WHEN {RETURN_HORIZON} > 0 THEN 1 END) * 100.0
                        / NULLIF(COUNT({RETURN_HORIZON}), 0) AS win_rate,
                    -- Average win (only positive returns)
                    AVG(CASE WHEN {RETURN_HORIZON} > 0 THEN {RETURN_HORIZON} END) AS avg_win,
                    -- Average loss (only negative returns, stored as positive magnitude)
                    AVG(CASE WHEN {RETURN_HORIZON} <= 0 THEN ABS({RETURN_HORIZON}) END) AS avg_loss,
                    -- Median return
                    MEDIAN({RETURN_HORIZON}) AS med_ret,
                    -- Alpha metrics
                    COUNT(CASE WHEN {ALPHA_HORIZON} > 0 THEN 1 END) * 100.0
                        / NULLIF(COUNT({ALPHA_HORIZON}), 0) AS alpha_wr,
                    AVG({ALPHA_HORIZON}) AS avg_alpha
                FROM backtest_results
                WHERE conviction_score >= {lo} AND conviction_score < {hi}
                  AND {phase_filter}
                  AND {RETURN_HORIZON} IS NOT NULL
            """).fetchone()

            n = result[0] or 0
            if n < 5:
                continue

            win_rate = result[1] or 50.0
            avg_win = result[2] or 0.0
            avg_loss = result[3] or 0.0
            med_ret = result[4] or 0.0
            alpha_wr = result[5]
            avg_alpha = result[6]

            # EV = p(win) * avg_win - p(loss) * avg_loss
            wr_frac = (win_rate or 50.0) / 100.0
            ev = wr_frac * avg_win - (1.0 - wr_frac) * avg_loss

            conn.execute("""
                INSERT INTO expectancy_calibration
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (signal_type, score_bucket) DO UPDATE SET
                    sample_count = excluded.sample_count,
                    win_rate = excluded.win_rate,
                    avg_win_pct = excluded.avg_win_pct,
                    avg_loss_pct = excluded.avg_loss_pct,
                    expected_value = excluded.expected_value,
                    median_return = excluded.median_return,
                    alpha_win_rate = excluded.alpha_win_rate,
                    avg_alpha = excluded.avg_alpha,
                    computed_at = excluded.computed_at
            """, [
                sig_type, bucket_label, n, win_rate, avg_win, avg_loss,
                round(ev, 2), med_ret, alpha_wr, avg_alpha, now,
            ])
            rows_created += 1

    logger.info("Expectancy calibration complete: {} rows", rows_created)
    return rows_created


def apply_expectancy_to_quarter(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> int:
    """Look up each ticker's conviction bucket → calibrated EV and write to
    intelligence_scores.expected_value.

    Uses the 'ALL' signal type for general lookup, then overrides with
    signal-specific EV when a ticker has a matching signal.

    Returns number of tickers updated.
    """
    logger.info("Applying expectancy values for quarter={}", quarter)
    _ensure_tables(conn)

    # Load calibration lookup
    cal_df = conn.execute("""
        SELECT signal_type, score_bucket, expected_value, win_rate
        FROM expectancy_calibration
    """).fetchdf()

    if cal_df.empty:
        logger.warning("No calibration data available. Run --calibrate first.")
        return 0

    # Build lookup: (signal_type, bucket) → ev
    cal_map = {}
    for _, row in cal_df.iterrows():
        key = (str(row["signal_type"]), str(row["score_bucket"]))
        cal_map[key] = float(row["expected_value"])

    # Load tickers for this quarter
    tickers_df = conn.execute("""
        SELECT ticker, conviction_score, accum_phase, swing_signal
        FROM intelligence_scores
        WHERE report_quarter = ?
    """, [quarter]).fetchdf()

    updated = 0
    now = datetime.utcnow().isoformat(timespec="seconds")

    for _, row in tickers_df.iterrows():
        conv = float(row.get("conviction_score") or 0)
        phase = str(row.get("accum_phase") or "")
        swing = str(row.get("swing_signal") or "")

        # Determine score bucket
        bucket = "0-20"
        for lo, hi in SCORE_BUCKETS:
            if lo <= conv < hi:
                bucket = f"{lo}-{hi}"
                break

        # Look up EV: prefer signal-specific, fall back to ALL
        ev = None
        if swing == "BUY" and phase in ("ACTIVE_ACCUM", "LATE_ACCUM", "EARLY_ACCUM"):
            ev = cal_map.get(("SWING_BUY", bucket))
        if ev is None and phase in ("ACTIVE_ACCUM", "LATE_ACCUM"):
            ev = cal_map.get(("LONGTERM_BUY", bucket))
        if ev is None:
            ev = cal_map.get(("ALL", bucket), 0.0)

        try:
            conn.execute("""
                UPDATE intelligence_scores
                SET expected_value = ?, computed_at = ?
                WHERE ticker = ? AND report_quarter = ?
            """, [round(ev, 2), now, str(row["ticker"]), quarter])
            updated += 1
        except Exception as e:
            logger.debug("EV update failed for {}: {}", row["ticker"], e)

    logger.info("Expectancy applied: {}/{} tickers for quarter={}", updated, len(tickers_df), quarter)
    return updated


def print_calibration_summary(conn: duckdb.DuckDBPyConnection) -> None:
    """Print calibration table for human review."""
    _ensure_tables(conn)

    rows = conn.execute("""
        SELECT signal_type, score_bucket, sample_count, win_rate,
               avg_win_pct, avg_loss_pct, expected_value, median_return,
               alpha_win_rate, avg_alpha
        FROM expectancy_calibration
        ORDER BY signal_type, score_bucket
    """).fetchall()

    if not rows:
        print("No calibration data. Run --calibrate first.")
        return

    print("\n" + "=" * 90)
    print("EXPECTANCY CALIBRATION TABLE")
    print("=" * 90)
    print(f"{'Signal':<14} {'Bucket':<8} {'N':>6} {'WinRate':>7} "
          f"{'AvgWin':>7} {'AvgLoss':>7} {'EV':>7} {'Median':>7} {'AlphaWR':>7} {'Alpha':>7}")
    print("-" * 90)

    current_type = None
    for r in rows:
        if r[0] != current_type:
            if current_type is not None:
                print()
            current_type = r[0]

        print(f"{r[0]:<14} {r[1]:<8} {r[2]:>6} {r[3]:>6.1f}% "
              f"{r[4]:>6.1f}% {r[5]:>6.1f}% {r[6]:>+6.1f}% {r[7]:>6.1f}% "
              f"{(r[8] or 0):>6.1f}% {(r[9] or 0):>+6.1f}%")

    # Key insight: does EV increase with score?
    print("\n" + "-" * 90)
    print("KEY QUESTION: Does higher conviction = higher EV?")
    print("-" * 90)
    all_rows = [r for r in rows if r[0] == "ALL"]
    if len(all_rows) >= 2:
        evs = [(r[1], r[6]) for r in all_rows]
        monotonic = all(evs[i][1] <= evs[i + 1][1] for i in range(len(evs) - 1))
        spread = evs[-1][1] - evs[0][1] if evs else 0
        print(f"  EV spread (lowest bucket to highest): {spread:+.1f}%")
        print(f"  Monotonically increasing: {'YES' if monotonic else 'NO'}")
        if spread > 5:
            print("  -> Scores have meaningful predictive power")
        elif spread > 0:
            print("  -> Scores have weak predictive power")
        else:
            print("  -> Scores may lack predictive power (needs investigation)")

    print("\n" + "=" * 90)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Expectancy Calibration Engine — connect scores to outcomes"
    )
    parser.add_argument(
        "--calibrate", action="store_true",
        help="Build expectancy_calibration table from backtest_results",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply calibrated EV to a quarter's intelligence_scores",
    )
    parser.add_argument(
        "--quarter", type=str, default=None,
        help="Quarter for --apply (e.g. 2025-Q3)",
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Print calibration summary table",
    )
    args = parser.parse_args()

    from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

    conn = duckdb.connect(str(WAREHOUSE_PATH))

    try:
        if args.calibrate:
            calibrate_expectancy(conn)

        if args.apply:
            if not args.quarter:
                parser.error("--apply requires --quarter")
            apply_expectancy_to_quarter(conn, args.quarter)

        if args.summary:
            print_calibration_summary(conn)

        if not any([args.calibrate, args.apply, args.summary]):
            parser.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    from signal_scanner.utils.logger import setup_logger
    setup_logger()
    main()
