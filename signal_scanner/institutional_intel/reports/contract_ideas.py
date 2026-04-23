"""Kubera Contract Ideas — Options trade ideas from institutional intelligence.

Generates actionable options contract ideas by combining:
- Platinum Report (top composite-scored stocks)
- AI Smart Signals (convergence patterns)
- Insider Activity (buying surge)
- Institutional Exits (bearish put ideas)

Each idea includes: ticker, direction (CALL/PUT), strike selection logic,
expiry guidance, conviction level, and supporting institutional evidence.
"""

from datetime import date, datetime, timedelta, timezone
from math import ceil, floor
from typing import Dict, List, Optional

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH


class KuberaContractIdeas:
    """Generate options contract ideas from institutional intelligence data."""

    def __init__(self) -> None:
        self._warehouse_path = str(WAREHOUSE_PATH)

    def _connect(self):
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        conn = safe_duckdb_connect(read_only=True)
        if conn is None:
            raise ConnectionError("DuckDB locked — data temporarily unavailable")
        return conn

    def generate_ideas(
        self,
        quarter: Optional[str] = None,
        max_ideas: int = 30,
    ) -> List[Dict]:
        """Generate ranked contract ideas from all institutional signals.

        Returns list of idea dicts sorted by conviction score descending.
        """
        conn = self._connect()
        try:
            quarter = quarter or self._latest_quarter(conn)
            if not quarter:
                return []

            ideas: List[Dict] = []

            # Source 1: Platinum picks → CALL ideas
            ideas.extend(self._ideas_from_platinum(conn, quarter))

            # Source 2: Institutional exits → PUT ideas
            ideas.extend(self._ideas_from_exits(conn, quarter))

            # Source 3: Insider buying surge → CALL ideas
            ideas.extend(self._ideas_from_insider_buying(conn, quarter))

            # Source 4: Contrarian opportunities → CALL ideas
            ideas.extend(self._ideas_from_contrarian(conn, quarter))

            # ── INTELLIGENCE GATE ──────────────────────────────────────
            # Cross-reference with intelligence_scores to enforce phase
            # consistency.  CALL ideas are blocked for DISTRIBUTION/DECLINE,
            # PUT ideas are blocked for ACTIVE_ACCUM/EARLY_ACCUM.
            # Contract conviction is capped at intelligence conviction + 20
            # to prevent massive divergence.
            ideas = self._apply_intelligence_gate(conn, quarter, ideas)

            # Deduplicate by ticker (keep highest conviction)
            seen = {}
            for idea in ideas:
                tk = idea["ticker"]
                if tk not in seen or idea["conviction_score"] > seen[tk]["conviction_score"]:
                    seen[tk] = idea
            ideas = list(seen.values())

            # Sort by conviction descending
            ideas.sort(key=lambda x: x["conviction_score"], reverse=True)

            # ── OPTIONS FLOW ENRICHMENT ───────────────────────────────
            flow_tickers = list({idea["ticker"] for idea in ideas})
            flow_map = self.fetch_options_flow_batch(conn, flow_tickers)
            for idea in ideas:
                flow = flow_map.get(idea["ticker"], {})
                idea["pc_ratio"] = round(flow["put_call_ratio_vol"], 2) if flow.get("put_call_ratio_vol") else None
                idea["pc_sentiment"] = flow.get("sentiment", "")
                idea["gamma_wall"] = flow.get("max_call_oi_strike")
                idea["put_wall"] = flow.get("max_put_oi_strike")
                idea["avg_iv"] = round(flow["avg_call_iv"], 3) if flow.get("avg_call_iv") else None

                # Override strike with gamma/put wall when within 5% of spot
                price = idea.get("current_price") or 0
                if price > 0 and flow:
                    direction = idea.get("direction") or idea.get("option_type", "")
                    if direction == "CALL" and flow.get("max_call_oi_strike"):
                        wall = flow["max_call_oi_strike"]
                        if price < wall <= price * 1.05:
                            idea["strike"] = wall
                            idea["strike_note"] = "GAMMA-WALL"
                    elif direction == "PUT" and flow.get("max_put_oi_strike"):
                        wall = flow["max_put_oi_strike"]
                        if price * 0.95 <= wall < price:
                            idea["strike"] = wall
                            idea["strike_note"] = "PUT-WALL"

            # Assign rank + institutional pressure (squeezer bar)
            for i, idea in enumerate(ideas[:max_ideas], 1):
                idea["rank"] = i
                idea["inst_pressure"] = self.compute_inst_pressure(
                    conn, idea["ticker"], quarter
                )

            return ideas[:max_ideas]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Intelligence Gate — enforce phase + conviction consistency
    # ------------------------------------------------------------------

    def _apply_intelligence_gate(
        self,
        conn: duckdb.DuckDBPyConnection,
        quarter: str,
        ideas: List[Dict],
    ) -> List[Dict]:
        """Filter + cap ideas based on intelligence_scores phase & conviction.

        - CALL ideas blocked for DISTRIBUTION / DECLINE phase stocks
        - PUT ideas blocked for ACTIVE_ACCUM / EARLY_ACCUM phase stocks
        - Contract conviction capped at intel conviction + 20 (prevents
          an 88-conviction CALL on a stock with real conviction 18)
        """
        if not ideas:
            return ideas

        tickers = list({idea["ticker"] for idea in ideas})
        if not tickers:
            return ideas

        # Batch-fetch intelligence data for all tickers
        intel_map: Dict[str, Dict] = {}
        try:
            placeholders = ",".join(["?"] * len(tickers))
            rows = conn.execute(f"""
                SELECT ticker, accum_phase, conviction_score, swing_signal,
                       distribution_warning
                FROM intelligence_scores
                WHERE report_quarter = ? AND ticker IN ({placeholders})
            """, [quarter] + tickers).fetchall()
            for r in rows:
                intel_map[r[0]] = {
                    "phase": r[1] or "",
                    "conviction": r[2] or 0,
                    "swing_signal": r[3] or "",
                    "dist_warning": bool(r[4]),
                }
        except Exception as exc:
            logger.debug("Intelligence gate query failed: {}", exc)
            return ideas  # Pass through ungated on error

        BEARISH_PHASES = {"DISTRIBUTION", "DECLINE"}
        BULLISH_PHASES = {"ACTIVE_ACCUM", "EARLY_ACCUM", "LATE_ACCUM"}

        gated: List[Dict] = []
        dropped = 0
        for idea in ideas:
            ticker = idea["ticker"]
            intel = intel_map.get(ticker)

            if not intel:
                # No intelligence data — keep idea but lower conviction
                idea["conviction_score"] = min(idea["conviction_score"], 50)
                gated.append(idea)
                continue

            phase = intel["phase"]
            intel_conv = intel["conviction"]
            direction = idea.get("direction", "CALL")

            # Phase gate: block contradictory ideas
            if direction == "CALL" and phase in BEARISH_PHASES:
                dropped += 1
                continue  # Don't suggest buying a stock smart money is exiting
            if direction == "PUT" and phase in BULLISH_PHASES:
                dropped += 1
                continue  # Don't suggest shorting a stock being accumulated

            # Conviction cap: contract conviction shouldn't wildly exceed
            # intelligence conviction.  Allow +20 headroom for momentum.
            max_conv = intel_conv + 20
            if idea["conviction_score"] > max_conv:
                idea["conviction_score"] = max_conv

            # Swing signal cross-check: AVOID = cap at 40
            if intel["swing_signal"] == "AVOID" and direction == "CALL":
                idea["conviction_score"] = min(idea["conviction_score"], 40)

            gated.append(idea)

        if dropped:
            logger.info("Intelligence gate: dropped {} contradictory ideas", dropped)

        return gated

    # ------------------------------------------------------------------
    # Source 1: Platinum Report → high-conviction CALL ideas
    # ------------------------------------------------------------------
    def _ideas_from_platinum(
        self, conn: duckdb.DuckDBPyConnection, quarter: str
    ) -> List[Dict]:
        rows = conn.execute(
            """
            SELECT
                q.ticker,
                i.issuer_name AS company,
                q.sector,
                q.current_price,
                q.avg_price_current,
                q.avg_price_change_pct,
                q.avg_volume_current,
                q.avg_volume_change_pct,
                q.inst_count_current,
                q.inst_count_change,
                q.inst_count_change_pct,
                q.shares_change_pct,
                q.value_change_pct,
                q.count_up_streak,
                q.shares_up_streak,
                q.price_returns_pct
            FROM agg_qoq_changes q
            LEFT JOIN dim_issuer i ON q.ticker = i.ticker
            WHERE q.current_quarter = ?
              AND q.inst_count_change > 0
              AND q.shares_change > 0
              AND q.current_price IS NOT NULL
              AND q.current_price > 5
            ORDER BY (
                COALESCE(q.inst_count_change_pct, 0) * 0.3 +
                COALESCE(q.shares_change_pct, 0) * 0.2 +
                COALESCE(q.avg_price_change_pct, 0) * 0.25 +
                q.count_up_streak * 10.0
            ) DESC
            LIMIT 20
            """,
            [quarter],
        ).fetchall()

        ideas = []
        columns = [
            "ticker", "company", "sector", "current_price", "avg_price_current",
            "avg_price_change_pct", "avg_volume_current", "avg_volume_change_pct",
            "inst_count_current", "inst_count_change", "inst_count_change_pct",
            "shares_change_pct", "value_change_pct", "count_up_streak",
            "shares_up_streak", "price_returns_pct",
        ]

        for row in rows:
            d = dict(zip(columns, row))
            price = d["current_price"] or 0
            if price <= 5:
                continue

            # Conviction scoring: institutional accumulation + price momentum
            conviction = 0.0
            streak = d["count_up_streak"] or 0
            shares_chg = d["shares_change_pct"] or 0
            count_chg = d["inst_count_change_pct"] or 0
            price_chg = d["avg_price_change_pct"] or 0

            # Streak bonus (up to 30 pts)
            conviction += min(30, streak * 10)
            # Shares accumulation (up to 25 pts)
            conviction += min(25, max(0, shares_chg * 0.5))
            # Institutional count growth (up to 20 pts)
            conviction += min(20, max(0, count_chg * 0.4))
            # Price momentum alignment (up to 15 pts)
            if price_chg and price_chg > 0:
                conviction += min(15, price_chg * 0.5)
            # Value growth (up to 10 pts)
            val_chg = d["value_change_pct"] or 0
            conviction += min(10, max(0, val_chg * 0.2))

            conviction = min(100, round(conviction, 1))
            if conviction < 25:
                continue

            strike = _pick_strike(price, "CALL")
            expiry = _pick_expiry(conviction)

            evidence = []
            if streak >= 2:
                evidence.append(f"{streak}Q accumulation streak")
            if shares_chg > 20:
                evidence.append(f"+{shares_chg:.0f}% shares QoQ")
            if count_chg > 10:
                evidence.append(f"+{count_chg:.0f}% inst count")
            if price_chg and price_chg > 0:
                evidence.append(f"+{price_chg:.1f}% avg price")
            inst = d["inst_count_current"]
            if inst:
                evidence.append(f"{inst} institutions holding")

            ideas.append({
                "ticker": d["ticker"],
                "company": d["company"] or "",
                "sector": d["sector"] or "Unknown",
                "direction": "CALL",
                "option_type": "CALL",
                "current_price": round(price, 2),
                "strike": strike,
                "expiry_guidance": expiry,
                "conviction_score": conviction,
                "conviction_label": _conviction_label(conviction),
                "source": "Platinum Report",
                "rationale": " | ".join(evidence[:4]),
                "inst_count": d["inst_count_current"],
                "inst_change": d["inst_count_change"],
                "shares_change_pct": round(shares_chg, 1),
                "price_change_pct": round(price_chg, 1) if price_chg else None,
                "streak": streak,
            })

        return ideas

    # ------------------------------------------------------------------
    # Source 2: Institutional Exits → PUT ideas
    # ------------------------------------------------------------------
    def _ideas_from_exits(
        self, conn: duckdb.DuckDBPyConnection, quarter: str
    ) -> List[Dict]:
        rows = conn.execute(
            """
            SELECT
                q.ticker,
                i.issuer_name AS company,
                q.sector,
                q.current_price,
                q.inst_count_current,
                q.inst_count_prior,
                q.inst_count_change,
                q.inst_count_change_pct,
                q.shares_change_pct,
                q.value_change_pct,
                q.avg_price_change_pct,
                q.price_returns_pct
            FROM agg_qoq_changes q
            LEFT JOIN dim_issuer i ON q.ticker = i.ticker
            WHERE q.current_quarter = ?
              AND q.inst_count_change_pct <= -15
              AND q.current_price IS NOT NULL
              AND q.current_price > 5
            ORDER BY q.inst_count_change_pct ASC
            LIMIT 15
            """,
            [quarter],
        ).fetchall()

        ideas = []
        columns = [
            "ticker", "company", "sector", "current_price",
            "inst_count_current", "inst_count_prior", "inst_count_change",
            "inst_count_change_pct", "shares_change_pct", "value_change_pct",
            "avg_price_change_pct", "price_returns_pct",
        ]

        for row in rows:
            d = dict(zip(columns, row))
            price = d["current_price"] or 0
            if price <= 5:
                continue

            count_chg = d["inst_count_change_pct"] or 0
            shares_chg = d["shares_change_pct"] or 0
            price_chg = d["avg_price_change_pct"] or 0

            conviction = 0.0
            # Severity of exit (up to 40 pts)
            conviction += min(40, abs(count_chg) * 0.8)
            # Shares also declining (up to 25 pts)
            if shares_chg < 0:
                conviction += min(25, abs(shares_chg) * 0.5)
            # Price declining confirms bearish thesis (up to 20 pts)
            if price_chg and price_chg < 0:
                conviction += min(20, abs(price_chg) * 1.0)
            # Value declining (up to 15 pts)
            val_chg = d["value_change_pct"] or 0
            if val_chg < 0:
                conviction += min(15, abs(val_chg) * 0.3)

            conviction = min(100, round(conviction, 1))
            if conviction < 30:
                continue

            strike = _pick_strike(price, "PUT")
            expiry = _pick_expiry(conviction)

            severity = "MASS EXIT" if count_chg <= -30 else "SIGNIFICANT EXIT"
            evidence = [
                severity,
                f"{count_chg:.0f}% inst count drop",
            ]
            if shares_chg < 0:
                evidence.append(f"{shares_chg:.0f}% shares decline")
            if price_chg and price_chg < 0:
                evidence.append(f"{price_chg:.1f}% avg price drop")

            ideas.append({
                "ticker": d["ticker"],
                "company": d["company"] or "",
                "sector": d["sector"] or "Unknown",
                "direction": "PUT",
                "option_type": "PUT",
                "current_price": round(price, 2),
                "strike": strike,
                "expiry_guidance": expiry,
                "conviction_score": conviction,
                "conviction_label": _conviction_label(conviction),
                "source": "Exit Analysis",
                "rationale": " | ".join(evidence[:4]),
                "inst_count": d["inst_count_current"],
                "inst_change": d["inst_count_change"],
                "shares_change_pct": round(shares_chg, 1),
                "price_change_pct": round(price_chg, 1) if price_chg else None,
                "streak": 0,
            })

        return ideas

    # ------------------------------------------------------------------
    # Source 3: Insider buying surge → CALL ideas
    # ------------------------------------------------------------------
    def _ideas_from_insider_buying(
        self, conn: duckdb.DuckDBPyConnection, quarter: str
    ) -> List[Dict]:
        try:
            rows = conn.execute(
                """
                SELECT
                    f.ticker,
                    i.issuer_name AS company,
                    q.sector,
                    q.current_price,
                    COUNT(*) AS buy_count,
                    SUM(f.shares * f.price) AS total_buy_value,
                    q.inst_count_change_pct,
                    q.shares_change_pct,
                    q.count_up_streak
                FROM fact_form4_transactions f
                LEFT JOIN dim_issuer i ON f.ticker = i.ticker
                LEFT JOIN agg_qoq_changes q
                    ON f.ticker = q.ticker AND q.current_quarter = ?
                WHERE f.direction = 'BUY'
                  AND f.transaction_date >= CURRENT_DATE - INTERVAL '60' DAY
                  AND f.ticker IS NOT NULL
                  AND q.current_price IS NOT NULL
                  AND q.current_price > 5
                GROUP BY f.ticker, i.issuer_name, q.sector, q.current_price,
                         q.inst_count_change_pct, q.shares_change_pct, q.count_up_streak
                HAVING COUNT(*) >= 2
                ORDER BY COUNT(*) DESC, SUM(f.shares * f.price) DESC
                LIMIT 15
                """,
                [quarter],
            ).fetchall()
        except Exception as e:
            logger.debug("Insider buying query error: {}", e)
            return []

        ideas = []
        columns = [
            "ticker", "company", "sector", "current_price", "buy_count",
            "total_buy_value", "inst_count_change_pct", "shares_change_pct",
            "count_up_streak",
        ]

        for row in rows:
            d = dict(zip(columns, row))
            price = d["current_price"] or 0
            buy_count = d["buy_count"] or 0
            buy_value = d["total_buy_value"] or 0

            conviction = 0.0
            # Number of insider buys (up to 35 pts)
            conviction += min(35, buy_count * 10)
            # Buy value magnitude (up to 25 pts)
            if buy_value > 1_000_000:
                conviction += 25
            elif buy_value > 500_000:
                conviction += 20
            elif buy_value > 100_000:
                conviction += 15
            elif buy_value > 50_000:
                conviction += 10
            # Institutional backing (up to 20 pts)
            count_chg = d["inst_count_change_pct"] or 0
            if count_chg > 0:
                conviction += min(20, count_chg * 0.4)
            # Streak bonus (up to 20 pts)
            streak = d["count_up_streak"] or 0
            conviction += min(20, streak * 8)

            conviction = min(100, round(conviction, 1))
            if conviction < 25:
                continue

            strike = _pick_strike(price, "CALL")
            expiry = _pick_expiry(conviction)

            evidence = [f"{buy_count} insider BUYs in 60 days"]
            if buy_value > 0:
                evidence.append(f"${buy_value:,.0f} total value")
            if count_chg > 0:
                evidence.append(f"+{count_chg:.0f}% inst count")
            if streak >= 2:
                evidence.append(f"{streak}Q accumulation streak")

            ideas.append({
                "ticker": d["ticker"],
                "company": d["company"] or "",
                "sector": d["sector"] or "Unknown",
                "direction": "CALL",
                "option_type": "CALL",
                "current_price": round(price, 2),
                "strike": strike,
                "expiry_guidance": expiry,
                "conviction_score": conviction,
                "conviction_label": _conviction_label(conviction),
                "source": "Insider Buying",
                "rationale": " | ".join(evidence[:4]),
                "inst_count": None,
                "inst_change": None,
                "shares_change_pct": round(d["shares_change_pct"], 1) if d["shares_change_pct"] else None,
                "price_change_pct": None,
                "streak": streak,
            })

        return ideas

    # ------------------------------------------------------------------
    # Source 4: Contrarian — accumulation at depressed prices
    # ------------------------------------------------------------------
    def _ideas_from_contrarian(
        self, conn: duckdb.DuckDBPyConnection, quarter: str
    ) -> List[Dict]:
        try:
            rows = conn.execute(
                """
                SELECT
                    q.ticker,
                    i.issuer_name AS company,
                    q.sector,
                    q.current_price,
                    q.avg_price_current,
                    q.avg_price_change_pct,
                    q.inst_count_change,
                    q.inst_count_change_pct,
                    q.shares_change_pct,
                    q.count_up_streak,
                    q.price_returns_pct,
                    -- Price near low: negative avg_price_change but inst accumulating
                    p_52w.low_52w
                FROM agg_qoq_changes q
                LEFT JOIN dim_issuer i ON q.ticker = i.ticker
                LEFT JOIN (
                    SELECT ticker, MIN(close) AS low_52w
                    FROM fact_daily_prices
                    WHERE trade_date >= CURRENT_DATE - INTERVAL '365' DAY
                      AND close IS NOT NULL AND close > 0
                    GROUP BY ticker
                ) p_52w ON q.ticker = p_52w.ticker
                WHERE q.current_quarter = ?
                  AND q.inst_count_change > 0
                  AND q.shares_change > 0
                  AND q.current_price IS NOT NULL
                  AND q.current_price > 5
                  AND p_52w.low_52w IS NOT NULL
                  AND q.current_price <= p_52w.low_52w * 1.15
                ORDER BY q.shares_change_pct DESC
                LIMIT 10
                """,
                [quarter],
            ).fetchall()
        except Exception:
            return []

        ideas = []
        columns = [
            "ticker", "company", "sector", "current_price", "avg_price_current",
            "avg_price_change_pct", "inst_count_change", "inst_count_change_pct",
            "shares_change_pct", "count_up_streak", "price_returns_pct", "low_52w",
        ]

        for row in rows:
            d = dict(zip(columns, row))
            price = d["current_price"] or 0
            low_52w = d["low_52w"] or 0

            conviction = 0.0
            # Close to 52-week low (up to 30 pts)
            if low_52w > 0:
                dist = ((price - low_52w) / low_52w) * 100
                conviction += max(0, 30 - dist * 2)
            # Institutional accumulation despite low price (up to 30 pts)
            shares_chg = d["shares_change_pct"] or 0
            conviction += min(30, max(0, shares_chg * 0.5))
            # Count change (up to 20 pts)
            count_chg = d["inst_count_change_pct"] or 0
            conviction += min(20, max(0, count_chg * 0.4))
            # Streak (up to 20 pts)
            streak = d["count_up_streak"] or 0
            conviction += min(20, streak * 8)

            conviction = min(100, round(conviction, 1))
            if conviction < 30:
                continue

            strike = _pick_strike(price, "CALL")
            expiry = _pick_expiry(conviction, longer_dated=True)

            evidence = [
                "Contrarian: near 52W low",
                f"Price ${price:.2f} vs 52W low ${low_52w:.2f}",
            ]
            if shares_chg > 0:
                evidence.append(f"+{shares_chg:.0f}% shares despite low price")
            if streak >= 2:
                evidence.append(f"{streak}Q accumulation streak")

            ideas.append({
                "ticker": d["ticker"],
                "company": d["company"] or "",
                "sector": d["sector"] or "Unknown",
                "direction": "CALL",
                "option_type": "CALL",
                "current_price": round(price, 2),
                "strike": strike,
                "expiry_guidance": expiry,
                "conviction_score": conviction,
                "conviction_label": _conviction_label(conviction),
                "source": "Contrarian",
                "rationale": " | ".join(evidence[:4]),
                "inst_count": None,
                "inst_change": d["inst_count_change"],
                "shares_change_pct": round(shares_chg, 1),
                "price_change_pct": round(d["avg_price_change_pct"], 1) if d["avg_price_change_pct"] else None,
                "streak": streak,
            })

        return ideas

    # ------------------------------------------------------------------
    # NEW: Weekly Options Ideas (0-7 DTE) — From Intraday Setups
    # ------------------------------------------------------------------
    def weekly_options_ideas(self, scanner_results: List[Dict]) -> List[Dict]:
        """Generate 0-7 DTE options ideas from intraday setups.

        Source: Intraday setups with setup_score >= 60.
        Strike: GEX-informed, near gamma walls for directional plays.
        Expiry: Session-aware (0DTE for POWER_HOUR+VERY_STRONG, 2-3DTE mid-day, etc.)
        """
        from signal_scanner.institutional_intel.reports.kubera_reports import KuberaReports

        # Pre-fetch options flow for all scanner symbols
        _weekly_flow_map: Dict[str, Dict] = {}
        try:
            _syms = list({r.get("symbol", "") for r in scanner_results if r.get("symbol")})
            if _syms:
                _wconn = self._connect()
                _weekly_flow_map = self.fetch_options_flow_batch(_wconn, _syms)
                _wconn.close()
        except Exception:
            pass  # graceful degradation

        ideas = []
        for row in scanner_results:
            sig = row.get("signal", "NEUTRAL")
            rec = row.get("recommendation", "HOLD")
            if sig not in ("LONG", "SHORT") or rec not in ("BUY", "SELL"):
                continue

            score = KuberaReports._intraday_setup_score(row)
            if score < 60:
                continue

            price = row.get("price") or 0
            if price <= 5:
                continue

            option_type = "CALL" if sig == "LONG" else "PUT"
            strike = _pick_strike(price, option_type)

            # Session-aware expiry
            session = row.get("session_time", "")
            state = row.get("stock_state", "")
            if session == "POWER_HOUR" and state == "VERY_STRONG":
                expiry = "0 DTE (Same-Day)"
            elif session in ("MID_DAY", "POWER_HOUR") and state in ("CONFIRMED", "VERY_STRONG"):
                expiry = "2-3 DTE"
            else:
                expiry = "5-7 DTE (Friday)"

            # Compute flags
            flags = []
            gex = row.get("gex_status", "")
            squeeze = row.get("inst_short_squeeze") or row.get("inst_squeeze") or 0
            if "BELOW_ZERO_GAMMA" in str(gex) and squeeze >= 50:
                flags.append("GAMMA-SQZ")
            if session in ("MID_DAY", "POWER_HOUR") and state in ("CONFIRMED", "VERY_STRONG") and score >= 70:
                flags.append("0DTE")
            sweep = row.get("sweep_reclaim_signal", "NONE")
            if sweep and sweep != "NONE":
                flags.append("SWEEP")
            fvg = row.get("fvg_signal", "NONE")
            if fvg and "BULLISH" in str(fvg) or "BEARISH" in str(fvg):
                flags.append("FVG")
            vwap_rev = row.get("vwap_reversion_signal", "NONE")
            if vwap_rev and vwap_rev != "NONE":
                flags.append("VWAP-REV")
            if row.get("rsi_bull_divergence") or row.get("rsi_bear_divergence"):
                flags.append("RSI-DIV")
            phase = row.get("inst_phase", "")
            if (sig == "LONG" and phase in ("ACTIVE_ACCUM", "EARLY_ACCUM", "LATE_ACCUM")) or \
               (sig == "SHORT" and phase in ("DISTRIBUTION", "DECLINE")):
                flags.append("INST+")

            # Options flow flags
            _flow = _weekly_flow_map.get(row.get("symbol", ""), {})
            if _flow.get("unusual_put_flag") and sig == "SHORT":
                flags.append("PUT-FLOW")
            if _flow.get("unusual_call_flag") and sig == "LONG":
                flags.append("CALL-FLOW")

            ideas.append({
                "symbol": row.get("symbol", ""),
                "direction": option_type,
                "strike": strike,
                "expiry_guidance": expiry,
                "setup_score": score,
                "flags": ", ".join(flags),
                "price": round(price, 2),
                "stop_loss": row.get("stop_loss"),
                "target_1": row.get("target_1"),
                "signal": sig,
                "session_time": session,
                "stock_state": state,
                "gex_status": gex,
                "mtf_agreement": row.get("mtf_agreement", ""),
                "pc_ratio": round(_flow["put_call_ratio_vol"], 2) if _flow.get("put_call_ratio_vol") else None,
                "pc_sentiment": _flow.get("sentiment", ""),
            })

        ideas.sort(key=lambda x: x["setup_score"], reverse=True)
        return ideas[:30]

    # ------------------------------------------------------------------
    # NEW: LEAPS Ideas (6-18 months) — From Longterm Confirmations
    # ------------------------------------------------------------------
    def leaps_ideas(self, quarter: Optional[str] = None, limit: int = 30) -> List[Dict]:
        """Generate LEAPS ideas from Ultimate/Platinum tier stocks (8+ confirmations)."""
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
                        q.current_price AS price,
                        s.accum_phase,
                        s.conviction_score,
                        s.longterm_signal,
                        s.longterm_options_suggestion,
                        s.expected_impact_quarters,
                        s.tier1_manager_count,
                        s.insider_cluster_detected,
                        s.cascade_stage,
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
                        ) AS confirmation_count
                    FROM intelligence_scores s
                    LEFT JOIN dim_issuer i ON s.ticker = i.ticker
                    LEFT JOIN agg_qoq_changes q ON s.ticker = q.ticker AND q.current_quarter = ?
                    WHERE s.report_quarter = ?
                )
                SELECT * FROM confirms
                WHERE confirmation_count >= 8
                  AND price IS NOT NULL AND price > 10
                ORDER BY confirmation_count DESC, conviction_score DESC
                LIMIT ?
                """,
                [quarter, quarter, limit],
            ).fetchall()

            columns = [
                "ticker", "company", "sector", "price", "accum_phase",
                "conviction_score", "longterm_signal", "longterm_options_suggestion",
                "expected_impact_quarters", "tier1_manager_count",
                "insider_cluster_detected", "cascade_stage", "confirmation_count",
            ]
            ideas = []
            for row in rows:
                d = dict(zip(columns, row))
                price = d["price"] or 0
                confirms = d["confirmation_count"]

                # Strike: ATM for Platinum (10/10), 5% OTM for Ultimate (8-9)
                if confirms >= 10:
                    strike = _pick_strike(price, "CALL")  # ~2% OTM
                    tier = "PLATINUM"
                else:
                    increment = 1.0 if price < 100 else (2.5 if price < 300 else 5.0)
                    target = price * 1.05  # 5% OTM
                    strike = round(ceil(target / increment) * increment, 2)
                    tier = "ULTIMATE"

                # Expiry: impact quarters * 3 + buffer (9-18m range)
                impact_q = d["expected_impact_quarters"] or 2
                months = min(18, max(9, impact_q * 3 + 3))
                expiry = f"{months}m LEAPS"

                ideas.append({
                    "ticker": d["ticker"],
                    "company": d["company"] or "",
                    "sector": d["sector"] or "Unknown",
                    "direction": "CALL",
                    "strike": strike,
                    "expiry_guidance": expiry,
                    "price": round(price, 2),
                    "tier": tier,
                    "confirmation_count": confirms,
                    "conviction_score": d["conviction_score"],
                    "longterm_signal": d["longterm_signal"] or "",
                    "leaps_suggestion": d["longterm_options_suggestion"] or "",
                    "accum_phase": d["accum_phase"] or "",
                    "tier1_count": d["tier1_manager_count"],
                    "insider": d["insider_cluster_detected"],
                    "cascade": d["cascade_stage"],
                })

            return ideas
        except Exception as e:
            logger.error(f"LEAPS ideas error: {e}")
            return []
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # NEW: Institutional Pressure (Squeezer Bar) 0-100
    # ------------------------------------------------------------------
    def compute_inst_pressure(self, conn: duckdb.DuckDBPyConnection, ticker: str, quarter: str) -> float:
        """Compute institutional pressure score (0-100) for squeezer bar."""
        try:
            row = conn.execute(
                """
                SELECT
                    s.conviction_score,
                    s.squeeze_score,
                    s.tier1_manager_count,
                    s.insider_cluster_detected,
                    s.ceo_cfo_buying,
                    s.accum_phase,
                    s.cascade_stage
                FROM intelligence_scores s
                WHERE s.ticker = ? AND s.report_quarter = ?
                """,
                [ticker, quarter],
            ).fetchone()
            if not row:
                return 0.0

            conviction, squeeze, tier1, insider, ceo_cfo, phase, cascade = row
            pressure = 0.0
            pressure += min(25, (conviction or 0) / 4)
            pressure += min(20, (squeeze or 0) / 5)
            pressure += min(15, (tier1 or 0) * 5)
            if insider:
                pressure += 15
            elif ceo_cfo:
                pressure += 10
            phase_map = {"ACTIVE_ACCUM": 15, "LATE_ACCUM": 12, "EARLY_ACCUM": 8}
            pressure += phase_map.get(phase or "", 0)
            pressure += min(10, (cascade or 0) * 4)

            # Options flow component (up to ~13 pts)
            try:
                of_row = conn.execute(
                    """SELECT put_call_ratio_vol, unusual_call_flag
                       FROM fact_options_flow
                       WHERE ticker = ?
                       ORDER BY snapshot_date DESC LIMIT 1""",
                    [ticker],
                ).fetchone()
                if of_row:
                    pcr = of_row[0] or 1.0
                    if phase in ("ACTIVE_ACCUM", "LATE_ACCUM", "EARLY_ACCUM") and pcr < 0.7:
                        pressure += 8  # bullish flow confirms accumulation
                    if of_row[1]:  # unusual_call_flag
                        pressure += 5
            except Exception:
                pass

            return min(100, round(pressure, 1))
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # Options Flow helpers
    # ------------------------------------------------------------------
    @staticmethod
    def fetch_options_flow_batch(
        conn: duckdb.DuckDBPyConnection,
        tickers: List[str],
    ) -> Dict[str, Dict]:
        """Batch-fetch latest options flow snapshot for *tickers*.

        Returns ``{ticker: {put_call_ratio_vol, put_call_ratio_oi,
        avg_call_iv, avg_put_iv, max_call_oi_strike, max_put_oi_strike,
        unusual_call_flag, unusual_put_flag, sentiment}}``.
        Returns ``{}`` on any error (graceful degradation).
        """
        if not tickers:
            return {}
        try:
            placeholders = ", ".join(["?"] * len(tickers))
            rows = conn.execute(
                f"""
                SELECT f.ticker, f.put_call_ratio_vol, f.put_call_ratio_oi,
                       f.avg_call_iv, f.avg_put_iv,
                       f.max_call_oi_strike, f.max_put_oi_strike,
                       f.unusual_call_flag, f.unusual_put_flag,
                       f.call_volume, f.put_volume
                FROM fact_options_flow f
                INNER JOIN (
                    SELECT ticker, MAX(snapshot_date) AS latest
                    FROM fact_options_flow
                    WHERE ticker IN ({placeholders})
                    GROUP BY ticker
                ) g ON f.ticker = g.ticker AND f.snapshot_date = g.latest
                """,
                tickers,
            ).fetchall()

            result: Dict[str, Dict] = {}
            for r in rows:
                pcr = r[1] or 1.0
                if pcr < 0.5:
                    sentiment = "BULLISH"
                elif pcr > 1.2:
                    sentiment = "BEARISH"
                else:
                    sentiment = "NEUTRAL"

                result[r[0]] = {
                    "put_call_ratio_vol": r[1],
                    "put_call_ratio_oi": r[2],
                    "avg_call_iv": r[3],
                    "avg_put_iv": r[4],
                    "max_call_oi_strike": r[5],
                    "max_put_oi_strike": r[6],
                    "unusual_call_flag": r[7],
                    "unusual_put_flag": r[8],
                    "call_volume": r[9],
                    "put_volume": r[10],
                    "sentiment": sentiment,
                }
            return result
        except Exception as exc:
            logger.debug("Options flow batch fetch failed: {}", exc)
            return {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _latest_quarter(self, conn: duckdb.DuckDBPyConnection) -> Optional[str]:
        from signal_scanner.institutional_intel.config import get_active_quarter
        q = get_active_quarter(conn)
        if q:
            return q
        row = conn.execute(
            "SELECT MAX(current_quarter) FROM agg_qoq_changes"
        ).fetchone()
        return row[0] if row and row[0] else None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _pick_strike(price: float, option_type: str) -> float:
    """Select a slightly OTM strike rounded to tradable increments."""
    increment = 1.0 if price < 100 else (2.5 if price < 300 else 5.0)
    offset_pct = 0.02  # ~2% OTM
    if option_type == "CALL":
        target = price * (1.0 + offset_pct)
        return round(ceil(target / increment) * increment, 2)
    target = price * (1.0 - offset_pct)
    return round(floor(target / increment) * increment, 2)


def _pick_expiry(conviction: float, longer_dated: bool = False) -> str:
    """Return expiry guidance string based on conviction level."""
    if longer_dated:
        if conviction >= 70:
            return "30-45 DTE (Monthly)"
        return "45-60 DTE (Monthly+)"

    if conviction >= 75:
        return "7-14 DTE (Weekly)"
    elif conviction >= 50:
        return "14-30 DTE (Bi-weekly)"
    else:
        return "30-45 DTE (Monthly)"


def _conviction_label(score: float) -> str:
    if score >= 75:
        return "HIGH"
    elif score >= 50:
        return "MEDIUM"
    else:
        return "LOW"
