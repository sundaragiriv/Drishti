"""Phase 4b: Trading Signal Generator.

Generates actionable trading signals across all three horizons:
    - Day Trading bias (LONG_ONLY / SHORT_ONLY / NEUTRAL)
    - Swing Trading (2-8 weeks): BUY / WATCH / AVOID / SHORT
    - Long Term (1-4 quarters): BUY / ACCUMULATE / HOLD / REDUCE / EXIT

Also generates options strategy suggestions for each horizon.

Signal logic is rule-based and derived from intelligence_scores data.
No ML required — uses threshold-based conviction scoring.
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


# ---------------------------------------------------------------------------
# Signal logic
# ---------------------------------------------------------------------------

def _day_bias(row: dict) -> str:
    """Determine intraday trading bias from institutional phase + conviction."""
    phase = str(row.get("accum_phase") or "DORMANT")
    conviction = _safe_float(row.get("conviction_score"))
    dist_warn = bool(row.get("distribution_warning"))

    if dist_warn:
        return "SHORT_ONLY"

    if phase in ("ACTIVE_ACCUM", "LATE_ACCUM") and conviction >= 60:
        return "LONG_ONLY"
    elif phase in ("DISTRIBUTION", "DECLINE"):
        return "SHORT_ONLY"
    elif phase == "EARLY_ACCUM" and conviction >= 50:
        return "LONG_ONLY"
    else:
        return "NEUTRAL"


def _swing_signal(row: dict) -> tuple[str, str, str, str, str]:
    """Determine swing signal and entry/exit parameters.

    Returns: (signal, entry_zone, target, stop_loss, options_suggestion)
    """
    phase = str(row.get("accum_phase") or "DORMANT")
    conviction = _safe_float(row.get("conviction_score"))
    dist_warn = bool(row.get("distribution_warning"))
    insider_cluster = bool(row.get("insider_cluster_detected"))
    divergence = _safe_float(row.get("divergence_magnitude"))
    tier1 = _safe_int(row.get("tier1_manager_count"))

    # Distribution / decline = avoid or short
    if dist_warn:
        severity = str(row.get("distribution_severity") or "MILD")
        if severity == "SEVERE":
            return (
                "SHORT",
                "Any rally to resistance",
                "-15% to -25%",
                "+5% above entry",
                "Buy ATM Puts, 30-45 DTE",
            )
        else:
            return "AVOID", "N/A — distribution warning active", "N/A", "N/A", "N/A"

    # DECLINE / DISTRIBUTION without dist_warn = SHORT if conviction is low
    if phase in ("DECLINE", "DISTRIBUTION") and conviction <= 35:
        return (
            "SHORT",
            "Short rallies to resistance — institutions exiting",
            "-10% to -20%",
            "+5% above entry",
            "Buy ATM Puts, 30-45 DTE",
        )

    if phase == "DECLINE":
        return "AVOID", "N/A — institutional exit phase", "N/A", "N/A", "N/A"

    # Strong buy zone (widened: ACTIVE or LATE_ACCUM, conviction >= 55)
    if phase in ("ACTIVE_ACCUM", "LATE_ACCUM") and conviction >= 55:
        entry = "Buy dips to 20-day MA support zone"
        if tier1 >= 2:
            entry = "Buy any pullback — tier-1 institutional support"
        if insider_cluster:
            entry += " | Insider cluster confirms — add on weakness"
        target = "+15% to +25% over 4-8 weeks"
        stop = "-7% or break below recent swing low"
        if tier1 >= 2 and conviction >= 75:
            opts = "Buy ATM Calls, 30-45 DTE; or Bull Call Spread"
        else:
            opts = "Buy slightly OTM Calls, 45 DTE"
        return "BUY", entry, target, stop, opts

    # EARLY_ACCUM with insider cluster = BUY (strong evidence in early phase)
    if phase == "EARLY_ACCUM" and conviction >= 55 and insider_cluster:
        return (
            "BUY",
            "Early accumulation with insider cluster — buy pullbacks to support",
            "+12% to +20% over 4-8 weeks",
            "-8% or break below recent swing low",
            "Buy OTM Calls, 45-60 DTE",
        )

    # Watch zone (widened: conviction >= 35)
    if phase in ("EARLY_ACCUM", "ACTIVE_ACCUM", "LATE_ACCUM") and conviction >= 35:
        entry = "Watch for price pullback to establish entry"
        if divergence >= 40.0:
            entry = "Bullish divergence active — buy pullbacks aggressively"
        target = "+10% to +20% over 4-6 weeks"
        stop = "-8%"
        opts = "Buy OTM Calls, 45-60 DTE on confirmed breakout"
        return "WATCH", entry, target, stop, opts

    # Expansion = late to the trade
    if phase == "EXPANSION":
        return (
            "WATCH",
            "Late stage — wait for consolidation before entry",
            "+8% if momentum continues",
            "-5% tight stop",
            "Buy OTM Calls only on momentum continuation",
        )

    # Default
    return "AVOID", "No institutional conviction basis for entry", "N/A", "N/A", "N/A"


def _longterm_signal(row: dict) -> tuple[str, str, str, str]:
    """Determine long-term signal for 1-4 quarter hold.

    Returns: (signal, thesis, target_quarter, leaps_strategy)
    """
    phase = str(row.get("accum_phase") or "DORMANT")
    conviction = _safe_float(row.get("conviction_score"))
    lag_conf = str(row.get("lag_confidence") or "LOW")
    lag_rationale = str(row.get("lag_rationale") or "")
    expected_q = _safe_int(row.get("expected_impact_quarters"), default=3)
    dist_warn = bool(row.get("distribution_warning"))
    tier1 = _safe_int(row.get("tier1_manager_count"))

    # Compute approximate target quarter
    from datetime import date
    now = date.today()
    target_year = now.year + (now.month + expected_q * 3 - 1) // 12
    target_month = (now.month + expected_q * 3 - 1) % 12 + 1
    target_q_num = (target_month - 1) // 3 + 1
    target_quarter = f"Q{target_q_num} {target_year}"

    if dist_warn:
        return (
            "REDUCE",
            "Distribution warning: smart money exiting. Reduce exposure, protect profits.",
            "N/A",
            "Buy protective Puts (LEAPS 6-12 months) as hedge",
        )

    if phase == "DECLINE":
        return "EXIT", "Institutional exit phase complete. Avoid or exit.", "N/A", "N/A"

    if phase in ("ACTIVE_ACCUM", "LATE_ACCUM") and conviction >= 55:
        leaps = "N/A"
        if tier1 >= 2 and conviction >= 70:
            leaps = f"Buy LEAPS Calls 12-18 months out, strike at current price"
        elif conviction >= 55:
            leaps = f"Buy LEAPS Calls 9-12 months, slightly OTM"
        thesis = (
            f"Institutional accumulation with {row.get('accum_phase_quarters', '?')} consecutive quarters. "
            f"{lag_rationale}"
        )
        if tier1 >= 1:
            thesis += f" Tier-1 institutional presence ({tier1} managers) adds quality."
        signal = "BUY" if conviction >= 70 else "ACCUMULATE"
        return signal, thesis, target_quarter, leaps

    if phase == "EARLY_ACCUM" and conviction >= 40:
        return (
            "ACCUMULATE",
            f"Early accumulation forming. {lag_rationale} Watch for confirmation next quarter.",
            target_quarter,
            "N/A — too early for LEAPS commitment",
        )

    if phase == "EXPANSION":
        return (
            "HOLD",
            "Expansion phase: retail momentum driving price. Institutions flat/fading.",
            "N/A — evaluate at next 13F cycle",
            "N/A",
        )

    return "AVOID", "Insufficient institutional conviction for long-term commitment.", "N/A", "N/A"


# ---------------------------------------------------------------------------
# Main pipeline function
# ---------------------------------------------------------------------------

def generate_trading_signals(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> list[dict]:
    """Generate day/swing/longterm signals for all tickers in the quarter.

    Reads from intelligence_scores (all prior dimensions must be populated).
    Returns list of dicts with signal fields.
    """
    logger.info("Generating trading signals for quarter={}", quarter)

    try:
        df = conn.execute("""
            SELECT
                ticker, accum_phase, accum_phase_quarters,
                conviction_score, lag_confidence, lag_rationale,
                expected_impact_quarters, cascade_stage,
                divergence_active, divergence_magnitude,
                tier1_manager_count, tier2_manager_count,
                insider_cluster_detected, insider_net_buy_count,
                ceo_cfo_buying, insider_score,
                distribution_warning, distribution_severity
            FROM intelligence_scores
            WHERE report_quarter = ?
        """, [quarter]).fetchdf()
    except Exception as e:
        logger.warning("Trading signal query failed for {}: {}", quarter, e)
        return []

    results = []
    for _, row in df.iterrows():
        row_dict = row.to_dict()

        bias = _day_bias(row_dict)
        swing_sig, entry, target, stop, swing_opts = _swing_signal(row_dict)
        lt_sig, lt_thesis, lt_target_q, lt_leaps = _longterm_signal(row_dict)

        results.append({
            "ticker": str(row_dict["ticker"]),
            "day_bias": bias,
            "swing_signal": swing_sig,
            "swing_entry_zone": entry,
            "swing_target": target,
            "swing_stop": stop,
            "swing_options_suggestion": swing_opts,
            "longterm_signal": lt_sig,
            "longterm_thesis": lt_thesis,
            "longterm_target_quarter": lt_target_q,
            "longterm_options_suggestion": lt_leaps,
        })

    logger.info("Trading signals generated: {} tickers for quarter={}", len(results), quarter)
    return results


def update_trading_signals_in_intelligence(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> int:
    """Write trading signals into intelligence_scores table."""
    results = generate_trading_signals(conn, quarter)
    if not results:
        return 0

    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    updated = 0

    for r in results:
        try:
            conn.execute("""
                UPDATE intelligence_scores
                SET day_bias = ?,
                    swing_signal = ?,
                    swing_entry_zone = ?,
                    swing_target = ?,
                    swing_stop = ?,
                    swing_options_suggestion = ?,
                    longterm_signal = ?,
                    longterm_thesis = ?,
                    longterm_target_quarter = ?,
                    longterm_options_suggestion = ?,
                    computed_at = ?
                WHERE ticker = ? AND report_quarter = ?
            """, [
                r["day_bias"],
                r["swing_signal"],
                r["swing_entry_zone"],
                r["swing_target"],
                r["swing_stop"],
                r["swing_options_suggestion"],
                r["longterm_signal"],
                r["longterm_thesis"],
                r["longterm_target_quarter"],
                r["longterm_options_suggestion"],
                now_iso,
                r["ticker"],
                quarter,
            ])
            updated += 1
        except Exception as e:
            logger.debug("Trading signal update failed for {}: {}", r["ticker"], e)

    logger.info("Trading signals updated: {}/{} for quarter={}", updated, len(results), quarter)
    return updated
