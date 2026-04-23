"""Market regime detection — SPY trend + VIX level via IBKR.

Determines overall market risk environment:
    RISK_ON:  SPY in uptrend and VIX < 18 (low fear, bullish)
    RISK_OFF: SPY in downtrend or VIX > 25 (high fear, defensive)
    NEUTRAL:  Mixed signals or transitional
"""

from dataclasses import dataclass
from typing import Optional

from loguru import logger

from signal_scanner.config import ScannerConfig


@dataclass
class MarketRegime:
    """Container for market regime state."""

    regime: str = "NEUTRAL"           # RISK_ON, RISK_OFF, NEUTRAL
    spy_trend: str = "SIDEWAYS"       # UPTREND, DOWNTREND, SIDEWAYS
    spy_price: Optional[float] = None
    spy_sma50: Optional[float] = None
    spy_rsi: Optional[float] = None
    vix_level: Optional[float] = None
    vix_status: str = "NORMAL"        # LOW, NORMAL, HIGH, EXTREME
    description: str = ""


def get_market_regime(
    config: Optional[ScannerConfig] = None,
    connector=None,
) -> MarketRegime:
    """Fetch SPY and VIX data via IBKR to determine current market regime.

    Called once per scan cycle (not per symbol).

    Args:
        config: Scanner configuration.
        connector: DataConnector instance for IBKR data. If None or not
            connected, returns a NEUTRAL regime with a warning.
    """
    cfg = config or ScannerConfig()

    spy_trend = "SIDEWAYS"
    spy_price = None
    spy_sma50 = None
    spy_rsi = None
    vix_level = None
    vix_status = "NORMAL"

    if connector is None or not connector.is_connected():
        logger.warning("IBKR not connected — cannot determine market regime")
        return MarketRegime(
            description="IBKR not connected — regime unavailable",
        )

    # Fetch SPY data via IBKR
    try:
        spy_df = connector.get_price_data(cfg.regime_benchmark, "1h")

        if spy_df is not None and not spy_df.empty:
            spy_price = round(float(spy_df["Close"].iloc[-1]), 2)

            # SMA 50
            if len(spy_df) >= 50:
                spy_sma50 = round(float(spy_df["Close"].rolling(50).mean().iloc[-1]), 2)

            # RSI 14
            if len(spy_df) >= 15:
                import pandas_ta as ta
                rsi_series = ta.rsi(spy_df["Close"], length=14)
                if rsi_series is not None and not rsi_series.empty:
                    spy_rsi = round(float(rsi_series.iloc[-1]), 1)

            # SPY trend
            if spy_sma50 is not None and spy_price is not None:
                if spy_price > spy_sma50:
                    spy_trend = "UPTREND"
                elif spy_price < spy_sma50:
                    spy_trend = "DOWNTREND"
    except Exception as e:
        logger.warning(f"Failed to fetch SPY data for regime: {e}")

    # Fetch VIX via IBKR
    try:
        vix_df = connector.get_price_data(cfg.regime_vix_symbol, "1h")
        if vix_df is not None and not vix_df.empty:
            vix_level = round(float(vix_df["Close"].iloc[-1]), 1)

            if vix_level > 35:
                vix_status = "EXTREME"
            elif vix_level > cfg.regime_vix_high:
                vix_status = "HIGH"
            elif vix_level < cfg.regime_vix_low:
                vix_status = "LOW"
            else:
                vix_status = "NORMAL"
    except Exception as e:
        logger.warning(f"Failed to fetch VIX data for regime: {e}")

    # Determine regime
    regime = _classify_regime(spy_trend, vix_status, vix_level, spy_rsi)
    description = _build_description(regime, spy_trend, spy_price, spy_sma50, vix_level, vix_status)

    result = MarketRegime(
        regime=regime,
        spy_trend=spy_trend,
        spy_price=spy_price,
        spy_sma50=spy_sma50,
        spy_rsi=spy_rsi,
        vix_level=vix_level,
        vix_status=vix_status,
        description=description,
    )

    logger.info(f"Market regime: {regime} | SPY {spy_trend} @ ${spy_price} | VIX {vix_level} ({vix_status})")
    return result


def _classify_regime(
    spy_trend: str,
    vix_status: str,
    vix_level: Optional[float],
    spy_rsi: Optional[float],
) -> str:
    """Classify market regime from SPY trend and VIX."""
    # Strong risk-off: VIX extreme or SPY downtrend + elevated VIX
    if vix_status == "EXTREME":
        return "RISK_OFF"
    if spy_trend == "DOWNTREND" and vix_status in ("HIGH", "EXTREME"):
        return "RISK_OFF"
    if spy_trend == "DOWNTREND" and spy_rsi is not None and spy_rsi < 40:
        return "RISK_OFF"

    # Strong risk-on: SPY uptrend + low VIX
    if spy_trend == "UPTREND" and vix_status == "LOW":
        return "RISK_ON"
    if spy_trend == "UPTREND" and vix_status == "NORMAL":
        return "RISK_ON"

    # Everything else is neutral/mixed
    return "NEUTRAL"


def _build_description(
    regime: str,
    spy_trend: str,
    spy_price: Optional[float],
    spy_sma50: Optional[float],
    vix_level: Optional[float],
    vix_status: str,
) -> str:
    """Build human-readable regime description."""
    parts = []

    if spy_price and spy_sma50:
        pct = round(((spy_price - spy_sma50) / spy_sma50) * 100, 1)
        parts.append(f"SPY ${spy_price} ({pct:+.1f}% vs SMA50)")

    if vix_level:
        parts.append(f"VIX {vix_level}")

    if regime == "RISK_ON":
        parts.append("Favor long positions")
    elif regime == "RISK_OFF":
        parts.append("Defensive — reduce size or favor shorts")
    else:
        parts.append("Mixed signals — be selective")

    return " | ".join(parts)
