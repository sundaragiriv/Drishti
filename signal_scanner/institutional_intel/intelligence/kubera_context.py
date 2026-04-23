"""Ask Kubera — Data Aggregation Engine.

Compiles all available intelligence data for a ticker into a structured
context dict that is passed to the Claude API for narrative generation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH, get_active_quarter


def _get_short_data(conn: duckdb.DuckDBPyConnection, ticker: str) -> dict:
    """Get short interest and short volume summary for a ticker."""
    result: Dict[str, Any] = {}
    try:
        # Latest short interest
        si_row = conn.execute("""
            SELECT short_interest, days_to_cover, avg_daily_volume, settlement_date
            FROM fact_short_interest
            WHERE ticker = ?
            ORDER BY settlement_date DESC LIMIT 1
        """, [ticker]).fetchone()
        if si_row:
            result["short_interest"] = int(si_row[0] or 0)
            result["days_to_cover"] = round(float(si_row[1] or 0), 2)
            result["si_avg_daily_volume"] = int(si_row[2] or 0)
            result["si_settlement_date"] = str(si_row[3] or "")

        # Short volume: avg ratio over last 20 trading days
        sv_row = conn.execute("""
            SELECT AVG(short_volume_ratio) AS avg_ratio,
                   MAX(CASE WHEN rn = 1 THEN short_volume_ratio END) AS latest_ratio
            FROM (
                SELECT short_volume_ratio,
                       ROW_NUMBER() OVER (ORDER BY trade_date DESC) as rn
                FROM fact_short_volume
                WHERE ticker = ? AND total_volume > 10000
            ) sub
            WHERE rn <= 20
        """, [ticker]).fetchone()
        if sv_row and sv_row[0]:
            result["short_volume_ratio_avg_20d"] = round(float(sv_row[0]), 1)
            result["short_volume_ratio_latest"] = round(float(sv_row[1] or 0), 1)

        # Dark pool: avg % over last 10 trading days
        dp_row = conn.execute("""
            SELECT AVG(dark_pool_pct), AVG(dark_pool_trades)
            FROM fact_dark_pool_daily
            WHERE ticker = ? AND trade_date >= CURRENT_DATE - INTERVAL 30 DAY
        """, [ticker]).fetchone()
        if dp_row and dp_row[0]:
            result["dark_pool_pct_avg_30d"] = round(float(dp_row[0]), 1)
            result["dark_pool_trades_avg_30d"] = int(dp_row[1] or 0)
    except Exception:
        pass
    return result


def _get_insider_pattern(conn: duckdb.DuckDBPyConnection, ticker: str) -> dict:
    """Get historical insider buying pattern for a ticker from agg_insider_patterns."""
    try:
        # Prefer OPPORTUNISTIC pattern (strongest academic signal)
        row = conn.execute("""
            SELECT pattern_type, sample_count, win_rate_90d, alpha_win_90d,
                   mean_return_90d, mean_alpha_90d, median_return_90d,
                   insider_effect_score
            FROM agg_insider_patterns
            WHERE ticker = ? AND pattern_type IN ('OPPORTUNISTIC', 'ALL')
            ORDER BY CASE pattern_type WHEN 'OPPORTUNISTIC' THEN 1 ELSE 2 END
            LIMIT 1
        """, [ticker]).fetchone()
        if row:
            return {
                "type": str(row[0]),
                "sample_count": int(row[1] or 0),
                "win_rate_90d": round(float(row[2] or 0), 1),
                "alpha_win_90d": round(float(row[3] or 0), 1),
                "mean_return_90d": round(float(row[4] or 0), 2),
                "mean_alpha_90d": round(float(row[5] or 0), 2),
                "median_return_90d": round(float(row[6] or 0), 2),
                "effect_score": round(float(row[7] or 0), 1),
            }
    except Exception:
        pass
    return {}


def build_stock_context(
    ticker: str,
    conn: duckdb.DuckDBPyConnection,
    quarter: Optional[str] = None,
) -> Dict[str, Any]:
    """Build comprehensive context dict for a ticker.

    Args:
        ticker: Stock ticker symbol
        conn: DuckDB read connection
        quarter: Target quarter (latest if None)

    Returns:
        Structured dict with all intelligence data for the ticker.
        Returns {} if ticker not found.
    """
    ticker = ticker.upper().strip()

    # Resolve quarter — use canonical get_active_quarter() for consistency
    # with swing ideas, options, AI signals, and ISR scorecard.
    if not quarter:
        quarter = get_active_quarter(conn)
        if not quarter:
            # Fallback: MAX from agg_qoq_changes
            row = conn.execute("SELECT MAX(current_quarter) FROM agg_qoq_changes").fetchone()
            quarter = row[0] if row else None

    if not quarter:
        return {}

    # --- Intelligence scores ---
    intel_row = conn.execute("""
        SELECT *
        FROM intelligence_scores
        WHERE ticker = ? AND report_quarter = ?
    """, [ticker, quarter]).fetchdf()

    if intel_row.empty:
        # Try to find ticker in QoQ changes at least
        qoq_check = conn.execute(
            "SELECT COUNT(*) FROM agg_qoq_changes WHERE ticker = ?", [ticker]
        ).fetchone()
        if not qoq_check or qoq_check[0] == 0:
            logger.warning("Ticker {} not found in warehouse", ticker)
            return {}

    intel = intel_row.iloc[0].to_dict() if not intel_row.empty else {}

    # --- Company info ---
    issuer_row = conn.execute("""
        SELECT issuer_name, sector, industry
        FROM dim_issuer WHERE ticker = ? LIMIT 1
    """, [ticker]).fetchone()
    company_name = issuer_row[0] if issuer_row else ticker
    sector = issuer_row[1] if issuer_row else intel.get("sector", "Unknown")
    industry = issuer_row[2] if issuer_row else ""

    # --- QoQ changes ---
    qoq_row = conn.execute("""
        SELECT
            inst_count_current, inst_count_prior, inst_count_change, inst_count_change_pct,
            shares_current, shares_prior, shares_change_pct,
            value_current_usd_k, value_prior_usd_k, value_change_pct,
            count_up_streak, shares_up_streak,
            avg_price_current, avg_price_prior, avg_price_change_pct,
            current_price
        FROM agg_qoq_changes
        WHERE ticker = ? AND current_quarter = ?
    """, [ticker, quarter]).fetchone()

    # --- Top 5 managers ---
    top_managers = conn.execute("""
        SELECT p.manager_name, p.value_usd_thousands, p.shares,
               COALESCE(t.tier, 3) AS tier
        FROM fact_13f_positions p
        LEFT JOIN dim_manager_tiers t ON p.manager_cik = t.manager_cik
        WHERE p.ticker = ?
        ORDER BY p.value_usd_thousands DESC LIMIT 5
    """, [ticker]).fetchdf()
    top_mgr_list = top_managers.to_dict("records") if not top_managers.empty else []

    # --- Insider activity (90 days) ---
    insider_recent = conn.execute("""
        SELECT insider_name, insider_role, transaction_date, direction, shares, price
        FROM fact_form4_transactions
        WHERE ticker = ?
          AND transaction_date >= CURRENT_DATE - INTERVAL 90 DAY
        ORDER BY transaction_date DESC LIMIT 10
    """, [ticker]).fetchdf()
    insider_list = insider_recent.to_dict("records") if not insider_recent.empty else []

    # --- Price summary (90-day detailed + 52-week stats) ---
    price_rows = conn.execute("""
        SELECT trade_date, close, volume
        FROM fact_daily_prices
        WHERE ticker = ?
          AND trade_date >= CURRENT_DATE - INTERVAL 365 DAY
        ORDER BY trade_date DESC
    """, [ticker]).fetchdf()

    current_price = qoq_row[15] if qoq_row and len(qoq_row) > 15 else None
    high_52w = low_52w = avg_volume = None
    return_30d_pct = avg_volume_30d = pct_from_low = None

    if not price_rows.empty:
        closes = price_rows["close"].dropna()
        volumes = price_rows["volume"].dropna()
        if len(closes) > 0:
            if current_price is None:
                current_price = float(closes.iloc[0])
            high_52w = float(closes.max())
            low_52w = float(closes.min())
            avg_volume = float(volumes.mean()) if len(volumes) > 0 else None
            # 30-day return
            idx_30 = min(30, len(closes) - 1)
            price_30d_ago = float(closes.iloc[idx_30])
            if price_30d_ago > 0:
                return_30d_pct = round((float(closes.iloc[0]) - price_30d_ago) / price_30d_ago * 100, 2)
            # 30-day avg volume
            avg_volume_30d = int(volumes.iloc[:30].mean()) if len(volumes) >= 1 else None
            # % from 52w low
            if low_52w and low_52w > 0 and current_price:
                pct_from_low = round((current_price - low_52w) / low_52w * 100, 2)

    pct_from_high = None
    if current_price and high_52w and high_52w > 0:
        pct_from_high = round((current_price - high_52w) / high_52w * 100, 2)

    # --- Sector context ---
    sector_row = conn.execute("""
        SELECT flow_pct, inflow_streak, net_inst_count_change
        FROM agg_sector_rotation
        WHERE sector = ? AND report_quarter = ?
    """, [sector, quarter]).fetchone()

    # --- Accumulation history (last 6 quarters) ---
    history_df = conn.execute("""
        SELECT current_quarter, inst_count_current, inst_count_change,
               shares_change_pct, avg_price_change_pct
        FROM agg_qoq_changes
        WHERE ticker = ?
        ORDER BY current_quarter DESC LIMIT 6
    """, [ticker]).fetchdf()
    history = history_df.to_dict("records") if not history_df.empty else []

    # --- Build context ---
    def _safe(val, decimals=1):
        if val is None:
            return None
        try:
            return round(float(val), decimals)
        except (TypeError, ValueError):
            return str(val)

    context: Dict[str, Any] = {
        "ticker": ticker,
        "company": company_name,
        "sector": sector or "Unknown",
        "industry": industry or "",
        "analysis_quarter": quarter,

        "current_phase": str(intel.get("accum_phase", "UNKNOWN")),
        "phase_quarters": int(intel.get("accum_phase_quarters", 0) or 0),
        "conviction_score": _safe(intel.get("conviction_score"), 1),
        "ml_score": _safe(intel.get("ml_score"), 1),
        "accumulation_strength": _safe(intel.get("accum_strength_score"), 1),

        "lag_estimate": f"{int(intel.get('expected_impact_quarters', 2) or 2)} quarters",
        "lag_confidence": str(intel.get("lag_confidence", "LOW")),
        "lag_rationale": str(intel.get("lag_rationale", "")),

        "institutional_summary": {
            "current_holders": int(qoq_row[0]) if qoq_row else 0,
            "prior_holders": int(qoq_row[1]) if qoq_row else 0,
            "holder_change": int(qoq_row[2]) if qoq_row else 0,
            "holder_change_pct": _safe(qoq_row[3]) if qoq_row else 0,
            "shares_current": _safe(qoq_row[4]) if qoq_row else 0,
            "shares_change_pct": _safe(qoq_row[6]) if qoq_row else 0,
            "value_usd_millions": _safe((qoq_row[7] or 0) / 1000, 1) if qoq_row else None,
            "value_change_pct": _safe(qoq_row[9]) if qoq_row else 0,
            "count_up_streak": int(qoq_row[10]) if qoq_row else 0,
            "tier1_holders": int(intel.get("tier1_manager_count", 0) or 0),
            "tier2_holders": int(intel.get("tier2_manager_count", 0) or 0),
            "new_initiations_this_quarter": int(intel.get("new_initiations_count", 0) or 0),
            "cascade_stage": int(intel.get("cascade_stage", 0) or 0),
            "max_manager_concentration_pct": _safe(intel.get("max_manager_concentration"), 2),
            "concentrated_managers_count": int(intel.get("concentrated_managers_count", 0) or 0),
            "top_5_managers": [
                {
                    "name": m.get("manager_name", ""),
                    "value_usd_k": _safe(m.get("value_usd_thousands")),
                    "tier": int(m.get("tier", 3)),
                }
                for m in top_mgr_list
            ],
        },

        "insider_summary": {
            "cluster_detected": bool(intel.get("insider_cluster_detected", False)),
            "net_buy_count_90d": int(intel.get("insider_net_buy_count", 0) or 0),
            "ceo_cfo_buying": bool(intel.get("ceo_cfo_buying", False)),
            "insider_score": _safe(intel.get("insider_score")),
            "insider_effect_score": _safe(intel.get("insider_effect_score")),
            "insider_hist_win_rate_90d": _safe(intel.get("insider_hist_win_rate")),
            "insider_hist_alpha_90d": _safe(intel.get("insider_hist_alpha")),
            "recent_transactions": [
                {
                    "name": t.get("insider_name", ""),
                    "role": t.get("insider_role", ""),
                    "date": str(t.get("transaction_date", "")),
                    "direction": t.get("direction", ""),
                    "shares": _safe(t.get("shares")),
                    "price": _safe(t.get("price")),
                }
                for t in insider_list
            ],
            "historical_pattern": _get_insider_pattern(conn, ticker),
        },

        "trend_and_pressure": {
            "trend_score": _safe(intel.get("trend_score")),
            "institutional_pressure": _safe(intel.get("institutional_pressure")),
        },

        "price_summary": {
            "current_price": _safe(current_price, 2),
            "return_30d_pct": _safe(return_30d_pct, 2),
            "avg_price_last_quarter": _safe(qoq_row[12]) if qoq_row else None,
            "avg_price_prior_quarter": _safe(qoq_row[13]) if qoq_row else None,
            "price_change_pct_qoq": _safe(qoq_row[14]) if qoq_row else None,
            "avg_volume_30d": avg_volume_30d,
            "avg_daily_volume_52w": _safe(avg_volume, 0),
            "high_52w": _safe(high_52w, 2),
            "low_52w": _safe(low_52w, 2),
            "pct_from_52w_high": _safe(pct_from_high, 1),
            "pct_from_52w_low": _safe(pct_from_low, 1),
        },

        "sector_context": {
            "sector_flow_pct_qoq": _safe(sector_row[0]) if sector_row else None,
            "sector_inflow_streak_quarters": int(sector_row[1]) if sector_row else 0,
            "sector_net_inst_change": int(sector_row[2]) if sector_row else 0,
            "sector_trend": (
                "STRONG_INFLOW" if sector_row and float(sector_row[0] or 0) > 10
                else "INFLOW" if sector_row and float(sector_row[0] or 0) > 0
                else "OUTFLOW"
            ) if sector_row else "UNKNOWN",
        },

        "accumulation_history_6q": [
            {
                "quarter": h.get("current_quarter", ""),
                "inst_count": int(h.get("inst_count_current", 0) or 0),
                "inst_change": int(h.get("inst_count_change", 0) or 0),
                "shares_change_pct": _safe(h.get("shares_change_pct")),
                "price_change_pct": _safe(h.get("avg_price_change_pct")),
            }
            for h in history
        ],

        "distribution_warning": bool(intel.get("distribution_warning", False)),
        "distribution_severity": intel.get("distribution_severity"),

        "trading_signals": {
            "day_bias": str(intel.get("day_bias", "NEUTRAL")),
            "swing_signal": intel.get("swing_signal"),
            "swing_entry_zone": intel.get("swing_entry_zone"),
            "swing_target": intel.get("swing_target"),
            "swing_stop": intel.get("swing_stop"),
            "swing_options": intel.get("swing_options_suggestion"),
            "longterm_signal": intel.get("longterm_signal"),
            "longterm_thesis": intel.get("longterm_thesis"),
            "longterm_target_quarter": intel.get("longterm_target_quarter"),
            "longterm_options": intel.get("longterm_options_suggestion"),
        },

        "conviction_breakdown": intel.get("conviction_breakdown"),

        "short_squeeze_data": {
            "squeeze_score": _safe(intel.get("squeeze_score")),
            "short_squeeze_score": _safe(intel.get("short_squeeze_score")),
            **_get_short_data(conn, ticker),
        },
    }

    return context
