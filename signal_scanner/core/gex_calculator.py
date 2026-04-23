"""Gamma Exposure (GEX) calculation engine.

Computes per-strike GEX, zero gamma level, and gamma walls
from options chain data.
"""

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from signal_scanner.config import GEXConfig
from signal_scanner.core.ibkr_connector import DataConnector


@dataclass
class GEXResult:
    """Container for GEX analysis results."""

    zero_gamma_level: Optional[float] = None
    gamma_wall_up: Optional[float] = None
    gamma_wall_down: Optional[float] = None
    gex_status: str = "UNKNOWN"  # ABOVE_ZERO_GAMMA, BELOW_ZERO_GAMMA, UNKNOWN
    net_gex_by_strike: Optional[pd.DataFrame] = field(default=None, repr=False)


class GEXCalculator:
    """Calculates Gamma Exposure metrics for a symbol.

    GEX per strike = Gamma * Open_Interest * Strike^2 * 100
    - Call GEX is positive (dealers long gamma from selling calls)
    - Put GEX is negative (dealers short gamma from selling puts)
    """

    def __init__(
        self,
        connector: DataConnector,
        config: Optional[GEXConfig] = None,
    ) -> None:
        self._connector = connector
        self._config = config or GEXConfig()

    def calculate_gex(self, symbol: str, current_price: float = 0.0) -> GEXResult:
        """Calculate GEX metrics for a symbol.

        Args:
            symbol: Ticker symbol.
            current_price: Spot price (0 = fetch from connector).

        Returns:
            GEXResult with zero gamma level, gamma walls, and status.
        """
        try:
            calls_df, puts_df, spot = self._connector.get_option_chain(
                symbol, self._config
            )

            if calls_df is None or puts_df is None or spot is None:
                logger.debug(f"No option chain data for {symbol}")
                return GEXResult()

            price = current_price if current_price > 0 else spot

            # Calculate per-strike GEX
            calls_gex = self._compute_strike_gex(calls_df, is_call=True)
            puts_gex = self._compute_strike_gex(puts_df, is_call=False)

            # Merge into net GEX by strike
            net_gex = self._merge_gex(calls_gex, puts_gex)

            if net_gex.empty:
                return GEXResult()

            # Find zero gamma level
            zero_gamma = self._find_zero_gamma(net_gex)

            # Find gamma walls
            wall_up, wall_down = self._find_gamma_walls(net_gex, price)

            # Determine GEX status
            if zero_gamma is not None:
                status = "ABOVE_ZERO_GAMMA" if price > zero_gamma else "BELOW_ZERO_GAMMA"
            else:
                status = "UNKNOWN"

            return GEXResult(
                zero_gamma_level=round(zero_gamma, 2) if zero_gamma else None,
                gamma_wall_up=round(wall_up, 2) if wall_up else None,
                gamma_wall_down=round(wall_down, 2) if wall_down else None,
                gex_status=status,
                net_gex_by_strike=net_gex,
            )

        except Exception as e:
            logger.error(f"GEX calculation failed for {symbol}: {e}")
            return GEXResult()

    @staticmethod
    def _compute_strike_gex(df: pd.DataFrame, is_call: bool) -> pd.DataFrame:
        """Compute GEX = Gamma * OI * Strike^2 * 100 per strike."""
        if df.empty or "gamma" not in df.columns or "openInterest" not in df.columns:
            return pd.DataFrame(columns=["strike", "gex"])

        result = df[["strike"]].copy()
        sign = 1.0 if is_call else -1.0
        result["gex"] = (
            sign
            * df["gamma"].fillna(0)
            * df["openInterest"].fillna(0)
            * df["strike"] ** 2
            * 100
        )
        return result[["strike", "gex"]]

    @staticmethod
    def _merge_gex(calls_gex: pd.DataFrame, puts_gex: pd.DataFrame) -> pd.DataFrame:
        """Merge call and put GEX into net GEX by strike."""
        if calls_gex.empty and puts_gex.empty:
            return pd.DataFrame(columns=["strike", "call_gex", "put_gex", "net_gex"])

        merged = pd.merge(
            calls_gex.rename(columns={"gex": "call_gex"}),
            puts_gex.rename(columns={"gex": "put_gex"}),
            on="strike",
            how="outer",
        ).fillna(0)

        merged["net_gex"] = merged["call_gex"] + merged["put_gex"]
        return merged.sort_values("strike").reset_index(drop=True)

    @staticmethod
    def _find_zero_gamma(net_gex: pd.DataFrame) -> Optional[float]:
        """Find the strike where net GEX crosses zero using linear interpolation."""
        if len(net_gex) < 2:
            return None

        values = net_gex["net_gex"].values
        strikes = net_gex["strike"].values
        signs = np.sign(values)

        # Find sign changes
        sign_changes = np.where(np.diff(signs) != 0)[0]
        if len(sign_changes) == 0:
            return None

        # Use the first zero crossing
        idx = sign_changes[0]
        s1, s2 = strikes[idx], strikes[idx + 1]
        g1, g2 = values[idx], values[idx + 1]

        # Linear interpolation
        denom = g2 - g1
        if abs(denom) < 1e-10:
            return float((s1 + s2) / 2)
        zero_gamma = s1 + (s2 - s1) * (-g1 / denom)
        return float(zero_gamma)

    @staticmethod
    def _find_gamma_walls(
        net_gex: pd.DataFrame, current_price: float
    ) -> tuple:
        """Find gamma walls above and below current price.

        Wall up: strike above price with highest positive net GEX (resistance).
        Wall down: strike below price with most negative net GEX (support).
        """
        above = net_gex[net_gex["strike"] > current_price]
        below = net_gex[net_gex["strike"] < current_price]

        wall_up = None
        wall_down = None

        if not above.empty:
            # Prefer positive net GEX above price for resistance.
            above_pos = above[above["net_gex"] > 0]
            if not above_pos.empty:
                max_idx = above_pos["net_gex"].idxmax()
                wall_up = float(above_pos.loc[max_idx, "strike"])
            else:
                max_idx = above["net_gex"].abs().idxmax()
                wall_up = float(above.loc[max_idx, "strike"])

        if not below.empty:
            # Prefer negative net GEX below price for support.
            below_neg = below[below["net_gex"] < 0]
            if not below_neg.empty:
                min_idx = below_neg["net_gex"].idxmin()
                wall_down = float(below_neg.loc[min_idx, "strike"])
            else:
                max_idx = below["net_gex"].abs().idxmax()
                wall_down = float(below.loc[max_idx, "strike"])

        return wall_up, wall_down
