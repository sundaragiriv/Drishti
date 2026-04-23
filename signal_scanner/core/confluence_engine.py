"""Six-factor gradient confluence scoring engine.

Scores LONG and SHORT independently using gradient (proportional) scoring,
then picks the direction with the higher score. Generates:
- ATR-based stop loss and scaled targets (T1 at 1R, T2 at 2R or gamma wall)
- R:R ratio with minimum gate (signals below min R:R demoted to HOLD)
- Trade conditions with trailing stop, exit triggers, and warnings
"""

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from signal_scanner.config import CONFLUENCE_WEIGHTS, REGIME_WEIGHTS, ScannerConfig
from signal_scanner.core.gex_calculator import GEXResult
from signal_scanner.core.technical_analyzer import TechnicalResult


@dataclass
class ConfluenceResult:
    """Container for confluence scoring output."""

    score: int = 0
    signal: str = "NEUTRAL"          # LONG, SHORT, NEUTRAL
    breakdown: Dict[str, Tuple[str, int]] = field(default_factory=dict)
    recommendation: str = "HOLD"     # BUY, SELL, HOLD
    trend_direction: str = "SIDEWAYS"
    stop_loss: Optional[float] = None
    target_1: Optional[float] = None   # T1: 1R (take partial profit)
    target_2: Optional[float] = None   # T2: 2R or gamma wall
    rr_ratio: Optional[float] = None   # Risk:Reward ratio
    trade_conditions: str = ""
    distance_to_resistance_pct: Optional[float] = None
    distance_to_support_pct: Optional[float] = None


