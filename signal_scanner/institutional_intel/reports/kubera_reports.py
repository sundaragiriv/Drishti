"""Kubera Signature Reports — generation engine.

Produces the 12 signature reports from institutional data:
1. Ultimate Report (consistent accumulation)
2. Kubera Diamonds - Shares Change Uptrend (>=50% QoQ shares increase)
3. Kubera Diamonds - Price & Volume Gainers
4. Kubera Diamonds - CSAPV Gainers (multi-factor alignment)
5. Platinum Report (composite scoring)
6. Institutional Exit Analysis
7. Sector-Wise Allocation
8. 52-Week Low Reversal
9. Kubera Weekly Gems
10. Smart Investor Moves
11. AI Alerts (event-driven — handled separately)
12. Build Your Own (custom screening — handled by UI)
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH


class KuberaReports:
    """Generate all Kubera signature reports from warehouse data."""

    def __init__(self) -> None:
        self._warehouse_path = str(WAREHOUSE_PATH)

    def _connect(self):
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        conn = safe_duckdb_connect(read_only=True)
        if conn is None:
            raise ConnectionError("DuckDB locked — data temporarily unavailable")
        return conn

    # ------------------------------------------------------------------
    # 1. ULTIMATE REPORT — Consistent institutional accumulation
    # ------------------------------------------------------------------
    def ultimate_report(
        self,
        min_quarters: int = 2,
        quarter: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict]:
        """Stocks with consistent institutional count increase for N+ quarters.

        This is Kubera's flagship report: "REPORT OF ALL INSTITUTIONS
        COUNTS CONSISTENTLY INCREASED".
        """
        conn = self._connect()
        try:
            # If no quarter specified, use canonical active quarter
            if not quarter:
                quarter = self._latest_quarter(conn)
                if not quarter:
                    return []

            rows = conn.execute(
                """
                SELECT
                    q.ticker,
                    i.issuer_name AS company,
                    q.sector,
                    q.inst_count_current,
                    q.inst_count_prior,
                    q.inst_count_change,
                    q.inst_count_change_pct,
                    q.shares_current,
                    q.shares_prior,
                    q.shares_change_pct,
                    q.value_current_usd_k,
                    q.value_prior_usd_k,
                    q.value_change_pct,
                    q.count_up_streak,
                    q.shares_up_streak,
                    q.value_up_streak
                FROM agg_qoq_changes q
                LEFT JOIN dim_issuer i ON q.ticker = i.ticker
                WHERE q.current_quarter = ?
                  AND q.count_up_streak >= ?
                  AND q.inst_count_change > 0
                ORDER BY q.count_up_streak DESC, q.inst_count_change_pct DESC
                LIMIT ?
                """,
                [quarter, min_quarters, limit],
            ).fetchdf()

            return rows.to_dict("records") if not rows.empty else []
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 2. TS DIAMONDS — Shares Change Uptrend (>=50% QoQ)
    # ------------------------------------------------------------------
    def diamonds_shares_uptrend(
        self,
        min_shares_change_pct: float = 50.0,
        quarter: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict]:
        """Stocks where institutional shares surged >= threshold% QoQ."""
        conn = self._connect()
        try:
            quarter = quarter or self._latest_quarter(conn)
            if not quarter:
                return []

            rows = conn.execute(
                """
                SELECT
                    q.ticker,
                    i.issuer_name AS company,
                    q.sector,
                    q.shares_current,
                    q.shares_prior,
                    q.shares_change,
                    q.shares_change_pct,
                    q.inst_count_current,
                    q.inst_count_change_pct,
                    q.value_change_pct,
                    q.shares_up_streak
                FROM agg_qoq_changes q
                LEFT JOIN dim_issuer i ON q.ticker = i.ticker
                WHERE q.current_quarter = ?
                  AND q.shares_change_pct >= ?
                ORDER BY q.shares_change_pct DESC
                LIMIT ?
                """,
                [quarter, min_shares_change_pct, limit],
            ).fetchdf()

            return rows.to_dict("records") if not rows.empty else []
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 3. TS DIAMONDS — CSAPV Gainers (multi-factor alignment)
    # ------------------------------------------------------------------
    def diamonds_csapv_gainers(
        self,
        quarter: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict]:
        """Stocks aligned across Counts, Shares, Approx Value (all positive QoQ).

        Price and Volume alignment require IBKR price data to be joined
        downstream. This method filters for the institutional C-S-A factors.
        """
        conn = self._connect()
        try:
            quarter = quarter or self._latest_quarter(conn)
            if not quarter:
                return []

            rows = conn.execute(
                """
                SELECT
                    q.ticker,
                    i.issuer_name AS company,
                    q.sector,
                    q.inst_count_change AS count_change,
                    q.inst_count_change_pct AS count_change_pct,
                    q.shares_change,
                    q.shares_change_pct,
                    q.value_change_usd_k,
                    q.value_change_pct,
                    q.count_up_streak,
                    q.shares_up_streak,
                    q.value_up_streak
                FROM agg_qoq_changes q
                LEFT JOIN dim_issuer i ON q.ticker = i.ticker
                WHERE q.current_quarter = ?
                  AND q.inst_count_change > 0
                  AND q.shares_change > 0
                  AND q.value_change_usd_k > 0
                ORDER BY q.value_change_pct DESC
                LIMIT ?
                """,
                [quarter, limit],
            ).fetchdf()

            return rows.to_dict("records") if not rows.empty else []
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 4. PLATINUM REPORT — 5-factor composite scoring (ThinkSabio-style)
    # ------------------------------------------------------------------
    def platinum_report(
        self,
        quarter: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict]:
        """Multi-factor scored ranking: institutional accumulation, value growth,
        price momentum, volume momentum, and streak stability.

        Composite Score = accumulation * 0.30 + value_growth * 0.20
                        + price_momentum * 0.25 + volume_momentum * 0.10
                        + stability * 0.15
        """
        conn = self._connect()
        try:
            quarter = quarter or self._latest_quarter(conn)
            if not quarter:
                return []

            rows = conn.execute(
                """
                SELECT
                    q.ticker,
                    i.issuer_name AS company,
                    q.sector,
                    -- Institutional metrics
                    q.shares_prior,
                    q.shares_current,
                    q.shares_change_pct,
                    q.inst_count_prior,
                    q.inst_count_current,
                    q.value_current_usd_k,
                    q.inst_count_change_pct AS count_change_pct,
                    q.value_change_pct,
                    q.count_up_streak,
                    q.shares_up_streak,
                    q.value_up_streak,
                    -- Price/volume data
                    q.avg_price_current,
                    q.avg_price_prior,
                    q.avg_price_change_pct,
                    q.avg_volume_current,
                    q.avg_volume_prior,
                    q.avg_volume_change_pct,
                    q.current_price,
                    q.price_on_report_date,
                    q.price_returns_pct,
                    -- Component scores
                    LEAST(100.0, GREATEST(0.0,
                        COALESCE(q.inst_count_change_pct, 0) * 0.5 +
                        COALESCE(q.shares_change_pct, 0) * 0.3 +
                        q.count_up_streak * 10.0
                    )) AS accumulation_score,
                    LEAST(100.0, GREATEST(0.0,
                        COALESCE(q.value_change_pct, 0) * 0.5 +
                        q.value_up_streak * 10.0
                    )) AS value_growth_score,
                    LEAST(100.0, GREATEST(0.0,
                        COALESCE(q.avg_price_change_pct, 0) * 1.0 +
                        CASE WHEN q.price_returns_pct > 0 THEN 20.0 ELSE 0.0 END
                    )) AS price_momentum_score,
                    LEAST(100.0, GREATEST(0.0,
                        COALESCE(q.avg_volume_change_pct, 0) * 0.5
                    )) AS volume_momentum_score,
                    LEAST(100.0, GREATEST(0.0,
                        CASE WHEN q.count_up_streak >= 3 AND q.shares_up_streak >= 3
                             THEN 80.0
                             WHEN q.count_up_streak >= 2 AND q.shares_up_streak >= 2
                             THEN 60.0
                             WHEN q.count_up_streak >= 1 AND q.shares_up_streak >= 1
                             THEN 40.0
                             ELSE 20.0 END
                    )) AS stability_score
                FROM agg_qoq_changes q
                LEFT JOIN dim_issuer i ON q.ticker = i.ticker
                WHERE q.current_quarter = ?
                  AND q.inst_count_change > 0
                  AND q.shares_change > 0
                ORDER BY (
                    LEAST(100.0, GREATEST(0.0,
                        COALESCE(q.inst_count_change_pct, 0) * 0.5 +
                        COALESCE(q.shares_change_pct, 0) * 0.3 +
                        q.count_up_streak * 10.0
                    )) * 0.30 +
                    LEAST(100.0, GREATEST(0.0,
                        COALESCE(q.value_change_pct, 0) * 0.5 +
                        q.value_up_streak * 10.0
                    )) * 0.20 +
                    LEAST(100.0, GREATEST(0.0,
                        COALESCE(q.avg_price_change_pct, 0) * 1.0 +
                        CASE WHEN q.price_returns_pct > 0 THEN 20.0 ELSE 0.0 END
                    )) * 0.25 +
                    LEAST(100.0, GREATEST(0.0,
                        COALESCE(q.avg_volume_change_pct, 0) * 0.5
                    )) * 0.10 +
                    LEAST(100.0, GREATEST(0.0,
                        CASE WHEN q.count_up_streak >= 3 AND q.shares_up_streak >= 3
                             THEN 80.0
                             WHEN q.count_up_streak >= 2 AND q.shares_up_streak >= 2
                             THEN 60.0
                             WHEN q.count_up_streak >= 1 AND q.shares_up_streak >= 1
                             THEN 40.0
                             ELSE 20.0 END
                    )) * 0.15
                ) DESC
                LIMIT ?
                """,
                [quarter, limit],
            ).fetchdf()

            if rows.empty:
                return []

            results = rows.to_dict("records")
            for i, r in enumerate(results, 1):
                r["composite_score"] = round(
                    r["accumulation_score"] * 0.30
                    + r["value_growth_score"] * 0.20
                    + r["price_momentum_score"] * 0.25
                    + r["volume_momentum_score"] * 0.10
                    + r["stability_score"] * 0.15,
                    1,
                )
                r["rank"] = i
                cp = r.get("current_price")
                rp = r.get("price_on_report_date")
                r["price_diff"] = round(cp - rp, 2) if cp and rp else None
                for key in (
                    "accumulation_score", "value_growth_score",
                    "price_momentum_score", "volume_momentum_score",
                    "stability_score",
                ):
                    r[key] = round(r[key], 1)

            return results
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 5. INSTITUTIONAL EXIT ANALYSIS
    # ------------------------------------------------------------------
    def institutional_exits(
        self,
        quarter: Optional[str] = None,
        min_count_decrease: int = -2,
        limit: int = 500,
    ) -> List[Dict]:
        """Tracks where institutions are reducing or exiting positions."""
        conn = self._connect()
        try:
            quarter = quarter or self._latest_quarter(conn)
            if not quarter:
                return []

            rows = conn.execute(
                """
                SELECT
                    q.ticker,
                    i.issuer_name AS company,
                    q.sector,
                    q.inst_count_current,
                    q.inst_count_prior,
                    q.inst_count_change,
                    q.inst_count_change_pct,
                    q.shares_change_pct,
                    q.value_change_pct,
                    CASE
                        WHEN q.inst_count_change_pct <= -30 THEN 'MASS_EXIT'
                        WHEN q.inst_count_change_pct <= -15 THEN 'SIGNIFICANT'
                        ELSE 'PARTIAL'
                    END AS exit_severity
                FROM agg_qoq_changes q
                LEFT JOIN dim_issuer i ON q.ticker = i.ticker
                WHERE q.current_quarter = ?
                  AND q.inst_count_change <= ?
                ORDER BY q.inst_count_change ASC
                LIMIT ?
                """,
                [quarter, min_count_decrease, limit],
            ).fetchdf()

            return rows.to_dict("records") if not rows.empty else []
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 6. SECTOR-WISE ALLOCATION
    # ------------------------------------------------------------------
    def sector_allocation(
        self,
        quarter: Optional[str] = None,
    ) -> List[Dict]:
        """Institutional capital allocation across sectors for a quarter."""
        conn = self._connect()
        try:
            quarter = quarter or self._latest_quarter(conn)
            if not quarter:
                return []

            rows = conn.execute(
                """
                SELECT
                    sector,
                    total_inst_count,
                    total_shares,
                    total_value_usd_k,
                    ticker_count
                FROM agg_sector_quarterly
                WHERE report_quarter = ?
                ORDER BY total_value_usd_k DESC
                """,
                [quarter],
            ).fetchdf()

            if rows.empty:
                return []

            # Add allocation percentages
            total_value = rows["total_value_usd_k"].sum()
            results = rows.to_dict("records")
            for r in results:
                r["allocation_pct"] = round(
                    (r["total_value_usd_k"] / total_value * 100) if total_value > 0 else 0,
                    2,
                )
                r["report_quarter"] = quarter

            return results
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 7. SMART INVESTOR MOVES
    # ------------------------------------------------------------------
    def smart_investor_moves(
        self,
        quarter: Optional[str] = None,
        top_n_managers: int = 50,
        limit: int = 500,
    ) -> List[Dict]:
        """Track buying/selling behavior of top institutional managers."""
        conn = self._connect()
        try:
            quarter = quarter or self._latest_quarter(conn)
            if not quarter:
                return []

            # Find the quarter's report_period date range
            rows = conn.execute(
                """
                WITH top_managers AS (
                    SELECT manager_cik, manager_name,
                           SUM(value_usd_thousands) AS total_value
                    FROM fact_13f_positions
                    GROUP BY manager_cik, manager_name
                    ORDER BY total_value DESC
                    LIMIT ?
                ),
                current_positions AS (
                    SELECT
                        f.manager_cik,
                        f.manager_name,
                        f.ticker,
                        SUM(f.shares) AS shares_current,
                        SUM(f.value_usd_thousands) AS value_current
                    FROM fact_13f_positions f
                    INNER JOIN top_managers tm ON f.manager_cik = tm.manager_cik
                    WHERE CONCAT(
                        EXTRACT(YEAR FROM f.report_period)::TEXT,
                        '-Q',
                        (((EXTRACT(MONTH FROM f.report_period)::INT - 1) / 3) + 1)::TEXT
                    ) = ?
                    GROUP BY f.manager_cik, f.manager_name, f.ticker
                )
                SELECT
                    cp.manager_name,
                    cp.ticker,
                    i.issuer_name AS company,
                    cp.shares_current,
                    cp.value_current,
                    q.shares_change_pct,
                    q.inst_count_change,
                    CASE
                        WHEN q.shares_change_pct > 20 THEN 'BUYING'
                        WHEN q.shares_change_pct < -20 THEN 'SELLING'
                        ELSE 'HOLDING'
                    END AS action
                FROM current_positions cp
                LEFT JOIN agg_qoq_changes q
                    ON cp.ticker = q.ticker AND q.current_quarter = ?
                LEFT JOIN dim_issuer i ON cp.ticker = i.ticker
                ORDER BY cp.value_current DESC
                LIMIT ?
                """,
                [top_n_managers, quarter, quarter, limit],
            ).fetchdf()

            return rows.to_dict("records") if not rows.empty else []
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 8. INSIDER ACTIVITY SUMMARY
    # ------------------------------------------------------------------
    def insider_activity(
        self,
        days: int = 30,
        direction_filter: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict]:
        """Recent insider buying/selling from Form 4 filings."""
        conn = self._connect()
        try:
            conditions = [f"transaction_date >= CURRENT_DATE - INTERVAL '{days}' DAY"]
            params: list = []

            if direction_filter:
                conditions.append("direction = ?")
                params.append(direction_filter.upper())

            where = " AND ".join(conditions)
            params.append(limit)

            rows = conn.execute(
                f"""
                SELECT
                    ticker,
                    issuer_name,
                    insider_name,
                    transaction_date,
                    direction,
                    shares,
                    price,
                    shares * price AS transaction_value,
                    ownership_after
                FROM fact_form4_transactions
                WHERE {where}
                ORDER BY transaction_date DESC
                LIMIT ?
                """,
                params,
            ).fetchdf()

            return rows.to_dict("records") if not rows.empty else []
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 9. CUSTOM SCREENING (Build Your Own)
    # ------------------------------------------------------------------
    def custom_screen(
        self,
        quarter: Optional[str] = None,
        min_inst_count: Optional[int] = None,
        min_shares_change_pct: Optional[float] = None,
        max_shares_change_pct: Optional[float] = None,
        min_count_change_pct: Optional[float] = None,
        max_count_change_pct: Optional[float] = None,
        min_value_change_pct: Optional[float] = None,
        sectors: Optional[List[str]] = None,
        min_streak: Optional[int] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        limit: int = 500,
    ) -> List[Dict]:
        """Custom screening with flexible institutional + change filters."""
        conn = self._connect()
        try:
            quarter = quarter or self._latest_quarter(conn)
            if not quarter:
                return []

            conditions = ["q.current_quarter = ?"]
            params: list = [quarter]

            if min_inst_count is not None:
                conditions.append("q.inst_count_current >= ?")
                params.append(min_inst_count)

            if min_shares_change_pct is not None:
                conditions.append("q.shares_change_pct >= ?")
                params.append(min_shares_change_pct)

            if max_shares_change_pct is not None:
                conditions.append("q.shares_change_pct <= ?")
                params.append(max_shares_change_pct)

            if min_count_change_pct is not None:
                conditions.append("q.inst_count_change_pct >= ?")
                params.append(min_count_change_pct)

            if max_count_change_pct is not None:
                conditions.append("q.inst_count_change_pct <= ?")
                params.append(max_count_change_pct)

            if min_value_change_pct is not None:
                conditions.append("q.value_change_pct >= ?")
                params.append(min_value_change_pct)

            if sectors:
                placeholders = ", ".join(["?"] * len(sectors))
                conditions.append(f"q.sector IN ({placeholders})")
                params.extend(sectors)

            if min_streak is not None:
                conditions.append("q.count_up_streak >= ?")
                params.append(min_streak)

            if min_price is not None:
                conditions.append("q.current_price >= ?")
                params.append(min_price)

            if max_price is not None:
                conditions.append("q.current_price <= ?")
                params.append(max_price)

            where = " AND ".join(conditions)
            params.append(limit)

            rows = conn.execute(
                f"""
                SELECT
                    q.ticker,
                    i.issuer_name AS company,
                    q.sector,
                    q.current_price,
                    q.inst_count_current,
                    q.inst_count_prior,
                    q.inst_count_change,
                    q.inst_count_change_pct,
                    q.shares_current,
                    q.shares_prior,
                    q.shares_change_pct,
                    q.value_current_usd_k,
                    q.value_change_pct,
                    q.count_up_streak,
                    q.shares_up_streak
                FROM agg_qoq_changes q
                LEFT JOIN dim_issuer i ON q.ticker = i.ticker
                WHERE {where}
                ORDER BY q.shares_change_pct DESC NULLS LAST
                LIMIT ?
                """,
                params,
            ).fetchdf()

            return rows.to_dict("records") if not rows.empty else []
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 10. CONTRACT IDEAS (Options trade ideas from institutional intel)
    # ------------------------------------------------------------------
    def contract_ideas(
        self,
        quarter: Optional[str] = None,
        max_ideas: int = 30,
    ) -> List[Dict]:
        """Generate options contract ideas from institutional intelligence."""
        from signal_scanner.institutional_intel.reports.contract_ideas import KuberaContractIdeas
        engine = KuberaContractIdeas()
        return engine.generate_ideas(quarter=quarter, max_ideas=max_ideas)

    # ------------------------------------------------------------------
    # 11. AI SMART SIGNALS
    # ------------------------------------------------------------------
    def ai_signals(
        self,
        quarter: Optional[str] = None,
        lookback_days: int = 30,
        signal_types: Optional[List[str]] = None,
    ) -> List[Dict]:
        """Generate AI Smart Signals combining institutional + price data."""
        from signal_scanner.institutional_intel.reports.ai_signals import AISignalEngine
        engine = AISignalEngine()
        return engine.detect_signals(
            quarter=quarter,
            lookback_days=lookback_days,
            signal_types=signal_types,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _latest_quarter(self, conn: duckdb.DuckDBPyConnection) -> Optional[str]:
        from signal_scanner.institutional_intel.config import get_active_quarter
        q = get_active_quarter(conn)
        if q:
            return q
        # Fallback: latest from agg_qoq_changes
        row = conn.execute(
            "SELECT MAX(current_quarter) FROM agg_qoq_changes"
        ).fetchone()
        return row[0] if row and row[0] else None

    def get_available_quarters(self) -> List[str]:
        """Return all quarters in agg_qoq_changes, newest first."""
        conn = self._connect()
        try:
            return [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT current_quarter FROM agg_qoq_changes ORDER BY current_quarter DESC"
                ).fetchall()
            ]
        finally:
            conn.close()

    def get_available_quarter_options(self) -> List[Dict]:
        """Return dropdown options with ticker counts and quality labels."""
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT q.current_quarter,
                       COUNT(*) AS tickers,
                       MAX(COALESCE(s.data_quality_score, 100.0)) AS quality
                FROM agg_qoq_changes q
                LEFT JOIN intelligence_scores s
                    ON s.ticker = q.ticker AND s.report_quarter = q.current_quarter
                GROUP BY q.current_quarter
                ORDER BY q.current_quarter DESC
            """).fetchall()
            options = []
            for quarter, cnt, quality in rows:
                if quality is None or quality >= 75:
                    label = f"{quarter}  ({cnt:,} tickers)"
                else:
                    label = f"{quarter}  ({cnt:,} tickers — early data)"
                options.append({"label": label, "value": quarter})
            return options
        except Exception:
            # Fallback: plain list
            return [{"label": q, "value": q} for q in self.get_available_quarters()]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # NEW: Intraday Setup Scoring — surface scanner signals as flags
    # ------------------------------------------------------------------
    @staticmethod
    def _intraday_setup_score(row: Dict) -> int:
        """Compute 0-100 intraday setup score from scanner MTF row fields.

        Points (max 100):
          GEX Alignment        0-15
          VWAP Position         0-15
          Sweep Reclaim         0-15
          FVG Signal            0-10
          RSI Divergence        0-10
          MTF Agreement         0-15
          Signal Momentum       0-10
          Session Timing        0-10
        Institutional Overlay (+bonus, capped at 100):
          Phase alignment       +5
          Squeeze pressure      +5
          Insider cluster       +3
          Triple lock           +5
        """
        pts = 0
        signal = row.get("signal", "NEUTRAL")

        # 1. GEX Alignment (0-15)
        gex = row.get("gex_status", "")
        if signal == "LONG" and "ABOVE" in str(gex):
            pts += 15
        elif signal == "LONG" and "BELOW" in str(gex):
            pts += 5
        elif signal == "SHORT" and "BELOW" in str(gex):
            pts += 15
        elif signal == "SHORT" and "ABOVE" in str(gex):
            pts += 5
        elif gex and gex != "UNKNOWN":
            pts += 3

        # 2. VWAP Position (0-15)
        vwap_st = row.get("vwap_status", "")
        vwap_rev = row.get("vwap_reversion_signal", "NONE")
        if vwap_rev and vwap_rev != "NONE":
            pts += 15  # Mean-reversion signal is highest conviction
        elif signal == "LONG" and vwap_st == "ABOVE_VWAP":
            zscore = abs(row.get("vwap_zscore") or 0)
            pts += min(15, 8 + int(zscore * 3))
        elif signal == "SHORT" and vwap_st == "BELOW_VWAP":
            zscore = abs(row.get("vwap_zscore") or 0)
            pts += min(15, 8 + int(zscore * 3))
        elif vwap_st and vwap_st != "UNKNOWN":
            pts += 3

        # 3. Sweep Reclaim (0-15)
        sweep = row.get("sweep_reclaim_signal", "NONE")
        if signal == "LONG" and sweep == "BULLISH_SWEEP_RECLAIM":
            pts += 15
        elif signal == "SHORT" and sweep == "BEARISH_SWEEP_RECLAIM":
            pts += 15
        elif sweep and sweep != "NONE":
            pts += 7

        # 4. FVG Signal (0-10)
        fvg = row.get("fvg_signal", "NONE")
        if signal == "LONG" and "BULLISH" in str(fvg):
            pts += 10
        elif signal == "SHORT" and "BEARISH" in str(fvg):
            pts += 10
        elif fvg and fvg != "NONE":
            pts += 4

        # 5. RSI Divergence (0-10)
        if signal == "LONG" and row.get("rsi_bull_divergence"):
            pts += 10
        elif signal == "SHORT" and row.get("rsi_bear_divergence"):
            pts += 10

        # 6. MTF Agreement (0-15)
        mtf = row.get("mtf_agreement", "0/0")
        try:
            parts = str(mtf).split("/")
            agree = int(parts[0])
            total = int(parts[1]) if len(parts) > 1 else 1
            if agree == total and total >= 3:
                pts += 15
            elif agree == total and total >= 2:
                pts += 12
            elif agree >= 2:
                pts += 8
            elif agree >= 1:
                pts += 4
        except (ValueError, IndexError):
            pass

        # 7. Signal Momentum (0-10)
        momentum = row.get("signal_momentum", "")
        if momentum == "STRENGTHENING":
            pts += 10
        elif momentum == "STABLE":
            pts += 6
        elif momentum == "NEW":
            pts += 3

        # 8. Session Timing (0-10)
        session = row.get("session_time", "")
        if session == "POWER_HOUR":
            pts += 10
        elif session == "MID_DAY":
            pts += 6
        elif session == "EARLY":
            pts += 3

        # Cap base score at 100
        pts = min(100, pts)

        # Institutional Overlay (bonus, still capped at 100)
        phase = row.get("inst_phase", "")
        if signal == "LONG" and phase in ("ACTIVE_ACCUM", "EARLY_ACCUM", "LATE_ACCUM"):
            pts += 5
        elif signal == "SHORT" and phase in ("DISTRIBUTION", "DECLINE"):
            pts += 5

        squeeze = row.get("inst_short_squeeze") or row.get("inst_squeeze") or 0
        if signal == "LONG" and squeeze >= 50:
            pts += 5

        insider = row.get("inst_insider") or False
        if signal == "LONG" and insider:
            pts += 3

        triple = row.get("inst_triple_lock") or False
        if triple:
            pts += 5

        return min(100, pts)

    @staticmethod
    def _build_trigger_badges(row: Dict) -> str:
        """Build comma-separated trigger badge string for display."""
        badges = []
        signal = row.get("signal", "NEUTRAL")

        # GEX alignment
        gex = row.get("gex_status", "")
        if signal == "LONG" and "ABOVE" in str(gex):
            badges.append("GEX+")
        elif signal == "SHORT" and "BELOW" in str(gex):
            badges.append("GEX+")

        # VWAP
        vwap_st = row.get("vwap_status", "")
        vwap_rev = row.get("vwap_reversion_signal", "NONE")
        if vwap_rev and vwap_rev != "NONE":
            badges.append("VWAP-REV")
        elif (signal == "LONG" and vwap_st == "ABOVE_VWAP") or \
             (signal == "SHORT" and vwap_st == "BELOW_VWAP"):
            badges.append("VWAP+")

        # Sweep
        sweep = row.get("sweep_reclaim_signal", "NONE")
        if sweep and sweep != "NONE":
            badges.append("SWEEP")

        # FVG
        fvg = row.get("fvg_signal", "NONE")
        if fvg and fvg != "NONE":
            badges.append("FVG")

        # RSI Divergence
        if row.get("rsi_bull_divergence") or row.get("rsi_bear_divergence"):
            badges.append("RSI-DIV")

        # MTF agreement
        mtf = row.get("mtf_agreement", "0/0")
        try:
            parts = str(mtf).split("/")
            if int(parts[0]) == int(parts[1]) and int(parts[1]) >= 3:
                badges.append("MTF 3/3")
        except (ValueError, IndexError):
            pass

        # Institutional overlays
        phase = row.get("inst_phase", "")
        if (signal == "LONG" and phase in ("ACTIVE_ACCUM", "EARLY_ACCUM", "LATE_ACCUM")) or \
           (signal == "SHORT" and phase in ("DISTRIBUTION", "DECLINE")):
            badges.append("INST+")

        squeeze = row.get("inst_short_squeeze") or row.get("inst_squeeze") or 0
        if squeeze >= 50:
            badges.append("SQZ")

        insider = row.get("inst_insider") or False
        if insider:
            badges.append("INS")

        triple = row.get("inst_triple_lock") or False
        if triple:
            badges.append("3LOCK")

        return ", ".join(badges)

    def intraday_setups(self, scanner_results: List[Dict]) -> List[Dict]:
        """Score and filter scanner MTF results into intraday setups.

        Source: scanner.last_mtf_results (refreshed every scan cycle).
        Filter: setup_score >= 50 AND signal IN (LONG, SHORT)
        """
        setups = []
        for row in scanner_results:
            sig = row.get("signal", "NEUTRAL")
            rec = row.get("recommendation", "HOLD")
            if sig not in ("LONG", "SHORT"):
                continue
            if rec not in ("BUY", "SELL"):
                continue

            score = self._intraday_setup_score(row)
            if score < 50:
                continue

            badges = self._build_trigger_badges(row)
            setups.append({
                "symbol": row.get("symbol", ""),
                "signal": sig,
                "setup_score": score,
                "trigger_badges": badges,
                "session_time": row.get("session_time", ""),
                "stock_state": row.get("stock_state", ""),
                "price": row.get("price"),
                "stop_loss": row.get("stop_loss"),
                "target_1": row.get("target_1"),
                "target_2": row.get("target_2"),
                "rr_ratio": row.get("rr_ratio"),
                "mtf_agreement": row.get("mtf_agreement", ""),
                "gex_status": row.get("gex_status", ""),
                "vwap_status": row.get("vwap_status", ""),
                "inst_phase": row.get("inst_phase", ""),
                "inst_conviction": row.get("inst_conviction"),
                "signal_momentum": row.get("signal_momentum", ""),
                "recommendation": rec,
            })

        setups.sort(key=lambda x: x["setup_score"], reverse=True)
        return setups

    # ------------------------------------------------------------------
    # NEW: Swing Ideas — from intelligence_scores
    # ------------------------------------------------------------------
    def swing_ideas(self, quarter: Optional[str] = None, limit: int = 200) -> List[Dict]:
        """Swing trade ideas from intelligence_scores where swing_signal is actionable."""
        conn = self._connect()
        try:
            quarter = quarter or self._latest_quarter(conn)
            if not quarter:
                return []

            rows = conn.execute(
                """
                SELECT
                    s.ticker,
                    i.issuer_name AS company,
                    q.sector,
                    s.swing_signal,
                    s.swing_entry_zone,
                    s.swing_target,
                    s.swing_stop,
                    s.swing_options_suggestion,
                    s.accum_phase,
                    s.conviction_score,
                    COALESCE(s.ml_score_v2, 0) AS ml_score_v2,
                    s.tier1_manager_count,
                    s.insider_cluster_detected,
                    s.squeeze_score,
                    COALESCE(s.expected_value, 0) AS expected_value,
                    q.inst_count_change_pct,
                    q.shares_change_pct,
                    q.count_up_streak,
                    q.current_price AS price
                FROM intelligence_scores s
                LEFT JOIN dim_issuer i ON s.ticker = i.ticker
                LEFT JOIN agg_qoq_changes q ON s.ticker = q.ticker AND q.current_quarter = ?
                WHERE s.report_quarter = ?
                  AND s.swing_signal IN ('BUY', 'SHORT', 'WATCH')
                ORDER BY COALESCE(s.expected_value, 0) DESC, s.conviction_score DESC
                LIMIT ?
                """,
                [quarter, quarter, limit],
            ).fetchdf()

            return rows.to_dict("records") if not rows.empty else []
        except Exception as e:
            logger.error(f"Swing ideas error: {e}")
            return []
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # NEW: Tiered Report — 10-Confirmation system
    # ------------------------------------------------------------------
    def tiered_report(
        self,
        quarter: Optional[str] = None,
        min_confirms: int = 6,
        max_confirms: int = 10,
        limit: int = 200,
    ) -> List[Dict]:
        """10-confirmation longterm report.

        Tiers: Platinum (10/10) | Ultimate (8-9/10) | Gold (6-7/10)
        """
        conn = self._connect()
        try:
            quarter = quarter or self._latest_quarter(conn)
            if not quarter:
                return []

            rows = conn.execute(
                """
                WITH confirms AS (
                    SELECT
                        s.ticker,
                        i.issuer_name AS company,
                        q.sector,
                        s.accum_phase,
                        s.conviction_score,
                        COALESCE(s.ml_score_v2, 0) AS ml_score_v2,
                        s.tier1_manager_count,
                        s.insider_cluster_detected,
                        s.cascade_stage,
                        s.distribution_warning,
                        s.squeeze_score,
                        COALESCE(s.expected_value, 0) AS expected_value,
                        s.longterm_signal,
                        s.longterm_options_suggestion,
                        q.inst_count_change,
                        q.inst_count_change_pct,
                        q.shares_change_pct,
                        q.count_up_streak,
                        q.shares_up_streak,
                        q.avg_price_change_pct,
                        s.price_momentum_90d,
                        s.price_above_200sma,
                        q.current_price AS price,
                        -- 10 binary confirmations
                        CASE WHEN s.accum_phase IN ('ACTIVE_ACCUM','EARLY_ACCUM','LATE_ACCUM') THEN 1 ELSE 0 END AS c1_phase,
                        CASE WHEN q.inst_count_change > 0 AND q.count_up_streak >= 2 THEN 1 ELSE 0 END AS c2_inst_growth,
                        CASE WHEN q.shares_change_pct > 0 AND q.shares_up_streak >= 1 THEN 1 ELSE 0 END AS c3_shares_accum,
                        CASE WHEN s.insider_cluster_detected = TRUE THEN 1 ELSE 0 END AS c4_insider,
                        CASE WHEN s.tier1_manager_count >= 2 THEN 1 ELSE 0 END AS c5_tier1,
                        CASE WHEN s.conviction_score >= 50 AND COALESCE(s.ml_score_v2, 0) >= 50 THEN 1 ELSE 0 END AS c6_ml_conviction,
                        CASE WHEN s.price_above_200sma = 1 THEN 1 ELSE 0 END AS c7_sma200,
                        CASE WHEN s.price_momentum_90d > 0 AND q.avg_price_change_pct > 0 THEN 1 ELSE 0 END AS c8_price_mom,
                        CASE WHEN s.cascade_stage >= 1 THEN 1 ELSE 0 END AS c9_cascade,
                        CASE WHEN s.distribution_warning = FALSE OR s.distribution_warning IS NULL THEN 1 ELSE 0 END AS c10_no_dist
                    FROM intelligence_scores s
                    LEFT JOIN dim_issuer i ON s.ticker = i.ticker
                    LEFT JOIN agg_qoq_changes q ON s.ticker = q.ticker AND q.current_quarter = ?
                    WHERE s.report_quarter = ?
                )
                SELECT *,
                    (c1_phase + c2_inst_growth + c3_shares_accum + c4_insider + c5_tier1
                     + c6_ml_conviction + c7_sma200 + c8_price_mom + c9_cascade + c10_no_dist) AS confirmation_count
                FROM confirms
                WHERE (c1_phase + c2_inst_growth + c3_shares_accum + c4_insider + c5_tier1
                       + c6_ml_conviction + c7_sma200 + c8_price_mom + c9_cascade + c10_no_dist)
                      BETWEEN ? AND ?
                ORDER BY (c1_phase + c2_inst_growth + c3_shares_accum + c4_insider + c5_tier1
                          + c6_ml_conviction + c7_sma200 + c8_price_mom + c9_cascade + c10_no_dist) DESC,
                         conviction_score DESC
                LIMIT ?
                """,
                [quarter, quarter, min_confirms, max_confirms, limit],
            ).fetchdf()

            if rows.empty:
                return []

            results = rows.to_dict("records")
            for i, r in enumerate(results, 1):
                confirms = r["confirmation_count"]
                if confirms >= 10:
                    r["tier"] = "PLATINUM"
                elif confirms >= 8:
                    r["tier"] = "ULTIMATE"
                else:
                    r["tier"] = "GOLD"

                # Build missing confirmations string for Ultimate/Gold
                missing = []
                names = [
                    "Phase", "Inst Growth", "Shares Accum", "Insider",
                    "Tier-1", "ML+Conv", "SMA200", "Price Mom", "Cascade", "No Dist",
                ]
                for j, key in enumerate([
                    "c1_phase", "c2_inst_growth", "c3_shares_accum", "c4_insider",
                    "c5_tier1", "c6_ml_conviction", "c7_sma200", "c8_price_mom",
                    "c9_cascade", "c10_no_dist",
                ]):
                    if not r.get(key):
                        missing.append(names[j])
                r["missing"] = ", ".join(missing) if missing else ""
                r["rank"] = i

            return results
        except Exception as e:
            logger.error(f"Tiered report error: {e}")
            return []
        finally:
            conn.close()

    def platinum_report_v2(self, quarter: Optional[str] = None) -> List[Dict]:
        """Platinum tier — 10/10 confirmations."""
        return self.tiered_report(quarter=quarter, min_confirms=10, max_confirms=10)

    def ultimate_report_v2(self, quarter: Optional[str] = None) -> List[Dict]:
        """Ultimate tier — 8-9/10 confirmations."""
        return self.tiered_report(quarter=quarter, min_confirms=8, max_confirms=9)

    def gold_report(self, quarter: Optional[str] = None) -> List[Dict]:
        """Gold tier — 6-7/10 confirmations."""
        return self.tiered_report(quarter=quarter, min_confirms=6, max_confirms=7)

    # ------------------------------------------------------------------
    # NEW: Short Squeeze Report
    # ------------------------------------------------------------------
    def short_squeeze_report(
        self, quarter: Optional[str] = None, min_score: float = 15, limit: int = 200,
    ) -> List[Dict]:
        """Squeeze candidates from intelligence_scores."""
        conn = self._connect()
        try:
            quarter = quarter or self._latest_quarter(conn)
            if not quarter:
                return []

            rows = conn.execute(
                """
                SELECT
                    s.ticker,
                    i.issuer_name AS company,
                    s.short_squeeze_score,
                    s.squeeze_score AS base_squeeze,
                    s.days_to_cover,
                    s.short_volume_ratio_avg,
                    s.dark_pool_pct_avg,
                    s.accum_phase,
                    s.conviction_score,
                    s.insider_cluster_detected,
                    s.tier1_manager_count,
                    q.inst_count_change_pct,
                    q.current_price AS price,
                    CASE
                        WHEN s.squeeze_score >= 50
                             AND s.accum_phase IN ('ACTIVE_ACCUM','LATE_ACCUM','EARLY_ACCUM')
                             AND s.conviction_score >= 45
                             AND s.days_to_cover >= 3
                        THEN TRUE ELSE FALSE
                    END AS golden_squeeze
                FROM intelligence_scores s
                LEFT JOIN dim_issuer i ON s.ticker = i.ticker
                LEFT JOIN agg_qoq_changes q ON s.ticker = q.ticker AND q.current_quarter = ?
                WHERE s.report_quarter = ?
                  AND s.squeeze_score >= ?
                ORDER BY s.squeeze_score DESC
                LIMIT ?
                """,
                [quarter, quarter, min_score, limit],
            ).fetchdf()

            return rows.to_dict("records") if not rows.empty else []
        except Exception as e:
            logger.error(f"Short squeeze report error: {e}")
            return []
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # NEW: Stock Ideas summary for stat cards
    # ------------------------------------------------------------------
    def get_stock_ideas_summary(self, quarter: Optional[str] = None) -> Dict:
        """Get summary counts for new Stock Ideas stat cards."""
        conn = self._connect()
        try:
            quarter = quarter or self._latest_quarter(conn)
            if not quarter:
                return {"quarter": None}

            stats = {"quarter": quarter}

            # Swing ideas count
            row = conn.execute(
                "SELECT COUNT(*) FROM intelligence_scores WHERE report_quarter = ? AND swing_signal IN ('BUY','SHORT')",
                [quarter],
            ).fetchone()
            stats["swing_count"] = row[0] if row else 0

            # Tiered counts (use CTE logic)
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN confirms = 10 THEN 1 ELSE 0 END) AS platinum,
                    SUM(CASE WHEN confirms BETWEEN 8 AND 9 THEN 1 ELSE 0 END) AS ultimate,
                    SUM(CASE WHEN confirms BETWEEN 6 AND 7 THEN 1 ELSE 0 END) AS gold
                FROM (
                    SELECT
                        (CASE WHEN s.accum_phase IN ('ACTIVE_ACCUM','EARLY_ACCUM','LATE_ACCUM') THEN 1 ELSE 0 END
                         + CASE WHEN q.inst_count_change > 0 AND q.count_up_streak >= 2 THEN 1 ELSE 0 END
                         + CASE WHEN q.shares_change_pct > 0 AND q.shares_up_streak >= 1 THEN 1 ELSE 0 END
                         + CASE WHEN s.insider_cluster_detected = TRUE THEN 1 ELSE 0 END
                         + CASE WHEN s.tier1_manager_count >= 2 THEN 1 ELSE 0 END
                         + CASE WHEN s.conviction_score >= 50 AND COALESCE(s.ml_score_v2, 0) >= 50 THEN 1 ELSE 0 END
                         + CASE WHEN s.price_above_200sma = 1 THEN 1 ELSE 0 END
                         + CASE WHEN s.price_momentum_90d > 0 AND q.avg_price_change_pct > 0 THEN 1 ELSE 0 END
                         + CASE WHEN s.cascade_stage >= 1 THEN 1 ELSE 0 END
                         + CASE WHEN s.distribution_warning = FALSE OR s.distribution_warning IS NULL THEN 1 ELSE 0 END
                        ) AS confirms
                    FROM intelligence_scores s
                    LEFT JOIN agg_qoq_changes q ON s.ticker = q.ticker AND q.current_quarter = ?
                    WHERE s.report_quarter = ?
                ) sub
                """,
                [quarter, quarter],
            ).fetchone()
            stats["platinum_count"] = row[0] if row and row[0] else 0
            stats["ultimate_count"] = row[1] if row and row[1] else 0
            stats["gold_count"] = row[2] if row and row[2] else 0

            # Squeeze count
            row = conn.execute(
                "SELECT COUNT(*) FROM intelligence_scores WHERE report_quarter = ? AND short_squeeze_score >= 30",
                [quarter],
            ).fetchone()
            stats["squeeze_count"] = row[0] if row else 0

            return stats
        except Exception as e:
            logger.error(f"Stock ideas summary error: {e}")
            return {"quarter": quarter}
        finally:
            conn.close()

    def get_report_summary(self, quarter: Optional[str] = None) -> Dict:
        """Get summary stats for all reports for a given quarter."""
        conn = self._connect()
        try:
            quarter = quarter or self._latest_quarter(conn)
            if not quarter:
                return {"quarter": None, "reports": {}}

            stats = {}

            # Ultimate report count
            row = conn.execute(
                "SELECT COUNT(*) FROM agg_qoq_changes WHERE current_quarter = ? AND count_up_streak >= 2 AND inst_count_change > 0",
                [quarter],
            ).fetchone()
            stats["ultimate_report"] = row[0] if row else 0

            # Diamonds shares uptrend
            row = conn.execute(
                "SELECT COUNT(*) FROM agg_qoq_changes WHERE current_quarter = ? AND shares_change_pct >= 50",
                [quarter],
            ).fetchone()
            stats["diamonds_shares_uptrend"] = row[0] if row else 0

            # CSAPV aligned
            row = conn.execute(
                "SELECT COUNT(*) FROM agg_qoq_changes WHERE current_quarter = ? AND inst_count_change > 0 AND shares_change > 0 AND value_change_usd_k > 0",
                [quarter],
            ).fetchone()
            stats["diamonds_csapv"] = row[0] if row else 0

            # Institutional exits
            row = conn.execute(
                "SELECT COUNT(*) FROM agg_qoq_changes WHERE current_quarter = ? AND inst_count_change <= -2",
                [quarter],
            ).fetchone()
            stats["institutional_exits"] = row[0] if row else 0

            # Total tickers tracked
            row = conn.execute(
                "SELECT COUNT(DISTINCT ticker) FROM agg_qoq_changes WHERE current_quarter = ?",
                [quarter],
            ).fetchone()
            stats["total_tickers"] = row[0] if row else 0

            return {"quarter": quarter, "reports": stats}
        finally:
            conn.close()
