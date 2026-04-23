"""Phase 1: Institutional Accumulation Phase Classifier.

Classifies each ticker into one of 7 phases based on QoQ institutional flows:
    DORMANT       — No meaningful institutional activity
    EARLY_ACCUM   — 1-2 quarters of rising institutional count
    ACTIVE_ACCUM  — 3+ quarters sustained accumulation (primary BUY zone)
    LATE_ACCUM    — Accumulation slowing, price moving, impact imminent
    EXPANSION     — Retail phase: price running, institutions flat/fading
    DISTRIBUTION  — Smart money reducing, large-cap selling
    DECLINE       — Institutional exit largely complete

Also computes:
    accum_phase_quarters — number of consecutive quarters in current phase
    accum_strength_score — 0-100 accumulation strength score
    expected_impact_quarters — estimated quarters until price impact
    lag_confidence — HIGH / MEDIUM / LOW
    lag_rationale — human-readable explanation

Usage (via run_pipeline.py):
    run_phase_classification(conn, quarter)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH


# ---------------------------------------------------------------------------
# Phase thresholds
# ---------------------------------------------------------------------------

# Minimum consecutive quarters of rising count to be ACTIVE_ACCUM
ACTIVE_ACCUM_MIN_STREAK = 3
EARLY_ACCUM_MIN_STREAK = 2

# Minimum count increase pct to count as an "up" quarter
COUNT_UP_THRESHOLD_PCT = 3.0   # at least 3% more institutions

# Maximum pct change that still qualifies as "flat" (for EXPANSION detection)
FLAT_THRESHOLD_PCT = 1.0

# Distribution: count dropping >= this pct
DISTRIBUTION_DROP_PCT = -5.0

# Minimum institutional count in prior quarter for QoQ change to be reliable.
# Quarters with fewer than this are likely incomplete 13F data (e.g. Q2 2025
# contamination where only ~152 managers filed).
MIN_PRIOR_COUNT = 20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _quarter_offset(quarter: str, offset: int) -> str:
    """Return the quarter string offset quarters before/after the given one."""
    year = int(quarter.split("-Q")[0])
    qnum = int(quarter.split("-Q")[1])
    total = (year * 4 + qnum - 1) + offset
    return f"{total // 4}-Q{total % 4 + 1}"


# ---------------------------------------------------------------------------
# Core classification logic
# ---------------------------------------------------------------------------

def _classify_phase(
    history: list[Dict[str, Any]],
) -> Dict[str, Any]:
    """Classify the phase from a list of QoQ change records (newest first).

    history: list of dicts with keys: count_change_pct, count_up, report_quarter
    Returns dict with classification fields.
    """
    if not history:
        return {
            "accum_phase": "DORMANT",
            "accum_phase_quarters": 0,
            "accum_strength_score": 0.0,
            "count_up_streak": 0,
            "count_down_streak": 0,
        }

    # Compute consecutive up/down streaks from most recent quarter.
    # Grace rule: 1 flat quarter mid-streak is allowed without breaking the streak.
    # 2 consecutive flat quarters = streak over.
    # Data quality: quarters with prior count < MIN_PRIOR_COUNT are unreliable
    # (e.g. incomplete 13F filings) — treat them as "unknown" (consume grace).
    count_up_streak = 0
    count_down_streak = 0
    flat_grace = 0

    for row in history:
        prior_count = _safe_float(row.get("inst_count_prior"))
        pct = _safe_float(row.get("count_change_pct"))

        current_count = _safe_float(row.get("inst_count_current"))

        # Skip unreliable quarters where either side of the QoQ comparison
        # has too few institutions (likely incomplete 13F filings)
        if prior_count < MIN_PRIOR_COUNT or current_count < MIN_PRIOR_COUNT:
            flat_grace += 1
            if flat_grace >= 2:
                break
            continue

        if pct >= COUNT_UP_THRESHOLD_PCT:
            flat_grace = 0
            if count_down_streak > 0:
                break
            count_up_streak += 1
        elif pct <= DISTRIBUTION_DROP_PCT:
            flat_grace = 0
            if count_up_streak > 0:
                break
            count_down_streak += 1
        else:  # flat quarter
            flat_grace += 1
            if flat_grace >= 2:
                # Two consecutive flat quarters ends the streak
                break
            # Single flat quarter: grace — streak count holds, continue scanning

    # Find the most recent quarter with reliable data for phase checks
    latest = history[0]
    latest_prior = _safe_float(latest.get("inst_count_prior"))
    latest_current = _safe_float(latest.get("inst_count_current"))
    latest_reliable = latest_prior >= MIN_PRIOR_COUNT and latest_current >= MIN_PRIOR_COUNT

    # Use reliable latest data (skip contaminated quarters for direct checks)
    if latest_reliable:
        latest_pct = _safe_float(latest.get("count_change_pct"))
        latest_count = _safe_float(latest.get("inst_count_current"))
    else:
        # Fall back to first reliable quarter for direct checks
        reliable = [
            r for r in history
            if _safe_float(r.get("inst_count_prior")) >= MIN_PRIOR_COUNT
            and _safe_float(r.get("inst_count_current")) >= MIN_PRIOR_COUNT
        ]
        if reliable:
            latest_pct = _safe_float(reliable[0].get("count_change_pct"))
            latest_count = _safe_float(reliable[0].get("inst_count_current"))
        else:
            latest_pct = 0.0
            latest_count = 0.0

    # Classify
    if count_up_streak >= ACTIVE_ACCUM_MIN_STREAK:
        # Check if slowing (latest quarter weaker than previous)
        if len(history) >= 2:
            prev_pct = _safe_float(history[1].get("count_change_pct"))
            if latest_pct < prev_pct * 0.5 and latest_pct < 5.0:
                phase = "LATE_ACCUM"
            else:
                phase = "ACTIVE_ACCUM"
        else:
            phase = "ACTIVE_ACCUM"
    elif count_up_streak >= EARLY_ACCUM_MIN_STREAK:
        phase = "EARLY_ACCUM"
    elif count_down_streak >= 2:
        phase = "DECLINE"
    elif count_down_streak == 1 and latest_pct <= DISTRIBUTION_DROP_PCT:
        phase = "DISTRIBUTION"
    elif abs(latest_pct) <= FLAT_THRESHOLD_PCT and latest_count > 50:
        # Many institutions holding flat while count was previously rising = EXPANSION
        prev_history = history[1:4] if len(history) > 1 else []
        had_accum = any(
            _safe_float(r.get("count_change_pct")) >= COUNT_UP_THRESHOLD_PCT
            and _safe_float(r.get("inst_count_prior")) >= MIN_PRIOR_COUNT
            for r in prev_history
        )
        phase = "EXPANSION" if had_accum else "DORMANT"
    else:
        phase = "DORMANT"

    # Strength score (0-100)
    strength = _compute_accum_strength(history, phase)

    return {
        "accum_phase": phase,
        "accum_phase_quarters": max(count_up_streak, count_down_streak, 1),
        "accum_strength_score": strength,
        "count_up_streak": count_up_streak,
        "count_down_streak": count_down_streak,
    }


def _compute_accum_strength(history: list[Dict], phase: str) -> float:
    """Compute 0-100 accumulation strength score based on phase + recent data."""
    if not history or phase in ("DORMANT", "DECLINE"):
        return 0.0

    latest = history[0]
    count_pct = _safe_float(latest.get("count_change_pct"))
    shares_pct = _safe_float(latest.get("shares_change_pct"))
    value_pct = _safe_float(latest.get("value_change_pct"))

    # Base score from phase
    phase_base = {
        "ACTIVE_ACCUM": 60.0,
        "LATE_ACCUM": 45.0,
        "EARLY_ACCUM": 35.0,
        "EXPANSION": 25.0,
        "DISTRIBUTION": 10.0,
        "DORMANT": 0.0,
        "DECLINE": 0.0,
    }.get(phase, 0.0)

    # Bonuses
    bonus = 0.0
    if count_pct > 10:
        bonus += 15.0
    elif count_pct > 5:
        bonus += 8.0
    elif count_pct > 2:
        bonus += 3.0

    if shares_pct > 10:
        bonus += 10.0
    elif shares_pct > 5:
        bonus += 5.0

    if value_pct > 15:
        bonus += 5.0

    # Streak bonus
    streak = len([r for r in history if _safe_float(r.get("count_change_pct")) >= COUNT_UP_THRESHOLD_PCT])
    bonus += min(streak * 3.0, 15.0)

    return min(100.0, phase_base + bonus)


def _estimate_lag(
    history: list[Dict],
    phase: str,
    count_up_streak: int,
) -> Tuple[str, str, str]:
    """Estimate price impact lag from institutional activity.

    Returns: (lag_estimate_str, lag_confidence, lag_rationale)
    """
    if phase == "DORMANT":
        return ("N/A", "LOW", "No accumulation detected.")

    if phase == "DECLINE":
        return ("N/A", "LOW", "Institutional exit phase — past impact window.")

    if phase == "EXPANSION":
        return ("0-1Q", "MEDIUM", "Price impact already in progress — retail momentum phase.")

    if phase == "DISTRIBUTION":
        return ("Peaking now", "MEDIUM", "Smart money reducing — likely at or near peak.")

    # ACCUM phases
    if count_up_streak >= 5:
        lag = "1-2Q"
        confidence = "HIGH"
        rationale = f"5+ quarter accumulation streak. Price impact typically 1-2Q from data release."
    elif count_up_streak >= 3:
        lag = "2-3Q"
        confidence = "HIGH"
        rationale = f"{count_up_streak}Q accumulation streak. Impact expected 2-3Q from filing disclosure."
    elif count_up_streak == 2:
        lag = "3-4Q"
        confidence = "MEDIUM"
        rationale = "2Q streak forming. Need 1 more quarter for HIGH confidence."
    else:
        lag = "4-6Q"
        confidence = "LOW"
        rationale = "Early stage — accumulation pattern not yet confirmed over multiple quarters."

    if phase == "LATE_ACCUM":
        lag = "1Q"
        confidence = "HIGH"
        rationale = "Late accumulation detected (slowing inflows). Price impact typically imminent (1Q)."

    return (lag, confidence, rationale)


# ---------------------------------------------------------------------------
# Main pipeline function
# ---------------------------------------------------------------------------

def run_phase_classification(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> int:
    """Classify all tickers for the given quarter and upsert into intelligence_scores.

    Args:
        conn: Open DuckDB connection
        quarter: e.g. "2024-Q3"

    Returns:
        Number of tickers classified.
    """
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    logger.info("Phase classification for quarter={}", quarter)

    # Load up to 8 quarters of history per ticker
    quarters_back = []
    q = quarter
    for _ in range(8):
        quarters_back.append(q)
        q = _quarter_offset(q, -1)
    quarters_placeholder = ",".join(["?" for _ in quarters_back])

    history_df = conn.execute(f"""
        SELECT
            ticker,
            current_quarter AS report_quarter,
            inst_count_current, inst_count_prior,
            inst_count_change_pct AS count_change_pct,
            shares_change_pct,
            value_change_pct,
            inst_count_change_pct >= {COUNT_UP_THRESHOLD_PCT} AS count_up
        FROM agg_qoq_changes
        WHERE current_quarter IN ({quarters_placeholder})
        ORDER BY ticker, current_quarter DESC
    """, quarters_back).fetchdf()

    if history_df.empty:
        logger.warning("No QoQ history found for quarter range ending at {}", quarter)
        return 0

    # Group by ticker
    tickers = history_df["ticker"].unique()
    rows_to_upsert = []

    for ticker in tickers:
        ticker_df = history_df[history_df["ticker"] == ticker].sort_values("report_quarter", ascending=False)
        history = ticker_df.to_dict("records")

        # Only classify for the target quarter (most recent row must be target quarter)
        if not history or history[0].get("report_quarter") != quarter:
            continue

        classification = _classify_phase(history)
        phase = classification["accum_phase"]
        streak = classification["count_up_streak"]
        lag, lag_conf, lag_rationale = _estimate_lag(history, phase, streak)

        # Determine expected_impact_quarters from lag string
        expected_q = 3  # default
        if lag.startswith("1-2"):
            expected_q = 2
        elif lag.startswith("2-3"):
            expected_q = 3
        elif lag.startswith("3-4"):
            expected_q = 4
        elif lag.startswith("4-6"):
            expected_q = 5
        elif lag in ("0-1Q", "1Q"):
            expected_q = 1

        rows_to_upsert.append((
            ticker,
            quarter,
            now_iso,
            phase,
            classification["accum_phase_quarters"],
            classification["accum_strength_score"],
            expected_q,
            lag_conf,
            lag_rationale,
        ))

    if not rows_to_upsert:
        logger.info("No tickers to classify for quarter={}", quarter)
        return 0

    conn.executemany("""
        INSERT INTO intelligence_scores (
            ticker, report_quarter, computed_at,
            accum_phase, accum_phase_quarters, accum_strength_score,
            expected_impact_quarters, lag_confidence, lag_rationale,
            cascade_stage
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT (ticker, report_quarter) DO UPDATE SET
            computed_at = excluded.computed_at,
            accum_phase = excluded.accum_phase,
            accum_phase_quarters = excluded.accum_phase_quarters,
            accum_strength_score = excluded.accum_strength_score,
            expected_impact_quarters = excluded.expected_impact_quarters,
            lag_confidence = excluded.lag_confidence,
            lag_rationale = excluded.lag_rationale
    """, rows_to_upsert)

    logger.info("Phase classification complete: {} tickers for quarter={}", len(rows_to_upsert), quarter)
    return len(rows_to_upsert)
