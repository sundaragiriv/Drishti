"""Technical analysis engine using pandas-ta.

Computes SMA-200, SMA-50, RSI-14, ADX-14, ATR-14, VWAP, volume ratio,
momentum slopes, trend direction, prior day levels, and recent high/low.
"""

from dataclasses import dataclass
from typing import Dict, Optional

import pandas as pd
from loguru import logger

from signal_scanner.config import ScannerConfig

# pandas-ta is imported at function level to defer the slow import


@dataclass
class TechnicalResult:
    """Container for technical analysis output."""

    current_price: float
    sma_200: Optional[float] = None
    sma_50: Optional[float] = None
    price_vs_sma: str = "UNKNOWN"  # ABOVE, BELOW, UNKNOWN
    price_vs_sma_pct: Optional[float] = None
    rsi: Optional[float] = None
    rsi_slope: Optional[float] = None        # Positive = rising, negative = falling
    adx: Optional[float] = None
    adx_slope: Optional[float] = None        # Positive = strengthening trend
    atr: Optional[float] = None              # Average True Range (14-period)
    volume_ratio: Optional[float] = None
    vwap: Optional[float] = None             # Volume-Weighted Average Price
    vwap_status: str = "UNKNOWN"             # ABOVE_VWAP, BELOW_VWAP, UNKNOWN
    trend_direction: str = "SIDEWAYS"        # UPTREND, DOWNTREND, SIDEWAYS
    recent_high: Optional[float] = None      # Highest high in lookback
    recent_low: Optional[float] = None       # Lowest low in lookback
    prior_day_high: Optional[float] = None   # Previous day's high
    prior_day_low: Optional[float] = None    # Previous day's low
    prior_day_close: Optional[float] = None  # Previous day's close
    vwap_zscore: Optional[float] = None
    vwap_std: Optional[float] = None
    vwap_reversion_signal: str = "NONE"      # LONG_REVERSION, SHORT_REVERSION, NONE
    rsi_bull_divergence: bool = False
    rsi_bear_divergence: bool = False
    sweep_reclaim_signal: str = "NONE"       # BULLISH_SWEEP_RECLAIM, BEARISH_SWEEP_RECLAIM, NONE
    sweep_level: Optional[float] = None
    # Fair Value Gap (3-bar imbalance) — nearest unfilled bullish and bearish zones
    fvg_bullish_low: Optional[float] = None  # Bottom of nearest active bullish FVG
    fvg_bullish_high: Optional[float] = None # Top of nearest active bullish FVG
    fvg_bearish_low: Optional[float] = None  # Bottom of nearest active bearish FVG
    fvg_bearish_high: Optional[float] = None # Top of nearest active bearish FVG
    fvg_signal: str = "NONE"                 # IN_BULLISH_FVG | NEAR_BULLISH_FVG | IN_BEARISH_FVG | NEAR_BEARISH_FVG | NONE


