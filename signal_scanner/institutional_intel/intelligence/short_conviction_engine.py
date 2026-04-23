"""Phase 2f: SHORT Conviction Score — parallel to conviction_score.py.

Measures institutional DISTRIBUTION pressure as a 0-100 short conviction score.
Mirrors the 6-dimensional LONG conviction engine but inverted for bear signals.

Dimensions (weights):
    1. Distribution Depth       0.35  (phase, severity, decline streak)
    2. Exit Cascade Quality     0.25  (inst count drop rate, shares leaving)
    3. Insider Selling          0.20  (net sells, CEO/CFO selling, dollar value)
    4. Short Interest Pressure  0.10  (short volume trend, DTC, dark pool)
    5. Sector Headwind          0.05  (negative sector rotation)
    6. Manager Exodus           0.05  (tier-1 manager exits)

Usage:
    update_short_conviction_in_intelligence(conn, quarter)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

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
# Dimension weights — must sum to 1.0
# ---------------------------------------------------------------------------
SHORT_WEIGHTS = {
    "distribution_depth":   0.35,
    "exit_cascade_quality": 0.25,
    "insider_selling":      0.20,
    "short_pressure":       0.10,
    "sector_headwind":      0.05,
    "manager_exodus":       0.05,
}


# ---------------------------------------------------------------------------
# Dimension 1: Distribution Depth
# ---------------------------------------------------------------------------
def _compute_distribution_depth(row: dict) -> float:
    """0-100: how deep is the institutional distribution?"""
    phase = str(row.get("accum_phase") or "DORMANT")
    severity = str(row.get("distribution_severity") or "NONE")
    dist_warning = bool(row.get("distribution_warning") or False)

    phase_base = {
        "DISTRIBUTION": 80.0,
        "DECLINE":      65.0,
        "DORMANT":      20.0,
        "EXPANSION":    10.0,
        "EARLY_ACCUM":   5.0,
        "ACTIVE_ACCUM":  0.0,
        "LATE_ACCUM":    0.0,
    }.get(phase, 5.0)

    severity_mult = {
        "SEVERE":   1.0,
        "MODERATE": 0.70,
        "MILD":     0.40,
        "NONE":     0.15,
    }.get(severity, 0.15)

    score = phase_base * severity_mult

    # Bonus if distribution_warning is explicitly set
    if dist_warning and phase in ("DISTRIBUTION", "DECLINE"):
        score = min(100.0, score + 10.0)

    return min(100.0, score)


# ---------------------------------------------------------------------------
# Dimension 2: Exit Cascade Quality
# ---------------------------------------------------------------------------
def _compute_exit_cascade(row: dict) -> float:
    """0-100: how aggressively are institutions exiting?"""
    count_change_pct = _safe_float(row.get("inst_count_change_pct"))   # negative = good for shorts
    shares_change_pct = _safe_float(row.get("shares_change_pct"))       # negative = good
    value_change_pct = _safe_float(row.get("value_change_pct"))         # negative = good
    count_up_streak = _safe_int(row.get("count_up_streak"))             # 0 = no buying streak

    # We want large negative changes — invert and scale
    # -50% change → 100 pts, -20% → 60 pts, 0% → 0 pts, positive → 0
    def _decline_score(pct: float) -> float:
        if pct >= 0:
            return 0.0
        return min(100.0, abs(pct) * 2.0)

    count_score = _decline_score(count_change_pct)
    shares_score = _decline_score(shares_change_pct)
    value_score = _decline_score(value_change_pct)

    # No buying streak = good for short (institutions not re-entering)
    streak_penalty = min(30.0, count_up_streak * 10.0)

    cascade = (count_score * 0.4 + shares_score * 0.3 + value_score * 0.3)
    return max(0.0, min(100.0, cascade - streak_penalty))


# ---------------------------------------------------------------------------
# Dimension 3: Insider Selling
# ---------------------------------------------------------------------------
def _compute_insider_selling(row: dict) -> float:
    """0-100: are insiders selling meaningfully?"""
    sell_count = _safe_int(row.get("insider_sell_count_60d"))
    net_sell_count = _safe_int(row.get("insider_net_sell_count_60d"))   # sells - buys
    sell_dollar = _safe_float(row.get("insider_sell_value_60d"))
    ceo_cfo_selling = bool(row.get("ceo_cfo_selling") or False)

    if sell_count == 0:
        return 0.0

    # Base: sell count score (5+ sells = strong signal)
    count_score = min(100.0, sell_count * 15.0)

    # Net: are there more sells than buys?
    net_bonus = min(30.0, max(0.0, net_sell_count * 10.0)) if net_sell_count > 0 else 0.0

    # Dollar value: $1M+ in sells is meaningful
    dollar_bonus = 0.0
    if sell_dollar >= 10_000_000:
        dollar_bonus = 25.0
    elif sell_dollar >= 1_000_000:
        dollar_bonus = 15.0
    elif sell_dollar >= 100_000:
        dollar_bonus = 5.0

    # CEO/CFO selling is a strong bear signal
    exec_bonus = 20.0 if ceo_cfo_selling else 0.0

    return min(100.0, count_score * 0.4 + net_bonus + dollar_bonus + exec_bonus)


# ---------------------------------------------------------------------------
# Dimension 4: Short Interest Pressure
# ---------------------------------------------------------------------------
def _compute_short_pressure(row: dict) -> float:
    """0-100: is the short interest / dark pool confirming distribution?"""
    svr_trend = _safe_float(row.get("short_volume_ratio_trend"))   # positive = rising shorts
    dark_pool = _safe_float(row.get("dark_pool_pct_avg"))          # > 50% = selling pressure
    dtc = _safe_float(row.get("days_to_cover"))                    # low DTC = shorts can exit fast

    # Rising short volume ratio trend is confirmation
    svr_score = min(100.0, max(0.0, svr_trend * 10.0)) if svr_trend > 0 else 0.0

    # Dark pool above 50% means institutional selling through dark venues
    dark_score = min(100.0, max(0.0, (dark_pool - 40.0) * 5.0)) if dark_pool > 40 else 0.0

    # Low DTC (< 3 days) = shorts are nimble, can exit = less squeeze risk = safe short
    # High DTC (> 10 days) = squeeze risk = penalty
    dtc_score = 80.0 if dtc <= 3 else (50.0 if dtc <= 7 else (20.0 if dtc <= 10 else 0.0))

    return min(100.0, svr_score * 0.4 + dark_score * 0.3 + dtc_score * 0.3)


# ---------------------------------------------------------------------------
# Master scorer
# ---------------------------------------------------------------------------
def compute_short_conviction_scores(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> list[dict]:
    """Compute SHORT conviction scores for all tickers in the quarter.

    Reads intelligence_scores + agg_qoq_changes + Form4 sell data.
    Returns list of dicts with short_conviction_score and short_swing_signal.
    """
    logger.info("Computing SHORT conviction scores for quarter={}", quarter)

    # Load base intelligence data
    try:
        scores_df = conn.execute("""
            SELECT
                i.ticker, i.accum_phase, i.distribution_warning,
                i.distribution_severity, i.tier1_manager_count,
                i.tier2_manager_count, i.manager_quality_score,
                i.short_volume_ratio_trend, i.dark_pool_pct_avg,
                i.days_to_cover, i.price_above_200sma,
                i.price_momentum_90d, i.data_quality_score,
                d.sector
            FROM intelligence_scores i
            LEFT JOIN dim_issuer d ON i.ticker = d.ticker
            WHERE i.report_quarter = ?
        """, [quarter]).fetchdf()
    except Exception as e:
        logger.warning("SHORT conviction query failed for {}: {}", quarter, e)
        return []

    # Load QoQ change data (for exit cascade)
    qoq_map: dict = {}
    try:
        qoq_df = conn.execute("""
            SELECT ticker, inst_count_change_pct, shares_change_pct,
                   value_change_pct, count_up_streak
            FROM agg_qoq_changes
            WHERE current_quarter = ?
        """, [quarter]).fetchdf()
        for _, r in qoq_df.iterrows():
            qoq_map[str(r["ticker"])] = r.to_dict()
    except Exception as e:
        logger.debug("QoQ data unavailable for SHORT conviction: {}", e)

    # Load insider SELL data (last 60 days)
    insider_sell_map: dict = {}
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).date().isoformat()
        insider_df = conn.execute("""
            SELECT
                ticker,
                SUM(CASE WHEN transaction_code='S' THEN 1 ELSE 0 END) as sell_count,
                SUM(CASE WHEN transaction_code='P' THEN 1 ELSE 0 END) as buy_count,
                SUM(CASE WHEN transaction_code='S' THEN COALESCE(shares,0)*COALESCE(price,0) ELSE 0 END) as sell_value,
                MAX(CASE WHEN transaction_code='S'
                    AND (
                        UPPER(COALESCE(insider_role,'')) LIKE '%CEO%'
                        OR UPPER(COALESCE(insider_role,'')) LIKE '%CFO%'
                    )
                    THEN 1 ELSE 0 END) as ceo_cfo_selling
            FROM fact_form4_transactions
            WHERE transaction_date >= ?
            AND transaction_code IN ('S', 'P')
            GROUP BY ticker
        """, [cutoff]).fetchdf()
        for _, r in insider_df.iterrows():
            t = str(r["ticker"])
            sells = _safe_int(r.get("sell_count"))
            buys = _safe_int(r.get("buy_count"))
            insider_sell_map[t] = {
                "insider_sell_count_60d":     sells,
                "insider_net_sell_count_60d": max(0, sells - buys),
                "insider_sell_value_60d":     _safe_float(r.get("sell_value")),
                "ceo_cfo_selling":            bool(r.get("ceo_cfo_selling")),
            }
    except Exception as e:
        logger.debug("Insider sell data unavailable for SHORT conviction: {}", e)

    # Load sector headwind (negative rotation = headwind for longs = tailwind for shorts)
    sector_headwind_map: dict = {}
    try:
        sec_df = conn.execute("""
            SELECT sector, flow_pct FROM agg_sector_rotation WHERE report_quarter = ?
        """, [quarter]).fetchdf()
        for _, r in sec_df.iterrows():
            # Invert: negative flow_pct is a headwind for longs = bullish for shorts
            sector_headwind_map[str(r["sector"] or "")] = -_safe_float(r.get("flow_pct"))
    except Exception:
        pass

    results = []

    for _, row in scores_df.iterrows():
        ticker = str(row["ticker"])
        row_dict = row.to_dict()

        # Merge QoQ data
        row_dict.update(qoq_map.get(ticker, {}))

        # Merge insider sell data
        row_dict.update(insider_sell_map.get(ticker, {
            "insider_sell_count_60d": 0,
            "insider_net_sell_count_60d": 0,
            "insider_sell_value_60d": 0.0,
            "ceo_cfo_selling": False,
        }))

        # 1. Distribution depth
        d1 = _compute_distribution_depth(row_dict)

        # 2. Exit cascade quality
        d2 = _compute_exit_cascade(row_dict)

        # 3. Insider selling
        d3 = _compute_insider_selling(row_dict)

        # 4. Short interest pressure
        d4 = _compute_short_pressure(row_dict)

        # 5. Sector headwind
        sector = str(row_dict.get("sector") or "")
        headwind = sector_headwind_map.get(sector, 0.0)
        d5 = min(100.0, max(0.0, 50.0 + headwind * 2.5))

        # 6. Manager exodus: lower tier-1 count relative to sector = exodus
        tier1 = _safe_int(row_dict.get("tier1_manager_count"))
        mgr_quality = _safe_float(row_dict.get("manager_quality_score"))
        # Low manager quality + distribution phase = managers have left
        d6 = min(100.0, max(0.0, 100.0 - mgr_quality)) if row_dict.get("accum_phase") in ("DISTRIBUTION", "DECLINE") else 0.0

        # Weighted composite
        score = (
            d1 * SHORT_WEIGHTS["distribution_depth"] +
            d2 * SHORT_WEIGHTS["exit_cascade_quality"] +
            d3 * SHORT_WEIGHTS["insider_selling"] +
            d4 * SHORT_WEIGHTS["short_pressure"] +
            d5 * SHORT_WEIGHTS["sector_headwind"] +
            d6 * SHORT_WEIGHTS["manager_exodus"]
        )
        score = round(min(100.0, max(0.0, score)), 2)

        # Momentum confirmation gate: don't short stocks with strong 90d momentum
        # unless distribution is severe (institutions are distributing into strength)
        momentum = _safe_float(row_dict.get("price_momentum_90d"))
        severity = str(row_dict.get("distribution_severity") or "NONE")
        if momentum > 20.0 and severity not in ("SEVERE", "MODERATE"):
            score = score * 0.6  # penalize — price still rising, wait for reversal

        # Short signal: score >= 45 AND distribution/decline phase
        # above_200=0 preferred but not required (distribution into strength is valid)
        above_200 = _safe_int(row_dict.get("price_above_200sma", -1))
        phase = str(row_dict.get("accum_phase") or "")
        short_signal = (
            score >= 45.0
            and phase in ("DISTRIBUTION", "DECLINE")
        )

        breakdown = {
            "distribution_depth":   round(d1, 1),
            "exit_cascade_quality": round(d2, 1),
            "insider_selling":      round(d3, 1),
            "short_pressure":       round(d4, 1),
            "sector_headwind":      round(d5, 1),
            "manager_exodus":       round(d6, 1),
            "weights": SHORT_WEIGHTS,
        }

        results.append({
            "ticker":                   ticker,
            "short_conviction_score":   score,
            "short_swing_signal":       "SHORT" if short_signal else None,
            "short_conviction_breakdown": json.dumps(breakdown),
        })

    results.sort(key=lambda x: x["short_conviction_score"], reverse=True)
    n_signals = sum(1 for r in results if r["short_swing_signal"] == "SHORT")
    logger.info(
        "SHORT conviction computed: {} tickers, {} SHORT signals for quarter={}",
        len(results), n_signals, quarter
    )
    return results


def update_short_conviction_in_intelligence(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> int:
    """Write short conviction scores into intelligence_scores table."""

    # Ensure columns exist
    for col, coltype in [
        ("short_conviction_score", "DOUBLE"),
        ("short_swing_signal", "VARCHAR"),
        ("short_conviction_breakdown", "VARCHAR"),
    ]:
        try:
            conn.execute(f"ALTER TABLE intelligence_scores ADD COLUMN {col} {coltype}")
            logger.info("Added column {} to intelligence_scores", col)
        except Exception:
            pass  # column already exists

    results = compute_short_conviction_scores(conn, quarter)
    if not results:
        return 0

    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    updated = 0

    for r in results:
        try:
            conn.execute("""
                UPDATE intelligence_scores
                SET short_conviction_score     = ?,
                    short_swing_signal         = ?,
                    short_conviction_breakdown = ?,
                    computed_at                = ?
                WHERE ticker = ? AND report_quarter = ?
            """, [
                r["short_conviction_score"],
                r["short_swing_signal"],
                r["short_conviction_breakdown"],
                now_iso,
                r["ticker"],
                quarter,
            ])
            updated += 1
        except Exception as e:
            logger.debug("SHORT conviction update failed for {}: {}", r["ticker"], e)

    logger.info(
        "SHORT conviction scores written: {}/{} for quarter={}",
        updated, len(results), quarter
    )
    return updated
