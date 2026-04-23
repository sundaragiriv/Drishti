"""Insider Outcome Engine — Historical Pattern Analysis for Form 4 Transactions.

Based on the "Insider Effect" model (Cohen-Malloy-Pomorski, J. Finance 2012):
- Classifies insider trades as ROUTINE vs OPPORTUNISTIC
- Computes forward returns at T+5/21/63/126 trading days
- Builds statistical profiles per ticker/role/size
- Generates predictive scores for new insider transactions

Key academic findings coded into this engine:
- Opportunistic trades yield 82 bps/month abnormal returns (routine: ~0)
- Open-market purchases only (code "P") — awards/exercises are noise
- Cluster buys (3+ insiders in tight window) = strongest signal
- Purchase size relative to market cap matters
- Directors often more predictive than C-suite for opportunistic trades

Usage:
    python -m signal_scanner.institutional_intel.intelligence.insider_outcome_engine --build
    python -m signal_scanner.institutional_intel.intelligence.insider_outcome_engine --patterns
    python -m signal_scanner.institutional_intel.intelligence.insider_outcome_engine --score --quarter 2025-Q3
    python -m signal_scanner.institutional_intel.intelligence.insider_outcome_engine --summary
"""

from __future__ import annotations

import argparse
import calendar
from datetime import datetime, timedelta

import duckdb
import numpy as np
from loguru import logger


# ─── Constants ────────────────────────────────────────────────────────────────

OPEN_MARKET_BUY = "P"          # Form 4 code for open-market purchase
MIN_PATTERN_SAMPLES = 5        # Minimum transactions for reliable pattern
DATA_START_DATE = "2016-01-01" # Earliest date with reliable price + Form 4 data

# Role classification: SEC Form 4 uses relationship categories,
# not specific titles. "Officer" = any C-suite/VP/officer.
# Priority: Officer > Director > 10% Owner > Other

# Size buckets (descending order for correct classification)
_SIZE_BUCKETS = [
    ("MEGA",   1_000_000),  # $1M+
    ("LARGE",    250_000),  # $250K-$1M
    ("MEDIUM",    50_000),  # $50K-$250K
    ("SMALL",         0),   # < $50K
]


# ─── Table Management ─────────────────────────────────────────────────────────

def _ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Create outcome and pattern tables if they don't exist."""

    conn.execute("""
        CREATE TABLE IF NOT EXISTS fact_insider_outcomes (
            ticker              TEXT NOT NULL,
            transaction_date    DATE NOT NULL,
            insider_name        TEXT NOT NULL,
            insider_role        TEXT,
            role_category       TEXT,
            transaction_code    TEXT,
            shares              DOUBLE,
            txn_price           DOUBLE,
            dollar_value        DOUBLE,
            size_category       TEXT,
            is_routine          BOOLEAN DEFAULT FALSE,
            entry_close         DOUBLE,
            close_t5            DOUBLE,
            close_t21           DOUBLE,
            close_t63           DOUBLE,
            close_t126          DOUBLE,
            return_5d           DOUBLE,
            return_30d          DOUBLE,
            return_90d          DOUBLE,
            return_180d         DOUBLE,
            spy_return_5d       DOUBLE,
            spy_return_30d      DOUBLE,
            spy_return_90d      DOUBLE,
            spy_return_180d     DOUBLE,
            alpha_5d            DOUBLE,
            alpha_30d           DOUBLE,
            alpha_90d           DOUBLE,
            alpha_180d          DOUBLE,
            computed_at         TIMESTAMP NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS agg_insider_patterns (
            ticker              TEXT NOT NULL,
            pattern_type        TEXT NOT NULL,
            role_category       TEXT DEFAULT 'ALL',
            size_category       TEXT DEFAULT 'ALL',
            sample_count        INTEGER DEFAULT 0,
            win_rate_5d         DOUBLE,
            win_rate_30d        DOUBLE,
            win_rate_90d        DOUBLE,
            win_rate_180d       DOUBLE,
            alpha_win_5d        DOUBLE,
            alpha_win_30d       DOUBLE,
            alpha_win_90d       DOUBLE,
            alpha_win_180d      DOUBLE,
            mean_return_5d      DOUBLE,
            mean_return_30d     DOUBLE,
            mean_return_90d     DOUBLE,
            mean_return_180d    DOUBLE,
            mean_alpha_5d       DOUBLE,
            mean_alpha_30d      DOUBLE,
            mean_alpha_90d      DOUBLE,
            mean_alpha_180d     DOUBLE,
            median_return_90d   DOUBLE,
            insider_effect_score DOUBLE DEFAULT 0,
            computed_at         TIMESTAMP NOT NULL,
            PRIMARY KEY (ticker, pattern_type, role_category, size_category)
        )
    """)

    # Add new columns to intelligence_scores
    for col_name, col_type in [
        ("insider_effect_score", "DOUBLE DEFAULT 0"),
        ("insider_hist_win_rate", "DOUBLE"),
        ("insider_hist_alpha", "DOUBLE"),
        ("insider_pattern_samples", "INTEGER DEFAULT 0"),
        ("trend_score", "DOUBLE DEFAULT 0"),
        ("institutional_pressure", "DOUBLE DEFAULT 0"),
    ]:
        try:
            conn.execute(
                f"ALTER TABLE intelligence_scores ADD COLUMN {col_name} {col_type}"
            )
        except Exception:
            pass  # Column already exists


