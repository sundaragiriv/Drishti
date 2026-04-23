"""Quarter-over-quarter diff engine.

Aggregates fact_13f_positions into quarterly snapshots per ticker,
then computes QoQ changes with streak tracking.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH


def _quarter_label(report_period_date) -> str:
    """Convert a date to 'YYYY-QN' label."""
    if isinstance(report_period_date, str):
        report_period_date = datetime.fromisoformat(report_period_date).date()
    q = ((report_period_date.month - 1) // 3) + 1
    return f"{report_period_date.year}-Q{q}"


def _prior_quarter(quarter_label: str) -> str:
    """Return the strictly previous quarter label. e.g. '2025-Q1' -> '2024-Q4'."""
    year, qn = quarter_label.split("-Q")
    year, q = int(year), int(qn)
    if q == 1:
        return f"{year - 1}-Q4"
    return f"{year}-Q{q - 1}"


def _prior_clean_quarter(
    conn: duckdb.DuckDBPyConnection,
    current_q: str,
) -> Optional[str]:
    """Return the most recent prior quarter that has data and is not contaminated.

    Walks backwards from current_q, skipping quarters in CONTAMINATED_QUARTERS
    (e.g. 2025-Q3, 2024-Q1) which contain unreliable data due to SEC bulk-data
    year-boundary gaps.  Falls back to the simple prior quarter if nothing clean
    is found within 4 steps (avoids infinite loops on very sparse history).
    """
    from signal_scanner.institutional_intel.jobs.data_cleanup import CONTAMINATED_QUARTERS

    candidate = _prior_quarter(current_q)
    for _ in range(4):          # look at most 4 quarters back
        if candidate in CONTAMINATED_QUARTERS:
            candidate = _prior_quarter(candidate)
            continue
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(AVG(inst_count), 0) FROM agg_quarterly_holdings WHERE report_quarter = ?",
            [candidate],
        ).fetchone()
        ticker_count, avg_inst = row[0], row[1]
        # Skip quarters that are sparse (insufficient institutional coverage).
        # Threshold: avg_inst < 50 flags partial ingests like Q2 2025 (avg=10).
        if ticker_count > 0 and avg_inst >= 50:
            return candidate
        if ticker_count > 0:
            logger.debug(
                "QoQ: skipping sparse quarter {} (avg_inst={:.1f} < 50), looking further back",
                candidate, avg_inst,
            )
        candidate = _prior_quarter(candidate)
    # Last-resort: plain prior quarter (original behaviour)
    return _prior_quarter(current_q)


def build_quarterly_snapshots(quarters: Optional[List[str]] = None) -> int:
    """Aggregate fact_13f_positions into agg_quarterly_holdings.

    Args:
        quarters: If provided, only rebuild these quarters (e.g. ['2025-Q3']).
                  If None, rebuild all quarters found in the data.

    Returns:
        Number of rows upserted.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = duckdb.connect(str(WAREHOUSE_PATH))
    try:
        # Build quarter filter
        quarter_filter = ""
        if quarters:
            quoted = ", ".join(f"'{q}'" for q in quarters)
            quarter_filter = f"WHERE quarter_label IN ({quoted})"

        # Aggregate: per ticker per quarter, count distinct institutions,
        # sum shares, sum value
        sql = f"""
            INSERT OR REPLACE INTO agg_quarterly_holdings
            SELECT
                ticker,
                quarter_label AS report_quarter,
                COUNT(DISTINCT manager_cik) AS inst_count,
                SUM(shares) AS total_shares,
                SUM(value_usd_thousands) AS total_value_usd_k,
                CASE WHEN COUNT(DISTINCT manager_cik) > 0
                     THEN SUM(shares) / COUNT(DISTINCT manager_cik)
                     ELSE 0 END AS avg_shares_per_inst,
                NULL AS sector,
                '{now_iso}' AS computed_at,
                NULL AS avg_price,
                NULL AS avg_volume,
                NULL AS quarter_end_price
            FROM (
                SELECT
                    ticker,
                    manager_cik,
                    shares,
                    value_usd_thousands,
                    CONCAT(
                        EXTRACT(YEAR FROM report_period)::INT::TEXT,
                        '-Q',
                        (((EXTRACT(MONTH FROM report_period)::INT - 1) // 3) + 1)::TEXT
                    ) AS quarter_label
                FROM fact_13f_positions
                WHERE ticker IS NOT NULL AND ticker != ''
            ) sub
            {quarter_filter}
            GROUP BY ticker, quarter_label
        """
        conn.execute(sql)

        # Count what was written
        count_sql = "SELECT COUNT(*) FROM agg_quarterly_holdings"
        if quarters:
            quoted = ", ".join(f"'{q}'" for q in quarters)
            count_sql += f" WHERE report_quarter IN ({quoted})"
        total = conn.execute(count_sql).fetchone()[0]

        logger.info(f"Quarterly snapshots built: {total} ticker-quarter rows")
        return total
    finally:
        conn.close()


