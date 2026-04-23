"""Related stocks & lead-lag correlation pipeline.

Phase 1: Load related company pairs from Polygon v1/related-companies
         → dim_related_companies

Phase 2: Compute rolling correlations + Granger causality between pairs
         → fact_stock_correlations

The lead-lag model identifies "VantagePoint-style" patterns:
  "When ticker_a moves strongly, does ticker_b follow in N days?"

This is used by the dashboard cascade intelligence to flag:
  "AAPL just broke out — watch MSFT, AMD, NVDA in next 3-5 days"

Usage:
    python -m signal_scanner.institutional_intel.jobs.related_stocks_loader --phase load-pairs
    python -m signal_scanner.institutional_intel.jobs.related_stocks_loader --phase correlations
    python -m signal_scanner.institutional_intel.jobs.related_stocks_loader --phase all
"""
from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests
from loguru import logger

from signal_scanner.institutional_intel.config import (
    MASSIVE_API_KEY,
    MASSIVE_BASE_URL,
    safe_duckdb_connect,
)


# ---------------------------------------------------------------------------
# Phase 1: Load related company pairs from Polygon
# ---------------------------------------------------------------------------

def load_related_companies(tickers: List[str], rps: float = 4.0) -> Dict:
    """Fetch related companies for each ticker from Polygon v1/related-companies."""
    delay = 1.0 / max(rps, 1.0)
    now_iso = datetime.now(timezone.utc).isoformat()
    all_pairs: List[tuple] = []
    errors = 0

    for i, ticker in enumerate(tickers):
        try:
            url = f"{MASSIVE_BASE_URL}/v1/related-companies/{ticker}"
            resp = requests.get(url, params={"apiKey": MASSIVE_API_KEY}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            related = data.get("results", [])
            for r in related:
                rel_ticker = (r.get("ticker") or "").upper()
                if rel_ticker and rel_ticker != ticker:
                    all_pairs.append((
                        ticker, rel_ticker, "polygon_related", "polygon", now_iso
                    ))
                    # Add reverse pair
                    all_pairs.append((
                        rel_ticker, ticker, "polygon_related", "polygon", now_iso
                    ))
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status == 429:
                logger.warning("Rate limited on related companies, sleeping 60s")
                time.sleep(60)
                continue
            errors += 1
        except Exception as exc:
            errors += 1
            logger.debug("Related companies error {}: {}", ticker, exc)

        if (i + 1) % 50 == 0:
            logger.info("  [{}/{}] done, {} pairs", i + 1, len(tickers), len(all_pairs))

        time.sleep(delay)

    if not all_pairs:
        logger.warning("[RELATED] No pairs found for {} tickers", len(tickers))
        return {"total_pairs": 0, "errors": errors}

    # Remove duplicates
    seen = set()
    unique_pairs = []
    for p in all_pairs:
        key = (p[0], p[1])
        if key not in seen:
            seen.add(key)
            unique_pairs.append(p)

    conn = safe_duckdb_connect(read_only=False)
    if conn is None:
        logger.error("[RELATED] Cannot connect to warehouse")
        return {"total_pairs": 0, "errors": errors}
    try:
        # Delete existing pairs for these tickers, then insert
        ticker_list = list({p[0] for p in unique_pairs})
        conn.execute("CREATE TEMP TABLE _rel_load AS SELECT * FROM dim_related_companies LIMIT 0")
        conn.executemany("INSERT INTO _rel_load VALUES (?, ?, ?, ?, ?)", unique_pairs)
        conn.execute("""
            DELETE FROM dim_related_companies
            WHERE (ticker, related_ticker) IN (SELECT ticker, related_ticker FROM _rel_load)
        """)
        conn.execute("INSERT INTO dim_related_companies SELECT * FROM _rel_load")
    finally:
        conn.close()

    logger.info("[RELATED] Saved {} unique pairs | {} errors", len(unique_pairs), errors)
    return {"total_pairs": len(unique_pairs), "errors": errors}


# ---------------------------------------------------------------------------
# Phase 2: Compute rolling correlations from daily prices
# ---------------------------------------------------------------------------

def compute_correlations(
    lookback_days: int = 60,
    min_correlation: float = 0.5,
    max_pairs: int = 5000,
) -> Dict:
    """Compute Pearson correlations for related stock pairs using daily returns.

    Uses fact_daily_prices for price data and dim_related_companies for pairs.
    Granger causality test (statsmodels) identifies lead-lag relationships.
    """
    conn = safe_duckdb_connect(read_only=True)
    if conn is None:
        return {"total_pairs": 0}

    try:
        since = (date.today() - timedelta(days=lookback_days + 5)).isoformat()

        # Get all related pairs that have price data
        pairs = conn.execute("""
            SELECT DISTINCT r.ticker, r.related_ticker
            FROM dim_related_companies r
            WHERE EXISTS (SELECT 1 FROM fact_daily_prices WHERE ticker = r.ticker AND trade_date >= ?)
              AND EXISTS (SELECT 1 FROM fact_daily_prices WHERE ticker = r.related_ticker AND trade_date >= ?)
            LIMIT ?
        """, [since, since, max_pairs]).fetchall()

        if not pairs:
            logger.warning("[CORRELATIONS] No related pairs with price data")
            return {"total_pairs": 0}

        logger.info("[CORRELATIONS] Computing correlations for {} pairs", len(pairs))

        # Fetch daily returns for all relevant tickers
        all_tickers = list({t for pair in pairs for t in pair})
        prices_df = conn.execute("""
            SELECT ticker, date, close
            FROM fact_daily_prices
            WHERE ticker IN ({})
              AND date >= ?
            ORDER BY ticker, date
        """.format(", ".join(f"'{t}'" for t in all_tickers)), [since]).df()
    finally:
        conn.close()

    if prices_df.empty:
        return {"total_pairs": 0}

    # Compute daily returns per ticker
    prices_df = prices_df.sort_values(["ticker", "date"])
    prices_df["ret"] = prices_df.groupby("ticker")["close"].pct_change()

    # Pivot to wide format: date × ticker
    ret_wide = prices_df.pivot(index="date", columns="ticker", values="ret").dropna(how="all")

    correlation_rows = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for ticker_a, ticker_b in pairs:
        if ticker_a not in ret_wide.columns or ticker_b not in ret_wide.columns:
            continue

        col_a = ret_wide[ticker_a].dropna()
        col_b = ret_wide[ticker_b].dropna()

        # Align to common dates
        common = col_a.index.intersection(col_b.index)
        if len(common) < 20:
            continue

        a_aligned = col_a.loc[common]
        b_aligned = col_b.loc[common]

        corr = float(a_aligned.corr(b_aligned))
        if abs(corr) < min_correlation:
            continue

        # Granger causality: does A Granger-cause B?
        granger_p = None
        try:
            import pandas as pd
            from statsmodels.tsa.stattools import grangercausalitytests
            import numpy as np
            data = pd.DataFrame({"b": b_aligned.values, "a": a_aligned.values})
            gc_result = grangercausalitytests(data, maxlag=3, verbose=False)
            # Use lag=2 F-test p-value
            granger_p = float(gc_result[2][0]["ssr_ftest"][1])
        except Exception:
            pass  # statsmodels may not be installed; skip Granger

        correlation_rows.append((
            ticker_a, ticker_b, lookback_days,
            round(corr, 4), granger_p, now_iso,
        ))

    if not correlation_rows:
        logger.info("[CORRELATIONS] No pairs exceeded correlation threshold {}", min_correlation)
        return {"total_pairs": 0}

    conn2 = safe_duckdb_connect(read_only=False)
    if conn2 is None:
        return {"total_pairs": 0}
    try:
        conn2.execute("CREATE TEMP TABLE _corr_load AS SELECT * FROM fact_stock_correlations LIMIT 0")
        conn2.executemany("INSERT INTO _corr_load VALUES (?, ?, ?, ?, ?, ?)", correlation_rows)
        conn2.execute("""
            DELETE FROM fact_stock_correlations
            WHERE (ticker_a, ticker_b, lookback_days) IN
                  (SELECT ticker_a, ticker_b, lookback_days FROM _corr_load)
        """)
        conn2.execute("INSERT INTO fact_stock_correlations SELECT * FROM _corr_load")
    finally:
        conn2.close()

    logger.info("[CORRELATIONS] Saved {} correlation pairs | lookback={}d", len(correlation_rows), lookback_days)
    return {"total_pairs": len(correlation_rows)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Related stocks & correlations")
    p.add_argument("--phase", choices=["load-pairs", "correlations", "all"], default="all")
    p.add_argument("--min-conviction", type=float, default=40)
    p.add_argument("--lookback-days", type=int, default=60)
    p.add_argument("--min-correlation", type=float, default=0.5)
    p.add_argument("--rps", type=float, default=4.0)
    args = p.parse_args()

    if args.phase in ("load-pairs", "all"):
        conn = safe_duckdb_connect(read_only=True)
        if conn:
            rows = conn.execute("""
                SELECT ticker FROM intelligence_scores
                WHERE conviction_score >= ?
                  AND ticker NOT IN ('N/A','NONE','NULL','')
                  AND LENGTH(ticker) <= 5
                ORDER BY conviction_score DESC LIMIT 300
            """, [args.min_conviction]).fetchall()
            conn.close()
            tickers = [r[0] for r in rows]
        else:
            tickers = []

        if tickers:
            result = load_related_companies(tickers, rps=args.rps)
            logger.info("Related pairs: {}", result)

    if args.phase in ("correlations", "all"):
        result = compute_correlations(
            lookback_days=args.lookback_days,
            min_correlation=args.min_correlation,
        )
        logger.info("Correlations: {}", result)


if __name__ == "__main__":
    main()