# ─── Step 1: Build Outcomes ───────────────────────────────────────────────────

def build_insider_outcomes(conn: duckdb.DuckDBPyConnection) -> int:
    """Build fact_insider_outcomes: join Form 4 open-market purchases with
    forward price returns at T+5/21/63/126 trading days.

    Uses DuckDB LEAD() window functions for efficient forward-looking prices,
    and ASOF JOIN to map transaction dates to nearest trading days.

    Returns number of outcome rows created.
    """
    logger.info("Building insider outcomes table...")
    _ensure_tables(conn)

    conn.execute("DELETE FROM fact_insider_outcomes")

    # One-shot SQL: LEAD gives N-th trading day ahead, ASOF maps dates
    conn.execute("""
        WITH
        -- Stock prices with forward lookahead (LEAD = trading days ahead)
        stock_fwd AS (
            SELECT
                ticker, trade_date, close,
                LEAD(close, 5)   OVER w AS close_t5,
                LEAD(close, 21)  OVER w AS close_t21,
                LEAD(close, 63)  OVER w AS close_t63,
                LEAD(close, 126) OVER w AS close_t126
            FROM fact_daily_prices
            WHERE close > 0
            WINDOW w AS (PARTITION BY ticker ORDER BY trade_date)
        ),
        -- SPY benchmark with same forward lookahead
        spy_fwd AS (
            SELECT
                trade_date,
                close AS spy_close,
                LEAD(close, 5)   OVER (ORDER BY trade_date) AS spy_t5,
                LEAD(close, 21)  OVER (ORDER BY trade_date) AS spy_t21,
                LEAD(close, 63)  OVER (ORDER BY trade_date) AS spy_t63,
                LEAD(close, 126) OVER (ORDER BY trade_date) AS spy_t126
            FROM fact_daily_prices
            WHERE ticker = 'SPY' AND close > 0
        ),
        -- Pre-join stock prices with SPY (avoids double ASOF)
        stock_spy AS (
            SELECT
                sf.ticker, sf.trade_date, sf.close,
                sf.close_t5, sf.close_t21, sf.close_t63, sf.close_t126,
                spf.spy_close, spf.spy_t5, spf.spy_t21, spf.spy_t63, spf.spy_t126
            FROM stock_fwd sf
            LEFT JOIN spy_fwd spf ON sf.trade_date = spf.trade_date
        ),
        -- Clean open-market purchases only
        buys AS (
            SELECT
                ticker, transaction_date, insider_name, insider_role,
                transaction_code, shares, price AS txn_price,
                shares * COALESCE(price, 0) AS dollar_value
            FROM fact_form4_transactions
            WHERE transaction_code = 'P'
              AND price > 0 AND shares > 0
              AND ticker NOT IN ('N/A', 'NONE', 'NULL', '')
              AND LENGTH(ticker) <= 5
              AND transaction_date >= '2016-01-01'
              AND transaction_date <= CURRENT_DATE
        ),
        -- ASOF JOIN: map each buy to nearest prior trading day
        joined AS (
            SELECT
                b.ticker, b.transaction_date, b.insider_name, b.insider_role,
                b.transaction_code, b.shares, b.txn_price, b.dollar_value,
                ss.close AS entry_close,
                ss.close_t5, ss.close_t21, ss.close_t63, ss.close_t126,
                -- Stock returns (%), clamped to [-100, +500] to prevent outlier contamination
                -- NULL-safe: only clamp when forward price exists, preserve NULL otherwise
                CASE WHEN ss.close > 0 AND ss.close_t5 IS NOT NULL
                     THEN LEAST(500, GREATEST(-100, (ss.close_t5 - ss.close) / ss.close * 100))
                END AS return_5d,
                CASE WHEN ss.close > 0 AND ss.close_t21 IS NOT NULL
                     THEN LEAST(500, GREATEST(-100, (ss.close_t21 - ss.close) / ss.close * 100))
                END AS return_30d,
                CASE WHEN ss.close > 0 AND ss.close_t63 IS NOT NULL
                     THEN LEAST(500, GREATEST(-100, (ss.close_t63 - ss.close) / ss.close * 100))
                END AS return_90d,
                CASE WHEN ss.close > 0 AND ss.close_t126 IS NOT NULL
                     THEN LEAST(500, GREATEST(-100, (ss.close_t126 - ss.close) / ss.close * 100))
                END AS return_180d,
                -- SPY returns (%)
                CASE WHEN ss.spy_close > 0 AND ss.spy_t5 IS NOT NULL
                     THEN (ss.spy_t5 - ss.spy_close) / ss.spy_close * 100 END AS spy_return_5d,
                CASE WHEN ss.spy_close > 0 AND ss.spy_t21 IS NOT NULL
                     THEN (ss.spy_t21 - ss.spy_close) / ss.spy_close * 100 END AS spy_return_30d,
                CASE WHEN ss.spy_close > 0 AND ss.spy_t63 IS NOT NULL
                     THEN (ss.spy_t63 - ss.spy_close) / ss.spy_close * 100 END AS spy_return_90d,
                CASE WHEN ss.spy_close > 0 AND ss.spy_t126 IS NOT NULL
                     THEN (ss.spy_t126 - ss.spy_close) / ss.spy_close * 100 END AS spy_return_180d
            FROM buys b
            ASOF JOIN stock_spy ss
                ON b.ticker = ss.ticker
                AND b.transaction_date >= ss.trade_date
            WHERE ss.close IS NOT NULL
              AND ss.close >= 1.0  -- Exclude penny stocks (near-zero denominator = garbage returns)
        )
        INSERT INTO fact_insider_outcomes
        SELECT
            ticker, transaction_date, insider_name, insider_role,
            -- Role + size categories (set below via UPDATE)
            NULL AS role_category,
            transaction_code, shares, txn_price, dollar_value,
            NULL AS size_category,
            FALSE AS is_routine,
            entry_close,
            close_t5, close_t21, close_t63, close_t126,
            return_5d, return_30d, return_90d, return_180d,
            spy_return_5d, spy_return_30d, spy_return_90d, spy_return_180d,
            -- Alpha = stock return - SPY return (clamped to [-100, +500])
            CASE WHEN return_5d IS NOT NULL AND spy_return_5d IS NOT NULL
                 THEN LEAST(500, GREATEST(-100, return_5d  - spy_return_5d))  END AS alpha_5d,
            CASE WHEN return_30d IS NOT NULL AND spy_return_30d IS NOT NULL
                 THEN LEAST(500, GREATEST(-100, return_30d - spy_return_30d)) END AS alpha_30d,
            CASE WHEN return_90d IS NOT NULL AND spy_return_90d IS NOT NULL
                 THEN LEAST(500, GREATEST(-100, return_90d - spy_return_90d)) END AS alpha_90d,
            CASE WHEN return_180d IS NOT NULL AND spy_return_180d IS NOT NULL
                 THEN LEAST(500, GREATEST(-100, return_180d - spy_return_180d)) END AS alpha_180d,
            CURRENT_TIMESTAMP AS computed_at
        FROM joined
    """)

    total = conn.execute("SELECT COUNT(*) FROM fact_insider_outcomes").fetchone()[0]
    logger.info("Insider outcomes built: {} transactions with price data", total)

    # Classify roles, sizes, and routine traders
    _update_categories(conn)
    classify_routine_traders(conn)

    return total


