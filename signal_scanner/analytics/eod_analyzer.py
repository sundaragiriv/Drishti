"""End-of-day analysis for paper trading results."""

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from signal_scanner.database.db_manager import DatabaseManager

_WAREHOUSE_PATH = Path(__file__).parents[2] / "data" / "warehouse" / "sec_intel.duckdb"


class EODAnalyzer:
    """Summarize daily outcomes and extract recurring loss patterns."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def run_for_date(self, trade_date: str) -> Optional[Dict]:
        """Analyze one trading date (YYYY-MM-DD) and persist output."""
        trades = self._db.get_closed_paper_trades_for_date(trade_date)
        if not trades:
            return None

        wins = [t for t in trades if float(t.get("realized_pnl") or 0.0) > 0]
        losses = [t for t in trades if float(t.get("realized_pnl") or 0.0) < 0]
        total = len(trades)
        realized = sum(float(t.get("realized_pnl") or 0.0) for t in trades)
        win_rate = round((len(wins) / total) * 100, 1) if total else 0.0
        gross_profit = sum(float(t.get("realized_pnl") or 0.0) for t in wins)
        gross_loss_abs = abs(sum(float(t.get("realized_pnl") or 0.0) for t in losses))
        profit_factor = round(gross_profit / gross_loss_abs, 2) if gross_loss_abs > 0 else 0.0
        avg_win = round((gross_profit / len(wins)), 2) if wins else 0.0
        expectancy = round((realized / total), 2) if total else 0.0

        loss_values = [float(t.get("realized_pnl") or 0.0) for t in losses]
        avg_loss = round(sum(loss_values) / len(loss_values), 2) if loss_values else 0.0
        max_loss = round(min(loss_values), 2) if loss_values else 0.0

        reason_counts = Counter(t.get("exit_reason") or "UNKNOWN" for t in losses)
        all_reason_counts = Counter(t.get("exit_reason") or "UNKNOWN" for t in trades)
        top_reason = reason_counts.most_common(1)[0][0] if reason_counts else "NONE"
        stop_loss_rate = round((all_reason_counts.get("STOP_LOSS", 0) / total) * 100, 1) if total else 0.0
        flip_rate = round((all_reason_counts.get("RECOMMENDATION_FLIP", 0) / total) * 100, 1) if total else 0.0

        regime_counts = Counter(t.get("entry_market_regime") or "UNKNOWN" for t in losses)
        session_counts = Counter(t.get("entry_session_time") or "UNKNOWN" for t in losses)
        symbol_counts = Counter(t.get("symbol") or "UNKNOWN" for t in losses)
        regime_perf = self._group_perf(trades, "entry_market_regime")
        session_perf = self._group_perf(trades, "entry_session_time")
        score_perf = self._score_bucket_perf(trades)
        intel_corr = self._intel_conviction_perf(trades)

        insights = {
            "loss_reason_counts": dict(reason_counts),
            "all_exit_reason_counts": dict(all_reason_counts),
            "loss_regime_counts": dict(regime_counts),
            "loss_session_counts": dict(session_counts),
            "loss_symbol_counts": dict(symbol_counts),
            "profit_factor": profit_factor,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "expectancy_per_trade": expectancy,
            "stop_loss_rate_pct": stop_loss_rate,
            "flip_rate_pct": flip_rate,
            "regime_performance": regime_perf,
            "session_performance": session_perf,
            "score_bucket_performance": score_perf,
            "intelligence_correlation": intel_corr,
        }
        actions = self._build_actions(
            reason_counts,
            regime_counts,
            session_counts,
            len(losses),
            profit_factor,
            expectancy,
            stop_loss_rate,
            flip_rate,
            score_perf,
            intel_corr,
        )

        row = {
            "trade_date": trade_date,
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "realized_pnl": round(realized, 2),
            "avg_loss": avg_loss,
            "max_loss": max_loss,
            "top_loss_reason": top_reason,
            "insights_json": json.dumps(insights),
            "suggested_actions": actions,
            "action_status": self._recommend_action_status(
                total=total,
                loss_count=len(losses),
                realized_pnl=realized,
                stop_loss_rate=stop_loss_rate,
                flip_rate=flip_rate,
                expectancy=expectancy,
                profit_factor=profit_factor,
            ),
            "created_ts": datetime.now(timezone.utc).isoformat(),
        }
        self._db.upsert_eod_analysis(row)
        logger.info(
            f"EOD analysis saved for {trade_date}: trades={total}, "
            f"win_rate={win_rate}%, pnl={row['realized_pnl']}"
        )
        return row

    def run_recent(self, days: int = 5) -> int:
        """Run analyses for the last N days and return number of persisted rows."""
        created = 0
        today = datetime.now(timezone.utc).date()
        for i in range(days):
            d = (today - timedelta(days=i)).isoformat()
            if self.run_for_date(d):
                created += 1
        return created

    @staticmethod
    def _build_actions(
        reason_counts: Counter,
        regime_counts: Counter,
        session_counts: Counter,
        loss_count: int,
        profit_factor: float,
        expectancy: float,
        stop_loss_rate: float,
        flip_rate: float,
        score_perf: Dict[str, Dict[str, float]],
        intel_corr: Optional[Dict[str, Dict]] = None,
    ) -> str:
        """Translate loss clusters into concrete strategy recommendations."""
        if loss_count == 0:
            return "No losses today; keep current gating and monitor consistency."

        actions: List[str] = []
        stop = reason_counts.get("STOP_LOSS", 0)
        flip = reason_counts.get("RECOMMENDATION_FLIP", 0)
        if stop / max(loss_count, 1) >= 0.6:
            actions.append("Raise entry quality gate: score>=70 and RR>=1.8 before entry.")
        if flip / max(loss_count, 1) >= 0.4:
            actions.append("Require higher persistence (signal_age>=2) to reduce whipsaw flips.")
        if regime_counts.get("RISK_OFF", 0) >= max(2, int(loss_count * 0.5)):
            actions.append("Reduce LONG exposure in RISK OFF regime; prefer HOLD/selective shorts.")
        if session_counts.get("MID_DAY", 0) >= max(2, int(loss_count * 0.5)):
            actions.append("Trim MID_DAY entries; prioritize EARLY/POWER_HOUR setups.")
        if profit_factor < 1.0 or expectancy < 0:
            actions.append("Defensive mode next session: require score>=70, signal_age>=2, RR>=1.8.")
        if stop_loss_rate >= 45:
            actions.append("Too many stop-outs: add ATR floor filter and avoid entries within 0.5% of stop.")
        if flip_rate >= 35:
            actions.append("Whipsaw detected: add 1-cycle confirmation before acting on recommendation flips.")
        low_bucket = score_perf.get("60-69", {})
        if low_bucket and low_bucket.get("win_rate", 0.0) < 40:
            actions.append("Reduce low-conviction entries: raise minimum actionable score from 60 to 65-70.")

        # Intelligence correlation feedback
        if intel_corr:
            high = intel_corr.get("HIGH", {})
            low = intel_corr.get("LOW", {})
            high_wr = high.get("win_rate", None)
            low_wr = low.get("win_rate", None)
            if high_wr is not None and low_wr is not None:
                if high_wr < low_wr - 10:
                    actions.append(
                        f"WARNING: High-conviction trades underperforming "
                        f"(high={high_wr:.0f}% vs low={low_wr:.0f}%) — review phase classifier accuracy."
                    )
                elif high_wr > 65:
                    actions.append(
                        f"REINFORCE: High-conviction (>=70) win rate is {high_wr:.0f}% — "
                        "raise min conviction gate to 60+ for all new entries."
                    )

        if not actions:
            actions.append("Losses were distributed; keep rules stable and review symbol-specific outliers.")
        return " ".join(actions)

    @staticmethod
    def _recommend_action_status(
        total: int,
        loss_count: int,
        realized_pnl: float,
        stop_loss_rate: float,
        flip_rate: float,
        expectancy: float,
        profit_factor: float,
    ) -> str:
        """Suggest review workflow status from observed daily quality metrics.

        IMPLEMENT triggers only on genuinely bad performance, not any net-negative
        day.  Previous thresholds (pnl<0, pf<1.0) were too sensitive and caused
        a permanent defensive lock when combined with the session restriction.
        """
        if total <= 0:
            return "IGNORE"
        # Require meaningful sample AND clearly bad metrics to lock down
        should_implement = (
            total >= 5
            and (
                profit_factor < 0.6
                or stop_loss_rate >= 50.0
                or flip_rate >= 40.0
            )
        )
        if should_implement:
            return "IMPLEMENT"
        # Moderate concern: negative P&L or low profit factor
        if realized_pnl < 0 or profit_factor < 1.0:
            return "WATCH"
        if loss_count > 0:
            return "WATCH"
        return "IGNORE"

    @staticmethod
    def _intel_conviction_perf(trades: List[Dict]) -> Optional[Dict[str, Dict]]:
        """Join closed trades with DuckDB intelligence_scores and bucket by conviction.

        Returns dict with 'HIGH' (>=70) and 'LOW' (<70) conviction buckets,
        or None if warehouse is unavailable.
        """
        if not trades:
            return None
        symbols = list({str(t.get("symbol") or "") for t in trades if t.get("symbol")})
        if not symbols:
            return None
        try:
            import duckdb  # lazy import — not installed on all envs

            if not _WAREHOUSE_PATH.exists():
                return None
            from signal_scanner.institutional_intel.config import safe_duckdb_connect
            conn = safe_duckdb_connect(read_only=True)
            if conn is None:
                return None
            try:
                best_q = conn.execute("""
                    SELECT report_quarter FROM intelligence_scores
                    WHERE data_quality_score >= 75
                    GROUP BY report_quarter HAVING COUNT(*) >= 500
                    ORDER BY report_quarter DESC LIMIT 1
                """).fetchone()
                if not best_q:
                    return None
                quarter = best_q[0]

                placeholders = ",".join("?" * len(symbols))
                rows = conn.execute(
                    f"""
                    SELECT ticker, conviction_score
                    FROM intelligence_scores
                    WHERE report_quarter = ? AND ticker IN ({placeholders})
                    """,
                    [quarter] + symbols,
                ).fetchall()
                conviction_map: Dict[str, float] = {r[0]: float(r[1] or 0) for r in rows}
            finally:
                conn.close()

            buckets: Dict[str, List[float]] = {"HIGH": [], "LOW": []}
            for t in trades:
                sym = str(t.get("symbol") or "")
                pnl = float(t.get("realized_pnl") or 0.0)
                conv = conviction_map.get(sym)
                if conv is None:
                    continue
                bucket = "HIGH" if conv >= 70 else "LOW"
                buckets[bucket].append(pnl)

            result: Dict[str, Dict] = {}
            for bname, vals in buckets.items():
                if not vals:
                    continue
                n = len(vals)
                wins = sum(1 for v in vals if v > 0)
                result[bname] = {
                    "trades": n,
                    "win_rate": round((wins / n) * 100, 1),
                    "avg_return": round(sum(vals) / n, 2),
                }
            return result or None
        except Exception as exc:
            logger.debug(f"Intel conviction perf skipped: {exc}")
            return None

    @staticmethod
    def _group_perf(trades: List[Dict], key: str) -> Dict[str, Dict[str, float]]:
        out: Dict[str, Dict[str, float]] = {}
        groups: Dict[str, List[float]] = {}
        for t in trades:
            k = str(t.get(key) or "UNKNOWN")
            groups.setdefault(k, []).append(float(t.get("realized_pnl") or 0.0))
        for k, vals in groups.items():
            n = len(vals)
            wins = sum(1 for v in vals if v > 0)
            out[k] = {
                "trades": n,
                "win_rate": round((wins / n) * 100, 1) if n else 0.0,
                "pnl": round(sum(vals), 2),
            }
        return out

    @staticmethod
    def _score_bucket_perf(trades: List[Dict]) -> Dict[str, Dict[str, float]]:
        buckets = {"60-69": [], "70-79": [], "80+": []}
        for t in trades:
            try:
                s = float(t.get("entry_score") or 0.0)
            except (TypeError, ValueError):
                continue
            pnl = float(t.get("realized_pnl") or 0.0)
            if s >= 80:
                buckets["80+"].append(pnl)
            elif s >= 70:
                buckets["70-79"].append(pnl)
            elif s >= 60:
                buckets["60-69"].append(pnl)
        out: Dict[str, Dict[str, float]] = {}
        for b, vals in buckets.items():
            if not vals:
                continue
            n = len(vals)
            wins = sum(1 for v in vals if v > 0)
            out[b] = {
                "trades": n,
                "win_rate": round((wins / n) * 100, 1),
                "pnl": round(sum(vals), 2),
            }
        return out