class ConfluenceEngine:
    """Calculates a 0-100 gradient confluence score from technical and GEX inputs.

    Scoring table (max 100 points, gradient):
        SMA Position:        15 pts (directional, gradient by distance)
        GEX Positioning:     25 pts (directional, gradient by distance from ZG)
        RSI Momentum:        20 pts (directional, gradient by RSI value)
        Volume Confirmation: 15 pts (direction-agnostic, gradient by ratio)
        Trend Strength:      15 pts (direction-agnostic, gradient by ADX with exhaustion penalty)
        VWAP Position:       10 pts (directional, binary — intraday institutional benchmark)
    """

    def score(
        self,
        tech: TechnicalResult,
        gex: GEXResult,
        config: Optional[ScannerConfig] = None,
        market_regime: str = "NEUTRAL",
    ) -> ConfluenceResult:
        """Calculate gradient confluence score with ATR-based trade parameters.

        Uses regime-adaptive weights: RISK_ON emphasises trend/momentum,
        RISK_OFF emphasises GEX/volume, NEUTRAL uses default balanced weights.
        """
        cfg = config or ScannerConfig()
        w = REGIME_WEIGHTS.get(market_regime, CONFLUENCE_WEIGHTS)

        long_score = 0
        short_score = 0
        breakdown: Dict[str, Tuple[str, int]] = {}

        # Factor 1: SMA Position (15 pts, gradient by distance from SMA)
        sma_pts = self._score_sma(tech, w["sma_position"])
        long_score += sma_pts[0]
        short_score += sma_pts[1]
        breakdown["SMA Position"] = sma_pts[2]

        # Factor 2: GEX Positioning (25 pts, gradient by distance from zero gamma)
        gex_pts = self._score_gex(tech, gex, w["gex_positioning"])
        long_score += gex_pts[0]
        short_score += gex_pts[1]
        breakdown["GEX Positioning"] = gex_pts[2]

        # Factor 3: RSI Momentum (20 pts, gradient)
        rsi_pts = self._score_rsi(tech, w["rsi_momentum"])
        long_score += rsi_pts[0]
        short_score += rsi_pts[1]
        breakdown["RSI Momentum"] = rsi_pts[2]

        # Factor 4: Volume Confirmation (15 pts, direction-agnostic, gradient)
        vol_pts = self._score_volume(tech, cfg, w["volume_confirmation"])
        long_score += vol_pts
        short_score += vol_pts
        breakdown["Volume Confirm"] = ("CONFIRMED" if vol_pts > 0 else "UNCONFIRMED", vol_pts)

        # Factor 5: Trend Strength (15 pts, direction-agnostic, gradient with exhaustion)
        trend_pts = self._score_trend(tech, cfg, w["trend_strength"])
        long_score += trend_pts
        short_score += trend_pts
        breakdown["Trend Strength"] = ("STRONG" if trend_pts > 7 else "WEAK", trend_pts)

        # Factor 6: VWAP Position (10 pts, directional)
        vwap_pts = self._score_vwap(tech, w["vwap_position"])
        long_score += vwap_pts[0]
        short_score += vwap_pts[1]
        breakdown["VWAP Position"] = vwap_pts[2]

        # When GEX data is unavailable, its weight was silently scored as 0.
        # Normalize scores to 100 so strong technicals aren't penalised for missing GEX.
        if gex.gex_status == "UNKNOWN" or gex.zero_gamma_level is None:
            available = 100 - w["gex_positioning"]
            if 0 < available < 100:
                long_score = int(round(min(long_score * 100 / available, 100)))
                short_score = int(round(min(short_score * 100 / available, 100)))

        # FVG conviction bonus — applied after normalization so it isn't inflated.
        # Adds up to 8 pts for price inside an institutional imbalance zone.
        fvg_long, fvg_short = self._score_fvg(tech)
        long_score = min(100, long_score + fvg_long)
        short_score = min(100, short_score + fvg_short)
        breakdown["FVG"] = (getattr(tech, "fvg_signal", "NONE"), fvg_long or fvg_short)

        # Determine signal direction (min 35 pts for directional signal)
        if long_score > short_score and long_score >= 35:
            signal = "LONG"
            final_score = long_score
        elif short_score > long_score and short_score >= 35:
            signal = "SHORT"
            final_score = short_score
        else:
            signal = "NEUTRAL"
            final_score = max(long_score, short_score)

        # ATR-based stop loss and targets
        stop_loss = self._calc_stop_atr(signal, tech, gex, cfg)
        target_1, target_2 = self._calc_targets_atr(signal, tech, gex, cfg, stop_loss)

        # R:R ratio
        rr_ratio = self._calc_rr(signal, tech.current_price, stop_loss, target_2)

        # Distance to levels
        dist_res, dist_sup = self._calc_distances(tech, gex)

        # Recommendation with R:R gate
        recommendation = self._get_recommendation(signal, final_score, rr_ratio, cfg)

        # Trade conditions
        trade_conditions = self._build_conditions(
            signal, final_score, tech, gex, stop_loss, target_1, target_2, rr_ratio, cfg
        )

        return ConfluenceResult(
            score=final_score,
            signal=signal,
            breakdown=breakdown,
            recommendation=recommendation,
            trend_direction=tech.trend_direction,
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            rr_ratio=rr_ratio,
            trade_conditions=trade_conditions,
            distance_to_resistance_pct=dist_res,
            distance_to_support_pct=dist_sup,
        )

    # ------------------------------------------------------------------
    # Gradient scoring functions
    # ------------------------------------------------------------------

    @staticmethod
    def _score_sma(tech: TechnicalResult, max_pts: int) -> tuple:
        """Gradient SMA scoring based on distance from SMA-200 + prior day level context.

        Prior Day High/Low breakouts add conviction to directional reads:
        - LONG + breaking PDH: +20% of max (strong momentum)
        - LONG + above PDC:    + 7% of max (holding above prior close)
        - SHORT + breaking PDL / below PDC: mirror
        """
        if tech.price_vs_sma_pct is None:
            return 0, 0, ("NEUTRAL", 0)

        pct = abs(tech.price_vs_sma_pct)
        if pct < 0.5:
            ratio = 0.2
        elif pct < 1.0:
            ratio = 0.4
        elif pct < 2.0:
            ratio = 0.6
        elif pct < 3.0:
            ratio = 0.8
        else:
            ratio = 1.0

        pts = int(max_pts * ratio)

        # Prior day level bonus — uses already-computed fields, no extra data cost.
        price = tech.current_price
        if tech.price_vs_sma == "ABOVE" and pts < max_pts:
            if tech.prior_day_high is not None and price > tech.prior_day_high:
                pts = min(max_pts, pts + int(max_pts * 0.20))  # breaking PDH
            elif tech.prior_day_close is not None and price > tech.prior_day_close:
                pts = min(max_pts, pts + int(max_pts * 0.07))  # holding above prior close
        elif tech.price_vs_sma == "BELOW" and pts < max_pts:
            if tech.prior_day_low is not None and price < tech.prior_day_low:
                pts = min(max_pts, pts + int(max_pts * 0.20))  # breaking PDL
            elif tech.prior_day_close is not None and price < tech.prior_day_close:
                pts = min(max_pts, pts + int(max_pts * 0.07))  # holding below prior close

        if tech.price_vs_sma == "ABOVE":
            return pts, 0, ("LONG", pts)
        elif tech.price_vs_sma == "BELOW":
            return 0, pts, ("SHORT", pts)
        return 0, 0, ("NEUTRAL", 0)

    @staticmethod
    def _score_gex(tech: TechnicalResult, gex: GEXResult, max_pts: int) -> tuple:
        """Gradient GEX scoring based on distance from zero gamma."""
        if gex.gex_status == "UNKNOWN" or gex.zero_gamma_level is None:
            return 0, 0, ("UNKNOWN", 0)

        price = tech.current_price
        if price <= 0 or gex.zero_gamma_level <= 0:
            return 0, 0, ("UNKNOWN", 0)

        dist_pct = abs((price - gex.zero_gamma_level) / gex.zero_gamma_level) * 100

        # Scale: 0-0.5% = 30%, 0.5-1% = 50%, 1-2% = 75%, 2%+ = 100%
        if dist_pct < 0.5:
            ratio = 0.3
        elif dist_pct < 1.0:
            ratio = 0.5
        elif dist_pct < 2.0:
            ratio = 0.75
        else:
            ratio = 1.0

        pts = int(max_pts * ratio)

        if gex.gex_status == "ABOVE_ZERO_GAMMA":
            return pts, 0, ("LONG", pts)
        else:
            return 0, pts, ("SHORT", pts)

    @staticmethod
    def _score_rsi(tech: TechnicalResult, max_pts: int) -> tuple:
        """Gradient RSI scoring — momentum distance, slope, and hidden divergence.

        Divergence bonus: RSI bull divergence (price lower, RSI higher) on a LONG
        signal indicates hidden buying pressure — adds conviction. Mirror for SHORT.
        """
        if tech.rsi is None:
            return 0, 0, ("NEUTRAL", 0)

        rsi = tech.rsi

        dist = abs(rsi - 50)
        if dist < 3:
            ratio = 0.1
        elif dist < 8:
            ratio = 0.3
        elif dist < 15:
            ratio = 0.6
        elif dist < 25:
            ratio = 0.85
        else:
            ratio = 1.0

        if rsi > 75 or rsi < 25:
            ratio *= 0.7

        pts = int(max_pts * ratio)

        # Slope bonus: +20% when momentum direction confirms signal
        if tech.rsi_slope is not None:
            if (rsi > 50 and tech.rsi_slope > 0) or (rsi < 50 and tech.rsi_slope < 0):
                pts = min(max_pts, int(pts * 1.2))

        # Hidden divergence bonus: price makes new low but RSI is recovering (bull),
        # or price makes new high but RSI is fading (bear). +10% of max per divergence.
        if rsi > 50 and tech.rsi_bull_divergence:
            pts = min(max_pts, pts + int(max_pts * 0.10))
        elif rsi < 50 and tech.rsi_bear_divergence:
            pts = min(max_pts, pts + int(max_pts * 0.10))

        if rsi > 50:
            return pts, 0, ("LONG", pts)
        elif rsi < 50:
            return 0, pts, ("SHORT", pts)
        return 0, 0, ("NEUTRAL", 0)

    @staticmethod
    def _score_volume(tech: TechnicalResult, cfg: ScannerConfig, max_pts: int) -> int:
        """Gradient volume scoring — ratio plus liquidity sweep/reclaim confirmation.

        A sweep/reclaim event (institutional defense of a level with volume) scores
        full points regardless of raw ratio because it IS institutional action.
        """
        # Liquidity sweep with reclaim = highest-conviction volume signal
        sweep = tech.sweep_reclaim_signal
        if sweep in ("BULLISH_SWEEP_RECLAIM", "BEARISH_SWEEP_RECLAIM"):
            return max_pts

        if tech.volume_ratio is None:
            return 0

        vr = tech.volume_ratio
        if vr < 1.0:
            return 0
        elif vr < cfg.volume_threshold:
            return int(max_pts * 0.3)
        elif vr < 1.5:
            return int(max_pts * 0.5)
        elif vr < 2.0:
            return int(max_pts * 0.75)
        elif vr < 3.0:
            return int(max_pts * 0.9)
        else:
            return max_pts

    @staticmethod
    def _score_trend(tech: TechnicalResult, cfg: ScannerConfig, max_pts: int) -> int:
        """Gradient ADX scoring with exhaustion penalty above 50."""
        if tech.adx is None:
            return 0

        adx = tech.adx
        if adx < 20:
            return 0
        elif adx < 25:
            pts = int(max_pts * 0.3)
        elif adx < 30:
            pts = int(max_pts * 0.6)
        elif adx < 40:
            pts = max_pts
        elif adx < 50:
            pts = int(max_pts * 0.8)  # Start tapering — trend may be exhausting
        else:
            pts = int(max_pts * 0.5)  # ADX > 50 often precedes reversal

        # Bonus if ADX is rising (trend strengthening)
        if tech.adx_slope is not None and tech.adx_slope > 0 and adx < 45:
            pts = min(max_pts, pts + 2)

        return pts

    @staticmethod
    def _score_vwap(tech: TechnicalResult, max_pts: int) -> tuple:
        """Gradient VWAP scoring using zscore distance — institutional benchmark.

        Direction (above/below VWAP) is the primary signal.
        zscore distance refines conviction:
          0 – 0.3σ : barely separated — 40% of points
          0.3 – 1.5σ: trending cleanly — full points (sweet spot)
          1.5 – 2.5σ: extended but valid — 70% of points
          > 2.5σ   : overextended, reversion risk — 40% of points
        Falls back to 60% when vwap_zscore is not available.
        """
        if tech.vwap_status == "UNKNOWN":
            return 0, 0, ("NEUTRAL", 0)

        z = tech.vwap_zscore  # signed; positive = above VWAP, negative = below

        if tech.vwap_status == "ABOVE_VWAP":
            if z is None:
                pts = int(max_pts * 0.6)
            else:
                z_abs = abs(z)
                if z_abs < 0.3:
                    pts = int(max_pts * 0.4)
                elif z_abs <= 1.5:
                    pts = max_pts
                elif z_abs <= 2.5:
                    pts = int(max_pts * 0.7)
                else:
                    pts = int(max_pts * 0.4)
            return pts, 0, ("LONG", pts)

        else:  # BELOW_VWAP
            if z is None:
                pts = int(max_pts * 0.6)
            else:
                z_abs = abs(z)
                if z_abs < 0.3:
                    pts = int(max_pts * 0.4)
                elif z_abs <= 1.5:
                    pts = max_pts
                elif z_abs <= 2.5:
                    pts = int(max_pts * 0.7)
                else:
                    pts = int(max_pts * 0.4)
            return 0, pts, ("SHORT", pts)

    @staticmethod
    def _score_fvg(tech: TechnicalResult) -> tuple:
        """FVG conviction bonus — added after base scoring, capped at 100 total.

        Price inside an unfilled imbalance zone means institutions left an order
        footprint that price is retesting.  This is one of the highest-quality
        intraday setups and deserves independent weight beyond the 6 base factors.

        Returns (long_bonus, short_bonus).
        """
        sig = getattr(tech, "fvg_signal", "NONE")
        if sig == "IN_BULLISH_FVG":
            return 8, 0   # Retesting a bullish imbalance — strong LONG support
        if sig == "NEAR_BULLISH_FVG":
            return 4, 0   # Approaching the bullish zone
        if sig == "IN_BEARISH_FVG":
            return 0, 8   # Retesting a bearish imbalance — strong SHORT resistance
        if sig == "NEAR_BEARISH_FVG":
            return 0, 4   # Approaching the bearish zone
        return 0, 0

    # ------------------------------------------------------------------
    # ATR-based stop loss and targets
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_stop_atr(
        signal: str,
        tech: TechnicalResult,
        gex: GEXResult,
        cfg: ScannerConfig,
    ) -> Optional[float]:
        """ATR-based stop loss, tightened by GEX levels if closer."""
        price = tech.current_price
        atr = tech.atr
        if price <= 0 or signal == "NEUTRAL":
            return None

        if atr is None or atr <= 0:
            # Fallback: 2% stop
            if signal == "LONG":
                return round(price * 0.98, 2)
            else:
                return round(price * 1.02, 2)

        atr_stop_dist = atr * cfg.atr_stop_multiplier

        if signal == "LONG":
            atr_stop = price - atr_stop_dist

            # Check if GEX support is tighter (closer to price but still below)
            gex_support = None
            if gex.gamma_wall_down is not None and gex.gamma_wall_down < price:
                gex_support = gex.gamma_wall_down
            if gex.zero_gamma_level is not None and gex.zero_gamma_level < price:
                if gex_support is None or gex.zero_gamma_level > gex_support:
                    gex_support = gex.zero_gamma_level

            # Use the tighter of ATR stop or GEX support (whichever is closer to price)
            if gex_support is not None and gex_support > atr_stop:
                return round(gex_support, 2)
            return round(atr_stop, 2)

        else:  # SHORT
            atr_stop = price + atr_stop_dist

            gex_resistance = None
            if gex.gamma_wall_up is not None and gex.gamma_wall_up > price:
                gex_resistance = gex.gamma_wall_up
            if gex.zero_gamma_level is not None and gex.zero_gamma_level > price:
                if gex_resistance is None or gex.zero_gamma_level < gex_resistance:
                    gex_resistance = gex.zero_gamma_level

            if gex_resistance is not None and gex_resistance < atr_stop:
                return round(gex_resistance, 2)
            return round(atr_stop, 2)

    @staticmethod
    def _calc_targets_atr(
        signal: str,
        tech: TechnicalResult,
        gex: GEXResult,
        cfg: ScannerConfig,
        stop_loss: Optional[float],
    ) -> tuple:
        """Calculate T1 (1R) and T2 (2R or gamma wall) targets."""
        price = tech.current_price
        if price <= 0 or signal == "NEUTRAL" or stop_loss is None:
            return None, None

        risk = abs(price - stop_loss)
        if risk <= 0:
            return None, None

        if signal == "LONG":
            t1_ratio = cfg.atr_target1_multiplier / cfg.atr_stop_multiplier
            t2_ratio = cfg.atr_target2_multiplier / cfg.atr_stop_multiplier
            t1 = round(price + risk * t1_ratio, 2)
            t2_atr = round(price + risk * t2_ratio, 2)

            # T2: use gamma wall if it's a reasonable target.
            if gex.gamma_wall_up is not None and gex.gamma_wall_up > t1:
                t2_gamma = gex.gamma_wall_up
                if t2_gamma <= price + risk * (t2_ratio + 1):
                    t2 = round(t2_gamma, 2)
                else:
                    t2 = t2_atr
            else:
                t2 = t2_atr

            return t1, t2

        else:  # SHORT
            t1_ratio = cfg.atr_target1_multiplier / cfg.atr_stop_multiplier
            t2_ratio = cfg.atr_target2_multiplier / cfg.atr_stop_multiplier
            t1 = round(price - risk * t1_ratio, 2)
            t2_atr = round(price - risk * t2_ratio, 2)

            if gex.gamma_wall_down is not None and gex.gamma_wall_down < t1:
                t2_gamma = gex.gamma_wall_down
                if t2_gamma >= price - risk * (t2_ratio + 1):
                    t2 = round(t2_gamma, 2)
                else:
                    t2 = t2_atr
            else:
                t2 = t2_atr

            return t1, t2

    @staticmethod
    def _calc_rr(
        signal: str,
        price: float,
        stop_loss: Optional[float],
        target: Optional[float],
    ) -> Optional[float]:
        """Calculate risk:reward ratio."""
        if signal == "NEUTRAL" or stop_loss is None or target is None or price <= 0:
            return None
        risk = abs(price - stop_loss)
        reward = abs(target - price)
        if risk <= 0:
            return None
        return round(reward / risk, 1)

    @staticmethod
    def _calc_distances(tech: TechnicalResult, gex: GEXResult) -> tuple:
        """Calculate distance to nearest resistance and support as percentage."""
        price = tech.current_price
        if price <= 0:
            return None, None

        dist_res = None
        dist_sup = None

        # Resistance: gamma wall up or recent high
        resistance = gex.gamma_wall_up
        if resistance is None and tech.recent_high is not None:
            resistance = tech.recent_high
        if resistance is not None and resistance > price:
            dist_res = round(((resistance - price) / price) * 100, 1)

        # Support: gamma wall down or recent low
        support = gex.gamma_wall_down
        if support is None and tech.recent_low is not None:
            support = tech.recent_low
        if support is not None and support < price:
            dist_sup = round(((price - support) / price) * 100, 1)

        return dist_res, dist_sup

    @staticmethod
    def _get_recommendation(
        signal: str,
        score: int,
        rr_ratio: Optional[float],
        cfg: ScannerConfig,
    ) -> str:
        """Map signal + score + R:R to recommendation. R:R gate enforced."""
        if signal == "NEUTRAL":
            return "HOLD"

        # Must have minimum score — aligned with paper entry threshold
        # to avoid showing BUY/SELL that will be rejected at entry gate
        if score < 70:
            return "HOLD"

        # R:R gate
        if rr_ratio is None or rr_ratio < cfg.min_rr_ratio:
            return "HOLD"

        if signal == "LONG":
            return "BUY"
        elif signal == "SHORT":
            return "SELL"
        return "HOLD"

    @staticmethod
    def _build_conditions(
        signal: str,
        score: int,
        tech: TechnicalResult,
        gex: GEXResult,
        stop_loss: Optional[float],
        target_1: Optional[float],
        target_2: Optional[float],
        rr_ratio: Optional[float],
        cfg: ScannerConfig,
    ) -> str:
        """Build trade conditions with ATR-aware trailing stops and exits."""
        if signal == "NEUTRAL":
            return "No trade — wait for clearer signal alignment"

        parts = []
        price = tech.current_price

        # Scaled exit plan
        if signal == "LONG" and target_1 and target_2 and stop_loss:
            parts.append(f"T1 ${target_1} — take 50% profit, move stop to breakeven")
            parts.append(f"T2 ${target_2} — close remaining, or trail 1 ATR below")
        elif signal == "SHORT" and target_1 and target_2 and stop_loss:
            parts.append(f"T1 ${target_1} — cover 50%, move stop to breakeven")
            parts.append(f"T2 ${target_2} — cover remaining, or trail 1 ATR above")

        # R:R warning
        if rr_ratio is not None and rr_ratio < cfg.min_rr_ratio:
            parts.append(f"CAUTION: R:R {rr_ratio}:1 below minimum — reduced conviction")

        # GEX wall proximity
        if signal == "LONG" and gex.gamma_wall_up and price > 0:
            wall_dist = round(((gex.gamma_wall_up - price) / price) * 100, 1)
            if wall_dist < 1.5:
                parts.append(f"CAUTION: Resistance ${gex.gamma_wall_up} only {wall_dist}% away")
        elif signal == "SHORT" and gex.gamma_wall_down and price > 0:
            wall_dist = round(((price - gex.gamma_wall_down) / price) * 100, 1)
            if wall_dist < 1.5:
                parts.append(f"CAUTION: Support ${gex.gamma_wall_down} only {wall_dist}% away")

        # RSI extremes
        if tech.rsi is not None:
            if tech.rsi > 75:
                parts.append("RSI >75 overbought — tighten stops")
            elif tech.rsi < 25:
                parts.append("RSI <25 oversold — tighten stops")

        # Momentum confirmation/divergence
        if tech.rsi_slope is not None:
            if signal == "LONG" and tech.rsi_slope < -3:
                parts.append("Momentum fading — RSI declining")
            elif signal == "SHORT" and tech.rsi_slope > 3:
                parts.append("Momentum fading — RSI rising")

        # Counter-trend warning
        if signal == "LONG" and tech.trend_direction == "DOWNTREND":
            parts.append("Counter-trend trade — reduce size")
        elif signal == "SHORT" and tech.trend_direction == "UPTREND":
            parts.append("Counter-trend trade — reduce size")

        # VWAP alignment
        if signal == "LONG" and tech.vwap_status == "BELOW_VWAP":
            parts.append("Below VWAP — institutional selling pressure")
        elif signal == "SHORT" and tech.vwap_status == "ABOVE_VWAP":
            parts.append("Above VWAP — institutional buying pressure")

        return " | ".join(parts) if parts else "Standard entry — monitor for exits"
