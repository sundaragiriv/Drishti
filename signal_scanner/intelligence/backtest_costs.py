"""Transaction cost model for backtesters.

Default knobs are conservative for retail IBKR:
- commission: 1 bp roundtrip (≈$0.50 per $10K trade — IBKR Pro tier)
- half_spread: 2 bps per side, 4 bps roundtrip (typical for liquid US equities;
  worse for small caps, hence "conservative")
- slip_atr_frac: 0.05 per side, 0.10 ATR roundtrip (assumes you eat 10% of an
  ATR moving against you on entry+exit)

Methodology: the cost is applied as an R-multiple deduction on every trade's
exit_r, regardless of outcome. Stops become slightly worse losses, targets
become slightly less profitable wins. Roundtrip cost is independent of
direction (long or short) and applies once per closed trade.

Usage:
    cost = Cost()  # defaults
    r_cost = cost.compute_r_cost(entry_price=100.0, r_unit=2.0, atr=1.5)
    net_exit_r = exit_r - r_cost
"""

from dataclasses import dataclass


@dataclass
class Cost:
    """Per-trade cost model.

    All fields are in basis points except slip_atr_frac (fraction of ATR).
    """
    commission_bps: float = 1.0      # roundtrip, total, fraction of notional
    half_spread_bps: float = 2.0     # per side; roundtrip = 2x
    slip_atr_frac: float = 0.05      # per side; roundtrip = 2x

    def compute_r_cost(self, entry_price: float, r_unit: float, atr: float | None = None) -> float:
        """Return the R-multiple deduction for one roundtrip.

        Args:
            entry_price: trade entry price (positive)
            r_unit: |entry - stop| in price terms (positive)
            atr: average true range; falls back to r_unit if None
        """
        if entry_price <= 0 or r_unit <= 0:
            return 0.0

        atr_used = float(atr) if (atr is not None and atr > 0) else r_unit

        # bps cost (commission + 2*half_spread) as fraction of notional
        bps_total = self.commission_bps + 2.0 * self.half_spread_bps
        bps_price = bps_total / 10_000.0 * entry_price  # cost in price units

        # slippage as 2-side fraction of ATR
        slip_price = 2.0 * self.slip_atr_frac * atr_used  # cost in price units

        total_price_cost = bps_price + slip_price
        return total_price_cost / r_unit  # convert to R-multiples

    def __str__(self) -> str:
        return (f"Cost(comm={self.commission_bps}bp, "
                f"hspread={self.half_spread_bps}bp, "
                f"slip={self.slip_atr_frac}*ATR)")


# Sentinel — the "no costs" cost. Useful for legacy backtests / sanity checks.
ZERO_COST = Cost(commission_bps=0.0, half_spread_bps=0.0, slip_atr_frac=0.0)


def apply_cost_to_result(result: dict, entry_price: float, r_unit: float,
                         cost: Cost, atr: float | None = None) -> None:
    """Mutate a `_track_r_targets`-style result dict, deducting costs from exit_r.

    Adds two new fields:
      - cost_r: cost deducted (always ≥ 0)
      - exit_r_gross: original exit_r before costs (preserves audit trail)
    """
    r_cost = cost.compute_r_cost(entry_price, r_unit, atr)
    result["cost_r"] = round(r_cost, 4)
    result["exit_r_gross"] = result.get("exit_r", 0.0)
    result["exit_r"] = round(result["exit_r_gross"] - r_cost, 4)
