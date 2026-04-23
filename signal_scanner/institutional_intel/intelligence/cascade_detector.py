"""Phase 2a: Copy-Cat Cascade Detector.

Detects when institutions are copying each other's 13F filings.
When institutions read public 13F disclosures and initiate copycat positions,
it creates a cascade of buying that compounds the original accumulation.

Cascade Stages:
    0 — No cascade (single institution or new initiation)
    1 — 2-4 new manager initiations in current quarter (early cascade)
    2 — 5-9 new initiations (mid cascade — conviction building)
    3 — 10+ new initiations (peak cascade — institutional FOMO)

New initiation = a manager who had 0 shares last quarter but has shares this quarter.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import duckdb
from loguru import logger


def _safe_int(val, default: int = 0) -> int:
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _quarter_to_report_period(quarter: str) -> str:
    """Convert 'YYYY-Qn' to the quarter-end date string 'YYYY-MM-DD'."""
    year, qnum = int(quarter.split("-Q")[0]), int(quarter.split("-Q")[1])
    end_month = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}[qnum]
    return f"{year}-{end_month}"


def compute_cascade_scores(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> list[dict]:
    """Compute cascade stage and new initiation count for all tickers in quarter.

    New initiation = manager had 0 shares prior quarter, now has >0 shares.
    Queries fact_13f_positions using report_period dates.

    Returns list of dicts: {ticker, new_initiations_count, cascade_stage, copycat_score}
    """
    logger.info("Computing cascade scores for quarter={}", quarter)

    current_period = _quarter_to_report_period(quarter)

    # Find new initiations using fact_13f_positions (manager-level positions)
    try:
        df = conn.execute("""
            WITH current_q AS (
                SELECT ticker, manager_cik, shares
                FROM fact_13f_positions
                WHERE report_period = ?::DATE
                  AND shares > 0
                  AND ticker IS NOT NULL
            ),
            prior_q AS (
                SELECT ticker, manager_cik, shares
                FROM fact_13f_positions
                WHERE report_period = (
                    SELECT MAX(report_period)
                    FROM fact_13f_positions
                    WHERE report_period < ?::DATE
                )
                  AND shares > 0
                  AND ticker IS NOT NULL
            ),
            new_initiations AS (
                SELECT c.ticker, COUNT(DISTINCT c.manager_cik) AS new_count
                FROM current_q c
                LEFT JOIN prior_q p ON c.ticker = p.ticker AND c.manager_cik = p.manager_cik
                WHERE p.manager_cik IS NULL
                GROUP BY c.ticker
            )
            SELECT ticker, new_count
            FROM new_initiations
            ORDER BY new_count DESC
        """, [current_period, current_period]).fetchdf()
    except Exception as e:
        logger.warning("Cascade query failed for {}: {}", quarter, e)
        return []

    results = []
    for _, row in df.iterrows():
        ticker = str(row["ticker"])
        new_count = _safe_int(row.get("new_count"))

        if new_count >= 10:
            stage = 3
            copycat_score = 100.0
        elif new_count >= 5:
            stage = 2
            copycat_score = min(100.0, 60.0 + (new_count - 5) * 8.0)
        elif new_count >= 2:
            stage = 1
            copycat_score = min(60.0, 20.0 + (new_count - 2) * 13.3)
        else:
            stage = 0
            copycat_score = 0.0

        results.append({
            "ticker": ticker,
            "new_initiations_count": new_count,
            "cascade_stage": stage,
            "copycat_score": round(copycat_score, 2),
        })

    logger.info("Cascade scores computed: {} tickers for quarter={}", len(results), quarter)
    return results


def update_cascade_in_intelligence(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> int:
    """Compute cascade scores and write them to intelligence_scores table."""
    results = compute_cascade_scores(conn, quarter)
    if not results:
        return 0

    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    updated = 0

    for r in results:
        try:
            conn.execute("""
                UPDATE intelligence_scores
                SET cascade_stage = ?,
                    new_initiations_count = ?,
                    copycat_score = ?,
                    computed_at = ?
                WHERE ticker = ? AND report_quarter = ?
            """, [
                r["cascade_stage"],
                r["new_initiations_count"],
                r["copycat_score"],
                now_iso,
                r["ticker"],
                quarter,
            ])
            updated += 1
        except Exception as e:
            logger.debug("Cascade update failed for {}: {}", r["ticker"], e)

    logger.info("Cascade scores updated: {}/{} tickers for quarter={}", updated, len(results), quarter)
    return updated
