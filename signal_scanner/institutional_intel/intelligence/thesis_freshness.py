"""Thesis Freshness — dark-pool + short-volume activity scoring.

Scores how "alive" a quarterly thesis is based on recent daily activity.
A high score means institutions are still active in this name.
A low score means the Dec 31 thesis may be stale / already played out.

Used as:
  - Swing Snipers ranking boost/demotion
  - ISR "Why Now" evidence
  - Market Drivers context

Not a hard gate initially — a continuous score (0-100) that influences ranking.

Usage:
    from thesis_freshness import compute_freshness_scores
    scores = compute_freshness_scores(conn, tickers)
"""

from __future__ import annotations

from typing import Any, Dict, List

from loguru import logger


def compute_freshness_scores(
    conn,
    tickers: List[str] = None,
    lookback_days: int = 30,
    recent_days: int = 5,
) -> Dict[str, Dict[str, Any]]:
    """Compute thesis freshness for tickers based on recent dark-pool + short-volume activity.

    Compares last N days of activity against the 30-day baseline.
    Returns dict of {ticker: {freshness_score, dp_ratio, svr_ratio, verdict, ...}}.
    """
    if not tickers:
        return {}

    placeholders = ",".join(["?"] * len(tickers))
    results = {}

    try:
        # Dark pool: recent vs baseline
        dp_rows = conn.execute(f"""
            SELECT
                ticker,
                AVG(dark_pool_pct) as avg_all,
                AVG(CASE WHEN trade_date >= CURRENT_DATE - INTERVAL '{recent_days}' DAY
                    THEN dark_pool_pct END) as avg_recent,
                AVG(CASE WHEN trade_date < CURRENT_DATE - INTERVAL '{recent_days}' DAY
                    THEN dark_pool_pct END) as avg_baseline,
                MAX(dark_pool_pct) as max_recent_dp
            FROM fact_dark_pool_daily
            WHERE ticker IN ({placeholders})
              AND trade_date >= CURRENT_DATE - INTERVAL '{lookback_days}' DAY
            GROUP BY ticker
        """, tickers).fetchall()

        dp_map = {}
        for r in dp_rows:
            dp_map[r[0]] = {
                "dp_avg": r[1] or 0,
                "dp_recent": r[2] or 0,
                "dp_baseline": r[3] or r[1] or 1,
                "dp_max_recent": r[4] or 0,
            }

        # Short volume: recent vs baseline
        sv_rows = conn.execute(f"""
            SELECT
                ticker,
                AVG(short_volume_ratio) as avg_all,
                AVG(CASE WHEN trade_date >= CURRENT_DATE - INTERVAL '{recent_days}' DAY
                    THEN short_volume_ratio END) as avg_recent,
                AVG(CASE WHEN trade_date < CURRENT_DATE - INTERVAL '{recent_days}' DAY
                    THEN short_volume_ratio END) as avg_baseline
            FROM fact_short_volume
            WHERE ticker IN ({placeholders})
              AND trade_date >= CURRENT_DATE - INTERVAL '{lookback_days}' DAY
            GROUP BY ticker
        """, tickers).fetchall()

        sv_map = {}
        for r in sv_rows:
            sv_map[r[0]] = {
                "svr_avg": r[1] or 0,
                "svr_recent": r[2] or 0,
                "svr_baseline": r[3] or r[1] or 1,
            }

        # Recent insider buys (Form 4)
        f4_rows = conn.execute(f"""
            SELECT ticker, COUNT(*) as buy_count
            FROM fact_form4_transactions
            WHERE ticker IN ({placeholders})
              AND transaction_code = 'P'
              AND transaction_date >= CURRENT_DATE - INTERVAL '{recent_days}' DAY
            GROUP BY ticker
        """, tickers).fetchall()

        f4_map = {r[0]: r[1] for r in f4_rows}

        # Compute freshness score per ticker
        for ticker in tickers:
            dp = dp_map.get(ticker, {})
            sv = sv_map.get(ticker, {})
            insider_buys = f4_map.get(ticker, 0)

            # DP ratio: recent / baseline (>1 = more active, <1 = less active)
            dp_baseline = dp.get("dp_baseline", 1)
            dp_ratio = dp.get("dp_recent", 0) / dp_baseline if dp_baseline > 0 else 0

            # SVR ratio: recent / baseline
            svr_baseline = sv.get("svr_baseline", 1)
            svr_ratio = sv.get("svr_recent", 0) / svr_baseline if svr_baseline > 0 else 0

            # Freshness score (0-100)
            # Higher = thesis more likely still active
            dp_score = min(40, dp_ratio * 35)  # DP activity vs baseline (0-40)
            svr_score = min(25, svr_ratio * 20)  # Short volume activity (0-25)
            insider_score = min(20, insider_buys * 10)  # Recent insider buys (0-20)
            base_score = 15  # Base assumption: thesis has some residual value

            freshness = round(min(100, dp_score + svr_score + insider_score + base_score), 1)

            # Verdict
            if freshness >= 70:
                verdict = "CONFIRMED"
            elif freshness >= 50:
                verdict = "ACTIVE"
            elif freshness >= 30:
                verdict = "FADING"
            else:
                verdict = "STALE"

            results[ticker] = {
                "freshness_score": freshness,
                "dp_ratio": round(dp_ratio, 2),
                "svr_ratio": round(svr_ratio, 2),
                "insider_buys_5d": insider_buys,
                "dp_recent": round(dp.get("dp_recent", 0), 1),
                "dp_baseline": round(dp_baseline, 1),
                "verdict": verdict,
            }

    except Exception as e:
        logger.warning("Thesis freshness error: {}", e)

    return results


def enrich_ideas_with_freshness(conn, ideas: list) -> list:
    """Add freshness scores to Swing Snipers idea list.

    Modifies ideas in place, adding:
      - thesis_freshness (0-100)
      - freshness_verdict (CONFIRMED/ACTIVE/FADING/STALE)
    """
    tickers = [i.get("symbol", "") for i in ideas if i.get("symbol")]
    if not tickers:
        return ideas

    scores = compute_freshness_scores(conn, tickers)

    for idea in ideas:
        ticker = idea.get("symbol", "")
        fs = scores.get(ticker, {})
        idea["thesis_freshness"] = fs.get("freshness_score", 0)
        idea["freshness_verdict"] = fs.get("verdict", "UNKNOWN")

    return ideas
