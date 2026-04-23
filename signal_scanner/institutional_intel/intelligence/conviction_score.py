"""Phase 2e: Master 6-Dimensional Conviction Score.

Combines all intelligence signals into a single 0-100 conviction score.
This is the primary ranking signal used by the Command Center dashboard.

Dimensions (weights):
    1. Institutional Depth       0.30  (phase, streak, count growth)
    2. Cascade Quality           0.25  (new initiations, copycat stage)
    3. Manager Quality           0.15  (tier-1 count, concentration)
    4. Insider Alignment         0.20  (cluster, CEO/CFO, net buy)
    5. Sector Tailwind           0.05  (sector rotation momentum)
    6. Lag Opportunity           0.05  (time between filing and price impact)

Usage:
    update_conviction_in_intelligence(conn, quarter)
"""

from __future__ import annotations

import json
from datetime import datetime

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


# ---------------------------------------------------------------------------
# Dimension weights
# ---------------------------------------------------------------------------
WEIGHTS = {
    "institutional_depth": 0.30,   # Most differentiating dimension
    "cascade_quality": 0.25,       # Captures real momentum
    "manager_quality": 0.15,
    "insider_alignment": 0.20,
    "sector_tailwind": 0.05,       # Centered at 50 for most, low signal
    "lag_opportunity": 0.05,       # Same value to most tickers, low signal
}


def _compute_institutional_depth(row: dict) -> float:
    """0-100 score from phase and accumulation strength."""
    phase = str(row.get("accum_phase") or "DORMANT")
    strength = _safe_float(row.get("accum_strength_score"))

    phase_base = {
        "ACTIVE_ACCUM": 80.0,
        "LATE_ACCUM": 65.0,
        "EARLY_ACCUM": 30.0,   # Lowered — barely above noise, don't reward generously
        "EXPANSION": 30.0,
        "DISTRIBUTION": 10.0,
        "DORMANT": 5.0,
        "DECLINE": 0.0,
    }.get(phase, 5.0)

    # Blend phase base with actual strength score
    return min(100.0, phase_base * 0.5 + strength * 0.5)


def _compute_cascade_quality(row: dict) -> float:
    """0-100 score from cascade stage and new initiations."""
    stage = _safe_int(row.get("cascade_stage"))
    new_inits = _safe_int(row.get("new_initiations_count"))
    copycat = _safe_float(row.get("copycat_score"))

    stage_base = {0: 0.0, 1: 30.0, 2: 60.0, 3: 90.0}.get(stage, 0.0)

    # Blend stage base with copycat score
    return min(100.0, stage_base * 0.5 + copycat * 0.5)


def _compute_lag_opportunity(row: dict) -> float:
    """0-100 score: higher when more lag remains (more time to enter)."""
    lag_conf = str(row.get("lag_confidence") or "LOW")
    expected_q = _safe_int(row.get("expected_impact_quarters"), default=3)
    phase = str(row.get("accum_phase") or "DORMANT")

    if phase in ("DISTRIBUTION", "DECLINE", "DORMANT"):
        return 0.0

    # More lag remaining + higher confidence = more opportunity
    conf_mult = {"HIGH": 1.0, "MEDIUM": 0.65, "LOW": 0.3}.get(lag_conf, 0.3)

    # Ideal lag is 2-4 quarters out
    if expected_q <= 1:
        lag_score = 40.0  # impact too close, entry window closing
    elif expected_q <= 3:
        lag_score = 90.0  # sweet spot
    elif expected_q <= 5:
        lag_score = 65.0  # early stage but still opportunity
    else:
        lag_score = 30.0  # too early

    return lag_score * conf_mult