class TechnicalAnalyzer:
    """Runs technical indicators on OHLCV data."""

    def analyze(
        self,
        df: pd.DataFrame,
        config: Optional[ScannerConfig] = None,
    ) -> TechnicalResult:
        """Compute all technical indicators for a price DataFrame.

        Args:
            df: OHLCV DataFrame with columns Open, High, Low, Close, Volume.
            config: Scanner configuration with indicator periods.

        Returns:
            TechnicalResult with all computed values.
        """
        cfg = config or ScannerConfig()

        if df is None or df.empty:
            return TechnicalResult(current_price=0.0)

        current_price = float(df["Close"].iloc[-1])

        # Need enough bars for the longest indicator (SMA-200)
        if len(df) < cfg.sma_period:
            logger.debug(
                f"Insufficient data: {len(df)} bars, need {cfg.sma_period} for SMA"
            )
            return self._partial_analyze(df, current_price, cfg)

        return self._full_analyze(df, current_price, cfg)

    def _full_analyze(
        self,
        df: pd.DataFrame,
        current_price: float,
        cfg: ScannerConfig,
    ) -> TechnicalResult:
        """Run full analysis when enough bars are available."""
        import pandas_ta as ta

        # SMA 200 (long-term)
        sma_series = ta.sma(df["Close"], length=cfg.sma_period)
        sma_200 = float(sma_series.iloc[-1]) if sma_series is not None and not sma_series.empty else None

        # SMA 50 (short-term, for trend detection)
        sma_50_series = ta.sma(df["Close"], length=cfg.sma_short_period)
        sma_50 = float(sma_50_series.iloc[-1]) if sma_50_series is not None and not sma_50_series.empty else None

        # RSI + slope
        rsi_series = ta.rsi(df["Close"], length=cfg.rsi_period)
        rsi = None
        rsi_slope = None
        if rsi_series is not None and not rsi_series.empty:
            rsi = float(rsi_series.iloc[-1])
            rsi_slope = self._calc_slope(rsi_series, cfg.momentum_slope_period)

        # ADX + slope
        adx_df = ta.adx(df["High"], df["Low"], df["Close"], length=cfg.adx_period)
        adx = None
        adx_slope = None
        if adx_df is not None and not adx_df.empty:
            adx_col = f"ADX_{cfg.adx_period}"
            if adx_col in adx_df.columns:
                val = adx_df[adx_col].iloc[-1]
                adx = float(val) if pd.notna(val) else None
                adx_slope = self._calc_slope(adx_df[adx_col], cfg.momentum_slope_period)

        # ATR
        atr = self._calc_atr(df, cfg.atr_period)

        # Volume ratio
        vol_avg = df["Volume"].rolling(window=cfg.volume_avg_period).mean()
        volume_ratio = None
        if vol_avg is not None and not vol_avg.empty and vol_avg.iloc[-1] > 0:
            volume_ratio = float(df["Volume"].iloc[-1] / vol_avg.iloc[-1])

        # VWAP
        vwap = self._calc_vwap(df)
        vwap_status = "UNKNOWN"
        if vwap is not None:
            vwap_status = "ABOVE_VWAP" if current_price > vwap else "BELOW_VWAP"

        # Price vs SMA
        price_vs_sma = "UNKNOWN"
        price_vs_sma_pct = None
        if sma_200 is not None and sma_200 > 0:
            price_vs_sma = "ABOVE" if current_price > sma_200 else "BELOW"
            price_vs_sma_pct = round(((current_price - sma_200) / sma_200) * 100, 2)

        # Trend direction
        trend_direction = self._detect_trend(current_price, sma_50, sma_200, adx)

        # Recent high/low (last 20 bars for support/resistance)
        lookback = min(20, len(df))
        recent_high = round(float(df["High"].iloc[-lookback:].max()), 2)
        recent_low = round(float(df["Low"].iloc[-lookback:].min()), 2)

        # Prior day levels
        prior_day_high, prior_day_low, prior_day_close = self._get_prior_day_levels(df)
        setup = self._detect_setup_signals(df, cfg, vwap=vwap, rsi_series=rsi_series)
        fvg = self._detect_fvg(df, lookback=cfg.fvg_lookback_bars)

        return TechnicalResult(
            current_price=round(current_price, 2),
            sma_200=round(sma_200, 2) if sma_200 else None,
            sma_50=round(sma_50, 2) if sma_50 else None,
            price_vs_sma=price_vs_sma,
            price_vs_sma_pct=price_vs_sma_pct,
            rsi=round(rsi, 2) if rsi is not None else None,
            rsi_slope=round(rsi_slope, 2) if rsi_slope is not None else None,
            adx=round(adx, 2) if adx is not None else None,
            adx_slope=round(adx_slope, 2) if adx_slope is not None else None,
            atr=round(atr, 4) if atr is not None else None,
            volume_ratio=round(volume_ratio, 2) if volume_ratio is not None else None,
            vwap=round(vwap, 2) if vwap is not None else None,
            vwap_status=vwap_status,
            trend_direction=trend_direction,
            recent_high=recent_high,
            recent_low=recent_low,
            prior_day_high=prior_day_high,
            prior_day_low=prior_day_low,
            prior_day_close=prior_day_close,
            vwap_zscore=setup["vwap_zscore"],
            vwap_std=setup["vwap_std"],
            vwap_reversion_signal=setup["vwap_reversion_signal"],
            rsi_bull_divergence=setup["rsi_bull_divergence"],
            rsi_bear_divergence=setup["rsi_bear_divergence"],
            sweep_reclaim_signal=setup["sweep_reclaim_signal"],
            sweep_level=setup["sweep_level"],
            fvg_bullish_low=fvg["fvg_bullish_low"],
            fvg_bullish_high=fvg["fvg_bullish_high"],
            fvg_bearish_low=fvg["fvg_bearish_low"],
            fvg_bearish_high=fvg["fvg_bearish_high"],
            fvg_signal=fvg["fvg_signal"],
        )

    def _partial_analyze(
        self,
        df: pd.DataFrame,
        current_price: float,
        cfg: ScannerConfig,
    ) -> TechnicalResult:
        """Run partial analysis when not enough bars for SMA-200."""
        import pandas_ta as ta

        rsi = None
        rsi_slope = None
        adx = None
        adx_slope = None
        atr = None
        volume_ratio = None
        sma_50 = None
        vwap = None
        vwap_status = "UNKNOWN"

        # SMA 50
        if len(df) >= cfg.sma_short_period:
            sma_50_series = ta.sma(df["Close"], length=cfg.sma_short_period)
            if sma_50_series is not None and not sma_50_series.empty:
                sma_50 = float(sma_50_series.iloc[-1])

        # RSI + slope
        if len(df) >= cfg.rsi_period + 1:
            rsi_series = ta.rsi(df["Close"], length=cfg.rsi_period)
            if rsi_series is not None and not rsi_series.empty:
                val = rsi_series.iloc[-1]
                rsi = float(val) if pd.notna(val) else None
                rsi_slope = self._calc_slope(rsi_series, cfg.momentum_slope_period)

        # ADX + slope
        if len(df) >= cfg.adx_period * 2:
            adx_df = ta.adx(df["High"], df["Low"], df["Close"], length=cfg.adx_period)
            if adx_df is not None and not adx_df.empty:
                adx_col = f"ADX_{cfg.adx_period}"
                if adx_col in adx_df.columns:
                    val = adx_df[adx_col].iloc[-1]
                    adx = float(val) if pd.notna(val) else None
                    adx_slope = self._calc_slope(adx_df[adx_col], cfg.momentum_slope_period)

        # ATR
        if len(df) >= cfg.atr_period + 1:
            atr = self._calc_atr(df, cfg.atr_period)

        # Volume ratio
        if len(df) >= cfg.volume_avg_period:
            vol_avg = df["Volume"].rolling(window=cfg.volume_avg_period).mean().iloc[-1]
            if vol_avg > 0:
                volume_ratio = float(df["Volume"].iloc[-1] / vol_avg)

        # VWAP
        vwap = self._calc_vwap(df)
        if vwap is not None:
            vwap_status = "ABOVE_VWAP" if current_price > vwap else "BELOW_VWAP"

        # Trend
        trend_direction = self._detect_trend(current_price, sma_50, None, adx)

        # Recent high/low
        lookback = min(20, len(df))
        recent_high = round(float(df["High"].iloc[-lookback:].max()), 2)
        recent_low = round(float(df["Low"].iloc[-lookback:].min()), 2)

        # Prior day levels
        prior_day_high, prior_day_low, prior_day_close = self._get_prior_day_levels(df)
        setup = self._detect_setup_signals(df, cfg, vwap=vwap, rsi_series=rsi_series if 'rsi_series' in locals() else None)
        fvg = self._detect_fvg(df, lookback=cfg.fvg_lookback_bars)

        return TechnicalResult(
            current_price=round(current_price, 2),
            rsi=round(rsi, 2) if rsi is not None else None,
            rsi_slope=round(rsi_slope, 2) if rsi_slope is not None else None,
            adx=round(adx, 2) if adx is not None else None,
            adx_slope=round(adx_slope, 2) if adx_slope is not None else None,
            atr=round(atr, 4) if atr is not None else None,
            volume_ratio=round(volume_ratio, 2) if volume_ratio is not None else None,
            vwap=round(vwap, 2) if vwap is not None else None,
            vwap_status=vwap_status,
            trend_direction=trend_direction,
            recent_high=recent_high,
            recent_low=recent_low,
            sma_50=round(sma_50, 2) if sma_50 else None,
            prior_day_high=prior_day_high,
            prior_day_low=prior_day_low,
            prior_day_close=prior_day_close,
            vwap_zscore=setup["vwap_zscore"],
            vwap_std=setup["vwap_std"],
            vwap_reversion_signal=setup["vwap_reversion_signal"],
            rsi_bull_divergence=setup["rsi_bull_divergence"],
            rsi_bear_divergence=setup["rsi_bear_divergence"],
            sweep_reclaim_signal=setup["sweep_reclaim_signal"],
            sweep_level=setup["sweep_level"],
            fvg_bullish_low=fvg["fvg_bullish_low"],
            fvg_bullish_high=fvg["fvg_bullish_high"],
            fvg_bearish_low=fvg["fvg_bearish_low"],
            fvg_bearish_high=fvg["fvg_bearish_high"],
            fvg_signal=fvg["fvg_signal"],
        )

    def _detect_setup_signals(
        self,
        df: pd.DataFrame,
        cfg: ScannerConfig,
        vwap: Optional[float],
        rsi_series: Optional[pd.Series],
    ) -> Dict[str, object]:
        """Detect liquidity sweep/reclaim and VWAP mean-reversion context."""
        out: Dict[str, object] = {
            "vwap_zscore": None,
            "vwap_std": None,
            "vwap_reversion_signal": "NONE",
            "rsi_bull_divergence": False,
            "rsi_bear_divergence": False,
            "sweep_reclaim_signal": "NONE",
            "sweep_level": None,
        }
        if df is None or df.empty or len(df) < 6:
            return out

        close = df["Close"]
        high = df["High"]
        low = df["Low"]
        volume = df["Volume"]
        current_close = float(close.iloc[-1])

        # RSI divergence check (simple but robust).
        look = max(3, int(cfg.rsi_divergence_lookback))
        look = min(look, len(df) - 2)
        if look >= 3 and rsi_series is not None and not rsi_series.empty:
            cur_rsi = rsi_series.iloc[-1]
            prev_rsi = rsi_series.iloc[-look - 1 : -1]
            prev_close = close.iloc[-look - 1 : -1]
            if pd.notna(cur_rsi) and not prev_rsi.dropna().empty and not prev_close.empty:
                prev_close_min = float(prev_close.min())
                prev_close_max = float(prev_close.max())
                prev_rsi_min = float(prev_rsi.min())
                prev_rsi_max = float(prev_rsi.max())
                out["rsi_bull_divergence"] = current_close <= prev_close_min and float(cur_rsi) > (prev_rsi_min + 0.5)
                out["rsi_bear_divergence"] = current_close >= prev_close_max and float(cur_rsi) < (prev_rsi_max - 0.5)

        # VWAP standard deviation mean-reversion signal.
        if vwap is not None and len(df) >= 3:
            session_df = self._get_session_df(df)
            if session_df is not None and len(session_df) >= 10:
                std = float(session_df["Close"].std(ddof=0))
                if std > 0:
                    prev_close = float(close.iloc[-2])
                    z_now = (current_close - float(vwap)) / std
                    z_prev = (prev_close - float(vwap)) / std
                    out["vwap_std"] = round(std, 4)
                    out["vwap_zscore"] = round(float(z_now), 3)
                    sd_threshold = float(cfg.vwap_reversion_sd_threshold)
                    if (
                        z_prev <= -sd_threshold
                        and z_now > -sd_threshold
                        and bool(out["rsi_bull_divergence"])
                    ):
                        out["vwap_reversion_signal"] = "LONG_REVERSION"
                    elif (
                        z_prev >= sd_threshold
                        and z_now < sd_threshold
                        and bool(out["rsi_bear_divergence"])
                    ):
                        out["vwap_reversion_signal"] = "SHORT_REVERSION"

        # Liquidity sweep/reclaim signal.
        reclaim_bars = max(1, int(cfg.liquidity_reclaim_max_bars))
        if len(df) > reclaim_bars + 5:
            base_lookback = min(int(cfg.liquidity_sweep_lookback), len(df) - reclaim_bars - 1)
            if base_lookback >= 5:
                base = df.iloc[-(base_lookback + reclaim_bars) : -reclaim_bars]
                recent = df.iloc[-reclaim_bars:]
                support = float(base["Low"].min())
                resistance = float(base["High"].max())
                out["sweep_level"] = round(support if current_close >= support else resistance, 2)

                vol_ma = volume.rolling(window=max(3, int(cfg.volume_avg_period))).mean()
                sweep_vol_threshold = float(cfg.liquidity_sweep_volume_spike_ratio)

                bull_idx = recent["Low"].idxmin()
                bear_idx = recent["High"].idxmax()

                bull_swept = float(df.loc[bull_idx, "Low"]) < support
                bear_swept = float(df.loc[bear_idx, "High"]) > resistance

                bull_vol_ratio = None
                bear_vol_ratio = None
                if bull_idx in vol_ma.index and pd.notna(vol_ma.loc[bull_idx]) and vol_ma.loc[bull_idx] > 0:
                    bull_vol_ratio = float(df.loc[bull_idx, "Volume"] / vol_ma.loc[bull_idx])
                if bear_idx in vol_ma.index and pd.notna(vol_ma.loc[bear_idx]) and vol_ma.loc[bear_idx] > 0:
                    bear_vol_ratio = float(df.loc[bear_idx, "Volume"] / vol_ma.loc[bear_idx])

                bull_reclaim = current_close > support
                bear_reclaim = current_close < resistance

                if bull_swept and bull_reclaim and (bull_vol_ratio is not None and bull_vol_ratio >= sweep_vol_threshold):
                    out["sweep_reclaim_signal"] = "BULLISH_SWEEP_RECLAIM"
                    out["sweep_level"] = round(support, 2)
                elif bear_swept and bear_reclaim and (bear_vol_ratio is not None and bear_vol_ratio >= sweep_vol_threshold):
                    out["sweep_reclaim_signal"] = "BEARISH_SWEEP_RECLAIM"
                    out["sweep_level"] = round(resistance, 2)

        return out

    @staticmethod
    def _detect_trend(
        price: float,
        sma_50: Optional[float],
        sma_200: Optional[float],
        adx: Optional[float],
    ) -> str:
        """Detect trend direction from SMA crossover and ADX."""
        if adx is not None and adx < 20:
            return "SIDEWAYS"

        if sma_50 is not None and sma_200 is not None:
            if sma_50 > sma_200 and price > sma_50:
                return "UPTREND"
            elif sma_50 < sma_200 and price < sma_50:
                return "DOWNTREND"
            else:
                return "SIDEWAYS"

        if sma_50 is not None:
            if price > sma_50:
                return "UPTREND"
            elif price < sma_50:
                return "DOWNTREND"

        return "SIDEWAYS"

    @staticmethod
    def _calc_atr(df: pd.DataFrame, period: int) -> Optional[float]:
        """Calculate Average True Range."""
        if len(df) < period + 1:
            return None
        high = df["High"]
        low = df["Low"]
        close = df["Close"]
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = true_range.rolling(window=period).mean().iloc[-1]
        return float(atr) if pd.notna(atr) else None

    @staticmethod
    def _calc_vwap(df: pd.DataFrame) -> Optional[float]:
        """Calculate VWAP for the current trading session."""
        if df.empty or "Volume" not in df.columns:
            return None
        try:
            if hasattr(df.index, 'date'):
                last_date = df.index[-1]
                if hasattr(last_date, 'date'):
                    today_mask = df.index.date == last_date.date()
                    session_df = df[today_mask]
                else:
                    session_df = df
            else:
                session_df = df

            if session_df.empty or session_df["Volume"].sum() == 0:
                return None

            typical_price = (session_df["High"] + session_df["Low"] + session_df["Close"]) / 3
            vwap = float((typical_price * session_df["Volume"]).sum() / session_df["Volume"].sum())
            return vwap
        except Exception:
            return None

    @staticmethod
    def _get_session_df(df: pd.DataFrame) -> Optional[pd.DataFrame]:
        """Return current session slice (same date as latest bar) when possible."""
        if df is None or df.empty:
            return None
        try:
            if hasattr(df.index, "date"):
                last = df.index[-1]
                if hasattr(last, "date"):
                    mask = df.index.date == last.date()
                    session_df = df[mask]
                    return session_df if not session_df.empty else df
            return df
        except Exception:
            return df

    @staticmethod
    def _calc_slope(series: pd.Series, period: int) -> Optional[float]:
        """Calculate the slope over the last N bars. Positive=rising, negative=falling."""
        if series is None or len(series) < period + 1:
            return None
        clean = series.dropna()
        if len(clean) < period + 1:
            return None
        current = clean.iloc[-1]
        past = clean.iloc[-period - 1]
        if pd.notna(current) and pd.notna(past):
            return float(current - past)
        return None

    @staticmethod
    def _detect_fvg(
        df: pd.DataFrame,
        lookback: int = 30,
        near_pct: float = 0.5,
    ) -> Dict:
        """Detect Fair Value Gaps — 3-bar price imbalances (institutional order flow footprints).

        A bullish FVG forms when bar[i].low > bar[i-2].high:
            the zone (high[i-2], low[i]) was never traded — pure buying pressure.
        A bearish FVG forms when bar[i].high < bar[i-2].low:
            the zone (high[i], low[i-2]) was never traded — pure selling pressure.

        Returns the most recent *unfilled* (active) FVG in each direction and whether
        current price is inside or approaching (within near_pct%) the zone.
        """
        out: Dict[str, object] = {
            "fvg_bullish_low": None,
            "fvg_bullish_high": None,
            "fvg_bearish_low": None,
            "fvg_bearish_high": None,
            "fvg_signal": "NONE",
        }
        if df is None or len(df) < 3:
            return out

        current_price = float(df["Close"].iloc[-1])
        bars = df.tail(min(lookback, len(df)))
        highs = bars["High"].values.astype(float)
        lows = bars["Low"].values.astype(float)
        n = len(bars)

        # Walk forward and keep the *most recent* gap in each direction.
        bullish_zone = None  # (zone_low, zone_high)
        bearish_zone = None

        for i in range(2, n):
            high_2ago = highs[i - 2]
            low_2ago = lows[i - 2]
            low_now = lows[i]
            high_now = highs[i]

            # Bullish FVG: current bar's low is above the high of the bar two candles ago.
            if low_now > high_2ago:
                zone = (high_2ago, low_now)
                # Active = price has not re-entered the zone from below (gap unfilled).
                if current_price >= zone[0]:
                    bullish_zone = zone  # keep updating so we have the most recent

            # Bearish FVG: current bar's high is below the low of the bar two candles ago.
            if high_now < low_2ago:
                zone = (high_now, low_2ago)
                # Active = price has not re-entered the zone from above (gap unfilled).
                if current_price <= zone[1]:
                    bearish_zone = zone

        if bullish_zone:
            out["fvg_bullish_low"] = round(bullish_zone[0], 4)
            out["fvg_bullish_high"] = round(bullish_zone[1], 4)
        if bearish_zone:
            out["fvg_bearish_low"] = round(bearish_zone[0], 4)
            out["fvg_bearish_high"] = round(bearish_zone[1], 4)

        # Determine price relationship to nearest active FVG.
        near_thresh = near_pct / 100.0

        in_bullish = (
            bullish_zone is not None
            and bullish_zone[0] <= current_price <= bullish_zone[1]
        )
        in_bearish = (
            bearish_zone is not None
            and bearish_zone[0] <= current_price <= bearish_zone[1]
        )
        near_bullish = (
            not in_bullish
            and bullish_zone is not None
            and bullish_zone[0] > 0
            and (current_price - bullish_zone[0]) / bullish_zone[0] <= near_thresh
            and current_price > bullish_zone[0]
        )
        near_bearish = (
            not in_bearish
            and bearish_zone is not None
            and bearish_zone[1] > 0
            and (bearish_zone[1] - current_price) / bearish_zone[1] <= near_thresh
            and current_price < bearish_zone[1]
        )

        if in_bullish:
            out["fvg_signal"] = "IN_BULLISH_FVG"
        elif in_bearish:
            out["fvg_signal"] = "IN_BEARISH_FVG"
        elif near_bullish:
            out["fvg_signal"] = "NEAR_BULLISH_FVG"
        elif near_bearish:
            out["fvg_signal"] = "NEAR_BEARISH_FVG"

        return out

    @staticmethod
    def _get_prior_day_levels(df: pd.DataFrame) -> tuple:
        """Extract prior trading day's high, low, close."""
        try:
            if hasattr(df.index, 'date') and hasattr(df.index[0], 'date'):
                dates = df.index.date
                unique_dates = sorted(set(dates))
                if len(unique_dates) < 2:
                    return None, None, None
                prev_date = unique_dates[-2]
                prev_mask = dates == prev_date
                prev_df = df[prev_mask]
                if prev_df.empty:
                    return None, None, None
                return (
                    round(float(prev_df["High"].max()), 2),
                    round(float(prev_df["Low"].min()), 2),
                    round(float(prev_df["Close"].iloc[-1]), 2),
                )
            else:
                if len(df) < 2:
                    return None, None, None
                prev = df.iloc[-2]
                return (
                    round(float(prev["High"]), 2),
                    round(float(prev["Low"]), 2),
                    round(float(prev["Close"]), 2),
                )
        except Exception:
            return None, None, None
