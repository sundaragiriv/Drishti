"""Daily Idea Revalidator — computes fast status on top of slow thesis.

Three-layer model:
  Layer 1 (SLOW): Institutional thesis — conviction, phase, ML score (quarterly)
  Layer 2 (FAST): Daily trade status — ACTIVE/STRETCHED/MISSED/etc
  Layer 3 (FAST): Execution context — current entry/stop/targets from today's price

The thesis layer is NEVER mutated by daily revalidation.
Daily status is stored separately in idea_ledger.daily_status.

Thesis date: the first date the signal was observable to the system
  (when 13F filings were ingested), NOT the portfolio period end date.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from loguru import logger


# ---------------------------------------------------------------------------
# Daily status states (fast layer, not idea lifecycle states)
# ---------------------------------------------------------------------------

STATUS_ACTIVE = "ACTIVE"            # Thesis valid, price in entry zone
STATUS_RECONFIRMED = "RECONFIRMED"  # Thesis valid + fresh daily signal confirms
STATUS_STRETCHED = "STRETCHED"      # Thesis valid, price moved 5-15% in thesis direction
STATUS_MISSED = "MISSED"            # Price moved >15% in thesis direction
STATUS_INVALIDATED = "INVALIDATED"  # Stop broken or thesis conditions failed
STATUS_STALE = "STALE"              # No confirmation in 30+ days, no movement

# Priority for display sorting (lower = higher priority)
STATUS_PRIORITY = {
    STATUS_RECONFIRMED: 0,
    STATUS_ACTIVE: 1,
    STATUS_STRETCHED: 2,
    STATUS_STALE: 3,
    STATUS_MISSED: 4,
    STATUS_INVALIDATED: 5,
}

# Thresholds
STRETCHED_PCT = 8.0      # >8% move in thesis direction = stretched
MISSED_PCT = 18.0         # >18% move in thesis direction = missed
INVALIDATED_STOP_MULT = 2.5  # stop broken by 2.5x ATR = invalidated
STALE_DAYS = 30           # no fresh signal in 30 days = stale
RECONFIRM_DAYS = 5        # fresh signal within 5 trading days = reconfirmed
ACTIVE_RR_MIN = 1.5       # minimum R:R from current price to still be ACTIVE


# ---------------------------------------------------------------------------
# User-facing tier mapping (presentation layer only)
# ---------------------------------------------------------------------------

def compute_user_tier(status: str, conviction: float, current_rr: float,
                      distance_pct: float) -> str:
    """Map internal status + quality metrics to user-facing tier.

    Tiers: Platinum / Gold / Silver / Bronze / Avoid
    Internal logic states are preserved separately.
    """
    if status in (STATUS_INVALIDATED, STATUS_MISSED):
        return "Avoid"

    if status == STATUS_RECONFIRMED:
        if conviction >= 70 and current_rr >= 2.0 and abs(distance_pct) <= 5:
            return "Platinum"
        if conviction >= 60 and current_rr >= 1.5:
            return "Gold"
        return "Silver"

    if status == STATUS_ACTIVE:
        if conviction >= 70 and current_rr >= 2.5 and abs(distance_pct) <= 3:
            return "Platinum"
        if conviction >= 65 and current_rr >= 2.0 and abs(distance_pct) <= 8:
            return "Gold"
        if conviction >= 55 and current_rr >= 1.5:
            return "Silver"
        return "Bronze"

    if status == STATUS_STRETCHED:
        if conviction >= 70 and current_rr >= 2.0:
            return "Silver"
        return "Bronze"

    # STALE or unknown
    return "Bronze"


TIER_PRIORITY = {
    "Platinum": 0,
    "Gold": 1,
    "Silver": 2,
    "Bronze": 3,
    "Avoid": 4,
}


def get_thesis_observable_date(conn, quarter: str) -> Optional[str]:
    """Get the first date the market could have known about a quarter's 13F data.

    This is when the bulk of filings were ingested, not the portfolio period end.
    Returns ISO date string (e.g., '2026-03-03').
    """
    try:
        # Find the date when cumulative filings crossed 50% of total
        # Map quarter string (e.g. "2025-Q4") to report_period date
        # Quarter format: YYYY-QN → period end: Q1=03-31, Q2=06-30, Q3=09-30, Q4=12-31
        q_map = {"Q1": "-03-31", "Q2": "-06-30", "Q3": "-09-30", "Q4": "-12-31"}
        year = quarter[:4]
        q_suffix = q_map.get(quarter[-2:], "-12-31")
        period_date = f"{year}{q_suffix}"

        rows = conn.execute("""
            SELECT DATE(ingested_at) as d, COUNT(DISTINCT manager_cik) as n
            FROM fact_13f_positions
            WHERE report_period = ?
            GROUP BY DATE(ingested_at)
            ORDER BY d
        """, [period_date]).fetchall()

        if not rows:
            return None

        total = sum(r[1] for r in rows)
        cumulative = 0
        for d, n in rows:
            cumulative += n
            if cumulative >= total * 0.5:
                return str(d)
        # Fallback: first bulk ingest date
        return str(rows[0][0])
    except Exception as e:
        logger.debug(f"get_thesis_observable_date error: {e}")
        return None


def compute_daily_status(
    thesis: Dict[str, Any],
    current_price: float,
    atr: float,
    fresh_signals: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute daily trade status for a single idea.

    Args:
        thesis: {side, thesis_price, conviction, accum_phase, ...}
        current_price: latest close price
        atr: current 20-day ATR
        fresh_signals: {has_insider_buy, has_dp_spike, has_svr_spike, has_squeeze_change,
                        days_since_last_signal}

    Returns: {status, reason, current_entry, current_stop, current_t1, current_t2,
              current_rr, distance_pct, thesis_price}
    """
    side = thesis.get("side", "LONG")
    thesis_price = float(thesis.get("thesis_price", 0))
    if thesis_price <= 0 or current_price <= 0 or atr <= 0:
        return _build_result(STATUS_STALE, "missing data", side, current_price, atr, thesis_price)

    # Distance from thesis entry (side-aware)
    if side == "LONG":
        distance_pct = (current_price - thesis_price) / thesis_price * 100
        stop_broken = current_price < thesis_price - INVALIDATED_STOP_MULT * atr
    else:  # SHORT
        distance_pct = (thesis_price - current_price) / thesis_price * 100
        stop_broken = current_price > thesis_price + INVALIDATED_STOP_MULT * atr

    # Compute current execution context (side-aware)
    exec_ctx = _compute_execution_context(side, current_price, atr)

    # --- Status determination (ordered by severity) ---

    # 1. INVALIDATED: stop clearly broken
    if stop_broken:
        return _build_result(
            STATUS_INVALIDATED,
            f"stop broken ({distance_pct:+.1f}% from thesis)",
            side, current_price, atr, thesis_price, exec_ctx,
        )

    # 2. MISSED: large move in thesis direction already happened
    if distance_pct > MISSED_PCT:
        return _build_result(
            STATUS_MISSED,
            f"moved {distance_pct:+.1f}% in thesis direction",
            side, current_price, atr, thesis_price, exec_ctx,
        )

    # 3. RECONFIRMED: fresh daily signal within last N days
    has_fresh = (
        fresh_signals.get("has_insider_buy")
        or fresh_signals.get("has_dp_spike")
        or fresh_signals.get("has_svr_spike")
        or fresh_signals.get("has_squeeze_change")
    )
    days_since = fresh_signals.get("days_since_last_signal", 999)
    if has_fresh and days_since <= RECONFIRM_DAYS:
        reasons = []
        if fresh_signals.get("has_insider_buy"):
            reasons.append("insider buy")
        if fresh_signals.get("has_dp_spike"):
            reasons.append("dark pool spike")
        if fresh_signals.get("has_svr_spike"):
            reasons.append("short volume spike")
        if fresh_signals.get("has_squeeze_change"):
            reasons.append("squeeze intensified")
        return _build_result(
            STATUS_RECONFIRMED,
            " + ".join(reasons),
            side, current_price, atr, thesis_price, exec_ctx,
        )

    # 4. STRETCHED: moderate move in thesis direction
    if distance_pct > STRETCHED_PCT:
        return _build_result(
            STATUS_STRETCHED,
            f"{distance_pct:+.1f}% from thesis — wait for pullback",
            side, current_price, atr, thesis_price, exec_ctx,
        )

    # 5. STALE: no fresh signals for a long time
    if days_since > STALE_DAYS:
        return _build_result(
            STATUS_STALE,
            f"no fresh signals in {days_since} days",
            side, current_price, atr, thesis_price, exec_ctx,
        )

    # 6. ACTIVE: thesis valid, price in zone, R:R acceptable
    # Check if current R:R is still attractive
    current_rr = exec_ctx.get("current_rr", 0)
    if current_rr < ACTIVE_RR_MIN and abs(distance_pct) < 3:
        return _build_result(
            STATUS_ACTIVE,
            f"in zone (R:R {current_rr:.1f})",
            side, current_price, atr, thesis_price, exec_ctx,
        )

    return _build_result(
        STATUS_ACTIVE,
        f"in entry zone ({distance_pct:+.1f}%)",
        side, current_price, atr, thesis_price, exec_ctx,
    )


