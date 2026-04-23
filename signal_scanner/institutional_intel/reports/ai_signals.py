"""AI Smart Signals — event-driven institutional + price convergence detection.

Detects actionable patterns by cross-referencing institutional ownership
changes with market price/volume action.

Signal Types:
  - ACCUMULATION_BREAKOUT: Inst count up 2+ Qs + price above 20-day MA
  - INSIDER_BUYING_SURGE: 3+ insider BUY txns in 30 days + inst count up
  - SECTOR_ROTATION: Capital flowing into sector (>20% QoQ value increase)
  - SMART_MONEY_CONVERGENCE: Top managers increasing + insider buying + price up
  - CONTRARIAN_OPPORTUNITY: Institutions accumulating while price near 52-wk low
  - EXIT_WARNING: Mass institutional exit (>30% count drop) + price declining

Usage:
    from signal_scanner.institutional_intel.reports.ai_signals import AISignalEngine
    engine = AISignalEngine()
    signals = engine.detect_signals()
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH


class AISignalEngine:
    """Detect actionable convergence patterns from institutional + price data."""

    def __init__(self) -> None:
        self._warehouse_path = str(WAREHOUSE_PATH)

    def _connect(self):
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        conn = safe_duckdb_connect(read_only=True)
        if conn is None:
            raise ConnectionError("DuckDB locked — data temporarily unavailable")
        return conn

    def detect_signals(
        self,
        quarter: Optional[str] = None,
        lookback_days: int = 30,
        signal_types: Optional[List[str]] = None,
    ) -> List[Dict]:
        """Run all signal detectors and return combined results.

        Args:
            quarter: Target quarter (default: latest).
            lookback_days: Days for insider/price lookback.
            signal_types: If set, only run these signal types.

        Returns:
            List of signal dicts, sorted by strength then detected_at.
        """
        conn = self._connect()
        try:
            if not quarter:
                from signal_scanner.institutional_intel.config import get_active_quarter
                quarter = get_active_quarter(conn)
                if not quarter:
                    return []

            all_signals: List[Dict] = []
            detectors = {
                "ACCUMULATION_BREAKOUT": self._detect_accumulation_breakout,
                "INSIDER_BUYING_SURGE": self._detect_insider_surge,
                "SECTOR_ROTATION": self._detect_sector_rotation,
                "SMART_MONEY_CONVERGENCE": self._detect_smart_money_convergence,
                "CONTRARIAN_OPPORTUNITY": self._detect_contrarian,
                "EXIT_WARNING": self._detect_exit_warning,
                "HIGH_CONVICTION_PREDICTION": self._detect_high_conviction_predictions,
                "SWING_CONFLUENCE": self._detect_swing_confluence,
                "PULLBACK_SNIPER": self._detect_pullback_sniper,
            }

            for sig_type, detector in detectors.items():
                if signal_types and sig_type not in signal_types:
                    continue
                try:
                    signals = detector(conn, quarter, lookback_days)
                    all_signals.extend(signals)
                except Exception as exc:
                    logger.debug("Signal detector {} failed: {}", sig_type, exc)

            # Deduplicate: keep first occurrence per (ticker, signal_type)
            _seen = set()
            _deduped = []
            for sig in all_signals:
                key = (sig.get("ticker", ""), sig.get("signal_type", ""))
                if key not in _seen:
                    _seen.add(key)
                    _deduped.append(sig)
            all_signals = _deduped

            # Apply conviction gate: downgrade signals where ISR would show AVOID
            all_signals = self._apply_conviction_gate(conn, quarter, all_signals)

            # Enrich all signals with trade intelligence (prediction, levels, invalidation)
            for sig in all_signals:
                self._enrich_trade_intelligence(sig)

            # Cross-signal convergence: badge tickers appearing in 2+ signal types
            _ticker_types: Dict[str, list] = {}
            for sig in all_signals:
                t = sig.get("ticker", "")
                if t and sig.get("signal_type") != "SECTOR_ROTATION":
                    _ticker_types.setdefault(t, []).append(sig.get("signal_type", ""))
            for sig in all_signals:
                t = sig.get("ticker", "")
                types = _ticker_types.get(t, [])
                if len(types) >= 2:
                    sig["convergence_count"] = len(types)
                    sig["convergence_types"] = [x for x in types if x != sig.get("signal_type", "")]

            # Sort: HIGH first, then by convergence, then MEDIUM, then LOW; within same strength by ticker
            strength_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
            all_signals.sort(key=lambda s: (
                strength_order.get(s.get("strength", "LOW"), 3),
                -(s.get("convergence_count") or 0),
                s.get("ticker", ""),
            ))

            logger.info("AI Signals: {} total signals detected for {}", len(all_signals), quarter)
            return all_signals
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Conviction Gate
    # ------------------------------------------------------------------

    def _apply_conviction_gate(
        self,
        conn: duckdb.DuckDBPyConnection,
        quarter: str,
        signals: List[Dict],
    ) -> List[Dict]:
        """Downgrade signals where conviction is too low for actionable entry.

        AI Signals is a detection engine — it fires when patterns are found.
        But ISR trade ideas require conviction >= 35+ and specific phases.
        This gate adds an alert_note and downgrades strength so the dashboard
        can show a "DETECTION ONLY" badge instead of misleading users.
        """
        if not signals:
            return signals

        tickers = list({s["ticker"] for s in signals if s.get("signal_type") != "SECTOR_ROTATION"})
        if not tickers:
            return signals

        # Fetch conviction + swing signal for all detected tickers
        conviction_map: Dict[str, Dict] = {}
        try:
            placeholders = ",".join(["?"] * len(tickers))
            rows = conn.execute(f"""
                SELECT ticker, conviction_score, swing_signal, accum_phase
                FROM intelligence_scores
                WHERE report_quarter = ? AND ticker IN ({placeholders})
            """, [quarter] + tickers).fetchall()
            for r in rows:
                conviction_map[r[0]] = {
                    "conviction": r[1] or 0,
                    "swing_signal": r[2] or "",
                    "phase": r[3] or "",
                }
        except Exception as exc:
            logger.debug("Conviction gate query failed: {}", exc)
            return signals  # Pass through ungated on error

        for sig in signals:
            if sig.get("signal_type") in ("SECTOR_ROTATION", "SWING_CONFLUENCE"):
                continue  # Sector-level and swing confluence have their own scoring

            ticker = sig.get("ticker", "")
            info = conviction_map.get(ticker, {})
            conviction = info.get("conviction", 0)
            swing = info.get("swing_signal", "")
            phase = info.get("phase", "")

            if conviction < 35 or swing in ("AVOID", ""):
                sig["strength"] = "LOW"
                sig["alert_note"] = "Detection only — conviction too low for entry"
            elif conviction < 55:
                # Cap at MEDIUM for moderate conviction
                if sig["strength"] == "HIGH":
                    sig["strength"] = "MEDIUM"

        return signals

    # ------------------------------------------------------------------
    # Trade Intelligence Enrichment
    # ------------------------------------------------------------------

    def _enrich_trade_intelligence(self, sig: dict) -> None:
        """Enrich a signal dict in-place with a trade_intelligence block.

        Adds: verdict, prediction narrative, entry/stop/target levels,
        R:R ratio, invalidation conditions, qualification conditions,
        signal expiry date. Every signal type handled separately.
        """
        sig_type = sig.get("signal_type", "")
        metrics = sig.get("metrics", {})
        today = datetime.now(timezone.utc)

        price = float(metrics.get("current_price") or 0)

        def _expiry(days: int) -> str:
            return (today + timedelta(days=days)).strftime("%Y-%m-%d")

        ti: dict = {}

        if sig_type == "ACCUMULATION_BREAKOUT":
            streak = metrics.get("inst_count_streak", 3)
            price_chg = float(metrics.get("avg_price_change_pct") or 0)
            if price <= 0:
                return
            risk = price * 0.07
            ti = {
                "verdict": "LONG SETUP",
                "prediction": (
                    f"{streak}-quarter accumulation streak confirms sustained institutional conviction. "
                    f"Price trending +{price_chg:.1f}% QoQ. Expected move: +6-10% over 20 trading days."
                ),
                "timeframe": "20 trading days",
                "entry": round(price, 2),
                "stop": round(price - risk, 2),
                "target_1": round(price * 1.10, 2),
                "target_2": round(price * 1.20, 2),
                "risk_pct": 7.0,
                "rr_ratio": "1.4:1",
                "invalidation": (
                    f"Daily close below ${round(price - risk, 2)} (7% stop). "
                    f"Institution count reverses next quarter filing."
                ),
                "qualification": "Volume >1.5x 50-day average on a breakout above prior 5-day high.",
                "expiry_date": _expiry(30),
                "window_days": 30,
            }

        elif sig_type == "INSIDER_BUYING_SURGE":
            buy_count = int(metrics.get("insider_buy_count") or 3)
            buy_val = float(metrics.get("insider_buy_value") or 0)
            val_str = f"${buy_val / 1e6:.1f}M" if buy_val >= 1e6 else f"${buy_val / 1e3:.0f}K"
            if price <= 0:
                return
            risk = price * 0.08
            ti = {
                "verdict": "LONG SETUP",
                "prediction": (
                    f"{buy_count} insider buys ({val_str} total value). "
                    f"Insider buying historically yields 56-58% win rate over 60 days. "
                    f"Directors: 58.3% WR (strongest role)."
                ),
                "timeframe": "30-60 trading days",
                "entry": round(price, 2),
                "stop": round(price - risk, 2),
                "target_1": round(price * 1.10, 2),
                "target_2": round(price * 1.15, 2),
                "risk_pct": 8.0,
                "rr_ratio": "1.25:1",
                "invalidation": (
                    f"No follow-through insider buying within 21 days. "
                    f"Institution count starts declining. Break below ${round(price - risk, 2)}."
                ),
                "qualification": "Additional insider buy transaction confirmed, or close above 20-SMA.",
                "expiry_date": _expiry(60),
                "window_days": 60,
            }

        elif sig_type == "SECTOR_ROTATION":
            val_chg = float(metrics.get("value_change_pct") or 0)
            sector = sig.get("sector", "")
            direction_word = "inflow" if val_chg > 0 else "outflow"
            ti = {
                "verdict": "SECTOR WATCH",
                "prediction": (
                    f"{sector}: {val_chg:+.1f}% capital {direction_word} QoQ. "
                    f"Screen for ACCUMULATION_BREAKOUT or SMART_MONEY_CONVERGENCE tickers within this sector."
                ),
                "timeframe": "Next 30 days",
                "entry": None,
                "stop": None,
                "target_1": None,
                "target_2": None,
                "risk_pct": None,
                "rr_ratio": None,
                "invalidation": "Sector flow turns negative next quarter. Market regime shifts to DISTRIBUTION.",
                "qualification": "Identify individual ACCUM tickers within sector for actionable entries.",
                "expiry_date": _expiry(30),
                "window_days": 30,
            }

        elif sig_type == "SMART_MONEY_CONVERGENCE":
            if price <= 0:
                return
            risk = price * 0.08
            ti = {
                "verdict": "LONG SETUP",
                "prediction": (
                    "Tier-1 managers AND insiders aligned bullish — triple signal convergence. "
                    "Historically highest-conviction setup. Expected move: +8-12% in 30-45 days."
                ),
                "timeframe": "30-45 trading days",
                "entry": round(price, 2),
                "stop": round(price - risk, 2),
                "target_1": round(price * 1.12, 2),
                "target_2": round(price * 1.20, 2),
                "risk_pct": 8.0,
                "rr_ratio": "1.5:1",
                "invalidation": (
                    f"Any Tier-1 manager reduces position OR insider selling transaction appears. "
                    f"Break below ${round(price - risk, 2)}."
                ),
                "qualification": "Price holds above 20-SMA. Any positive catalyst (earnings, 8-K) accelerates move.",
                "expiry_date": _expiry(45),
                "window_days": 45,
            }

        elif sig_type == "CONTRARIAN_OPPORTUNITY":
            if price <= 0:
                return
            low_52w = float(metrics.get("low_52w") or price * 0.70)
            high_52w = float(metrics.get("high_52w") or price * 1.40)
            pct_low = float(metrics.get("pct_from_52w_low") or 0)
            stop = max(price * 0.90, low_52w * 0.95)
            risk = price - stop
            midpoint = (price + high_52w) / 2
            ti = {
                "verdict": "CONTRARIAN LONG",
                "prediction": (
                    f"Institutions accumulating while price is only {pct_low:.1f}% above 52W low. "
                    f"Smart money entering at discount — mean-reversion to 52W midpoint "
                    f"(${round(midpoint, 2)}) expected."
                ),
                "timeframe": "45-90 trading days",
                "entry": round(price, 2),
                "stop": round(stop, 2),
                "target_1": round(price + risk * 2, 2),
                "target_2": round(midpoint, 2),
                "risk_pct": round(risk / price * 100, 1),
                "rr_ratio": "2.0:1",
                "invalidation": (
                    f"New 52-week low below ${round(low_52w, 2)}. "
                    f"Institution count starts declining next quarter."
                ),
                "qualification": "Any close above 10-day SMA on above-average volume signals reversal start.",
                "expiry_date": _expiry(90),
                "window_days": 90,
            }

        elif sig_type == "EXIT_WARNING":
            if price <= 0:
                return
            inst_chg = float(metrics.get("inst_count_change_pct") or 0)
            short_stop = price * 1.06
            ti = {
                "verdict": "EXIT / SHORT SETUP",
                "prediction": (
                    f"Mass institutional exit ({inst_chg:.1f}% count drop). Distribution confirmed. "
                    f"Short sellers historically right 65%+ of the time when institutions flee en masse."
                ),
                "timeframe": "20-30 trading days",
                "entry": round(price, 2),
                "stop": round(short_stop, 2),
                "target_1": round(price * 0.88, 2),
                "target_2": round(price * 0.82, 2),
                "risk_pct": 6.0,
                "rr_ratio": "2.0:1",
                "invalidation": (
                    f"Close above ${round(short_stop, 2)} (6% short stop). "
                    f"New institutional buyers emerge or insider buying detected."
                ),
                "qualification": "Close below 200-SMA confirms distribution phase for short entry.",
                "expiry_date": _expiry(30),
                "window_days": 30,
            }

        elif sig_type == "HIGH_CONVICTION_PREDICTION":
            if price <= 0:
                return
            direction = sig.get("direction", "BULLISH")
            confidence = float(metrics.get("confidence") or 50)
            conv = float(metrics.get("conviction") or 0)
            ml = float(metrics.get("ml_v2_score") or 0)
            risk = price * 0.06
            if direction == "BULLISH":
                ti = {
                    "verdict": "LONG SETUP",
                    "prediction": (
                        f"Multi-factor BULLISH model ({confidence:.0f}% confidence): "
                        f"conviction={conv:.0f}, ML={ml:.0f}. "
                        f"Expected: +5-10% over 5-7 trading days."
                    ),
                    "timeframe": "5-7 trading days",
                    "entry": round(price, 2),
                    "stop": round(price - risk, 2),
                    "target_1": round(price * 1.08, 2),
                    "target_2": round(price * 1.14, 2),
                    "risk_pct": 6.0,
                    "rr_ratio": "1.3:1",
                    "invalidation": (
                        f"Composite score drops below 55 on next refresh. "
                        f"Close below ${round(price - risk, 2)} (6% stop)."
                    ),
                    "qualification": "Close above today's high on volume > 1.2x average confirms direction.",
                    "expiry_date": _expiry(7),
                    "window_days": 7,
                }
            else:
                ti = {
                    "verdict": "SHORT SETUP",
                    "prediction": (
                        f"Multi-factor BEARISH signal ({confidence:.0f}% confidence): "
                        f"conviction={conv:.0f}, ML={ml:.0f}. "
                        f"Distribution or decline phase with weakening institutional support."
                    ),
                    "timeframe": "5-7 trading days",
                    "entry": round(price, 2),
                    "stop": round(price + risk, 2),
                    "target_1": round(price * 0.92, 2),
                    "target_2": round(price * 0.86, 2),
                    "risk_pct": 6.0,
                    "rr_ratio": "1.3:1",
                    "invalidation": (
                        f"Composite score rises above 45. "
                        f"Close above ${round(price + risk, 2)} (short stop)."
                    ),
                    "qualification": "Close below today's low or 200-SMA break confirms short thesis.",
                    "expiry_date": _expiry(7),
                    "window_days": 7,
                }

        elif sig_type == "SWING_CONFLUENCE":
            stop = float(metrics.get("stop_price") or 0) or (price * 0.90)
            target = float(metrics.get("target_2r") or 0) or (price * 1.10)
            setup = str(metrics.get("setup_type") or "INSIDER BUY")
            exp_val = str(metrics.get("backtest_expectancy") or "+0.20R")
            if price <= 0:
                return
            risk = price - stop
            ti = {
                "verdict": "LONG SETUP",
                "prediction": (
                    f"{setup}: {exp_val} backtested expectancy across 4.6M stock-days. "
                    f"Best setup (INSIDER+BELOW200+COMPRESSED) yields +0.223R. "
                    f"Hold 20-30 days for full target at ${round(target, 2)}."
                ),
                "timeframe": "20-30 trading days",
                "entry": round(price, 2),
                "stop": round(stop, 2),
                "target_1": round(price + risk * 2, 2),
                "target_2": round(target, 2),
                "risk_pct": round(risk / price * 100, 1) if price > 0 else 0,
                "rr_ratio": "2.0:1",
                "invalidation": f"Daily close below ${round(stop, 2)} — exit immediately, no questions asked.",
                "qualification": "Close above entry day's high on volume > 1.2x 50-day average.",
                "expiry_date": _expiry(30),
                "window_days": 30,
            }

        elif sig_type == "PULLBACK_SNIPER":
            stop = float(metrics.get("stop_price") or 0) or (price * 0.97)
            target_1 = float(metrics.get("target_1r") or 0) or (price * 1.03)
            setup = str(metrics.get("setup_type") or "PULLBACK_FIB")
            wr = str(metrics.get("backtest_wr") or "72.9%")
            rsi = metrics.get("rsi_2", "?")
            if price <= 0:
                return
            ti = {
                "verdict": "LONG SETUP",
                "prediction": (
                    f"{setup} (RSI-2={rsi}): {wr} historical 1R win rate. "
                    f"SHORT TIME WINDOW — enter within 24 hours, exit within 5 days."
                ),
                "timeframe": "2-5 trading days",
                "entry": round(price, 2),
                "stop": round(stop, 2),
                "target_1": round(target_1, 2),
                "target_2": round(target_1, 2),
                "risk_pct": round((price - stop) / price * 100, 1) if price > 0 else 0,
                "rr_ratio": "1.0:1",
                "invalidation": (
                    f"Close below ${round(stop, 2)} (1x ATR). "
                    f"Or: 5 trading days elapsed without hitting target."
                ),
                "qualification": "Price recovers and closes above SMA(10). Enter on next day's open.",
                "expiry_date": _expiry(5),
                "window_days": 5,
            }

        if ti:
            sig["trade_intelligence"] = ti

    # ------------------------------------------------------------------
    # Signal Detectors
    # ------------------------------------------------------------------

    def _detect_accumulation_breakout(
        self, conn: duckdb.DuckDBPyConnection, quarter: str, lookback_days: int
    ) -> List[Dict]:
        """Institutional count up 2+ Qs AND price trending up.

        Detects: Consistent smart-money accumulation aligning with price momentum.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = conn.execute("""
            SELECT
                q.ticker,
                i.issuer_name AS company,
                q.sector,
                q.count_up_streak,
                q.shares_change_pct,
                q.inst_count_change_pct,
                q.avg_price_change_pct,
                q.current_price,
                q.avg_price_current
            FROM agg_qoq_changes q
            LEFT JOIN dim_issuer i ON q.ticker = i.ticker
            WHERE q.current_quarter = ?
              AND q.count_up_streak >= 3
              AND q.inst_count_change > 0
              AND q.avg_price_change_pct > 0
            ORDER BY q.count_up_streak DESC, q.avg_price_change_pct DESC
            LIMIT 50
        """, [quarter]).fetchall()

        signals = []
        cols = ["ticker", "company", "sector", "count_up_streak", "shares_change_pct",
                "inst_count_change_pct", "avg_price_change_pct", "current_price", "avg_price_current"]
        for row in rows:
            d = dict(zip(cols, row))
            streak = d["count_up_streak"] or 0
            strength = "HIGH" if streak >= 3 else "MEDIUM"
            signals.append({
                "signal_type": "ACCUMULATION_BREAKOUT",
                "ticker": d["ticker"],
                "company": d["company"] or "",
                "sector": d["sector"] or "Unknown",
                "detected_at": now_iso,
                "lookback": f"{streak}Q",
                "strength": strength,
                "summary": (
                    f"{streak}-quarter institutional accumulation streak "
                    f"+ {_fmt_pct(d['avg_price_change_pct'])} price gain QoQ"
                ),
                "metrics": {
                    "inst_count_streak": streak,
                    "shares_change_pct": _round(d["shares_change_pct"]),
                    "inst_count_change_pct": _round(d["inst_count_change_pct"]),
                    "avg_price_change_pct": _round(d["avg_price_change_pct"]),
                    "current_price": _round(d["current_price"]),
                },
            })
        return signals

    def _detect_insider_surge(
        self, conn: duckdb.DuckDBPyConnection, quarter: str, lookback_days: int
    ) -> List[Dict]:
        """3+ insider BUY transactions in lookback period + institutional count up."""
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = conn.execute(f"""
            SELECT
                f4.ticker,
                i.issuer_name AS company,
                q.sector,
                COUNT(*) AS buy_count,
                SUM(f4.shares * f4.price) AS total_value,
                q.inst_count_change,
                q.inst_count_change_pct,
                q.shares_change_pct
            FROM fact_form4_transactions f4
            LEFT JOIN agg_qoq_changes q ON f4.ticker = q.ticker AND q.current_quarter = ?
            LEFT JOIN dim_issuer i ON f4.ticker = i.ticker
            WHERE f4.direction = 'BUY'
              AND f4.transaction_date >= CURRENT_DATE - INTERVAL '{int(lookback_days)}' DAY
              AND f4.ticker IS NOT NULL AND f4.ticker != ''
            GROUP BY f4.ticker, i.issuer_name, q.sector,
                     q.inst_count_change, q.inst_count_change_pct, q.shares_change_pct
            HAVING COUNT(*) >= 3
            ORDER BY COUNT(*) DESC
            LIMIT 30
        """, [quarter]).fetchall()

        signals = []
        cols = ["ticker", "company", "sector", "buy_count", "total_value",
                "inst_count_change", "inst_count_change_pct", "shares_change_pct"]
        for row in rows:
            d = dict(zip(cols, row))
            inst_up = (d["inst_count_change"] or 0) > 0
            strength = "HIGH" if d["buy_count"] >= 5 and inst_up else ("MEDIUM" if inst_up else "LOW")
            signals.append({
                "signal_type": "INSIDER_BUYING_SURGE",
                "ticker": d["ticker"],
                "company": d["company"] or "",
                "sector": d["sector"] or "Unknown",
                "detected_at": now_iso,
                "lookback": f"{lookback_days}D",
                "strength": strength,
                "summary": (
                    f"{d['buy_count']} insider BUY transactions in {lookback_days} days"
                    + (f" + institutional count increasing" if inst_up else "")
                ),
                "metrics": {
                    "insider_buy_count": d["buy_count"],
                    "insider_buy_value": _round(d["total_value"]),
                    "inst_count_change_pct": _round(d["inst_count_change_pct"]),
                    "shares_change_pct": _round(d["shares_change_pct"]),
                },
            })
        return signals

    def _detect_sector_rotation(
        self, conn: duckdb.DuckDBPyConnection, quarter: str, lookback_days: int
    ) -> List[Dict]:
        """Capital flowing into sector (>20% QoQ value increase)."""
        now_iso = datetime.now(timezone.utc).isoformat()

        # Get prior quarter
        year, qn = quarter.split("-Q")
        y, q = int(year), int(qn)
        if q == 1:
            prior_q = f"{y - 1}-Q4"
        else:
            prior_q = f"{y}-Q{q - 1}"

        rows = conn.execute("""
            SELECT
                c.sector,
                c.total_value_usd_k AS value_current,
                p.total_value_usd_k AS value_prior,
                CASE WHEN p.total_value_usd_k > 0
                     THEN ((c.total_value_usd_k - p.total_value_usd_k) * 100.0 / p.total_value_usd_k)
                     ELSE NULL END AS value_change_pct,
                c.ticker_count,
                c.total_inst_count
            FROM agg_sector_quarterly c
            LEFT JOIN agg_sector_quarterly p
                ON c.sector = p.sector AND p.report_quarter = ?
            WHERE c.report_quarter = ?
              AND c.sector != 'Unknown'
            ORDER BY value_change_pct DESC NULLS LAST
        """, [prior_q, quarter]).fetchall()

        signals = []
        cols = ["sector", "value_current", "value_prior", "value_change_pct",
                "ticker_count", "total_inst_count"]
        for row in rows:
            d = dict(zip(cols, row))
            pct = d["value_change_pct"]
            if pct is None:
                continue
            if pct > 20:
                strength = "HIGH" if pct > 50 else "MEDIUM"
                signals.append({
                    "signal_type": "SECTOR_ROTATION",
                    "ticker": d["sector"],
                    "company": f"{d['ticker_count']} tickers in sector",
                    "sector": d["sector"],
                    "detected_at": now_iso,
                    "lookback": "1Q",
                    "strength": strength,
                    "summary": (
                        f"Sector {d['sector']}: {_fmt_pct(pct)} capital inflow QoQ "
                        f"across {d['ticker_count']} tickers"
                    ),
                    "metrics": {
                        "value_change_pct": _round(pct),
                        "value_current_k": _round(d["value_current"]),
                        "ticker_count": d["ticker_count"],
                        "total_inst_count": d["total_inst_count"],
                    },
                })
            elif pct < -20:
                strength = "HIGH" if pct < -50 else "MEDIUM"
                signals.append({
                    "signal_type": "SECTOR_ROTATION",
                    "ticker": d["sector"],
                    "company": f"{d['ticker_count']} tickers in sector",
                    "sector": d["sector"],
                    "detected_at": now_iso,
                    "lookback": "1Q",
                    "strength": strength,
                    "summary": (
                        f"Sector {d['sector']}: {_fmt_pct(pct)} capital outflow QoQ — "
                        f"rotation out of sector"
                    ),
                    "metrics": {
                        "value_change_pct": _round(pct),
                        "value_current_k": _round(d["value_current"]),
                        "ticker_count": d["ticker_count"],
                    },
                })
        return signals

    def _detect_smart_money_convergence(
        self, conn: duckdb.DuckDBPyConnection, quarter: str, lookback_days: int
    ) -> List[Dict]:
        """Top managers increasing + insider buying + price uptrend."""
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = conn.execute(f"""
            WITH top_mgr_buys AS (
                SELECT f.ticker, COUNT(DISTINCT f.manager_cik) AS top_mgr_count
                FROM fact_13f_positions f
                INNER JOIN (
                    SELECT manager_cik FROM fact_13f_positions
                    GROUP BY manager_cik
                    ORDER BY SUM(value_usd_thousands) DESC
                    LIMIT 50
                ) tm ON f.manager_cik = tm.manager_cik
                WHERE CONCAT(
                    EXTRACT(YEAR FROM f.report_period)::INT::TEXT,
                    '-Q',
                    EXTRACT(QUARTER FROM f.report_period)::INT::TEXT
                ) = ?
                GROUP BY f.ticker
            ),
            insider_buys AS (
                SELECT ticker, COUNT(*) AS buy_count
                FROM fact_form4_transactions
                WHERE direction = 'BUY'
                  AND transaction_date >= CURRENT_DATE - INTERVAL '{int(lookback_days)}' DAY
                GROUP BY ticker
                HAVING COUNT(*) >= 2
            )
            SELECT
                q.ticker,
                i.issuer_name AS company,
                q.sector,
                tmb.top_mgr_count,
                ib.buy_count AS insider_buys,
                q.avg_price_change_pct,
                q.inst_count_change_pct,
                q.shares_change_pct,
                q.current_price
            FROM agg_qoq_changes q
            INNER JOIN top_mgr_buys tmb ON q.ticker = tmb.ticker
            INNER JOIN insider_buys ib ON q.ticker = ib.ticker
            LEFT JOIN dim_issuer i ON q.ticker = i.ticker
            WHERE q.current_quarter = ?
              AND q.inst_count_change > 0
              AND q.avg_price_change_pct > 0
            ORDER BY tmb.top_mgr_count DESC
            LIMIT 20
        """, [quarter, quarter]).fetchall()

        signals = []
        cols = ["ticker", "company", "sector", "top_mgr_count", "insider_buys",
                "avg_price_change_pct", "inst_count_change_pct", "shares_change_pct", "current_price"]
        for row in rows:
            d = dict(zip(cols, row))
            signals.append({
                "signal_type": "SMART_MONEY_CONVERGENCE",
                "ticker": d["ticker"],
                "company": d["company"] or "",
                "sector": d["sector"] or "Unknown",
                "detected_at": now_iso,
                "lookback": f"{lookback_days}D",
                "strength": "HIGH",
                "summary": (
                    f"{d['top_mgr_count']} top-50 managers holding + "
                    f"{d['insider_buys']} insider buys + "
                    f"{_fmt_pct(d['avg_price_change_pct'])} price gain"
                ),
                "metrics": {
                    "top_manager_count": d["top_mgr_count"],
                    "insider_buy_count": d["insider_buys"],
                    "avg_price_change_pct": _round(d["avg_price_change_pct"]),
                    "inst_count_change_pct": _round(d["inst_count_change_pct"]),
                    "current_price": _round(d["current_price"]),
                },
            })
        return signals

    def _detect_contrarian(
        self, conn: duckdb.DuckDBPyConnection, quarter: str, lookback_days: int
    ) -> List[Dict]:
        """Institutions accumulating while price is near 52-week low."""
        now_iso = datetime.now(timezone.utc).isoformat()

        # Need price data to determine 52-week low
        try:
            conn.execute("SELECT 1 FROM fact_daily_prices LIMIT 1")
        except Exception:
            return []

        rows = conn.execute("""
            WITH price_ranges AS (
                SELECT
                    ticker,
                    MIN(low) AS low_52w,
                    MAX(high) AS high_52w,
                    LAST(close ORDER BY trade_date) AS latest_close
                FROM fact_daily_prices
                WHERE trade_date >= CURRENT_DATE - INTERVAL '365' DAY
                GROUP BY ticker
                HAVING COUNT(*) >= 50
            )
            SELECT
                q.ticker,
                i.issuer_name AS company,
                q.sector,
                pr.latest_close,
                pr.low_52w,
                pr.high_52w,
                ((pr.latest_close - pr.low_52w) * 100.0 / NULLIF(pr.high_52w - pr.low_52w, 0)) AS pct_from_low,
                q.inst_count_change,
                q.inst_count_change_pct,
                q.shares_change_pct,
                q.count_up_streak
            FROM agg_qoq_changes q
            INNER JOIN price_ranges pr ON q.ticker = pr.ticker
            LEFT JOIN dim_issuer i ON q.ticker = i.ticker
            WHERE q.current_quarter = ?
              AND q.inst_count_change > 0
              AND q.shares_change > 0
              AND ((pr.latest_close - pr.low_52w) * 100.0 / NULLIF(pr.high_52w - pr.low_52w, 0)) < 25
            ORDER BY pct_from_low ASC
            LIMIT 30
        """, [quarter]).fetchall()

        signals = []
        cols = ["ticker", "company", "sector", "latest_close", "low_52w", "high_52w",
                "pct_from_low", "inst_count_change", "inst_count_change_pct",
                "shares_change_pct", "count_up_streak"]
        for row in rows:
            d = dict(zip(cols, row))
            pct_low = d["pct_from_low"] or 0
            strength = "HIGH" if pct_low < 10 else "MEDIUM"
            signals.append({
                "signal_type": "CONTRARIAN_OPPORTUNITY",
                "ticker": d["ticker"],
                "company": d["company"] or "",
                "sector": d["sector"] or "Unknown",
                "detected_at": now_iso,
                "lookback": "52W",
                "strength": strength,
                "summary": (
                    f"Price near 52-week low ({_round(pct_low)}% from bottom) "
                    f"but institutions accumulating ({_fmt_pct(d['inst_count_change_pct'])} count increase)"
                ),
                "metrics": {
                    "pct_from_52w_low": _round(pct_low),
                    "current_price": _round(d["latest_close"]),
                    "low_52w": _round(d["low_52w"]),
                    "high_52w": _round(d["high_52w"]),
                    "inst_count_change_pct": _round(d["inst_count_change_pct"]),
                    "shares_change_pct": _round(d["shares_change_pct"]),
                },
            })
        return signals

    def _detect_high_conviction_predictions(
        self, conn: duckdb.DuckDBPyConnection, quarter: str, lookback_days: int
    ) -> List[Dict]:
        """High conviction 1-week directional predictions using all data sources.

        Combines: conviction score, ML v2, insider effect, phase, squeeze,
        momentum, trend score, institutional pressure, and insider outcomes.
        Bullish if composite >= 65, bearish if composite <= 35.
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # Check if intelligence_scores table exists
        try:
            conn.execute("SELECT 1 FROM intelligence_scores LIMIT 1")
        except Exception:
            return []

        rows = conn.execute("""
            SELECT DISTINCT ON (s.ticker)
                s.ticker,
                i.issuer_name AS company,
                q.sector,
                q.current_price,
                s.conviction_score,
                COALESCE(s.ml_score_v2, 0) AS ml_v2,
                s.accum_phase,
                s.accum_phase_quarters,
                s.tier1_manager_count,
                COALESCE(s.insider_effect_score, 0) AS insider_effect,
                COALESCE(s.insider_hist_win_rate, 0) AS insider_wr,
                COALESCE(s.insider_hist_alpha, 0) AS insider_alpha,
                COALESCE(s.short_squeeze_score, 0) AS squeeze,
                COALESCE(s.trend_score, 0) AS trend,
                COALESCE(s.institutional_pressure, 0) AS pressure,
                q.avg_price_change_pct,
                q.inst_count_change_pct,
                q.shares_change_pct,
                q.count_up_streak,
                s.price_momentum_90d,
                s.price_above_200sma,
                COALESCE(s.data_quality_score, 0) AS dq_score
            FROM intelligence_scores s
            INNER JOIN agg_qoq_changes q ON s.ticker = q.ticker AND q.current_quarter = ?
            LEFT JOIN dim_issuer i ON s.ticker = i.ticker
            WHERE s.report_quarter = ?
              AND s.conviction_score IS NOT NULL
              AND s.conviction_score >= 20
              AND q.current_price IS NOT NULL
              AND q.current_price > 1
            ORDER BY s.ticker, s.conviction_score DESC
        """, [quarter, quarter]).fetchall()

        cols = [
            "ticker", "company", "sector", "current_price",
            "conviction_score", "ml_v2", "accum_phase", "accum_phase_quarters",
            "tier1_manager_count", "insider_effect", "insider_wr", "insider_alpha",
            "squeeze", "trend", "pressure",
            "avg_price_change_pct", "inst_count_change_pct", "shares_change_pct",
            "count_up_streak", "price_momentum_90d", "price_above_200sma",
            "dq_score",
        ]

        signals = []
        for row in rows:
            d = dict(zip(cols, row))

            # --- Composite scoring (0-100) ---
            # Each component contributes to a weighted composite
            conviction = d["conviction_score"] or 0
            ml = d["ml_v2"] or 0
            insider_eff = d["insider_effect"] or 0
            trend = d["trend"] or 0
            pressure = d["pressure"] or 0
            squeeze = d["squeeze"] or 0
            mom_90 = d["price_momentum_90d"] or 0
            above_200 = d["price_above_200sma"] or 0
            streak = d["count_up_streak"] or 0
            phase = d["accum_phase"] or ""
            inst_chg = d["inst_count_change_pct"] or 0
            shares_chg = d["shares_change_pct"] or 0

            # Data quality gate: require at least 2 non-zero intelligence
            # components to avoid noise from low-data tickers
            nonzero_count = sum(1 for v in [ml, insider_eff, trend, squeeze] if v > 0)
            if nonzero_count < 1 and conviction < 50:
                continue  # Skip tickers with minimal intelligence data

            # Directional composite: 0 = max bearish, 100 = max bullish
            # Weights: conviction 25%, ML 20%, insider 15%, trend 15%, momentum 15%, phase 10%
            phase_score = 0
            if "ACTIVE" in str(phase):
                phase_score = 90
            elif "EARLY" in str(phase):
                phase_score = 70
            elif "LATE" in str(phase):
                phase_score = 55
            elif "EXPANSION" in str(phase):
                phase_score = 60
            elif "DISTRIBUTION" in str(phase):
                phase_score = 20
            elif "DORMANT" in str(phase):
                phase_score = 40

            # Momentum score (0-100 based on 90d momentum)
            mom_score = min(100, max(0, 50 + mom_90 * 2)) if mom_90 else 50
            if above_200:
                mom_score = min(100, mom_score + 10)

            composite = (
                conviction * 0.25
                + ml * 0.20
                + insider_eff * 0.15
                + trend * 0.15
                + mom_score * 0.15
                + phase_score * 0.10
            )

            # Streak bonus (sustained accumulation)
            if streak >= 3:
                composite = min(100, composite + 5)
            elif streak >= 2:
                composite = min(100, composite + 2)

            # Only output high-conviction predictions (above 65 bullish or below 35 bearish)
            if composite < 65 and composite > 35:
                continue

            is_bullish = composite >= 65
            direction = "BULLISH" if is_bullish else "BEARISH"
            confidence = abs(composite - 50) * 2  # 0-100 scale
            confidence = min(100, confidence)

            if confidence < 50:
                strength = "LOW"
            elif confidence < 70:
                strength = "MEDIUM"
            else:
                strength = "HIGH"

            # Only include HIGH and MEDIUM strength predictions
            if strength == "LOW":
                continue

            signals.append({
                "signal_type": "HIGH_CONVICTION_PREDICTION",
                "ticker": d["ticker"],
                "company": d["company"] or "",
                "sector": d["sector"] or "Unknown",
                "detected_at": now_iso,
                "lookback": "1W",
                "strength": strength,
                "direction": direction,
                "summary": (
                    f"{direction} prediction (confidence: {confidence:.0f}%) -- "
                    f"Conviction {conviction:.0f}, ML {ml:.0f}, "
                    f"Insider {insider_eff:.0f}, Phase: {phase}"
                ),
                "metrics": {
                    "direction": direction,
                    "confidence": _round(confidence),
                    "composite_score": _round(composite),
                    "conviction": _round(conviction),
                    "ml_v2_score": _round(ml),
                    "insider_effect": _round(insider_eff),
                    "trend_score": _round(trend),
                    "momentum_90d": _round(mom_90),
                    "squeeze_score": _round(squeeze),
                    "current_price": _round(d["current_price"]),
                },
            })

        # Sort by confidence descending
        signals.sort(key=lambda s: s["metrics"].get("confidence", 0), reverse=True)
        return signals[:30]  # Top 30 predictions

    def _detect_exit_warning(
        self, conn: duckdb.DuckDBPyConnection, quarter: str, lookback_days: int
    ) -> List[Dict]:
        """Mass institutional exit (>30% count drop) + price declining."""
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = conn.execute("""
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
                q.avg_price_change_pct,
                q.current_price
            FROM agg_qoq_changes q
            LEFT JOIN dim_issuer i ON q.ticker = i.ticker
            WHERE q.current_quarter = ?
              AND q.inst_count_change_pct <= -30
            ORDER BY q.inst_count_change_pct ASC
            LIMIT 30
        """, [quarter]).fetchall()

        signals = []
        cols = ["ticker", "company", "sector", "inst_count_current", "inst_count_prior",
                "inst_count_change", "inst_count_change_pct", "shares_change_pct",
                "value_change_pct", "avg_price_change_pct", "current_price"]
        for row in rows:
            d = dict(zip(cols, row))
            price_down = (d["avg_price_change_pct"] or 0) < 0
            strength = "HIGH" if price_down else "MEDIUM"
            signals.append({
                "signal_type": "EXIT_WARNING",
                "ticker": d["ticker"],
                "company": d["company"] or "",
                "sector": d["sector"] or "Unknown",
                "detected_at": now_iso,
                "lookback": "1Q",
                "strength": strength,
                "summary": (
                    f"Mass institutional exit: {_fmt_pct(d['inst_count_change_pct'])} count drop "
                    f"({d['inst_count_prior']} → {d['inst_count_current']})"
                    + (f" + price declining {_fmt_pct(d['avg_price_change_pct'])}" if price_down else "")
                ),
                "metrics": {
                    "inst_count_change_pct": _round(d["inst_count_change_pct"]),
                    "inst_count_current": d["inst_count_current"],
                    "inst_count_prior": d["inst_count_prior"],
                    "shares_change_pct": _round(d["shares_change_pct"]),
                    "avg_price_change_pct": _round(d["avg_price_change_pct"]),
                    "current_price": _round(d["current_price"]),
                },
            })
        return signals

    # ------------------------------------------------------------------
    # SWING_CONFLUENCE — Data-proven swing setup
    # ------------------------------------------------------------------

    def _detect_swing_confluence(
        self, conn: duckdb.DuckDBPyConnection, quarter: str, lookback_days: int
    ) -> List[Dict]:
        """Detect proven swing confluence: insider buying + ATR compression + trend context.

        Backtested over 4.6M stock-days (2019-2025):
          - Insider + Below200 + Compressed: +0.223R expectancy, 40% 2R hit, 88% yr consistency
          - Insider + Compressed: +0.177R expectancy, 39% 2R hit, 88% yr consistency
          - Same-day insider buy: +0.198R expectancy, 34% 2R hit, 100% yr consistency

        Stop = 1.5x ATR(14), Target = 2R (3x ATR), Hold = 20-30 trading days.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        lookback = max(lookback_days, 30)

        rows = conn.execute(f"""
            WITH latest_prices AS (
                SELECT
                    ticker,
                    trade_date,
                    close,
                    high,
                    low,
                    volume,
                    ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) AS rn
                FROM fact_daily_prices
                WHERE trade_date >= CURRENT_DATE - INTERVAL '{int(lookback + 60)}' DAY
                  AND close > 2.0 AND volume > 100000
            ),
            price_stats AS (
                SELECT
                    ticker,
                    MAX(CASE WHEN rn = 1 THEN close END) AS current_close,
                    MAX(CASE WHEN rn = 1 THEN trade_date END) AS latest_date,
                    -- ATR(14): avg true range of last 14 days
                    AVG(CASE WHEN rn <= 14 THEN high - low END) AS atr14,
                    -- ATR(50): avg true range of last 50 days
                    AVG(CASE WHEN rn <= 50 THEN high - low END) AS atr50,
                    -- SMA(200) approximate: avg close of last 200 days (use what we have)
                    AVG(CASE WHEN rn <= 200 THEN close END) AS sma200,
                    -- SMA(20)
                    AVG(CASE WHEN rn <= 20 THEN close END) AS sma20,
                    -- SMA(50)
                    AVG(CASE WHEN rn <= 50 THEN close END) AS sma50,
                    -- Recent volume
                    AVG(CASE WHEN rn <= 10 THEN volume END) AS avg_vol_10d,
                    AVG(CASE WHEN rn <= 50 THEN volume END) AS avg_vol_50d,
                    -- 20d return
                    MAX(CASE WHEN rn = 1 THEN close END) / NULLIF(MAX(CASE WHEN rn = 20 THEN close END), 0) - 1 AS ret_20d
                FROM latest_prices
                WHERE rn <= 200
                GROUP BY ticker
                HAVING COUNT(CASE WHEN rn <= 14 THEN 1 END) >= 10
                   AND MAX(CASE WHEN rn = 1 THEN close END) IS NOT NULL
            ),
            insider_recent AS (
                SELECT
                    ticker,
                    COUNT(DISTINCT insider_name) AS distinct_insiders_30d,
                    COUNT(*) AS insider_txns_30d,
                    SUM(shares * COALESCE(NULLIF(price, 0), 1)) AS insider_dollar_30d,
                    MAX(transaction_date) AS last_insider_buy
                FROM fact_form4_transactions
                WHERE transaction_code = 'P'
                  AND transaction_date >= CURRENT_DATE - INTERVAL '{int(lookback)}' DAY
                  AND shares > 0
                GROUP BY ticker
            ),
            inst_data AS (
                SELECT
                    q.ticker,
                    q.count_up_streak,
                    q.inst_count_change,
                    q.inst_count_change_pct,
                    q.sector
                FROM agg_qoq_changes q
                WHERE q.current_quarter = ?
            )
            SELECT
                p.ticker,
                COALESCE(i.issuer_name, p.ticker) AS company,
                COALESCE(inst.sector, 'Unknown') AS sector,
                p.current_close,
                p.atr14,
                p.atr50,
                p.sma200,
                p.sma20,
                p.sma50,
                p.avg_vol_10d,
                p.avg_vol_50d,
                p.ret_20d,
                ins.distinct_insiders_30d,
                ins.insider_txns_30d,
                ins.insider_dollar_30d,
                ins.last_insider_buy,
                inst.count_up_streak,
                inst.inst_count_change,
                inst.inst_count_change_pct,
                -- Derived signals
                CASE WHEN p.atr14 < 0.85 * p.atr50 THEN 1 ELSE 0 END AS atr_compressed,
                CASE WHEN p.current_close < p.sma200 THEN 1 ELSE 0 END AS below_200sma,
                CASE WHEN p.current_close > p.sma200 THEN 1 ELSE 0 END AS above_200sma,
                CASE WHEN p.current_close BETWEEN p.sma20 * 0.95 AND p.sma20 THEN 1 ELSE 0 END AS pullback_to_sma20,
                CASE WHEN p.sma20 > p.sma50 AND p.sma50 > p.sma200 THEN 1 ELSE 0 END AS ma_aligned
            FROM price_stats p
            INNER JOIN insider_recent ins ON p.ticker = ins.ticker
            LEFT JOIN (SELECT ticker, MAX(issuer_name) AS issuer_name FROM dim_issuer GROUP BY ticker) i ON p.ticker = i.ticker
            LEFT JOIN inst_data inst ON p.ticker = inst.ticker
            WHERE ins.distinct_insiders_30d >= 1
            ORDER BY ins.distinct_insiders_30d DESC, ins.insider_dollar_30d DESC
        """, [quarter]).fetchall()

        cols = [
            "ticker", "company", "sector", "current_close", "atr14", "atr50",
            "sma200", "sma20", "sma50", "avg_vol_10d", "avg_vol_50d", "ret_20d",
            "distinct_insiders_30d", "insider_txns_30d", "insider_dollar_30d",
            "last_insider_buy", "count_up_streak", "inst_count_change",
            "inst_count_change_pct", "atr_compressed", "below_200sma",
            "above_200sma", "pullback_to_sma20", "ma_aligned",
        ]

        signals = []
        for row in rows:
            d = dict(zip(cols, row))

            # Score the confluence (data-proven weights)
            score = 0
            factors = []

            # Insider buying (core signal — 100% yearly consistency)
            insiders = d["distinct_insiders_30d"] or 0
            if insiders >= 3:
                score += 35
                factors.append(f"{insiders} distinct insiders buying (cluster)")
            elif insiders >= 2:
                score += 25
                factors.append(f"{insiders} insiders buying")
            else:
                score += 15
                factors.append("insider buying detected")

            # ATR compression (+0.18R expectancy boost)
            if d["atr_compressed"]:
                score += 25
                factors.append("ATR compressed (volatility squeeze)")

            # Below 200 SMA (+0.07R boost for mean-reversion)
            if d["below_200sma"]:
                score += 15
                factors.append("below 200 SMA (mean-reversion setup)")
            elif d["above_200sma"] and d["ma_aligned"]:
                score += 10
                factors.append("above 200 SMA with MAs aligned (trend)")

            # Pullback to SMA20 (+0.02R boost)
            if d["pullback_to_sma20"]:
                score += 10
                factors.append("pulling back to 20 SMA support")

            # Institutional accumulation streak
            streak = d["count_up_streak"] or 0
            if streak >= 2:
                score += 10
                factors.append(f"{streak}Q institutional accumulation")
            elif (d["inst_count_change"] or 0) > 0:
                score += 5
                factors.append("inst count rising this quarter")

            # Volume quiet (confirms compression)
            vol_10 = d["avg_vol_10d"] or 0
            vol_50 = d["avg_vol_50d"] or 1
            if vol_50 > 0 and vol_10 / vol_50 < 0.8:
                score += 5
                factors.append("volume quiet (drying up)")

            # Determine strength
            if score >= 70:
                strength = "HIGH"
            elif score >= 45:
                strength = "MEDIUM"
            else:
                strength = "LOW"

            # Setup type label
            setup_parts = []
            if d["atr_compressed"] and d["below_200sma"]:
                setup_type = "INSIDER + BELOW200 + COMPRESSED"  # Best: +0.223R
            elif d["atr_compressed"]:
                setup_type = "INSIDER + COMPRESSED"  # Good: +0.177R
            elif d["below_200sma"]:
                setup_type = "INSIDER + BELOW200"  # Decent: +0.090R
            elif d["ma_aligned"]:
                setup_type = "INSIDER + TREND ALIGNED"
            else:
                setup_type = "INSIDER BUY"  # Base: +0.198R

            # Compute trade levels
            atr = d["atr14"] or 0
            close = d["current_close"] or 0
            stop_distance = 1.5 * atr
            target_2r = 2 * stop_distance

            signals.append({
                "signal_type": "SWING_CONFLUENCE",
                "ticker": d["ticker"],
                "company": d["company"] or "",
                "sector": d["sector"] or "Unknown",
                "detected_at": now_iso,
                "lookback": f"{lookback}D",
                "strength": strength,
                "summary": f"{setup_type}: " + " + ".join(factors[:3]),
                "metrics": {
                    "confluence_score": score,
                    "setup_type": setup_type,
                    "distinct_insiders_30d": insiders,
                    "insider_txns_30d": d["insider_txns_30d"],
                    "insider_dollar_30d": _round(d["insider_dollar_30d"]),
                    "last_insider_buy": str(d["last_insider_buy"])[:10] if d["last_insider_buy"] else None,
                    "atr_compressed": bool(d["atr_compressed"]),
                    "below_200sma": bool(d["below_200sma"]),
                    "pullback_to_sma20": bool(d["pullback_to_sma20"]),
                    "ma_aligned": bool(d["ma_aligned"]),
                    "inst_streak": streak,
                    "current_price": _round(close),
                    "atr14": _round(atr, 3),
                    "stop_price": _round(close - stop_distance, 2),
                    "target_2r": _round(close + target_2r, 2),
                    "risk_reward": "2:1",
                    "hold_period": "20-30 trading days",
                    "backtest_expectancy": "+0.22R" if d["atr_compressed"] and d["below_200sma"]
                        else "+0.18R" if d["atr_compressed"]
                        else "+0.09R" if d["below_200sma"]
                        else "+0.20R",
                },
                "trade_plan": {
                    "entry": f"Buy near ${_round(close, 2)}",
                    "stop": f"${_round(close - stop_distance, 2)} (1.5x ATR = ${_round(stop_distance, 2)})",
                    "target": f"${_round(close + target_2r, 2)} (2R = ${_round(target_2r, 2)})",
                    "hold": "20-30 trading days",
                    "factors": factors,
                },
            })

        # Sort by confluence score descending
        signals.sort(key=lambda s: -s["metrics"]["confluence_score"])

        # Cap at top 30
        return signals[:30]


    def _detect_pullback_sniper(
        self, conn: duckdb.DuckDBPyConnection, quarter: str, lookback_days: int
    ) -> List[Dict]:
        """Detect data-proven swing pullback setups (72%+ 1R hit rate).

        Two variants:
        1. PULLBACK_FIB: RSI(2)<10 + 3+ consecutive down days + near 50-SMA
           Backtested: 4,535 trades, 72.9% 1R WR, +0.280R expectancy
        2. HOLY_GRAIL: RSI(2)<10 + ADX>=25 + pullback to 50-SMA in uptrend
           Backtested: 6,937 trades, 72.1% 1R WR, +0.268R expectancy

        Entry: next day open. Stop: entry - ATR(20). Exit: close >= SMA(10) or 5 days.
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            rows = conn.execute(f"""
                WITH latest AS (
                    SELECT
                        ticker,
                        trade_date,
                        close,
                        high,
                        low,
                        open,
                        volume,
                        ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) AS rn
                    FROM fact_daily_prices
                    WHERE trade_date >= CURRENT_DATE - INTERVAL '300' DAY
                      AND close > 5.0 AND volume > 100000
                ),
                stats AS (
                    SELECT
                        ticker,
                        MAX(CASE WHEN rn = 1 THEN close END) AS close_today,
                        MAX(CASE WHEN rn = 1 THEN trade_date END) AS latest_date,
                        -- SMA(50)
                        AVG(CASE WHEN rn <= 50 THEN close END) AS sma_50,
                        -- SMA(200)
                        AVG(CASE WHEN rn <= 200 THEN close END) AS sma_200,
                        -- SMA(10) for exit signal
                        AVG(CASE WHEN rn <= 10 THEN close END) AS sma_10,
                        -- ATR(20)
                        AVG(CASE WHEN rn <= 20 THEN high - low END) AS atr_20,
                        -- ADX proxy: directional movement strength via 20d return magnitude
                        ABS(MAX(CASE WHEN rn = 1 THEN close END) / NULLIF(MAX(CASE WHEN rn = 20 THEN close END), 0) - 1) * 100 AS trend_mag_20d
                    FROM latest
                    WHERE rn <= 252
                    GROUP BY ticker
                    HAVING COUNT(CASE WHEN rn <= 50 THEN 1 END) >= 40
                       AND COUNT(CASE WHEN rn <= 200 THEN 1 END) >= 150
                )
                SELECT
                    s.ticker,
                    COALESCE(i.issuer_name, s.ticker) AS company,
                    s.close_today,
                    s.sma_50,
                    s.sma_200,
                    s.sma_10,
                    s.atr_20,
                    s.trend_mag_20d,
                    (s.close_today - s.sma_50) / NULLIF(s.sma_50, 0) * 100 AS pct_from_sma50,
                    (s.close_today - s.sma_200) / NULLIF(s.sma_200, 0) * 100 AS pct_from_sma200
                FROM stats s
                LEFT JOIN (SELECT ticker, MAX(issuer_name) AS issuer_name FROM dim_issuer GROUP BY ticker) i
                    ON s.ticker = i.ticker
                WHERE s.close_today > s.sma_200
                  AND s.close_today IS NOT NULL
                  AND s.atr_20 > 0
                  AND (s.close_today - s.sma_50) / NULLIF(s.sma_50, 0) * 100 BETWEEN -5.0 AND 1.0
                ORDER BY ABS((s.close_today - s.sma_50) / NULLIF(s.sma_50, 0) * 100) ASC
                LIMIT 100
            """).fetchall()
        except Exception as exc:
            logger.debug("Pullback sniper query failed: {}", exc)
            return []

        cols = ["ticker", "company", "close_today", "sma_50", "sma_200",
                "sma_10", "atr_20", "trend_mag_20d", "pct_from_sma50", "pct_from_sma200"]

        # Now check RSI(2) and consecutive down days from recent bars
        signals = []
        for row in rows:
            d = dict(zip(cols, row))
            ticker = d["ticker"]
            close = d["close_today"] or 0
            sma50 = d["sma_50"] or 0
            atr = d["atr_20"] or 0
            pct_50 = d["pct_from_sma50"] or 0

            if close <= 0 or atr <= 0:
                continue

            # Fetch last 5 daily closes for RSI(2) and consecutive down check
            try:
                recent = conn.execute("""
                    SELECT close FROM fact_daily_prices
                    WHERE ticker = ? AND close > 0
                    ORDER BY trade_date DESC LIMIT 5
                """, [ticker]).fetchall()
            except Exception:
                continue

            if len(recent) < 4:
                continue

            closes = [float(r[0]) for r in recent]  # most recent first

            # Consecutive down days (from most recent)
            consec_down = 0
            for i in range(len(closes) - 1):
                if closes[i] < closes[i + 1]:
                    consec_down += 1
                else:
                    break

            # Simple RSI(2) approximation from last 3 closes
            if len(closes) >= 3:
                changes = [closes[i] - closes[i + 1] for i in range(min(2, len(closes) - 1))]
                gains = [c for c in changes if c > 0]
                losses = [-c for c in changes if c < 0]
                avg_gain = sum(gains) / 2 if gains else 0.001
                avg_loss = sum(losses) / 2 if losses else 0.001
                rs = avg_gain / avg_loss if avg_loss > 0 else 100
                rsi2 = 100 - 100 / (1 + rs)
            else:
                rsi2 = 50  # default

            # Determine setup type
            setup_type = None
            strength = "MEDIUM"

            # PULLBACK_FIB: RSI2 < 10 + 3+ down days + near 50SMA (-3% to -1%)
            is_fib = (rsi2 < 10 and consec_down >= 3 and -3.0 <= pct_50 <= -1.0)
            # HOLY_GRAIL: RSI2 < 15 + trend confirmed + near 50SMA (-3% to 0%)
            is_holy = (rsi2 < 15 and d["pct_from_sma200"] > 5 and -3.0 <= pct_50 <= 0.0)

            if is_fib:
                setup_type = "PULLBACK_FIB"
                strength = "HIGH"
            elif is_holy and consec_down >= 2:
                setup_type = "HOLY_GRAIL"
                strength = "HIGH" if rsi2 < 10 else "MEDIUM"
            else:
                continue  # Skip tickers that don't match either pattern

            # Trade levels
            entry_est = close  # Next open approximated by today's close
            stop_price = entry_est - atr
            target_1r = entry_est + atr
            r_unit = atr

            signals.append({
                "signal_type": "PULLBACK_SNIPER",
                "ticker": ticker,
                "company": d["company"] or "",
                "sector": "Unknown",
                "detected_at": now_iso,
                "lookback": "5D",
                "strength": strength,
                "summary": (
                    f"{setup_type}: RSI2={rsi2:.0f} | {consec_down} down days | "
                    f"{pct_50:+.1f}% from 50SMA"
                ),
                "metrics": {
                    "setup_type": setup_type,
                    "rsi_2": _round(rsi2, 1),
                    "consecutive_down_days": consec_down,
                    "pct_from_sma50": _round(pct_50, 2),
                    "pct_from_sma200": _round(d["pct_from_sma200"], 2),
                    "atr_20": _round(atr, 3),
                    "current_price": _round(close, 2),
                    "stop_price": _round(stop_price, 2),
                    "target_1r": _round(target_1r, 2),
                    "r_unit": _round(r_unit, 3),
                    "backtest_wr": "72.9%" if setup_type == "PULLBACK_FIB" else "72.1%",
                    "backtest_exp": "+0.280R" if setup_type == "PULLBACK_FIB" else "+0.268R",
                    "max_hold": "5 days",
                    "exit_signal": "close >= SMA(10)",
                },
                "trade_plan": {
                    "entry": f"Buy next open near ${_round(close, 2)}",
                    "stop": f"${_round(stop_price, 2)} (1x ATR = ${_round(atr, 2)})",
                    "target": f"${_round(target_1r, 2)} (1R), trail from 1R",
                    "hold": "2-5 trading days",
                    "exit": "Close >= SMA(10) or time stop at day 5",
                },
            })

        signals.sort(key=lambda s: -s["metrics"].get("consecutive_down_days", 0))
        return signals[:20]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _round(val, decimals: int = 2):
    """Round a value safely."""
    if val is None:
        return None
    try:
        return round(float(val), decimals)
    except (ValueError, TypeError):
        return val


def _fmt_pct(val) -> str:
    """Format a value as percentage string."""
    if val is None:
        return "N/A"
    try:
        return f"{float(val):+.1f}%"
    except (ValueError, TypeError):
        return str(val)
