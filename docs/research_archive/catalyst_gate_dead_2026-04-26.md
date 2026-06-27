# Catalyst Gate — Dead on Arrival (Tier 1)

**Date:** 2026-04-26
**Verdict:** **Do not deploy as blocker.** The Tier 1 catalyst rules consistently
hurt performance on the populations where we have statistical power.
**Sample:** 3,255 accumulation stock-days, window 2026-02-26 → 2026-04-10.

## Summary table — all 4 rule variants tested

| Population | Variant | Flag-Net | Clean-Net | Δ (clean − flagged) | Verdict |
|---|---|---|---|---|---|
| ALL accum (n=3255) | any neg news | −0.237R | −0.335R | **−0.098R** | HURTS |
| ALL accum | 8-K only | −0.248R | −0.326R | −0.077R | HURTS |
| Conv≥65 (n=1092) | any neg news | −0.256R | −0.341R | −0.085R | HURTS |
| Conv≥65 | 8-K only | −0.179R | −0.341R | −0.162R | HURTS |
| ML v2 ≥90 (n=336) | any neg news | +0.096R | −0.434R | −0.530R | HURTS |
| ML v2 ≥90 | 8-K only | +0.075R | −0.407R | −0.482R | HURTS |
| Triple Lock (n=42) | 8-K only | −0.325R | −0.075R | +0.250R | "ADDS" (n too small) |

**Negative delta = clean is better than flagged = gate is hurting us.**

The pattern is consistent across all 4 variants and 3 populations with
statistical power: **flagged trades outperform clean trades.** The Triple
Lock cell shows the opposite signal but n=42 (16 flagged + 26 clean) is
not enough to be confident — 0% hit rate on 16 trades is exactly what
random sampling produces ~10% of the time at our universe's base rate.

## Why does the gate hurt?

Three plausible reasons:

1. **News and 8-Ks correlate with momentum.** Companies with active news
   flow and recent material filings are *moving* — and the 13F-driven
   accumulation thesis works best when there's also a near-term
   fundamental catalyst pushing the price.

2. **Selection effect.** Tickers with NO news in a 5-day window are the
   "boring stagnant" subset. These names are more likely to drift
   sideways into the −2*ATR stop than to break out to +1R.

3. **Asymmetric event types.** Our 8-K material flags include
   acquisitions (often +EV for targets), positive earnings beats
   (catalyze upward moves), and CEO changes (mixed). We were treating
   all of these as "block this trade" when many are reasons to ENTER.

## What this means

**Drop the blocking gate.** The data is loud: filtering trades by
backward-looking news/8-K signals removes more winners than losers.
We were about to add a feature that would make the system worse.

This is exactly the kind of finding the user's principle ("build on
proven methods, not concepts") is designed to catch — **before**
shipping live.

## Action

- Revert IdeaBridge to enter all eligible ideas regardless of catalyst
  flag (cohort B no longer blocks).
- **Keep** the catalyst metadata logging — it's still useful diagnostic
  data and we can mine it differently later (e.g., does positive news
  correlate with bigger winners?).
- **Keep** cohort tagging on paper_trades for future A/B experiments.
- Document the negative result. Mark Tier 1 catalyst gate as DEAD.

## What to try next (different premise)

The data suggests we should explore the OPPOSITE hypothesis:

> **Hypothesis:** Trades with active news/8-K signals outperform
> "boring" tickers by ~0.1R per trade. Filter the universe TO include
> active names, not exclude them.

If true, the new ranking would be:
- Triple Lock + recent 8-K + recent news = highest priority
- Triple Lock + boring ticker = lower priority

This is a Tier-2-and-beyond research question. Not for today.

## How to regenerate

```bash
python -m research.catalyst_historical_backtest
```

Reads only DuckDB. Output is the full variant × population table.