def _compute_execution_context(side: str, price: float, atr: float) -> dict:
    """Compute current-price execution levels."""
    if side == "LONG":
        stop = round(price - 1.5 * atr, 2)
        risk = price - stop
        t1 = round(price + 2.5 * risk, 2)
        t2 = round(price + 4.0 * risk, 2)
    else:
        stop = round(price + 1.5 * atr, 2)
        risk = stop - price
        t1 = round(price - 2.5 * risk, 2)
        t2 = round(price - 4.0 * risk, 2)

    rr = round(abs(t1 - price) / risk, 1) if risk > 0 else 0
    return {
        "current_entry": round(price, 2),
        "current_stop": stop,
        "current_t1": t1,
        "current_t2": t2,
        "current_rr": rr,
    }


def _build_result(
    status: str, reason: str, side: str,
    current_price: float, atr: float, thesis_price: float,
    exec_ctx: dict = None,
) -> dict:
    if exec_ctx is None:
        exec_ctx = _compute_execution_context(side, current_price, atr)

    if side == "LONG":
        distance_pct = (current_price - thesis_price) / thesis_price * 100 if thesis_price else 0
    else:
        distance_pct = (thesis_price - current_price) / thesis_price * 100 if thesis_price else 0

    return {
        "status": status,
        "reason": reason,
        "thesis_price": round(thesis_price, 2),
        "distance_pct": round(distance_pct, 1),
        **exec_ctx,
    }


