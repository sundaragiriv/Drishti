"""Signal ranking, filtering, and multi-timeframe aggregation.

Collapses per-symbol per-timeframe rows into a single row per symbol
with MTF agreement scoring. Higher timeframes set directional bias,
lower timeframes provide entry timing.
"""

from collections import defaultdict
from typing import Dict, List, Optional

from signal_scanner.config import ScannerConfig

# Timeframe hierarchy — higher index = higher weight for direction
TF_HIERARCHY = {"5m": 1, "15m": 2, "1h": 3}


class SignalRanker:
    """Ranks and filters scan results across all symbols and timeframes."""

    @staticmethod
    def rank_signals(
        results: List[Dict],
        min_score: int = 0,
        signal_filter: str = "ALL",
    ) -> List[Dict]:
        """Sort results by score descending with optional filters."""
        filtered = [
            r for r in results
            if r.get("score", 0) >= min_score
            and (signal_filter == "ALL" or r.get("signal") == signal_filter)
        ]
        return sorted(filtered, key=lambda x: x.get("score", 0), reverse=True)

    @staticmethod
    def get_top_movers(results: List[Dict], n: int = 10) -> List[Dict]:
        """Return top N signals by score."""
        return sorted(results, key=lambda x: x.get("score", 0), reverse=True)[:n]

    @staticmethod
    def aggregate_mtf(results: List[Dict]) -> List[Dict]:
        """Collapse per-TF rows into one row per symbol with MTF agreement.

        Logic:
        - Direction from the HIGHEST timeframe with a directional signal
        - Score = TF-weighted average (higher TF = more weight)
        - MTF agreement = count of TFs agreeing on direction
        - Entry data (stop, targets) from LOWEST TF for precision
        - All enrichment fields preserved from best available source
        """
        symbol_signals: Dict[str, List[Dict]] = defaultdict(list)
        for r in results:
            symbol_signals[r["symbol"]].append(r)

        aggregated = []
        min_rr = float(ScannerConfig().min_rr_ratio)
        for symbol, signals in symbol_signals.items():
            if not signals:
                continue

            # Sort by TF hierarchy (lowest first)
            signals_sorted = sorted(
                signals,
                key=lambda s: TF_HIERARCHY.get(s.get("timeframe", ""), 0),
            )

            # Direction from highest TF with directional signal
            direction_signal = "NEUTRAL"
            for s in reversed(signals_sorted):
                if s.get("signal") in ("LONG", "SHORT"):
                    direction_signal = s["signal"]
                    break

            # MTF agreement
            directional = [s for s in signals if s.get("signal") in ("LONG", "SHORT")]
            if directional:
                agree_count = sum(1 for s in directional if s["signal"] == direction_signal)
                total_tf = len(signals)
                mtf_agreement = f"{agree_count}/{total_tf}"
                mtf_score = agree_count / total_tf
            else:
                mtf_agreement = f"0/{len(signals)}"
                mtf_score = 0.0

            # TF-weighted score
            total_weight = 0
            weighted_sum = 0
            for s in signals:
                w = TF_HIERARCHY.get(s.get("timeframe", ""), 1)
                weighted_sum += s.get("score", 0) * w
                total_weight += w
            avg_score = round(weighted_sum / total_weight) if total_weight > 0 else 0

            # MTF RSI alignment bonus: all available TF RSIs pointing the same way
            # as the direction signal adds 5% confidence — cross-timeframe momentum.
            tf_rsis = [s.get("rsi") for s in signals if s.get("rsi") is not None]
            if len(tf_rsis) >= 2 and direction_signal in ("LONG", "SHORT"):
                if direction_signal == "LONG" and all(r > 52 for r in tf_rsis):
                    avg_score = min(100, int(avg_score * 1.05))
                elif direction_signal == "SHORT" and all(r < 48 for r in tf_rsis):
                    avg_score = min(100, int(avg_score * 1.05))

            # Entry params from lowest TF (most precise)
            entry_tf = signals_sorted[0]

            # Best indicators from highest-scoring signal
            best = max(signals, key=lambda s: s.get("score", 0))

            # Recommendation logic with MTF boost/penalty
            rec = entry_tf.get("recommendation", "HOLD")
            rr = entry_tf.get("rr_ratio")
            rr_ok = rr is not None and rr >= min_rr
            if rec in ("BUY", "SELL") and not rr_ok:
                rec = "HOLD"
            if direction_signal != "NEUTRAL" and rec == "HOLD":
                # Promote to actionable rec when directional consensus is strong enough,
                # not only in perfect 3/3 agreement.
                if avg_score >= 60 and mtf_score >= 0.67 and rr_ok:
                    rec = "BUY" if direction_signal == "LONG" else "SELL"
            elif mtf_score < 0.5:
                rec = "HOLD"

            confirms = int(entry_tf.get("signal_age") or 1)
            stock_state = SignalRanker._derive_stock_state(rec, confirms)

            row = {
                "symbol": symbol,
                "signal": direction_signal,
                "recommendation": rec,
                "stock_state": stock_state,
                "recommendation_confirms": confirms,
                "score": avg_score,
                "mtf_agreement": mtf_agreement,
                "mtf_score": mtf_score,
                "price": entry_tf.get("price"),
                "trend_direction": best.get("trend_direction", "SIDEWAYS"),
                "stop_loss": entry_tf.get("stop_loss"),
                "target_1": entry_tf.get("target_1"),
                "target_2": entry_tf.get("target_2"),
                "rr_ratio": entry_tf.get("rr_ratio"),
                "trade_conditions": entry_tf.get("trade_conditions", ""),
                "rsi": best.get("rsi"),
                "rsi_slope": best.get("rsi_slope"),
                "adx": best.get("adx"),
                "adx_slope": best.get("adx_slope"),
                "atr": best.get("atr"),
                "volume_ratio": best.get("volume_ratio"),
                "vwap": best.get("vwap"),
                "vwap_status": best.get("vwap_status", "UNKNOWN"),
                "gex_status": best.get("gex_status", ""),
                "zero_gamma_level": best.get("zero_gamma_level"),
                "gamma_wall_up": best.get("gamma_wall_up"),
                "gamma_wall_down": best.get("gamma_wall_down"),
                "sma_200": best.get("sma_200"),
                "sma_50": best.get("sma_50"),
                "price_vs_sma": best.get("price_vs_sma"),
                "price_vs_sma_pct": best.get("price_vs_sma_pct"),
                "relative_strength": best.get("relative_strength"),
                "market_regime": best.get("market_regime", ""),
                "signal_age": best.get("signal_age", 1),
                "session_time": best.get("session_time", ""),
                "distance_to_resistance_pct": best.get("distance_to_resistance_pct"),
                "distance_to_support_pct": best.get("distance_to_support_pct"),
                "prior_day_high": best.get("prior_day_high"),
                "prior_day_low": best.get("prior_day_low"),
                "prior_day_close": best.get("prior_day_close"),
                "vwap_zscore": entry_tf.get("vwap_zscore"),
                "vwap_std": entry_tf.get("vwap_std"),
                "vwap_reversion_signal": entry_tf.get("vwap_reversion_signal", "NONE"),
                "rsi_bull_divergence": bool(entry_tf.get("rsi_bull_divergence", False)),
                "rsi_bear_divergence": bool(entry_tf.get("rsi_bear_divergence", False)),
                "sweep_reclaim_signal": entry_tf.get("sweep_reclaim_signal", "NONE"),
                "sweep_level": entry_tf.get("sweep_level"),
                "fvg_signal": best.get("fvg_signal", "NONE"),
                "fvg_bullish_low": best.get("fvg_bullish_low"),
                "fvg_bullish_high": best.get("fvg_bullish_high"),
                "fvg_bearish_low": best.get("fvg_bearish_low"),
                "fvg_bearish_high": best.get("fvg_bearish_high"),
                "sector": best.get("sector", "Unknown"),
                "last_updated": best.get("last_updated", ""),
                "timeframe": f"MTF({entry_tf.get('timeframe', '')})",
                "timeframes": {s.get("timeframe", ""): s for s in signals},
            }

            # Pass through all institutional intelligence fields from the best signal.
            # These are set by multi_symbol_scanner.py and must survive MTF aggregation.
            for k, v in best.items():
                if k.startswith("inst_") and k not in row:
                    row[k] = v

            aggregated.append(row)

        return sorted(aggregated, key=lambda x: x.get("score", 0), reverse=True)

    @staticmethod
    def _derive_stock_state(recommendation: str, confirms: int) -> str:
        """Map recommendation persistence to stock-strength state."""
        if recommendation not in ("BUY", "SELL"):
            return "N/A"
        c = max(1, int(confirms or 1))
        if c >= 5:
            return "VERY_STRONG"
        if c >= 2:
            return "CONFIRMED"
        return "NEW"
