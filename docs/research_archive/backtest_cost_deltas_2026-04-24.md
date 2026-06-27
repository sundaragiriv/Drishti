# Backtest Cost Delta Report — 2026-04-24

Applying realistic transaction cost model to existing R-based backtests
(review item #3). Cost model:

```
Cost(commission_bps=1.0, half_spread_bps=2.0, slip_atr_frac=0.05)
→ per-trade roundtrip ≈ 0.15 R at 1R-ATR stop frame
```

## Headline finding

**Every previously-validated swing setup has negative net expectancy after
realistic costs at the 1R-ATR stop frame used in `rr_analysis.py`.**

The only setup with *gross* positive expectancy (Insider + Below200 + Pullback,
n=1,058, +0.09 R) flips to **−0.06 R net** — i.e. losing money.

## Full table from rr_analysis.py (2020-2025 accumulation universe)

| Setup | N | 2R Hit | Stopped | Gross | Net |
|---|---|---|---|---|---|
| ALL accumulation | 1,654,703 | 25.9% | 62.2% | −0.104R | **−0.254R** |
| Below 200 + Comp + PB | 67,077 | 25.4% | 63.1% | −0.124R | **−0.274R** |
| Conv≥65 + Below200 + Comp + PB | 20,195 | 28.2% | 60.8% | −0.045R | **−0.195R** |
| **Insider + Below200 + PB** | **1,058** | **31.4%** | **53.4%** | **+0.094R** | **−0.056R** |
| EARLY + Below200 + PB | 185,096 | 26.5% | 58.2% | −0.051R | **−0.201R** |
| Above200 + Aligned + PB | 153,220 | 26.0% | 62.1% | −0.100R | **−0.250R** |
| Conv≥65 + Above200 + Aligned + PB | 68,599 | 26.4% | 61.4% | −0.087R | **−0.237R** |

## Caveats

1. **1R-ATR stops** in this analysis are tight. The live paper trader uses 2*ATR
   stops, which roughly halves the per-trade R-cost (~0.075 R instead of 0.15 R).
   At that frame, the Insider setup's net would be positive but marginal.

2. **No size-weighting / regime filter / volume filter applied.** The 1.65M
   stock-day universe includes thinly-traded names where the slippage assumption
   is optimistic.

3. **No purged CV.** Forward-return windows of 5-10 days overlap on consecutive
   trade dates → expectancy stats are correlated, not iid. Confidence intervals
   are wider than they look.

## Implication for system

- Stop-multiple research is mandatory before any production claim. Switching
  from 1*ATR to 2*ATR roughly halves cost burden but also halves win rate.
  The optimal stop multiple is an empirical question that hasn't been answered
  in our data.
- Conviction filtering helps weakly (Conv≥65 setups have slightly less-bad nets).
  Insider setups help meaningfully but are sample-thin (n=1,058).
- The review was correct: building hand-tuned weights without cost-aware
  validation was building on noise.

## How to regenerate

```bash
# Default cost (recommended)
python -m research.rr_analysis

# Gross-only (legacy comparison)
QUANT_BRIDGE_COST_R=0.0 python -m research.rr_analysis

# Custom cost level
QUANT_BRIDGE_COST_R=0.10 python -m research.rr_analysis
```
