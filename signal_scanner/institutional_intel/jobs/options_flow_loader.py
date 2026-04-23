"""Options Flow data loader using Polygon v3/snapshot/options.

Fetches daily options snapshot for each ticker and aggregates:
  - Call/put volume and open interest
  - Put/call ratios (volume + OI)
  - Average implied volatility by side
  - Max OI strike per side (gamma/put wall)
  - Unusual activity flags (vol > 3x 20-day avg)

Usage:
    python -m signal_scanner.institutional_intel.jobs.options_flow_loader
    python -m signal_scanner.institutional_intel.jobs.options_flow_loader --tickers AAPL,TSLA,NVDA
    python -m signal_scanner.institutional_intel.jobs.options_flow_loader --min-conviction 50
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

import duckdb
import requests
from loguru import logger

from signal_scanner.institutional_intel.config import (
    MASSIVE_API_KEY,
    MASSIVE_BASE_URL,
    WAREHOUSE_PATH,
    safe_duckdb_connect,
)
from signal_scanner.core.readiness import latest_complete_trading_day


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _api_key() -> str:
    key = MASSIVE_API_KEY or os.environ.get("MASSIVE_API_KEY", "")
    if not key:
        raise ValueError("MASSIVE_API_KEY not set")
    return key


def _fetch_options_snapshot(ticker: str, exp_gte: str, exp_lte: str) -> List[Dict]:
    """Fetch all options contracts for a ticker within expiration window."""
    url = f"{MASSIVE_BASE_URL}/v3/snapshot/options/{ticker}"
    params = {
        "apiKey": _api_key(),
        "expiration_date.gte": exp_gte,
        "expiration_date.lte": exp_lte,
        "limit": "250",
    }
    all_results: List[Dict] = []
    pages = 0
    while url and pages < 80:
        try:
            resp = requests.get(url, params=params if pages == 0 else None, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            all_results.extend(data.get("results", []))
            pages += 1
            url = data.get("next_url")
            if url:
                url = url + f"&apiKey={_api_key()}"
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status == 429:
                logger.warning("Rate limited on options snapshot, sleeping 60s")
                time.sleep(60)
                continue
            logger.debug("Options snapshot {} HTTP {}: {}", ticker, status, exc)
            break
        except Exception as exc:
            logger.debug("Options snapshot {} error: {}", ticker, exc)
            break
    return all_results


def _aggregate_contracts(contracts: List[Dict], snapshot_date: str) -> Optional[Dict]:
    """Aggregate raw option contracts into daily flow metrics."""
    if not contracts:
        return None

    call_vol = 0
    put_vol = 0
    call_oi = 0
    put_oi = 0
    call_iv_sum = 0.0
    call_iv_n = 0
    put_iv_sum = 0.0
    put_iv_n = 0
    call_oi_by_strike: Dict[float, int] = {}
    put_oi_by_strike: Dict[float, int] = {}

    for c in contracts:
        details = c.get("details", {}) or {}
        day = c.get("day", {}) or {}
        greeks = c.get("greeks", {}) or {}

        ctype = details.get("contract_type", "").lower()
        strike = details.get("strike_price") or 0.0
        vol = int(day.get("volume") or 0)
        oi = int(c.get("open_interest") or 0)
        iv = float(greeks.get("theta") and c.get("implied_volatility") or 0)  # use IV field if available

        # implied_volatility is at top level or in details
        iv_val = float(c.get("implied_volatility") or greeks.get("implied_volatility") or 0)

        if ctype == "call":
            call_vol += vol
            call_oi += oi
            if iv_val > 0:
                call_iv_sum += iv_val
                call_iv_n += 1
            if strike > 0:
                call_oi_by_strike[strike] = call_oi_by_strike.get(strike, 0) + oi
        elif ctype == "put":
            put_vol += vol
            put_oi += oi
            if iv_val > 0:
                put_iv_sum += iv_val
                put_iv_n += 1
            if strike > 0:
                put_oi_by_strike[strike] = put_oi_by_strike.get(strike, 0) + oi

    put_call_vol = round(put_vol / call_vol, 3) if call_vol > 0 else None
    put_call_oi = round(put_oi / call_oi, 3) if call_oi > 0 else None
    avg_call_iv = round(call_iv_sum / call_iv_n, 4) if call_iv_n > 0 else None
    avg_put_iv = round(put_iv_sum / put_iv_n, 4) if put_iv_n > 0 else None
    max_call_strike = max(call_oi_by_strike, key=call_oi_by_strike.get) if call_oi_by_strike else None
    max_put_strike = max(put_oi_by_strike, key=put_oi_by_strike.get) if put_oi_by_strike else None

    return {
        "call_volume": call_vol,
        "put_volume": put_vol,
        "call_oi": call_oi,
        "put_oi": put_oi,
        "put_call_ratio_vol": put_call_vol,
        "put_call_ratio_oi": put_call_oi,
        "avg_call_iv": avg_call_iv,
        "avg_put_iv": avg_put_iv,
        "max_call_oi_strike": max_call_strike,
        "max_put_oi_strike": max_put_strike,
    }


# ---------------------------------------------------------------------------
# Main loader
# ---------------------------------------------------------------------------

def load_options_flow(
    tickers: List[str],
    snapshot_date: Optional[date] = None,
    exp_weeks_ahead: int = 8,
    rps: float = 3.0,
) -> Dict:
    """Load options flow snapshot for given tickers and store in fact_options_flow."""
    tickers = list(dict.fromkeys(t.upper() for t in tickers if t))
    snap_date = snapshot_date or latest_complete_trading_day()
    snap_str = snap_date.isoformat()

    from datetime import timedelta
    exp_gte = snap_str
    exp_lte = (snap_date + timedelta(weeks=exp_weeks_ahead)).isoformat()

    logger.info("[OPTIONS FLOW] {} tickers | exp window {} to {}", len(tickers), exp_gte, exp_lte)

    rows = []
    now_iso = datetime.now(timezone.utc).isoformat()
    delay = 1.0 / max(rps, 1.0)
    errors = 0

    for i, ticker in enumerate(tickers):
        try:
            contracts = _fetch_options_snapshot(ticker, exp_gte, exp_lte)
            if contracts:
                agg = _aggregate_contracts(contracts, snap_str)
                if agg and (agg["call_volume"] > 0 or agg["put_volume"] > 0):
                    rows.append((
                        ticker, snap_str,
                        agg["call_volume"], agg["put_volume"],
                        agg["call_oi"], agg["put_oi"],
                        agg["put_call_ratio_vol"], agg["put_call_ratio_oi"],
                        agg["avg_call_iv"], agg["avg_put_iv"],
                        agg["max_call_oi_strike"], agg["max_put_oi_strike"],
                        False, False,  # unusual flags (computed later)
                        "polygon_snapshot", now_iso,
                    ))
        except Exception as exc:
            errors += 1
            logger.debug("Options flow error {}: {}", ticker, exc)

        if (i + 1) % 20 == 0:
            logger.info("  [{}/{}] done, {} rows", i + 1, len(tickers), len(rows))

        time.sleep(delay)

    if not rows:
        logger.warning("[OPTIONS FLOW] No data for {} tickers", len(tickers))
        return {"total_rows": 0, "errors": errors}

    # Upsert into fact_options_flow
    conn = safe_duckdb_connect(read_only=False)
    if conn is None:
        logger.error("[OPTIONS FLOW] Cannot connect to warehouse")
        return {"total_rows": 0, "errors": errors}
    try:
        conn.execute("CREATE TEMP TABLE _of_load AS SELECT * FROM fact_options_flow LIMIT 0")
        conn.executemany("INSERT INTO _of_load VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.execute("""
            DELETE FROM fact_options_flow
            WHERE (ticker, snapshot_date) IN (SELECT ticker, snapshot_date::DATE FROM _of_load)
        """)
        conn.execute("""
            INSERT INTO fact_options_flow
            SELECT ticker, snapshot_date::DATE, call_volume, put_volume,
                   call_oi, put_oi, put_call_ratio_vol, put_call_ratio_oi,
                   avg_call_iv, avg_put_iv, max_call_oi_strike, max_put_oi_strike,
                   unusual_call_flag, unusual_put_flag, source, ingested_at::TIMESTAMP
            FROM _of_load
        """)
    finally:
        conn.close()

    logger.info("[OPTIONS FLOW] Saved {} rows for {} tickers | {} errors",
                len(rows), len(tickers), errors)
    return {"total_rows": len(rows), "errors": errors}


def get_options_tickers(min_conviction: float = 40) -> List[str]:
    """Get top-conviction tickers for options flow (capped at 500)."""
    conn = safe_duckdb_connect(read_only=True)
    if conn is None:
        return []
    try:
        rows = conn.execute("""
            SELECT DISTINCT ticker FROM intelligence_scores
            WHERE conviction_score >= ?
              AND accum_phase IN ('ACTIVE_ACCUM', 'LATE_ACCUM', 'EARLY_ACCUM')
              AND ticker NOT IN ('N/A','NONE','NULL','')
              AND LENGTH(ticker) <= 5
            ORDER BY conviction_score DESC
            LIMIT 500
        """, [min_conviction]).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Options flow loader")
    p.add_argument("--tickers", default="", help="Comma-separated tickers (default: top conviction)")
    p.add_argument("--min-conviction", type=float, default=40)
    p.add_argument("--exp-weeks", type=int, default=8, help="Expiration window weeks ahead")
    p.add_argument("--rps", type=float, default=3.0)
    args = p.parse_args()

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = get_options_tickers(min_conviction=args.min_conviction)

    if not tickers:
        logger.warning("No tickers to load")
        return

    logger.info("Loading options flow for {} tickers", len(tickers))
    result = load_options_flow(tickers, exp_weeks_ahead=args.exp_weeks, rps=args.rps)
    logger.info("Complete: {}", result)


if __name__ == "__main__":
    main()