def _update_categories(conn: duckdb.DuckDBPyConnection) -> None:
    """Set role_category and size_category using CASE expressions."""
    # SEC Form 4 insider_role values: Officer, Director, TenPercentOwner, Other
    # and combinations like "Director,Officer". Priority: Officer > Director > 10%.
    conn.execute("""
        UPDATE fact_insider_outcomes
        SET
            role_category = CASE
                WHEN insider_role LIKE '%Officer%'        THEN 'OFFICER'
                WHEN insider_role LIKE '%Director%'       THEN 'DIRECTOR'
                WHEN insider_role LIKE '%TenPercent%'     THEN '10PCT_OWNER'
                ELSE 'OTHER'
            END,
            size_category = CASE
                WHEN dollar_value >= 1000000 THEN 'MEGA'
                WHEN dollar_value >= 250000  THEN 'LARGE'
                WHEN dollar_value >= 50000   THEN 'MEDIUM'
                ELSE 'SMALL'
            END
    """)
    logger.info("Categories updated (role + size)")


# ─── Step 2: Classify Routine vs Opportunistic ───────────────────────────────

def classify_routine_traders(conn: duckdb.DuckDBPyConnection) -> int:
    """Classify insiders as ROUTINE using Cohen-Malloy-Pomorski method.

    Routine = insider traded in the same calendar month for 3+ consecutive years.
    Opportunistic = everything else (irregular timing — much more predictive).

    Uses islands-and-gaps SQL pattern on fact_form4_transactions.
    """
    logger.info("Classifying routine vs opportunistic traders...")

    # Find routine insider+ticker combos using consecutive-year detection
    updated = conn.execute("""
        WITH
        -- All distinct insider+ticker+month+year combos (open-market buys only)
        insider_months AS (
            SELECT DISTINCT
                insider_name, ticker,
                EXTRACT(MONTH FROM transaction_date)::INTEGER AS txn_month,
                EXTRACT(YEAR FROM transaction_date)::INTEGER AS txn_year
            FROM fact_form4_transactions
            WHERE transaction_code = 'P'
              AND insider_name IS NOT NULL
              AND transaction_date >= '2016-01-01'
        ),
        -- Islands and gaps: detect consecutive years in same month
        with_group AS (
            SELECT *,
                txn_year - ROW_NUMBER() OVER (
                    PARTITION BY insider_name, ticker, txn_month
                    ORDER BY txn_year
                ) AS grp
            FROM insider_months
        ),
        -- Find max streak length per insider+ticker
        streak_lengths AS (
            SELECT
                insider_name, ticker,
                MAX(streak_len) AS max_streak
            FROM (
                SELECT insider_name, ticker, txn_month, grp,
                       COUNT(*) AS streak_len
                FROM with_group
                GROUP BY insider_name, ticker, txn_month, grp
            ) sub
            GROUP BY insider_name, ticker
        ),
        -- Routine = 3+ consecutive years in any same month
        routine_pairs AS (
            SELECT insider_name, ticker
            FROM streak_lengths
            WHERE max_streak >= 3
        )
        -- Mark routine transactions in outcomes table
        UPDATE fact_insider_outcomes o
        SET is_routine = TRUE
        FROM routine_pairs r
        WHERE o.insider_name = r.insider_name
          AND o.ticker = r.ticker
    """).fetchone()

    routine_count = conn.execute(
        "SELECT COUNT(*) FROM fact_insider_outcomes WHERE is_routine = TRUE"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM fact_insider_outcomes").fetchone()[0]
    opp_count = total - routine_count

    logger.info(
        "Routine classification done: {} routine ({:.1f}%), {} opportunistic ({:.1f}%)",
        routine_count, routine_count / max(total, 1) * 100,
        opp_count, opp_count / max(total, 1) * 100,
    )
    return routine_count


# ─── Step 3: Build Patterns ──────────────────────────────────────────────────

def build_insider_patterns(conn: duckdb.DuckDBPyConnection) -> int:
    """Build agg_insider_patterns: statistical profiles per ticker at multiple
    granularity levels.

    Pattern types:
        ALL           — all open-market buys for the ticker
        OPPORTUNISTIC — only non-routine buys (Cohen-Malloy-Pomorski filter)
        ROLE          — grouped by role_category (CEO, CFO, DIRECTOR, etc.)
        SIZE          — grouped by size_category (SMALL, MEDIUM, LARGE, MEGA)
    """
    logger.info("Building insider patterns...")
    _ensure_tables(conn)

    conn.execute("DELETE FROM agg_insider_patterns")

    # Template for win rate / alpha aggregation
    agg_sql = """
        COUNT(*) AS sample_count,
        COUNT(CASE WHEN return_5d > 0 THEN 1 END) * 100.0
            / NULLIF(COUNT(return_5d), 0) AS win_rate_5d,
        COUNT(CASE WHEN return_30d > 0 THEN 1 END) * 100.0
            / NULLIF(COUNT(return_30d), 0) AS win_rate_30d,
        COUNT(CASE WHEN return_90d > 0 THEN 1 END) * 100.0
            / NULLIF(COUNT(return_90d), 0) AS win_rate_90d,
        COUNT(CASE WHEN return_180d > 0 THEN 1 END) * 100.0
            / NULLIF(COUNT(return_180d), 0) AS win_rate_180d,
        COUNT(CASE WHEN alpha_5d > 0 THEN 1 END) * 100.0
            / NULLIF(COUNT(alpha_5d), 0) AS alpha_win_5d,
        COUNT(CASE WHEN alpha_30d > 0 THEN 1 END) * 100.0
            / NULLIF(COUNT(alpha_30d), 0) AS alpha_win_30d,
        COUNT(CASE WHEN alpha_90d > 0 THEN 1 END) * 100.0
            / NULLIF(COUNT(alpha_90d), 0) AS alpha_win_90d,
        COUNT(CASE WHEN alpha_180d > 0 THEN 1 END) * 100.0
            / NULLIF(COUNT(alpha_180d), 0) AS alpha_win_180d,
        AVG(return_5d) AS mean_return_5d,
        AVG(return_30d) AS mean_return_30d,
        AVG(return_90d) AS mean_return_90d,
        AVG(return_180d) AS mean_return_180d,
        AVG(alpha_5d) AS mean_alpha_5d,
        AVG(alpha_30d) AS mean_alpha_30d,
        AVG(alpha_90d) AS mean_alpha_90d,
        AVG(alpha_180d) AS mean_alpha_180d,
        MEDIAN(return_90d) AS median_return_90d
    """

    # Level 1: ALL buys per ticker
    conn.execute(f"""
        INSERT INTO agg_insider_patterns
        SELECT ticker, 'ALL' AS pattern_type, 'ALL' AS role_category,
               'ALL' AS size_category, {agg_sql},
               0 AS insider_effect_score, CURRENT_TIMESTAMP AS computed_at
        FROM fact_insider_outcomes
        GROUP BY ticker
        HAVING COUNT(*) >= {MIN_PATTERN_SAMPLES}
    """)
    lvl1 = conn.execute(
        "SELECT COUNT(*) FROM agg_insider_patterns WHERE pattern_type='ALL'"
    ).fetchone()[0]

    # Level 2: OPPORTUNISTIC only (the strongest academic signal)
    conn.execute(f"""
        INSERT INTO agg_insider_patterns
        SELECT ticker, 'OPPORTUNISTIC' AS pattern_type, 'ALL' AS role_category,
               'ALL' AS size_category, {agg_sql},
               0 AS insider_effect_score, CURRENT_TIMESTAMP AS computed_at
        FROM fact_insider_outcomes
        WHERE is_routine = FALSE
        GROUP BY ticker
        HAVING COUNT(*) >= {MIN_PATTERN_SAMPLES}
    """)
    lvl2 = conn.execute(
        "SELECT COUNT(*) FROM agg_insider_patterns WHERE pattern_type='OPPORTUNISTIC'"
    ).fetchone()[0]

    # Level 3: By ROLE per ticker
    conn.execute(f"""
        INSERT INTO agg_insider_patterns
        SELECT ticker, 'ROLE' AS pattern_type, role_category,
               'ALL' AS size_category, {agg_sql},
               0 AS insider_effect_score, CURRENT_TIMESTAMP AS computed_at
        FROM fact_insider_outcomes
        GROUP BY ticker, role_category
        HAVING COUNT(*) >= {MIN_PATTERN_SAMPLES}
    """)
    lvl3 = conn.execute(
        "SELECT COUNT(*) FROM agg_insider_patterns WHERE pattern_type='ROLE'"
    ).fetchone()[0]

    # Level 4: By SIZE per ticker
    conn.execute(f"""
        INSERT INTO agg_insider_patterns
        SELECT ticker, 'SIZE' AS pattern_type, 'ALL' AS role_category,
               size_category, {agg_sql},
               0 AS insider_effect_score, CURRENT_TIMESTAMP AS computed_at
        FROM fact_insider_outcomes
        GROUP BY ticker, size_category
        HAVING COUNT(*) >= {MIN_PATTERN_SAMPLES}
    """)
    lvl4 = conn.execute(
        "SELECT COUNT(*) FROM agg_insider_patterns WHERE pattern_type='SIZE'"
    ).fetchone()[0]

    # Compute insider_effect_score for all patterns
    _score_patterns(conn)

    total = lvl1 + lvl2 + lvl3 + lvl4
    logger.info(
        "Patterns built: {} total (ALL={}, OPPORTUNISTIC={}, ROLE={}, SIZE={})",
        total, lvl1, lvl2, lvl3, lvl4,
    )
    return total


def _score_patterns(conn: duckdb.DuckDBPyConnection) -> None:
    """Compute insider_effect_score (0-100) for each pattern row.

    Score formula:
        Win rate @ 90d component:     0-40 pts  (50% = 0, 75% = 40)
        Alpha win rate @ 90d:         0-30 pts  (50% = 0, 75% = 30)
        Mean alpha @ 90d:             0-20 pts  (0% = 0, 10% = 20)
        Sample reliability:           0-10 pts  (20+ samples = 10)
    """
    conn.execute("""
        UPDATE agg_insider_patterns
        SET insider_effect_score = LEAST(100, GREATEST(0,
            -- Win rate component (0-40)
            LEAST(40, GREATEST(0, (COALESCE(win_rate_90d, 50) - 50) * 1.6))
            -- Alpha win rate component (0-30)
            + LEAST(30, GREATEST(0, (COALESCE(alpha_win_90d, 50) - 50) * 1.2))
            -- Mean alpha component (0-20)
            + LEAST(20, GREATEST(0, COALESCE(mean_alpha_90d, 0) * 2))
            -- Sample reliability (0-10)
            + CASE
                WHEN sample_count >= 20 THEN 10
                WHEN sample_count >= 10 THEN 7
                WHEN sample_count >= 5  THEN 4
                ELSE 1
              END
        ))
    """)


# ─── Step 4: Score a Quarter ─────────────────────────────────────────────────

def score_insider_effect_for_quarter(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> list[dict]:
    """For each ticker in the quarter, compute insider_effect_score using
    historical patterns + current quarter's insider activity.

    Logic:
        1. Find recent insider buys within the quarter's window
        2. Look up historical pattern from agg_insider_patterns
        3. Apply modifiers for opportunistic, cluster, CEO/CFO
        4. Return per-ticker scores

    For tickers with NO recent insider buys: score = 0 (no signal).
    """
    logger.info("Scoring insider effect for quarter={}", quarter)

    # Derive quarter window (same as insider_intelligence.py)
    year = int(quarter.split("-Q")[0])
    qnum = int(quarter.split("-Q")[1])
    quarter_end_month = {1: 3, 2: 6, 3: 9, 4: 12}[qnum]
    last_day = calendar.monthrange(year, quarter_end_month)[1]
    quarter_end = datetime(year, quarter_end_month, last_day)
    window_end = quarter_end + timedelta(days=45)
    window_start = window_end - timedelta(days=90)

    ws = window_start.strftime("%Y-%m-%d")
    we = window_end.strftime("%Y-%m-%d")

    # Get recent buys per ticker in this quarter's window
    recent_df = conn.execute("""
        SELECT
            ticker,
            COUNT(*) AS buy_count,
            COUNT(DISTINCT insider_name) AS unique_buyers,
            SUM(CASE WHEN is_routine = FALSE THEN 1 ELSE 0 END) AS opp_count,
            MAX(CASE WHEN role_category = 'OFFICER' THEN 1 ELSE 0 END) AS has_officer,
            SUM(dollar_value) AS total_dollar_value
        FROM fact_insider_outcomes
        WHERE transaction_date >= ? AND transaction_date <= ?
        GROUP BY ticker
    """, [ws, we]).fetchdf()

    if recent_df.empty:
        logger.info("No recent insider buys for quarter={}", quarter)
        return []

    # Load patterns (prefer OPPORTUNISTIC, fallback to ALL)
    patterns_df = conn.execute("""
        SELECT ticker, pattern_type, insider_effect_score,
               win_rate_90d, alpha_win_90d, mean_alpha_90d, sample_count
        FROM agg_insider_patterns
        WHERE pattern_type IN ('OPPORTUNISTIC', 'ALL')
        ORDER BY ticker, pattern_type DESC
    """).fetchdf()

    # Build lookup: prefer OPPORTUNISTIC pattern, fallback to ALL
    pattern_map = {}
    for _, p in patterns_df.iterrows():
        t = str(p["ticker"])
        if t not in pattern_map or str(p["pattern_type"]) == "OPPORTUNISTIC":
            pattern_map[t] = p.to_dict()

    results = []
    for _, row in recent_df.iterrows():
        ticker = str(row["ticker"])
        pattern = pattern_map.get(ticker)

        # Base score from historical pattern
        base_score = float(pattern["insider_effect_score"]) if pattern else 0.0
        hist_win_rate = float(pattern["win_rate_90d"] or 50) if pattern else None
        hist_alpha = float(pattern["mean_alpha_90d"] or 0) if pattern else None
        hist_samples = int(pattern["sample_count"] or 0) if pattern else 0

        # Modifiers based on current quarter activity
        modifier = 0.0
        opp_count = int(row.get("opp_count") or 0)
        buy_count = int(row.get("buy_count") or 0)
        unique_buyers = int(row.get("unique_buyers") or 0)
        has_officer = bool(row.get("has_officer"))

        # +15 if majority of recent buys are opportunistic
        if buy_count > 0 and opp_count / buy_count > 0.5:
            modifier += 15.0
        # -10 if all recent buys are routine (low signal)
        elif opp_count == 0:
            modifier -= 10.0

        # +12 if cluster detected (3+ unique buyers)
        if unique_buyers >= 3:
            modifier += 12.0

        # +8 if an Officer is buying (C-suite/VP — highest conviction insider)
        if has_officer:
            modifier += 8.0

        # Final score (0-100)
        final_score = round(min(100, max(0, base_score + modifier)), 1)

        results.append({
            "ticker": ticker,
            "insider_effect_score": final_score,
            "insider_hist_win_rate": hist_win_rate,
            "insider_hist_alpha": hist_alpha,
            "insider_pattern_samples": hist_samples,
        })

    logger.info(
        "Insider effect scored: {} tickers for quarter={}", len(results), quarter
    )
    return results


# ─── Step 5: Trend Score ─────────────────────────────────────────────────────

def compute_trend_scores(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> dict[str, float]:
    """Compute trend_score (0-100) for each ticker from OHLCV data.

    Components:
        1. Linear regression slope (0-40): is price going up?
        2. R-squared (0-30): is the trend clean/linear?
        3. Price vs 50-day SMA (0-30): is price above its moving average?

    Returns {ticker: trend_score}.
    """
    logger.info("Computing trend scores for quarter={}", quarter)

    year = int(quarter.split("-Q")[0])
    qnum = int(quarter.split("-Q")[1])
    quarter_end_month = {1: 3, 2: 6, 3: 9, 4: 12}[qnum]
    last_day = calendar.monthrange(year, quarter_end_month)[1]
    quarter_end = datetime(year, quarter_end_month, last_day)
    ref_date = quarter_end + timedelta(days=45)
    start_date = (ref_date - timedelta(days=120)).strftime("%Y-%m-%d")
    end_date = ref_date.strftime("%Y-%m-%d")

    price_df = conn.execute("""
        SELECT p.ticker, p.trade_date, p.high, p.low, p.close
        FROM fact_daily_prices p
        INNER JOIN (
            SELECT DISTINCT ticker FROM intelligence_scores
            WHERE report_quarter = ?
        ) i ON p.ticker = i.ticker
        WHERE p.trade_date >= ? AND p.trade_date <= ?
          AND p.close > 0
        ORDER BY p.ticker, p.trade_date
    """, [quarter, start_date, end_date]).fetchdf()

    if price_df.empty:
        return {}

    results = {}
    for ticker, group in price_df.groupby("ticker"):
        closes = group["close"].values.astype(float)
        if len(closes) < 30:
            continue

        # Use last 63 trading days (or all available if < 63)
        n = min(63, len(closes))
        c = closes[-n:]

        # Component 1: Linear regression slope (0-40)
        x = np.arange(n, dtype=float)
        slope, intercept = np.polyfit(x, c, 1)
        y_pred = slope * x + intercept
        ss_res = np.sum((c - y_pred) ** 2)
        ss_tot = np.sum((c - np.mean(c)) ** 2)
        r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0

        daily_pct = slope / np.mean(c) * 100
        # Scale: -0.3%/day = 0, +0.3%/day = 40
        slope_score = min(40, max(0, (daily_pct + 0.3) / 0.6 * 40))

        # Component 2: R-squared quality (0-30)
        r2_score = min(30, max(0, r_squared * 30))

        # Component 3: Price vs 50-day SMA (0-30)
        sma_n = min(50, len(closes))
        sma = np.mean(closes[-sma_n:])
        pct_above = (closes[-1] - sma) / sma * 100
        sma_score = min(30, max(0, (pct_above + 10) / 20 * 30))

        trend = round(min(100, slope_score + r2_score + sma_score), 1)
        results[str(ticker)] = trend

    logger.info("Trend scores computed: {} tickers", len(results))
    return results


# ─── Step 6: Institutional Pressure ──────────────────────────────────────────

def compute_institutional_pressure(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> dict[str, float]:
    """Compute institutional_pressure (0-100) from QoQ accumulation velocity.

    Components:
        Count growth velocity  (0-30): how fast institutions are adding
        Value growth velocity  (0-30): how fast AUM is growing
        Streak bonus           (0-20): consecutive quarters of accumulation
        Shares growth          (0-20): volume of accumulation
    """
    logger.info("Computing institutional pressure for quarter={}", quarter)

    qoq_df = conn.execute("""
        SELECT
            ticker,
            inst_count_change_pct,
            value_change_pct,
            shares_change_pct,
            count_up_streak
        FROM agg_qoq_changes
        WHERE current_quarter = ?
    """, [quarter]).fetchdf()

    if qoq_df.empty:
        return {}

    results = {}
    for _, row in qoq_df.iterrows():
        count_pct = float(row.get("inst_count_change_pct") or 0)
        value_pct = float(row.get("value_change_pct") or 0)
        shares_pct = float(row.get("shares_change_pct") or 0)
        streak = int(row.get("count_up_streak") or 0)

        # Count growth (0-30): >20% = max
        count_score = min(30, max(0, count_pct * 1.5))
        # Value growth (0-30): >30% = max
        value_score = min(30, max(0, value_pct))
        # Streak (0-20): 4+ quarters = max
        streak_score = min(20, streak * 5)
        # Shares growth (0-20): >20% = max
        shares_score = min(20, max(0, shares_pct))

        pressure = round(min(100, max(0,
            count_score + value_score + streak_score + shares_score
        )), 1)
        results[str(row["ticker"])] = pressure

    logger.info("Institutional pressure computed: {} tickers", len(results))
    return results


# ─── Step 7: Update Intelligence Scores ──────────────────────────────────────

def update_insider_effect_in_intelligence(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> int:
    """Write insider effect + trend + pressure scores into intelligence_scores."""
    _ensure_tables(conn)

    # Compute all three signal layers
    insider_results = score_insider_effect_for_quarter(conn, quarter)
    trend_scores = compute_trend_scores(conn, quarter)
    pressure_scores = compute_institutional_pressure(conn, quarter)

    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    updated = 0

    # Build merged update set
    all_tickers = set()
    insider_map = {}
    for r in insider_results:
        t = r["ticker"]
        insider_map[t] = r
        all_tickers.add(t)
    all_tickers.update(trend_scores.keys())
    all_tickers.update(pressure_scores.keys())

    for ticker in all_tickers:
        ie = insider_map.get(ticker, {})
        try:
            conn.execute("""
                UPDATE intelligence_scores
                SET insider_effect_score = ?,
                    insider_hist_win_rate = ?,
                    insider_hist_alpha = ?,
                    insider_pattern_samples = ?,
                    trend_score = ?,
                    institutional_pressure = ?,
                    computed_at = ?
                WHERE ticker = ? AND report_quarter = ?
            """, [
                ie.get("insider_effect_score", 0),
                ie.get("insider_hist_win_rate"),
                ie.get("insider_hist_alpha"),
                ie.get("insider_pattern_samples", 0),
                trend_scores.get(ticker, 0),
                pressure_scores.get(ticker, 0),
                now_iso,
                ticker,
                quarter,
            ])
            updated += 1
        except Exception as e:
            logger.debug("Update failed for {}: {}", ticker, e)

    logger.info(
        "Intelligence updated: {}/{} tickers for quarter={} "
        "(insider_effect={}, trend={}, pressure={})",
        updated, len(all_tickers), quarter,
        len(insider_results), len(trend_scores), len(pressure_scores),
    )
    return updated


# ─── Step 8: Summary ─────────────────────────────────────────────────────────

def print_summary(conn: duckdb.DuckDBPyConnection) -> None:
    """Print validation statistics about outcomes and patterns."""
    _ensure_tables(conn)

    total = conn.execute(
        "SELECT COUNT(*) FROM fact_insider_outcomes"
    ).fetchone()[0]

    if total == 0:
        logger.info("No outcomes built yet. Run --build first.")
        return

    print("\n" + "=" * 70)
    print("INSIDER OUTCOME ENGINE — SUMMARY")
    print("=" * 70)

    # Basic stats
    stats = conn.execute("""
        SELECT
            COUNT(*) AS total,
            COUNT(CASE WHEN is_routine THEN 1 END) AS routine,
            COUNT(CASE WHEN NOT is_routine THEN 1 END) AS opportunistic,
            COUNT(return_90d) AS has_90d,
            MIN(transaction_date) AS earliest,
            MAX(transaction_date) AS latest,
            COUNT(DISTINCT ticker) AS tickers
        FROM fact_insider_outcomes
    """).fetchone()

    print(f"\nTotal outcomes:    {stats[0]:,}")
    print(f"Routine:           {stats[1]:,} ({stats[1]/max(stats[0],1)*100:.1f}%)")
    print(f"Opportunistic:     {stats[2]:,} ({stats[2]/max(stats[0],1)*100:.1f}%)")
    print(f"With 90d return:   {stats[3]:,}")
    print(f"Date range:        {stats[4]} to {stats[5]}")
    print(f"Unique tickers:    {stats[6]:,}")

    # Win rates: ALL vs OPPORTUNISTIC (key validation)
    print("\n" + "-" * 70)
    print("WIN RATES — ALL vs OPPORTUNISTIC (Cohen-Malloy-Pomorski validation)")
    print("-" * 70)

    for label, where in [("ALL buys", "1=1"), ("OPPORTUNISTIC only", "NOT is_routine")]:
        wr = conn.execute(f"""
            SELECT
                COUNT(CASE WHEN return_5d > 0 THEN 1 END) * 100.0
                    / NULLIF(COUNT(return_5d), 0) AS wr_5d,
                COUNT(CASE WHEN return_30d > 0 THEN 1 END) * 100.0
                    / NULLIF(COUNT(return_30d), 0) AS wr_30d,
                COUNT(CASE WHEN return_90d > 0 THEN 1 END) * 100.0
                    / NULLIF(COUNT(return_90d), 0) AS wr_90d,
                COUNT(CASE WHEN return_180d > 0 THEN 1 END) * 100.0
                    / NULLIF(COUNT(return_180d), 0) AS wr_180d,
                AVG(alpha_90d) AS mean_alpha_90d,
                COUNT(CASE WHEN alpha_90d > 0 THEN 1 END) * 100.0
                    / NULLIF(COUNT(alpha_90d), 0) AS alpha_wr_90d
            FROM fact_insider_outcomes
            WHERE {where}
        """).fetchone()
        print(f"\n  {label}:")
        print(f"    Win rate:  5d={wr[0]:.1f}%  30d={wr[1]:.1f}%"
              f"  90d={wr[2]:.1f}%  180d={wr[3]:.1f}%")
        print(f"    Alpha 90d: mean={wr[4]:.2f}%  beat SPY={wr[5]:.1f}%")

    # Win rates by role
    print("\n" + "-" * 70)
    print("WIN RATES BY ROLE (Directors often > CEO for opportunistic trades)")
    print("-" * 70)

    roles = conn.execute("""
        SELECT
            role_category,
            COUNT(*) AS n,
            COUNT(CASE WHEN return_90d > 0 THEN 1 END) * 100.0
                / NULLIF(COUNT(return_90d), 0) AS wr_90d,
            AVG(alpha_90d) AS alpha_90d
        FROM fact_insider_outcomes
        WHERE NOT is_routine
        GROUP BY role_category
        HAVING COUNT(return_90d) >= 10
        ORDER BY wr_90d DESC
    """).fetchall()

    for r in roles:
        print(f"  {r[0]:<14} n={r[1]:>6,}  wr_90d={r[2]:>5.1f}%  alpha={r[3]:>+6.2f}%")

    # Win rates by size
    print("\n" + "-" * 70)
    print("WIN RATES BY SIZE (Larger buys = higher conviction)")
    print("-" * 70)

    sizes = conn.execute("""
        SELECT
            size_category,
            COUNT(*) AS n,
            COUNT(CASE WHEN return_90d > 0 THEN 1 END) * 100.0
                / NULLIF(COUNT(return_90d), 0) AS wr_90d,
            AVG(alpha_90d) AS alpha_90d
        FROM fact_insider_outcomes
        WHERE NOT is_routine
        GROUP BY size_category
        ORDER BY CASE size_category
            WHEN 'MEGA' THEN 1 WHEN 'LARGE' THEN 2
            WHEN 'MEDIUM' THEN 3 ELSE 4 END
    """).fetchall()

    for s in sizes:
        print(f"  {s[0]:<10} n={s[1]:>6,}  wr_90d={s[2]:>5.1f}%  alpha={s[3]:>+6.2f}%")

    # Top patterns
    print("\n" + "-" * 70)
    print("TOP 15 PATTERNS BY INSIDER EFFECT SCORE")
    print("-" * 70)

    top = conn.execute("""
        SELECT ticker, pattern_type, role_category, sample_count,
               win_rate_90d, alpha_win_90d, mean_alpha_90d, insider_effect_score
        FROM agg_insider_patterns
        WHERE sample_count >= 5
        ORDER BY insider_effect_score DESC
        LIMIT 15
    """).fetchall()

    print(f"  {'Ticker':<8} {'Type':<14} {'Role':<10} {'N':>5} "
          f"{'WR90d':>6} {'AlphaWR':>7} {'Alpha':>7} {'Score':>6}")
    for t in top:
        print(f"  {t[0]:<8} {t[1]:<14} {t[2]:<10} {t[3]:>5} "
              f"{t[4]:>5.1f}% {t[5]:>6.1f}% {t[6]:>+6.2f}% {t[7]:>5.1f}")

    print("\n" + "=" * 70)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Insider Outcome Engine — historical pattern analysis"
    )
    parser.add_argument(
        "--build", action="store_true",
        help="Build fact_insider_outcomes table (one-time, ~5 min)",
    )
    parser.add_argument(
        "--patterns", action="store_true",
        help="Build agg_insider_patterns aggregation",
    )
    parser.add_argument(
        "--score", action="store_true",
        help="Score a quarter with insider effect + trend + pressure",
    )
    parser.add_argument(
        "--quarter", type=str, default=None,
        help="Quarter to score (e.g. 2025-Q3). Required with --score.",
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Print validation statistics",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Run full pipeline: build + patterns + summary",
    )
    args = parser.parse_args()

    from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

    conn = duckdb.connect(str(WAREHOUSE_PATH))

    try:
        if args.build or args.all:
            build_insider_outcomes(conn)

        if args.patterns or args.all:
            build_insider_patterns(conn)

        if args.score:
            if not args.quarter:
                parser.error("--score requires --quarter")
            update_insider_effect_in_intelligence(conn, args.quarter)

        if args.summary or args.all:
            print_summary(conn)

        if not any([args.build, args.patterns, args.score, args.summary, args.all]):
            parser.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    from signal_scanner.utils.logger import setup_logger
    setup_logger()
    main()
