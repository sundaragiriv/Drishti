"""Phase 3: Sector Rotation Clock.

Computes net institutional capital flows per sector per quarter.
Maps sectors to the standard 4-phase economic cycle:
    early_recovery  — capital flowing into Financials, Consumer Discretionary
    mid_expansion   — Technology, Industrials, Materials
    late_cycle      — Energy, Commodities, Real Estate
    defensive       — Utilities, Consumer Staples, Healthcare

Outputs to agg_sector_rotation table and intelligence_scores.

Key functions:
    compute_sector_rotation(conn, quarter) — main computation
    get_sector_rotation_summary(conn, quarter) — for dashboard display
    infer_cycle_phase(sector_flows) — map flows to cycle phase
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple

import duckdb
from loguru import logger


# ---------------------------------------------------------------------------
# Sector → Cycle Phase mapping
# ---------------------------------------------------------------------------
SECTOR_CYCLE_MAP = {
    # Early recovery
    "Financial Services": "early_recovery",
    "Financials": "early_recovery",
    "Consumer Cyclical": "early_recovery",
    "Consumer Discretionary": "early_recovery",
    # Mid expansion
    "Technology": "mid_expansion",
    "Information Technology": "mid_expansion",
    "Communication Services": "mid_expansion",
    "Industrials": "mid_expansion",
    "Materials": "mid_expansion",
    "Basic Materials": "mid_expansion",
    # Late cycle
    "Energy": "late_cycle",
    "Real Estate": "late_cycle",
    "Commodities": "late_cycle",
    # Defensive
    "Utilities": "defensive",
    "Consumer Defensive": "defensive",
    "Consumer Staples": "defensive",
    "Healthcare": "defensive",
    "Health Care": "defensive",
}

CYCLE_ORDER = ["early_recovery", "mid_expansion", "late_cycle", "defensive"]


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


def compute_sector_rotation(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> int:
    """Compute sector rotation flows and write to agg_sector_rotation.

    Joins agg_qoq_changes with dim_issuer to get sector-level flows.
    Net flow = sum of value_change_k across all tickers in sector.

    Returns number of sectors written.
    """
    logger.info("Computing sector rotation for quarter={}", quarter)
    now_iso = datetime.utcnow().isoformat(timespec="seconds")

    try:
        df = conn.execute("""
            SELECT
                COALESCE(d.sector, 'Unknown') AS sector,
                COUNT(DISTINCT q.ticker) AS ticker_count,
                SUM(q.value_current_usd_k - COALESCE(q.value_prior_usd_k, q.value_current_usd_k)) AS net_flow_k,
                SUM(q.value_current_usd_k) AS total_value_k,
                SUM(q.inst_count_current - COALESCE(q.inst_count_prior, q.inst_count_current)) AS net_inst_count_change
            FROM agg_qoq_changes q
            LEFT JOIN dim_issuer d ON q.ticker = d.ticker
            WHERE q.current_quarter = ?
            GROUP BY COALESCE(d.sector, 'Unknown')
            ORDER BY net_flow_k DESC
        """, [quarter]).fetchdf()
    except Exception as e:
        logger.warning("Sector rotation query failed for {}: {}", quarter, e)
        return 0

    if df.empty:
        logger.info("No sector data for quarter={}", quarter)
        return 0

    # Compute flow_pct: net_flow_k as % of total_value_k per sector
    rows = []
    for _, row in df.iterrows():
        sector = str(row["sector"] or "Unknown")
        total_val = _safe_float(row.get("total_value_k"))
        net_flow = _safe_float(row.get("net_flow_k"))
        flow_pct = (net_flow / total_val * 100.0) if total_val > 0 else 0.0
        ticker_count = _safe_int(row.get("ticker_count"))
        net_inst = _safe_int(row.get("net_inst_count_change"))

        # Compute inflow streak (how many consecutive quarters of positive flow)
        try:
            prior_flows = conn.execute("""
                SELECT flow_pct FROM agg_sector_rotation
                WHERE sector = ?
                  AND report_quarter < ?
                ORDER BY report_quarter DESC
                LIMIT 4
            """, [sector, quarter]).fetchall()
            streak = 0
            for (pf,) in prior_flows:
                if _safe_float(pf) > 0:
                    streak += 1
                else:
                    break
            if flow_pct > 0:
                streak += 1
        except Exception:
            streak = 1 if flow_pct > 0 else 0

        rows.append((
            sector, quarter, total_val, net_flow, round(flow_pct, 4),
            net_inst, ticker_count, streak, now_iso,
        ))

    if rows:
        conn.executemany("""
            INSERT INTO agg_sector_rotation (
                sector, report_quarter, total_value_k, net_flow_k, flow_pct,
                net_inst_count_change, ticker_count, inflow_streak, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (sector, report_quarter) DO UPDATE SET
                total_value_k = excluded.total_value_k,
                net_flow_k = excluded.net_flow_k,
                flow_pct = excluded.flow_pct,
                net_inst_count_change = excluded.net_inst_count_change,
                ticker_count = excluded.ticker_count,
                inflow_streak = excluded.inflow_streak,
                computed_at = excluded.computed_at
        """, rows)
        logger.info("Sector rotation written: {} sectors for quarter={}", len(rows), quarter)

    return len(rows)


def get_sector_rotation_summary(
    conn: duckdb.DuckDBPyConnection,
    quarter: Optional[str] = None,
) -> Dict:
    """Return sector rotation summary for dashboard display.

    Returns dict with:
        sectors: list of {sector, flow_pct, net_flow_k, ticker_count, inflow_streak, cycle_phase}
        cycle_phase: inferred current cycle phase
        top_inflow_sectors: top 3 by flow_pct
        top_outflow_sectors: bottom 3 by flow_pct
    """
    q_filter = ""
    params = []
    if quarter:
        q_filter = "WHERE report_quarter = ?"
        params = [quarter]
    else:
        q_filter = "WHERE report_quarter = (SELECT MAX(report_quarter) FROM agg_sector_rotation)"

    try:
        df = conn.execute(f"""
            SELECT sector, flow_pct, net_flow_k, ticker_count, inflow_streak
            FROM agg_sector_rotation
            {q_filter}
            ORDER BY flow_pct DESC
        """, params).fetchdf()
    except Exception as e:
        logger.warning("Sector rotation summary failed: {}", e)
        return {"sectors": [], "cycle_phase": "unknown"}

    sectors = []
    flow_by_cycle = {phase: 0.0 for phase in CYCLE_ORDER}

    for _, row in df.iterrows():
        sector = str(row["sector"] or "Unknown")
        flow = _safe_float(row.get("flow_pct"))
        cycle_phase = SECTOR_CYCLE_MAP.get(sector, "defensive")
        flow_by_cycle[cycle_phase] = flow_by_cycle.get(cycle_phase, 0.0) + flow

        sectors.append({
            "sector": sector,
            "flow_pct": round(flow, 2),
            "net_flow_k": _safe_float(row.get("net_flow_k")),
            "ticker_count": _safe_int(row.get("ticker_count")),
            "inflow_streak": _safe_int(row.get("inflow_streak")),
            "cycle_phase": cycle_phase,
        })

    cycle_phase = infer_cycle_phase(flow_by_cycle)
    inflow = [s for s in sectors if s["flow_pct"] > 0][:3]
    outflow = sorted([s for s in sectors if s["flow_pct"] < 0], key=lambda x: x["flow_pct"])[:3]

    return {
        "sectors": sectors,
        "cycle_phase": cycle_phase,
        "flow_by_cycle": flow_by_cycle,
        "top_inflow_sectors": inflow,
        "top_outflow_sectors": outflow,
    }


def infer_cycle_phase(flow_by_cycle: Dict[str, float]) -> str:
    """Infer current economic cycle phase from sector flows.

    Whichever cycle bucket is getting the most institutional inflows
    is assumed to be where smart money thinks the economy is heading.
    """
    if not flow_by_cycle:
        return "unknown"

    best_phase = max(flow_by_cycle, key=lambda k: flow_by_cycle[k])
    if flow_by_cycle[best_phase] <= 0:
        return "defensive"  # all sectors seeing outflows = risk-off
    return best_phase