def compute_qoq_changes(quarters: Optional[List[str]] = None) -> int:
    """Compute quarter-over-quarter changes from agg_quarterly_holdings.

    For each ticker in a given quarter, computes the diff vs prior quarter
    across inst_count, shares, and value. Also tracks consecutive up-streaks.

    Args:
        quarters: If provided, only compute for these quarters.
                  If None, compute for all quarters with a prior quarter.

    Returns:
        Number of QoQ diff rows upserted.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = duckdb.connect(str(WAREHOUSE_PATH))
    try:
        # Get all distinct quarters ordered
        all_quarters = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT report_quarter FROM agg_quarterly_holdings ORDER BY report_quarter"
            ).fetchall()
        ]

        if len(all_quarters) < 2:
            logger.info("Need at least 2 quarters to compute QoQ changes")
            return 0

        target_quarters = quarters or all_quarters[1:]  # skip first (no prior)
        total_upserted = 0

        for current_q in target_quarters:
            prior_q = _prior_clean_quarter(conn, current_q)
            if prior_q is None:
                logger.debug(f"No clean prior quarter found for {current_q}, skipping")
                continue
            # Log when we had to skip a contaminated quarter
            simple_prior = _prior_quarter(current_q)
            if prior_q != simple_prior:
                logger.info(
                    f"QoQ {current_q}: skipped contaminated {simple_prior}, "
                    f"using {prior_q} as baseline"
                )

            # Detect early-filer situation: current quarter has far fewer managers
            # than the prior (< 40%). In that case do a same-manager comparison
            # directly from fact_13f_positions so we compare like-for-like.
            curr_managers = conn.execute(
                "SELECT COUNT(DISTINCT manager_cik) FROM fact_13f_positions "
                "WHERE CONCAT(EXTRACT(YEAR FROM report_period)::INT::TEXT, '-Q', "
                "      (((EXTRACT(MONTH FROM report_period)::INT - 1) // 3) + 1)::TEXT) = ?",
                [current_q],
            ).fetchone()[0]
            prior_managers = conn.execute(
                "SELECT COUNT(DISTINCT manager_cik) FROM fact_13f_positions "
                "WHERE CONCAT(EXTRACT(YEAR FROM report_period)::INT::TEXT, '-Q', "
                "      (((EXTRACT(MONTH FROM report_period)::INT - 1) // 3) + 1)::TEXT) = ?",
                [prior_q],
            ).fetchone()[0]

            early_filer_mode = (
                prior_managers > 0
                and curr_managers / prior_managers < 0.85
            )
            if early_filer_mode:
                logger.info(
                    f"QoQ {current_q}: early-filer mode "
                    f"({curr_managers} vs {prior_managers} managers). "
                    f"Using same-manager comparison vs {prior_q}."
                )

            # Load existing streaks for prior quarter (for incrementing)
            prior_streaks: Dict[str, Dict[str, int]] = {}
            try:
                streak_rows = conn.execute(
                    "SELECT ticker, count_up_streak, shares_up_streak, value_up_streak "
                    "FROM agg_qoq_changes WHERE current_quarter = ?",
                    [prior_q],
                ).fetchall()
                for row in streak_rows:
                    prior_streaks[row[0]] = {
                        "count": row[1] or 0,
                        "shares": row[2] or 0,
                        "value": row[3] or 0,
                    }
            except Exception:
                pass  # Table might not exist yet on first run

            if early_filer_mode:
                # Same-manager comparison: compare each early filer's current
                # position to their own position in the prior clean quarter.
                # Gives true buy/sell signal from the institutions that have filed.
                diff_rows = conn.execute(
                    """
                    WITH early_managers AS (
                        SELECT DISTINCT manager_cik
                        FROM fact_13f_positions
                        WHERE CONCAT(EXTRACT(YEAR FROM report_period)::INT::TEXT, '-Q',
                              (((EXTRACT(MONTH FROM report_period)::INT-1)//3)+1)::TEXT) = ?
                    ),
                    cur AS (
                        SELECT ticker,
                               COUNT(DISTINCT manager_cik)     AS inst_count,
                               SUM(shares)                     AS total_shares,
                               SUM(value_usd_thousands)        AS total_value
                        FROM fact_13f_positions
                        WHERE CONCAT(EXTRACT(YEAR FROM report_period)::INT::TEXT, '-Q',
                              (((EXTRACT(MONTH FROM report_period)::INT-1)//3)+1)::TEXT) = ?
                          AND ticker IS NOT NULL AND ticker != ''
                        GROUP BY ticker
                    ),
                    pri AS (
                        SELECT f.ticker,
                               COUNT(DISTINCT f.manager_cik)   AS inst_count,
                               SUM(f.shares)                   AS total_shares,
                               SUM(f.value_usd_thousands)      AS total_value
                        FROM fact_13f_positions f
                        JOIN early_managers m ON f.manager_cik = m.manager_cik
                        WHERE CONCAT(EXTRACT(YEAR FROM f.report_period)::INT::TEXT, '-Q',
                              (((EXTRACT(MONTH FROM f.report_period)::INT-1)//3)+1)::TEXT) = ?
                        GROUP BY f.ticker
                    )
                    SELECT
                        c.ticker,
                        ? AS current_quarter,
                        ? AS prior_quarter,
                        c.inst_count   AS inst_count_current,
                        COALESCE(p.inst_count, 0) AS inst_count_prior,
                        c.inst_count - COALESCE(p.inst_count, 0) AS inst_count_change,
                        CASE WHEN COALESCE(p.inst_count, 0) > 0
                             THEN ((c.inst_count - p.inst_count) * 100.0 / p.inst_count)
                             ELSE NULL END AS inst_count_change_pct,
                        c.total_shares AS shares_current,
                        COALESCE(p.total_shares, 0) AS shares_prior,
                        c.total_shares - COALESCE(p.total_shares, 0) AS shares_change,
                        CASE WHEN COALESCE(p.total_shares, 0) > 0
                             THEN ((c.total_shares - p.total_shares) * 100.0 / p.total_shares)
                             ELSE NULL END AS shares_change_pct,
                        c.total_value  AS value_current_usd_k,
                        COALESCE(p.total_value, 0) AS value_prior_usd_k,
                        c.total_value - COALESCE(p.total_value, 0) AS value_change_usd_k,
                        CASE WHEN COALESCE(p.total_value, 0) > 0
                             THEN ((c.total_value - p.total_value) * 100.0 / p.total_value)
                             ELSE NULL END AS value_change_pct,
                        NULL AS sector
                    FROM cur c
                    LEFT JOIN pri p ON c.ticker = p.ticker
                    """,
                    [current_q, current_q, prior_q, current_q, prior_q],
                ).fetchall()
            else:
                # Standard comparison: aggregate-level QoQ vs prior quarter
                diff_rows = conn.execute(
                    """
                SELECT
                    c.ticker,
                    c.report_quarter AS current_quarter,
                    ? AS prior_quarter,
                    c.inst_count AS inst_count_current,
                    COALESCE(p.inst_count, 0) AS inst_count_prior,
                    c.inst_count - COALESCE(p.inst_count, 0) AS inst_count_change,
                    CASE WHEN COALESCE(p.inst_count, 0) > 0
                         THEN ((c.inst_count - p.inst_count) * 100.0 / p.inst_count)
                         ELSE NULL END AS inst_count_change_pct,
                    c.total_shares AS shares_current,
                    COALESCE(p.total_shares, 0) AS shares_prior,
                    c.total_shares - COALESCE(p.total_shares, 0) AS shares_change,
                    CASE WHEN COALESCE(p.total_shares, 0) > 0
                         THEN ((c.total_shares - p.total_shares) * 100.0 / p.total_shares)
                         ELSE NULL END AS shares_change_pct,
                    c.total_value_usd_k AS value_current_usd_k,
                    COALESCE(p.total_value_usd_k, 0) AS value_prior_usd_k,
                    c.total_value_usd_k - COALESCE(p.total_value_usd_k, 0) AS value_change_usd_k,
                    CASE WHEN COALESCE(p.total_value_usd_k, 0) > 0
                         THEN ((c.total_value_usd_k - p.total_value_usd_k) * 100.0 / p.total_value_usd_k)
                         ELSE NULL END AS value_change_pct,
                    c.sector
                FROM agg_quarterly_holdings c
                LEFT JOIN agg_quarterly_holdings p
                    ON c.ticker = p.ticker AND p.report_quarter = ?
                WHERE c.report_quarter = ?
                """,
                [prior_q, prior_q, current_q],
            ).fetchall()

            for row in diff_rows:
                ticker = row[0]
                count_change = row[5]
                shares_change = row[9]
                value_change = row[13]

                prev = prior_streaks.get(ticker, {"count": 0, "shares": 0, "value": 0})
                count_streak = (prev["count"] + 1) if count_change > 0 else 0
                shares_streak = (prev["shares"] + 1) if shares_change > 0 else 0
                value_streak = (prev["value"] + 1) if value_change > 0 else 0

                conn.execute(
                    """
                    INSERT OR REPLACE INTO agg_qoq_changes (
                        ticker, current_quarter, prior_quarter,
                        inst_count_current, inst_count_prior, inst_count_change,
                        inst_count_change_pct, shares_current, shares_prior,
                        shares_change, shares_change_pct,
                        value_current_usd_k, value_prior_usd_k,
                        value_change_usd_k, value_change_pct,
                        count_up_streak, shares_up_streak, value_up_streak,
                        sector, computed_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        ticker,           # ticker
                        current_q,        # current_quarter
                        prior_q,          # prior_quarter
                        row[3],           # inst_count_current
                        row[4],           # inst_count_prior
                        count_change,     # inst_count_change
                        row[6],           # inst_count_change_pct
                        row[7],           # shares_current
                        row[8],           # shares_prior
                        shares_change,    # shares_change
                        row[10],          # shares_change_pct
                        row[11],          # value_current_usd_k
                        row[12],          # value_prior_usd_k
                        value_change,     # value_change_usd_k
                        row[14],          # value_change_pct
                        count_streak,     # count_up_streak
                        shares_streak,    # shares_up_streak
                        value_streak,     # value_up_streak
                        row[15],          # sector
                        now_iso,          # computed_at
                    ],
                )
                total_upserted += 1

            logger.info(f"QoQ changes computed for {current_q}: {len(diff_rows)} tickers")

        # Build sector aggregation
        _build_sector_aggregation(conn, target_quarters, now_iso)

        # Enrich with price/volume data from Massive.com
        _enrich_with_price_data(conn, target_quarters)

        # Propagate sector from dim_issuer (must run after snapshot build clears sectors)
        conn.execute("""
            UPDATE agg_quarterly_holdings aq
            SET sector = di.sector
            FROM (
                SELECT ticker, FIRST(sector ORDER BY sector) AS sector
                FROM dim_issuer
                WHERE sector IS NOT NULL AND sector != ''
                GROUP BY ticker
            ) di
            WHERE aq.ticker = di.ticker
              AND (aq.sector IS NULL OR aq.sector = '')
        """)
        conn.execute("""
            UPDATE agg_qoq_changes qc
            SET sector = di.sector
            FROM (
                SELECT ticker, FIRST(sector ORDER BY sector) AS sector
                FROM dim_issuer
                WHERE sector IS NOT NULL AND sector != ''
                GROUP BY ticker
            ) di
            WHERE qc.ticker = di.ticker
              AND (qc.sector IS NULL OR qc.sector = '')
        """)
        logger.info("Sector data propagated from dim_issuer to agg tables")

        logger.info(f"Total QoQ diff rows: {total_upserted}")
        return total_upserted
    finally:
        conn.close()


def _enrich_with_price_data(conn: duckdb.DuckDBPyConnection, quarters: List[str]) -> int:
    """Enrich agg_quarterly_holdings with price/volume from fact_daily_prices.

    Computes per ticker per quarter:
      - avg_price: average daily close
      - avg_volume: average daily volume
      - quarter_end_price: close on last trading day of the quarter

    Then propagates to agg_qoq_changes as current/prior + change %.
    """
    # Check if fact_daily_prices has data
    try:
        price_count = conn.execute(
            "SELECT COUNT(*) FROM fact_daily_prices"
        ).fetchone()[0]
    except Exception:
        price_count = 0

    if price_count == 0:
        logger.debug("No price data in fact_daily_prices, skipping enrichment")
        return 0

    enriched = 0
    for q in quarters:
        # Update agg_quarterly_holdings with price averages
        conn.execute("""
            UPDATE agg_quarterly_holdings
            SET avg_price = sub.avg_close,
                avg_volume = sub.avg_vol,
                quarter_end_price = sub.last_close
            FROM (
                SELECT
                    ticker,
                    AVG(close) AS avg_close,
                    AVG(volume::DOUBLE) AS avg_vol,
                    LAST(close ORDER BY trade_date) AS last_close,
                    CONCAT(
                        EXTRACT(YEAR FROM trade_date)::INT::TEXT,
                        '-Q',
                        EXTRACT(QUARTER FROM trade_date)::INT::TEXT
                    ) AS q_label
                FROM fact_daily_prices
                WHERE close IS NOT NULL AND close > 0
                GROUP BY ticker, q_label
            ) sub
            WHERE agg_quarterly_holdings.ticker = sub.ticker
              AND agg_quarterly_holdings.report_quarter = sub.q_label
              AND agg_quarterly_holdings.report_quarter = ?
        """, [q])

        # Update agg_qoq_changes with price/volume from both quarters
        # Use subquery to pre-join current+prior since DuckDB UPDATE FROM
        # doesn't allow cross-referencing target table alias in JOIN conditions
        prior_q = f"{int(q[:4]) - (1 if q.endswith('Q1') else 0)}-Q{int(q[-1]) - 1 if not q.endswith('Q1') else 4}"
        conn.execute("""
            UPDATE agg_qoq_changes
            SET avg_price_current = sub.avg_price_c,
                avg_price_prior = sub.avg_price_p,
                avg_price_change_pct = CASE
                    WHEN sub.avg_price_p IS NOT NULL AND sub.avg_price_p > 0
                    THEN ((sub.avg_price_c - sub.avg_price_p) * 100.0 / sub.avg_price_p)
                    ELSE NULL END,
                avg_volume_current = sub.avg_volume_c,
                avg_volume_prior = sub.avg_volume_p,
                avg_volume_change_pct = CASE
                    WHEN sub.avg_volume_p IS NOT NULL AND sub.avg_volume_p > 0
                    THEN ((sub.avg_volume_c - sub.avg_volume_p) * 100.0 / sub.avg_volume_p)
                    ELSE NULL END
            FROM (
                SELECT c.ticker,
                       c.avg_price AS avg_price_c,
                       c.avg_volume AS avg_volume_c,
                       p.avg_price AS avg_price_p,
                       p.avg_volume AS avg_volume_p
                FROM agg_quarterly_holdings c
                LEFT JOIN agg_quarterly_holdings p
                    ON c.ticker = p.ticker AND p.report_quarter = ?
                WHERE c.report_quarter = ?
            ) sub
            WHERE agg_qoq_changes.ticker = sub.ticker
              AND agg_qoq_changes.current_quarter = ?
        """, [prior_q, q, q])

        # Update current_price (latest available price per ticker)
        conn.execute("""
            UPDATE agg_qoq_changes
            SET current_price = sub.latest_close
            FROM (
                SELECT ticker, LAST(close ORDER BY trade_date) AS latest_close
                FROM fact_daily_prices
                WHERE close IS NOT NULL AND close > 0
                GROUP BY ticker
            ) sub
            WHERE agg_qoq_changes.ticker = sub.ticker
              AND agg_qoq_changes.current_quarter = ?
        """, [q])

        # Compute returns: current_price vs quarter_end_price
        conn.execute("""
            UPDATE agg_qoq_changes
            SET price_on_report_date = c.quarter_end_price,
                price_returns_pct = CASE
                    WHEN c.quarter_end_price IS NOT NULL AND c.quarter_end_price > 0
                         AND agg_qoq_changes.current_price IS NOT NULL
                    THEN ((agg_qoq_changes.current_price - c.quarter_end_price) * 100.0 / c.quarter_end_price)
                    ELSE NULL END
            FROM agg_quarterly_holdings c
            WHERE agg_qoq_changes.ticker = c.ticker
              AND agg_qoq_changes.current_quarter = c.report_quarter
              AND agg_qoq_changes.current_quarter = ?
        """, [q])

        row = conn.execute(
            "SELECT COUNT(*) FROM agg_qoq_changes "
            "WHERE current_quarter = ? AND avg_price_current IS NOT NULL",
            [q],
        ).fetchone()
        enriched += row[0] if row else 0

    logger.info("Price enrichment: {} ticker-quarters with price data", enriched)
    return enriched


def _build_sector_aggregation(
    conn: duckdb.DuckDBPyConnection,
    quarters: List[str],
    now_iso: str,
) -> None:
    """Aggregate quarterly holdings by sector."""
    for q in quarters:
        conn.execute(
            """
            INSERT OR REPLACE INTO agg_sector_quarterly
            SELECT
                COALESCE(sector, 'Unknown') AS sector,
                report_quarter,
                SUM(inst_count) AS total_inst_count,
                SUM(total_shares) AS total_shares,
                SUM(total_value_usd_k) AS total_value_usd_k,
                COUNT(DISTINCT ticker) AS ticker_count,
                ? AS computed_at
            FROM agg_quarterly_holdings
            WHERE report_quarter = ?
            GROUP BY COALESCE(sector, 'Unknown'), report_quarter
            """,
            [now_iso, q],
        )


def get_available_quarters() -> List[str]:
    """Return all quarters available in agg_quarterly_holdings, ordered."""
    conn = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT report_quarter FROM agg_quarterly_holdings ORDER BY report_quarter"
            ).fetchall()
        ]
    finally:
        conn.close()


def get_qoq_data(
    quarter: str,
    min_shares_change_pct: Optional[float] = None,
    min_count_change_pct: Optional[float] = None,
    sectors: Optional[List[str]] = None,
    limit: int = 500,
) -> List[Dict]:
    """Fetch QoQ change data with optional filters."""
    conn = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    try:
        conditions = ["current_quarter = ?"]
        params: list = [quarter]

        if min_shares_change_pct is not None:
            conditions.append("shares_change_pct >= ?")
            params.append(min_shares_change_pct)

        if min_count_change_pct is not None:
            conditions.append("inst_count_change_pct >= ?")
            params.append(min_count_change_pct)

        if sectors:
            placeholders = ", ".join(["?"] * len(sectors))
            conditions.append(f"sector IN ({placeholders})")
            params.extend(sectors)

        where = " AND ".join(conditions)
        params.append(limit)

        rows = conn.execute(
            f"""
            SELECT * FROM agg_qoq_changes
            WHERE {where}
            ORDER BY shares_change_pct DESC NULLS LAST
            LIMIT ?
            """,
            params,
        ).fetchdf()

        return rows.to_dict("records") if not rows.empty else []
    finally:
        conn.close()
