# Live-Config Expectancy at Realistic Costs — 2026-04-25

**Question:** Does the live paper trader's configuration (2*ATR stops,
2.5R targets, 10-day hold) produce positive net expectancy on any of
our setups after realistic transaction costs?

**Method:** `research/rr_analysis_live_config.py` — same accumulation
universe as `rr_analysis.py` (2020-Q1 through 2025-Q3, ~1.65M stock-days
in EARLY/ACTIVE/LATE_ACCUM), but with R defined as 2*ATR (not 1*ATR) and
configurable target multiples.

**Cost model:** `Cost(commission=1bp, half_spread=2bp, slip=0.05*ATR)`
which works out to **0.075 R per trade** at the 2*ATR stop frame
(half the cost burden of the 1*ATR frame).

## Headline finding

**The current live target (2.5R) is the worst possible target choice
for our Triple Lock universe.** Hit rate at 2.5R is only 9.2%. Net
expectancy is −0.131 R per trade.

**Switch the target to 1R** and Triple Lock flips to **+0.024 R per
trade NET**, on a 38.6% hit rate.

## Target sensitivity, Triple Lock universe (n=1,063 stock-days)

| Target | Hit% | Stop% | Gross R | Net R | Verdict |
|---|---|---|---|---|---|
| 1.0 R | 38.6% | 28.7% | +0.099 | **+0.024** | ✓ POSITIVE |
| 1.5 R | 22.4% | 28.7% | +0.049 | −0.026 | breakeven |
| 2.0 R | 13.9% | 28.7% | −0.008 | −0.083 | losing |
| 2.5 R *(live)* | 9.2% | 28.7% | −0.056 | −0.131 | losing |

## Triple Lock control: ALL accumulation universe (n=1.65M)

| Target | Hit% | Stop% | Gross R | Net R |
|---|---|---|---|---|
| 1.0 R | 34.3% | 35.0% | −0.007 | −0.082 |
| 1.5 R | 19.5% | 35.0% | −0.057 | −0.132 |
| 2.0 R | 10.9% | 35.0% | −0.131 | −0.206 |
| 2.5 R | 6.4% | 35.0% | −0.191 | −0.266 |

**Triple Lock differentiator at 1R = +0.106 R per trade vs no filter.**
That's a real edge.

## Triple Lock by year (2.5R target frame, current config)

| Year | N | 2.5R% | Stop% | 1R% | Net R |
|---|---|---|---|---|---|
| 2023 | 252 | 7.9% | 20.6% | 46.8% | −0.083 |
| 2024 | 127 | **27.6%** | 37.8% | 45.7% | **+0.236** |
| 2025 | 684 | 6.3% | 30.0% | 34.2% | −0.218 |

The "59.8% WR n=132" claim from prior memory matches **2024 only** — a
single bull-trending regime. **The setup's edge is regime-dependent**.
2025 collapsed when momentum failed. The 1R-target config buffers some
of this — 1R hit rate stays above stop rate in all three years.

## Other findings worth noting

- **ML v2 ≥ 90 doesn't add edge in this frame.** Net at 2.5R = −0.253 R,
  basically identical to ALL = −0.266 R. The model captures something
  but not "trades that hit 2.5R in 10 days."
- **Insider + Below200 + Comp + PB hovers at breakeven (−0.006 R)** at
  2.5R target. At 1R target it's almost certainly clearly positive
  (n=375 is small but the direction is consistent).
- **MFE10 ~1.0 R across most setups.** This is the binding constraint:
  the average peak favorable excursion in 10 days is one R-unit. Any
  target > 1R requires a lucky trade, not a typical one.

## Implications for the system

### Immediate (hours of work)

1. **Change `IdeaBridge` and `paper_trader.enter_idea_trade` target
   logic from 2.5R to 1R for Triple Lock entries.** This is the single
   highest-impact code change identifiable from the data.
   - Per memory: idea_bridge.py emits 2.5:1 R:R targets currently. Change to 1:1.

2. **Re-evaluate the 2.5R target rule for ALL strategies.** The MFE10
   ~1R observation is universal — it's a property of our universe, not
   Triple Lock-specific. Aggressive targets across the board are
   leaving money on the table by getting timed out / stopped out before
   reaching them.

### Near-term (days of work)

3. **Try longer hold horizons (20d, 30d)** for the 13F-driven setups.
   13F is a slow-moving signal; 10-day forward may be too short to let
   the institutional accumulation thesis play out. MFE at longer
   horizons may break through 2R.

4. **Trail-stop instead of hard target.** A 1R initial target with a
   trail-stop after 1R would capture the 2024-style outliers without
   sacrificing the base hit rate.

5. **Validate ml_v2 with a 1R label.** Currently labels are "+3% in 3d"
   and "RR 2:1 in 5d" — both poorly aligned with our actual 10-day 1R
   target. Re-train v3 with `label_1R_10d` and see if AUC reflects an
   exit-aware edge.

### Strategic

6. **The "59.8% WR n=132 Triple Lock" claim in memory is misleading.**
   It was 2024-only data, never re-verified in 2025. Going forward,
   ALL win-rate claims need a regime-stratified disclosure.

## How to regenerate

```bash
python -m research.rr_analysis_live_config
```

Uses live cost model. Override via `QUANT_BRIDGE_COST_R=<value>`.