def compute_conviction_scores(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> list[dict]:
    """Compute 6-dimensional conviction scores for all tickers in the quarter.

    Reads from intelligence_scores (which should have all prior dimensions populated).
    Also reads sector rotation data for the sector tailwind dimension.

    Returns list of dicts with conviction_score and conviction_breakdown fields.
    """
    logger.info("Computing conviction scores for quarter={}", quarter)

    try:
        scores_df = conn.execute("""
            SELECT
                i.ticker, i.accum_phase, i.accum_phase_quarters,
                i.accum_strength_score, i.expected_impact_quarters,
                i.lag_confidence, i.cascade_stage, i.new_initiations_count,
                i.copycat_score, i.divergence_active, i.divergence_magnitude,
                i.tier1_manager_count, i.tier2_manager_count,
                i.manager_quality_score, i.max_manager_concentration,
                i.concentrated_managers_count,
                i.insider_cluster_detected, i.insider_net_buy_count,
                i.ceo_cfo_buying, i.insider_score,
                d.sector
            FROM intelligence_scores i
            LEFT JOIN dim_issuer d ON i.ticker = d.ticker
            WHERE i.report_quarter = ?
        """, [quarter]).fetchdf()
    except Exception as e:
        logger.warning("Conviction score query failed for {}: {}", quarter, e)
        return []

    # Sector tailwind: pre-load sector rotation flow_pct
    sector_tailwind_map = {}
    try:
        sector_df = conn.execute("""
            SELECT sector, flow_pct
            FROM agg_sector_rotation
            WHERE report_quarter = ?
        """, [quarter]).fetchdf()
        for _, row in sector_df.iterrows():
            sector_tailwind_map[str(row["sector"] or "")] = _safe_float(row.get("flow_pct"))
    except Exception:
        pass

    results = []

    for _, row in scores_df.iterrows():
        row_dict = row.to_dict()

        # 1. Institutional depth
        d1 = _compute_institutional_depth(row_dict)

        # 2. Cascade quality
        d2 = _compute_cascade_quality(row_dict)

        # 3. Manager quality
        d3 = _safe_float(row_dict.get("manager_quality_score"))

        # 4. Insider alignment
        d4 = _safe_float(row_dict.get("insider_score"))

        # Insider divergence bonus: bullish divergence amplifies insider signal
        if _safe_float(row_dict.get("divergence_magnitude")) > 30.0:
            d4 = min(100.0, d4 + 15.0)

        # 5. Sector tailwind
        sector = str(row_dict.get("sector") or "")
        sector_flow = sector_tailwind_map.get(sector, 0.0)
        # Normalize: +20% sector flow = 100, -20% = 0
        d5 = min(100.0, max(0.0, 50.0 + sector_flow * 2.5))

        # 6. Lag opportunity
        d6 = _compute_lag_opportunity(row_dict)

        # Weighted composite
        score = (
            d1 * WEIGHTS["institutional_depth"] +
            d2 * WEIGHTS["cascade_quality"] +
            d3 * WEIGHTS["manager_quality"] +
            d4 * WEIGHTS["insider_alignment"] +
            d5 * WEIGHTS["sector_tailwind"] +
            d6 * WEIGHTS["lag_opportunity"]
        )
        score = round(min(100.0, max(0.0, score)), 2)

        breakdown = {
            "institutional_depth": round(d1, 1),
            "cascade_quality": round(d2, 1),
            "manager_quality": round(d3, 1),
            "insider_alignment": round(d4, 1),
            "sector_tailwind": round(d5, 1),
            "lag_opportunity": round(d6, 1),
            "weights": WEIGHTS,
        }

        results.append({
            "ticker": str(row_dict["ticker"]),
            "conviction_score": score,
            "conviction_breakdown": json.dumps(breakdown),
        })

    logger.info("Conviction scores computed: {} tickers for quarter={}", len(results), quarter)
    return results


def update_conviction_in_intelligence(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> int:
    """Write conviction scores into intelligence_scores table."""
    results = compute_conviction_scores(conn, quarter)
    if not results:
        return 0

    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    updated = 0

    for r in results:
        try:
            conn.execute("""
                UPDATE intelligence_scores
                SET conviction_score = ?,
                    conviction_breakdown = ?,
                    computed_at = ?
                WHERE ticker = ? AND report_quarter = ?
            """, [
                r["conviction_score"],
                r["conviction_breakdown"],
                now_iso,
                r["ticker"],
                quarter,
            ])
            updated += 1
        except Exception as e:
            logger.debug("Conviction update failed for {}: {}", r["ticker"], e)

    logger.info("Conviction scores updated: {}/{} for quarter={}", updated, len(results), quarter)
    return updated