# ---------------------------------------------------------------------------
# Batch revalidation (runs in EOD pipeline or on Sniper Board load)
# ---------------------------------------------------------------------------

def revalidate_all_ideas(conn_duckdb, ideas: List[Dict]) -> List[Dict]:
    """Revalidate all alive ideas with current prices + fresh signals.

    Args:
        conn_duckdb: DuckDB read-only connection
        ideas: list of idea dicts from _load_sniper_ideas or idea_ledger

    Returns: ideas enriched with daily_status fields
    """
    if not ideas or not conn_duckdb:
        return ideas

    tickers = list({i.get("symbol", "") for i in ideas if i.get("symbol")})
    if not tickers:
        return ideas

    # Get thesis observable date
    from signal_scanner.institutional_intel.config import get_active_quarter
    quarter = get_active_quarter(conn_duckdb)
    thesis_date = get_thesis_observable_date(conn_duckdb, quarter)
    if not thesis_date:
        thesis_date = "2026-03-03"  # fallback

    # Load current prices + ATR
    price_data = _load_prices_and_atr(conn_duckdb, tickers)

    # Load thesis prices (price on thesis observable date)
    thesis_prices = _load_thesis_prices(conn_duckdb, tickers, thesis_date)

    # Load fresh signals (last 5 trading days)
    fresh = _load_fresh_signals(conn_duckdb, tickers)

    # Compute status for each idea
    for idea in ideas:
        ticker = idea.get("symbol", "")
        side = idea.get("side", "LONG")
        pd_ = price_data.get(ticker, {})
        current_price = pd_.get("close", 0)
        atr = pd_.get("atr", 0)
        tp = thesis_prices.get(ticker, current_price)

        thesis = {
            "side": side,
            "thesis_price": tp,
            "conviction": idea.get("_conviction", 0),
            "accum_phase": idea.get("_phase", ""),
        }
        signals = fresh.get(ticker, {})

        result = compute_daily_status(thesis, current_price, atr, signals)

        # Enrich idea with daily status (DOES NOT mutate thesis fields)
        idea["daily_status"] = result["status"]
        idea["status_reason"] = result["reason"]
        idea["thesis_price"] = result["thesis_price"]
        idea["distance_pct"] = result["distance_pct"]
        idea["current_price"] = result["current_entry"]
        idea["current_stop"] = result["current_stop"]
        idea["current_t1"] = result["current_t1"]
        idea["current_t2"] = result["current_t2"]
        idea["current_rr"] = result["current_rr"]
        # Override stale price fields with current execution context
        idea["entry_price"] = result["thesis_price"]
        idea["stop_price"] = result["current_stop"]
        idea["target_1"] = result["current_t1"]
        idea["target_2"] = result["current_t2"]
        idea["rr_ratio"] = result["current_rr"]

        # User-facing tier (presentation layer — internal status preserved)
        conv = idea.get("_conviction", 0) or 0
        idea["tier"] = compute_user_tier(
            result["status"], conv, result["current_rr"], result["distance_pct"],
        )

        # Options expression available flag (from DuckDB fact_options_contracts)
        idea["options_available"] = ""  # populated below

        # Predictive placeholders (Phase D)
        idea["pred_5d"] = ""
        idea["pred_conf"] = ""

    # Check options expression availability from fact_options_contracts (DuckDB)
    try:
        opt_symbols = set(
            r[0] for r in conn_duckdb.execute(
                "SELECT DISTINCT underlying FROM fact_options_contracts "
                "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM fact_options_contracts) "
                "AND open_interest >= 100"
            ).fetchall()
        )
        for idea in ideas:
            if idea.get("symbol") in opt_symbols:
                idea["options_available"] = "Yes"
    except Exception:
        pass

    return ideas


