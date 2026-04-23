"""Phase 4a: Distribution Warning Detector.

Detects when smart money is beginning to exit positions before price declines.
Early detection of distribution patterns before the crowd notices.

Distribution Warning Severity:
    MILD     — One indicator flipping: count dropping slightly while price still elevated
    MODERATE — Count down + shares down + value down in same quarter
    SEVERE   — Rapid multi-quarter distribution: 2+ consecutive decline quarters with heavy selling

This is the RISK section of the intelligence system. A SEVERE distribution
warning on a ticker the user holds should trigger exit or protective hedging.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import duckdb
from loguru import logger


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


def detect_distribution_warnings(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> list[dict]:
    """Detect distribution patterns for all tickers in the given quarter.

    Analyzes the last 2 quarters of QoQ change data for distribution signals.

    Returns list of dicts: {ticker, distribution_warning, distribution_severity}
    """
    logger.info("Detecting distribution warnings for quarter={}", quarter)

    try:
        # Get last 2 quarters of data
        df = conn.execute("""
            SELECT
                ticker,
                current_quarter AS report_quarter,
                inst_count_change_pct AS count_change_pct,
                shares_change_pct, value_change_pct,
                inst_count_current, inst_count_prior,
                avg_price_change_pct
            FROM agg_qoq_changes
            WHERE current_quarter IN (
                SELECT DISTINCT current_quarter
                FROM agg_qoq_changes
                WHERE current_quarter <= ?
                ORDER BY current_quarter DESC
                LIMIT 2
            )
            ORDER BY ticker, current_quarter DESC
        """, [quarter]).fetchdf()
    except Exception as e:
        logger.warning("Distribution query failed for {}: {}", quarter, e)
        return []

    if df.empty:
        return []

    results = []

    for ticker, group in df.groupby("ticker"):
        group = group.sort_values("report_quarter", ascending=False)
        rows = group.to_dict("records")
        latest = rows[0]
        prior = rows[1] if len(rows) > 1 else None

        # Only analyze tickers that are in the current quarter
        if latest.get("report_quarter") != quarter:
            continue

        count_pct = _safe_float(latest.get("count_change_pct"))
        shares_pct = _safe_float(latest.get("shares_change_pct"))
        value_pct = _safe_float(latest.get("value_change_pct"))
        price_pct = _safe_float(latest.get("avg_price_change_pct"))

        warning = False
        severity = None

        # SEVERE: 2 consecutive quarters of heavy distribution
        if prior:
            prior_count = _safe_float(prior.get("count_change_pct"))
            if count_pct <= -8.0 and prior_count <= -5.0:
                warning = True
                severity = "SEVERE"
            # MODERATE: all three metrics down significantly
            elif count_pct <= -5.0 and shares_pct <= -5.0 and value_pct <= -10.0:
                warning = True
                severity = "MODERATE"
            # MILD: count dropping while price elevated (topping out)
            elif count_pct <= -3.0 and price_pct >= 5.0:
                warning = True
                severity = "MILD"
        else:
            if count_pct <= -8.0 and shares_pct <= -5.0:
                warning = True
                severity = "MODERATE"
            elif count_pct <= -3.0 and price_pct >= 5.0:
                warning = True
                severity = "MILD"

        results.append({
            "ticker": str(ticker),
            "distribution_warning": warning,
            "distribution_severity": severity if warning else None,
        })

    logger.info("Distribution detection complete: {}/{} with warnings for quarter={}",
                sum(1 for r in results if r["distribution_warning"]), len(results), quarter)
    return results


def update_distribution_in_intelligence(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> int:
    """Write distribution warnings into intelligence_scores table."""
    results = detect_distribution_warnings(conn, quarter)
    if not results:
        return 0

    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    updated = 0

    for r in results:
        try:
            conn.execute("""
                UPDATE intelligence_scores
                SET distribution_warning = ?,
                    distribution_severity = ?,
                    computed_at = ?
                WHERE ticker = ? AND report_quarter = ?
            """, [
                r["distribution_warning"],
                r["distribution_severity"],
                now_iso,
                r["ticker"],
                quarter,
            ])
            updated += 1
        except Exception as e:
            logger.debug("Distribution update failed for {}: {}", r["ticker"], e)

    logger.info("Distribution warnings updated: {}/{} for quarter={}", updated, len(results), quarter)
    return updated
