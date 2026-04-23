"""Phase 2c: Manager Quality + Tier Classification.

Classifies institutional managers into tiers by AUM:
    Tier 1 — Top 20 by AUM (BlackRock, Vanguard, State Street, Fidelity, etc.)
    Tier 2 — Top 21-100 by AUM
    Tier 3 — All others (smaller funds, regional, hedge funds with <$10B)

Manager Quality Score contributions:
    - Tier-1 manager count (heavily weighted)
    - Portfolio concentration: manager allocates >3% of total portfolio to this stock
    - Combined tier-weighted holder count

Concentration signal: when a top-20 manager allocates >3% of their portfolio
to one stock, it indicates maximum conviction — this is a very rare and bullish signal.
"""

from __future__ import annotations

from datetime import datetime

import duckdb
from loguru import logger


# ---------------------------------------------------------------------------
# Tier-1 manager CIKs (from SEC EDGAR — top institutional managers by AUM)
# This list covers the top-20 US institutional managers as of 2024.
# ---------------------------------------------------------------------------
TIER1_MANAGER_NAMES = {
    "blackrock", "vanguard", "state street", "fidelity", "capital group",
    "t. rowe price", "geode capital", "jpmorgan", "goldman sachs", "morgan stanley",
    "wellington management", "invesco", "northern trust", "bank of america",
    "price t rowe", "dimensional fund", "american century", "columbia threadneedle",
    "franklin templeton", "putnam", "neuberger berman", "pimco",
}

TIER2_MANAGER_NAMES = {
    "citadel", "two sigma", "renaissance", "millennium", "aqr capital",
    "point72", "d.e. shaw", "winton", "tudor investment", "baupost",
    "pershing square", "third point", "greenlight", "appaloosa", "Elliott",
    "bridgewater", "man group", "marshall wace", "grantham mayo",
}


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


def _quarter_to_report_period(quarter: str) -> str:
    """Convert 'YYYY-Qn' to the quarter-end date string 'YYYY-MM-DD'."""
    year, qnum = int(quarter.split("-Q")[0]), int(quarter.split("-Q")[1])
    end_month = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}[qnum]
    return f"{year}-{end_month}"


def _assign_tier_by_name(manager_name: str) -> int:
    """Assign tier 1/2/3 based on manager name heuristics."""
    name_lower = str(manager_name or "").lower()
    for t1 in TIER1_MANAGER_NAMES:
        if t1 in name_lower:
            return 1
    for t2 in TIER2_MANAGER_NAMES:
        if t2 in name_lower:
            return 2
    return 3


def build_manager_tiers(conn: duckdb.DuckDBPyConnection) -> int:
    """Populate dim_manager_tiers from fact_13f_positions manager data.

    Assigns tiers based on total AUM (sum of reported value across all positions).
    """
    logger.info("Building manager tier table...")

    try:
        # Compute total AUM per manager (sum of value_usd_thousands across all filings)
        conn.execute("""
            INSERT INTO dim_manager_tiers (manager_cik, manager_name, total_aum_k, tier)
            SELECT
                manager_cik,
                MAX(manager_name) AS manager_name,
                SUM(value_usd_thousands) AS total_aum_k,
                0 AS tier
            FROM fact_13f_positions
            GROUP BY manager_cik
            ON CONFLICT (manager_cik) DO UPDATE SET
                manager_name = excluded.manager_name,
                total_aum_k = excluded.total_aum_k
        """)
    except Exception as e:
        logger.warning("Manager tier AUM insert failed: {}", e)
        return 0

    # Now assign tiers: top 20 by AUM = Tier 1, 21-100 = Tier 2, rest = Tier 3
    try:
        conn.execute("""
            UPDATE dim_manager_tiers
            SET tier = CASE
                WHEN rn <= 20 THEN 1
                WHEN rn <= 100 THEN 2
                ELSE 3
            END
            FROM (
                SELECT manager_cik,
                       ROW_NUMBER() OVER (ORDER BY total_aum_k DESC) AS rn
                FROM dim_manager_tiers
            ) ranked
            WHERE dim_manager_tiers.manager_cik = ranked.manager_cik
        """)
    except Exception as e:
        # Fallback: assign by name
        logger.warning("Tier rank update failed, falling back to name matching: {}", e)
        try:
            managers = conn.execute("SELECT manager_cik, manager_name FROM dim_manager_tiers").fetchdf()
            for _, row in managers.iterrows():
                tier = _assign_tier_by_name(str(row["manager_name"] or ""))
                conn.execute("UPDATE dim_manager_tiers SET tier = ? WHERE manager_cik = ?",
                             [tier, row["manager_cik"]])
        except Exception as e2:
            logger.error("Name-based tier assignment also failed: {}", e2)
            return 0

    count = conn.execute("SELECT COUNT(*) FROM dim_manager_tiers").fetchone()[0]
    logger.info("Manager tier table built: {} managers", count)
    return count