def _load_prices_and_atr(conn, tickers: list) -> Dict[str, dict]:
    """Load latest close + 20-day ATR for tickers."""
    placeholders = ",".join(["?"] * len(tickers))
    try:
        rows = conn.execute(f"""
            WITH latest AS (
                SELECT ticker, close, high, low, trade_date,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) as rn
                FROM fact_daily_prices
                WHERE ticker IN ({placeholders})
            ),
            atr_data AS (
                SELECT ticker, AVG(high - low) as atr_20
                FROM (
                    SELECT ticker, high, low,
                           ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) as rn
                    FROM fact_daily_prices
                    WHERE ticker IN ({placeholders})
                ) sub
                WHERE rn <= 20
                GROUP BY ticker
            )
            SELECT l.ticker, l.close, a.atr_20
            FROM latest l
            JOIN atr_data a ON l.ticker = a.ticker
            WHERE l.rn = 1
        """, tickers + tickers).fetchall()
        return {r[0]: {"close": float(r[1]), "atr": float(r[2] or 0)} for r in rows}
    except Exception as e:
        logger.warning(f"Price/ATR load error: {e}")
        return {}


def _load_thesis_prices(conn, tickers: list, thesis_date: str) -> Dict[str, float]:
    """Load closing prices on or near the thesis observable date."""
    placeholders = ",".join(["?"] * len(tickers))
    try:
        rows = conn.execute(f"""
            SELECT DISTINCT ON (ticker) ticker, close
            FROM fact_daily_prices
            WHERE ticker IN ({placeholders})
              AND trade_date <= ?
            ORDER BY ticker, trade_date DESC
        """, tickers + [thesis_date]).fetchall()
        return {r[0]: float(r[1]) for r in rows}
    except Exception as e:
        logger.warning(f"Thesis price load error: {e}")
        return {}


def _load_fresh_signals(conn, tickers: list) -> Dict[str, dict]:
    """Load fresh confirmation signals from last 5 trading days."""
    placeholders = ",".join(["?"] * len(tickers))
    result: Dict[str, dict] = {t: {
        "has_insider_buy": False,
        "has_dp_spike": False,
        "has_svr_spike": False,
        "has_squeeze_change": False,
        "days_since_last_signal": 999,
    } for t in tickers}

    # Form 4 insider buys (last 5 trading days)
    try:
        f4 = conn.execute(f"""
            SELECT DISTINCT ticker
            FROM fact_form4_transactions
            WHERE ticker IN ({placeholders})
              AND transaction_date >= CURRENT_DATE - INTERVAL '7' DAY
              AND transaction_code = 'P'
        """, tickers).fetchall()
        for r in f4:
            if r[0] in result:
                result[r[0]]["has_insider_buy"] = True
                result[r[0]]["days_since_last_signal"] = 0
    except Exception as e:
        logger.debug(f"F4 fresh signal check: {e}")

    # Dark pool spike (DP% > 45% in last 5 days)
    try:
        dp = conn.execute(f"""
            SELECT DISTINCT ticker
            FROM fact_dark_pool_daily
            WHERE ticker IN ({placeholders})
              AND trade_date >= CURRENT_DATE - INTERVAL '7' DAY
              AND dark_pool_pct > 45
        """, tickers).fetchall()
        for r in dp:
            if r[0] in result:
                result[r[0]]["has_dp_spike"] = True
                result[r[0]]["days_since_last_signal"] = min(
                    result[r[0]]["days_since_last_signal"], 3
                )
    except Exception as e:
        logger.debug(f"DP fresh signal check: {e}")

    # Short volume spike (SVR > 50% in last 5 days)
    try:
        sv = conn.execute(f"""
            SELECT DISTINCT ticker
            FROM fact_short_volume
            WHERE ticker IN ({placeholders})
              AND trade_date >= CURRENT_DATE - INTERVAL '7' DAY
              AND short_volume_ratio > 0.50
        """, tickers).fetchall()
        for r in sv:
            if r[0] in result:
                result[r[0]]["has_svr_spike"] = True
                result[r[0]]["days_since_last_signal"] = min(
                    result[r[0]]["days_since_last_signal"], 3
                )
    except Exception as e:
        logger.debug(f"SVR fresh signal check: {e}")

    return result
