"""Phase 2b: Smart Money Divergence Scanner.

Detects divergence between institutional accumulation and price action:
    - BULLISH divergence: Price falling while institutions accumulate (highest quality)
    - BEARISH divergence: Price rising while institutions distribute (exit warning)

Divergence magnitude (0-100) indicates the strength of the signal.
A bullish divergence of 80+ is one of the highest-conviction setups in the system.
"""

from __future__ import annotations

from datetime import datetime

import duckdb
from loguru import logger


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def scan_divergences(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> list[dict]:
    """Detect price-vs-institutional divergences for the given quarter.

    Requires: agg_qoq_changes.avg_price_change_pct (from price enrichment).
    Falls back gracefully if price data is not available.

    Returns list of dicts per ticker with divergence fields.
    """
    logger.info("Scanning divergences for quarter={}", quarter)

    try:
        df = conn.execute("""
            SELECT
                ticker,
                inst_count_change_pct AS count_change_pct,
                shares_change_pct,
                value_change_pct,
                avg_price_change_pct,
                price_returns_pct
            FROM agg_qoq_changes
            WHERE current_quarter = ?
              AND inst_count_change_pct IS NOT NULL
        """, [quarter]).fetchdf()
    except Exception as e:
        logger.warning("Divergence query failed for {}: {}", quarter, e)
        return []

    results = []
    for _, row in df.iterrows():
        ticker = str(row["ticker"])
        count_pct = _safe_float(row.get("count_change_pct"))
        shares_pct = _safe_float(row.get("shares_change_pct"))
        price_pct = _safe_float(row.get("avg_price_change_pct"))
        returns_pct = _safe_float(row.get("price_returns_pct"))

        # Use price change from QoQ data; fall back to returns
        price_change = price_pct if price_pct != 0.0 else returns_pct

        divergence_active = False
        divergence_magnitude = 0.0

        # BULLISH divergence: institutions accumulating while price drops
        if count_pct >= 3.0 and price_change <= -3.0:
            divergence_active = True
            # Magnitude: stronger the accumulation relative to price drop, higher the score
            inst_strength = min(count_pct, 30.0) / 30.0  # normalize 0-1
            price_weakness = min(abs(price_change), 30.0) / 30.0
            # Add shares confirmation
            shares_bonus = min(max(shares_pct, 0), 20.0) / 20.0 * 20.0
            divergence_magnitude = min(100.0, (inst_strength * 40.0 + price_weakness * 40.0 + shares_bonus))

        # BEARISH divergence: institutions distributing while price is still elevated
        elif count_pct <= -3.0 and price_change >= 3.0:
            divergence_active = True
            dist_strength = min(abs(count_pct), 30.0) / 30.0
            price_elevation = min(price_change, 30.0) / 30.0
            divergence_magnitude = -(min(100.0, dist_strength * 40.0 + price_elevation * 40.0))

        results.append({
            "ticker": ticker,
            "divergence_active": divergence_active,
            "divergence_magnitude": round(divergence_magnitude, 2),
        })

    logger.info("Divergence scan complete: {}/{} with active divergence for quarter={}",
                sum(1 for r in results if r["divergence_active"]), len(results), quarter)
    return results


def update_divergence_in_intelligence(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> int:
    """Write divergence scores into intelligence_scores table."""
    results = scan_divergences(conn, quarter)
    if not results:
        return 0

    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    updated = 0

    for r in results:
        try:
            conn.execute("""
                UPDATE intelligence_scores
                SET divergence_active = ?,
                    divergence_magnitude = ?,
                    computed_at = ?
                WHERE ticker = ? AND report_quarter = ?
            """, [
                r["divergence_active"],
                r["divergence_magnitude"],
                now_iso,
                r["ticker"],
                quarter,
            ])
            updated += 1
        except Exception as e:
            logger.debug("Divergence update failed for {}: {}", r["ticker"], e)

    logger.info("Divergence scores updated: {}/{} for quarter={}", updated, len(results), quarter)
    return updated