def compute_manager_quality(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> list[dict]:
    """Compute manager quality scores for all tickers in the given quarter.

    Metrics:
        tier1_manager_count — number of tier-1 managers holding stock
        tier2_manager_count — number of tier-2 managers holding stock
        manager_quality_score — 0-100 weighted score
        max_manager_concentration — highest % of any manager's portfolio in this stock
        concentrated_managers_count — managers with >3% concentration
    """
    logger.info("Computing manager quality for quarter={}", quarter)

    current_period = _quarter_to_report_period(quarter)

    try:
        df = conn.execute("""
            SELECT
                h.ticker,
                COUNT(DISTINCT CASE WHEN t.tier = 1 THEN h.manager_cik END) AS tier1_count,
                COUNT(DISTINCT CASE WHEN t.tier = 2 THEN h.manager_cik END) AS tier2_count,
                COUNT(DISTINCT CASE WHEN t.tier = 3 OR t.tier IS NULL THEN h.manager_cik END) AS tier3_count,
                MAX(CASE
                    WHEN mgr_total.total_aum_k > 0 THEN
                        h.value_usd_thousands / mgr_total.total_aum_k * 100.0
                    ELSE 0
                END) AS max_concentration_pct,
                COUNT(DISTINCT CASE
                    WHEN mgr_total.total_aum_k > 0 AND
                         h.value_usd_thousands / mgr_total.total_aum_k * 100.0 > 3.0
                    THEN h.manager_cik
                END) AS concentrated_count
            FROM fact_13f_positions h
            LEFT JOIN dim_manager_tiers t ON h.manager_cik = t.manager_cik
            LEFT JOIN (
                SELECT manager_cik, SUM(value_usd_thousands) AS total_aum_k
                FROM fact_13f_positions
                WHERE report_period = ?::DATE
                GROUP BY manager_cik
            ) mgr_total ON h.manager_cik = mgr_total.manager_cik
            WHERE h.report_period = ?::DATE
              AND h.shares > 0
              AND h.ticker IS NOT NULL
            GROUP BY h.ticker
        """, [current_period, current_period]).fetchdf()
    except Exception as e:
        logger.warning("Manager quality query failed for {}: {}", quarter, e)
        return []

    results = []
    for _, row in df.iterrows():
        ticker = str(row["ticker"])
        tier1 = _safe_int(row.get("tier1_count"))
        tier2 = _safe_int(row.get("tier2_count"))
        tier3 = _safe_int(row.get("tier3_count"))
        max_conc = _safe_float(row.get("max_concentration_pct"))
        conc_count = _safe_int(row.get("concentrated_count"))

        # Quality score (0-100)
        # Tier-1 presence is the most important signal
        tier1_bonus = min(tier1 * 15.0, 50.0)   # up to 50 pts from tier-1
        tier2_bonus = min(tier2 * 5.0, 20.0)    # up to 20 pts from tier-2
        tier3_bonus = min(tier3 * 0.5, 10.0)    # up to 10 pts from tier-3

        # Concentration bonus: max conviction bet
        conc_bonus = 0.0
        if max_conc >= 5.0:
            conc_bonus = 20.0
        elif max_conc >= 3.0:
            conc_bonus = 12.0
        elif max_conc >= 1.5:
            conc_bonus = 5.0

        quality_score = min(100.0, tier1_bonus + tier2_bonus + tier3_bonus + conc_bonus)

        results.append({
            "ticker": ticker,
            "tier1_manager_count": tier1,
            "tier2_manager_count": tier2,
            "manager_quality_score": round(quality_score, 2),
            "max_manager_concentration": round(max_conc, 4),
            "concentrated_managers_count": conc_count,
        })

    logger.info("Manager quality computed: {} tickers for quarter={}", len(results), quarter)
    return results


def update_manager_quality_in_intelligence(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> int:
    """Write manager quality scores into intelligence_scores table."""
    results = compute_manager_quality(conn, quarter)
    if not results:
        return 0

    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    updated = 0

    for r in results:
        try:
            conn.execute("""
                UPDATE intelligence_scores
                SET tier1_manager_count = ?,
                    tier2_manager_count = ?,
                    manager_quality_score = ?,
                    max_manager_concentration = ?,
                    concentrated_managers_count = ?,
                    computed_at = ?
                WHERE ticker = ? AND report_quarter = ?
            """, [
                r["tier1_manager_count"],
                r["tier2_manager_count"],
                r["manager_quality_score"],
                r["max_manager_concentration"],
                r["concentrated_managers_count"],
                now_iso,
                r["ticker"],
                quarter,
            ])
            updated += 1
        except Exception as e:
            logger.debug("Manager quality update failed for {}: {}", r["ticker"], e)

    logger.info("Manager quality updated: {}/{} for quarter={}", updated, len(results), quarter)
    return updated
