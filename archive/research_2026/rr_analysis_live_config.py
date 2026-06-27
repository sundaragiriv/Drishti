"""RR analysis at the LIVE paper-trader config.

The original `rr_analysis.py` used a 1*ATR stop / 2R target frame which
overstates cost burden vs how we actually trade. The live paper trader
(per memory + paper_trader.py) uses:

  - Stop:   2 * ATR      (entry - 2*atr for LONG, entry + 2*atr for SHORT)
  - Target: 2.5 R         (= 2.5 * stop_distance = 5 * ATR)
  - 1 R     = stop distance = 2 * ATR

At this frame, per-trade cost (commission + half-spread + slippage) drops
roughly in half because r_unit doubles. This script answers:

  "Do any of our setups produce positive net expectancy at the config we
   actually trade with?"

Includes a TRIPLE LOCK section since that's the live system's strongest
historical filter (memory says 59.8% WR n=132 — we revalidate here with
costs).
"""
import duckdb
import os
import time

from signal_scanner.intelligence.backtest_costs import Cost

t0 = time.time()
conn = duckdb.connect("data/warehouse/sec_intel.duckdb", read_only=True)

# Cost per trade as R-multiple at 2*ATR stops (r_unit = 2*ATR, atr_for_cost = ATR).
# Approximation on representative entry=$100, atr=1, r_unit=2.
_default_cost = Cost()
COST_R = float(os.environ.get(
    "QUANT_BRIDGE_COST_R",
    _default_cost.compute_r_cost(entry_price=100.0, r_unit=2.0, atr=1.0),
))

print("=" * 75)
print("RR ANALYSIS @ LIVE PAPER CONFIG: 2*ATR stops, 2.5R targets")
print("=" * 75)
print(f"[Cost model] {_default_cost} -> per-trade cost ~{COST_R:.4f} R")

# ----------------------------------------------------------------------
# Build the universe + R-targets at 2*ATR stop frame
# ----------------------------------------------------------------------
conn.execute("""
    CREATE TEMP TABLE rr_live AS
    WITH universe AS (
        SELECT ticker, report_quarter, conviction_score, accum_phase,
               accum_phase_quarters,
               COALESCE(triple_lock, FALSE) AS triple_lock,
               COALESCE(ml_score_v2, 0) AS ml_v2,
               COALESCE(inst_f4_distinct_60d, 0) AS f4_count,
               COALESCE(squeeze_score, 0) AS squeeze_score,
               COALESCE(insider_effect_score, 0) AS insider_effect,
               COALESCE(institutional_pressure, 0) AS inst_pressure,
               CASE
                   WHEN report_quarter LIKE '%-Q1' THEN CAST(LEFT(report_quarter,4)||'-05-15' AS DATE)
                   WHEN report_quarter LIKE '%-Q2' THEN CAST(LEFT(report_quarter,4)||'-08-15' AS DATE)
                   WHEN report_quarter LIKE '%-Q3' THEN CAST(LEFT(report_quarter,4)||'-11-15' AS DATE)
                   WHEN report_quarter LIKE '%-Q4' THEN CAST(CAST(CAST(LEFT(report_quarter,4) AS INT)+1 AS VARCHAR)||'-02-15' AS DATE)
               END AS avail_date,
               CASE
                   WHEN report_quarter LIKE '%-Q1' THEN CAST(LEFT(report_quarter,4)||'-08-14' AS DATE)
                   WHEN report_quarter LIKE '%-Q2' THEN CAST(LEFT(report_quarter,4)||'-11-14' AS DATE)
                   WHEN report_quarter LIKE '%-Q3' THEN CAST(CAST(CAST(LEFT(report_quarter,4) AS INT)+1 AS VARCHAR)||'-02-14' AS DATE)
                   WHEN report_quarter LIKE '%-Q4' THEN CAST(CAST(CAST(LEFT(report_quarter,4) AS INT)+1 AS VARCHAR)||'-05-14' AS DATE)
               END AS expire_date
        FROM intelligence_scores
        WHERE accum_phase IN ('EARLY_ACCUM','ACTIVE_ACCUM','LATE_ACCUM')
          AND report_quarter >= '2020-Q1' AND report_quarter <= '2025-Q3'
    ),
    priced AS (
        SELECT p.ticker, p.trade_date, p.close, p.high, p.low, p.open, p.volume,
               u.conviction_score, u.accum_phase, u.accum_phase_quarters,
               u.triple_lock, u.ml_v2, u.f4_count, u.squeeze_score,
               u.insider_effect, u.inst_pressure,
               AVG(p.high-p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS atr20,
               AVG(p.high-p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS atr60,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS sma200,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) AS sma50,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS sma20,
               LAG(p.close,5) OVER w AS pc5,
               LEAD(p.high,1)  OVER w AS h1,  LEAD(p.high,2)  OVER w AS h2,
               LEAD(p.high,3)  OVER w AS h3,  LEAD(p.high,4)  OVER w AS h4,
               LEAD(p.high,5)  OVER w AS h5,  LEAD(p.high,6)  OVER w AS h6,
               LEAD(p.high,7)  OVER w AS h7,  LEAD(p.high,8)  OVER w AS h8,
               LEAD(p.high,9)  OVER w AS h9,  LEAD(p.high,10) OVER w AS h10,
               LEAD(p.low,1)   OVER w AS l1,  LEAD(p.low,2)   OVER w AS l2,
               LEAD(p.low,3)   OVER w AS l3,  LEAD(p.low,4)   OVER w AS l4,
               LEAD(p.low,5)   OVER w AS l5,  LEAD(p.low,6)   OVER w AS l6,
               LEAD(p.low,7)   OVER w AS l7,  LEAD(p.low,8)   OVER w AS l8,
               LEAD(p.low,9)   OVER w AS l9,  LEAD(p.low,10)  OVER w AS l10,
               LEAD(p.close,5) OVER w AS ct5, LEAD(p.close,10) OVER w AS ct10
        FROM fact_daily_prices p
        INNER JOIN universe u ON p.ticker = u.ticker
            AND p.trade_date >= u.avail_date AND p.trade_date <= u.expire_date
        WHERE p.close > 5 AND p.volume > 0
        WINDOW w AS (PARTITION BY p.ticker ORDER BY p.trade_date)
    )
    SELECT *,
        CASE WHEN close < sma200 THEN 1 ELSE 0 END AS below_200,
        CASE WHEN close > sma200 THEN 1 ELSE 0 END AS above_200,
        CASE WHEN atr20/NULLIF(atr60,0) < 0.85 THEN 1 ELSE 0 END AS compressed,
        (close - pc5)/NULLIF(pc5,0) AS ret_5d,
        CASE WHEN sma20 > sma50 THEN 1 ELSE 0 END AS sma20_gt_50,

        -- LIVE FRAME: 2*ATR stops, 2.5R targets (= 5*ATR move)
        -- For LONG entry at close, 1R = 2*atr20; targets long-side only.
        CASE WHEN GREATEST(h1,h2,h3,h4,h5) >= close + 5*atr20
              AND LEAST(l1,l2,l3,l4,l5) > close - 2*atr20
            THEN 1 ELSE 0 END AS hit_2_5R_5d,
        CASE WHEN GREATEST(h1,h2,h3,h4,h5,h6,h7,h8,h9,h10) >= close + 5*atr20
              AND LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10) > close - 2*atr20
            THEN 1 ELSE 0 END AS hit_2_5R_10d,
        CASE WHEN LEAST(l1,l2,l3,l4,l5) <= close - 2*atr20 THEN 1 ELSE 0 END AS stopped_5d,
        CASE WHEN LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10) <= close - 2*atr20 THEN 1 ELSE 0 END AS stopped_10d,

        -- Same with 1R hit (intermediate take-partial level)
        CASE WHEN GREATEST(h1,h2,h3,h4,h5) >= close + 2*atr20
              AND LEAST(l1,l2,l3,l4,l5) > close - 2*atr20
            THEN 1 ELSE 0 END AS hit_1R_5d_2x,
        CASE WHEN GREATEST(h1,h2,h3,h4,h5,h6,h7,h8,h9,h10) >= close + 2*atr20
              AND LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10) > close - 2*atr20
            THEN 1 ELSE 0 END AS hit_1R_10d_2x,

        (GREATEST(h1,h2,h3,h4,h5,h6,h7,h8,h9,h10) - close) / NULLIF(2*atr20,0) AS mfe_10d_R,
        (close - LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10)) / NULLIF(2*atr20,0) AS mae_10d_R
    FROM priced
    WHERE h10 IS NOT NULL AND l10 IS NOT NULL AND atr20 > 0
          AND sma200 IS NOT NULL AND atr60 IS NOT NULL AND pc5 IS NOT NULL
""")

ct = conn.execute("SELECT COUNT(*) FROM rr_live").fetchone()[0]
print(f"\nUniverse: {ct:,} stock-days in accumulation (2020 - 2025-Q3)")

# ----------------------------------------------------------------------
# Headline expectancy table at LIVE config
# ----------------------------------------------------------------------
print("\n" + "=" * 75)
print("EXPECTANCY @ LIVE CONFIG (10-day forward, 2.5R target with 1R = 2*ATR stop)")
print("=" * 75)

setups = [
    ("ALL accumulation",                   "1=1"),
    ("Below 200 + Comp + PB",              "below_200=1 AND compressed=1 AND ret_5d<-0.02"),
    ("Conv>=65 + Below200 + Comp + PB",    "conviction_score>=65 AND below_200=1 AND compressed=1 AND ret_5d<-0.02"),
    ("Insider + Below200 + PB",            "f4_count>=1 AND below_200=1 AND ret_5d<-0.02"),
    ("Insider + Below200 + Comp + PB",     "f4_count>=1 AND below_200=1 AND compressed=1 AND ret_5d<-0.02"),
    ("EARLY + Below200 + PB",              "accum_phase='EARLY_ACCUM' AND below_200=1 AND ret_5d<-0.02"),
    ("Above200 + Aligned + PB",            "above_200=1 AND sma20_gt_50=1 AND ret_5d<-0.02"),
    ("Conv>=65 + Above200 + Aligned + PB", "conviction_score>=65 AND above_200=1 AND sma20_gt_50=1 AND ret_5d<-0.02"),
    ("--- TRIPLE LOCK ---",                None),
    ("Triple Lock (any)",                  "triple_lock=TRUE"),
    ("Triple Lock + Below200 + PB",        "triple_lock=TRUE AND below_200=1 AND ret_5d<-0.02"),
    ("Triple Lock + Above200 + Aligned",   "triple_lock=TRUE AND above_200=1 AND sma20_gt_50=1"),
    ("--- ML V2 TOP DECILE ---",           None),
    ("ML v2 >= 80",                        "ml_v2>=80"),
    ("ML v2 >= 90",                        "ml_v2>=90"),
    ("ML v2 >= 90 + Conv>=65",             "ml_v2>=90 AND conviction_score>=65"),
]

print(f"\n{'Setup':<42} {'N':>9} {'2.5R%':>7} {'Stop%':>7} {'Gross R':>9} {'Net R':>9} {'MFE10':>6} {'MAE10':>6}")
print("-" * 105)

for label, where in setups:
    if where is None:
        print(f"\n{label}")
        continue
    r = conn.execute(f"""
        SELECT COUNT(*),
               AVG(hit_2_5R_10d)*100,
               AVG(stopped_10d)*100,
               AVG(mfe_10d_R), AVG(mae_10d_R)
        FROM rr_live WHERE {where}
    """).fetchone()
    if r[0] < 30:
        print(f"  {label:<40} {r[0]:>9,} (n<30, skip)")
        continue
    hit = (r[1] or 0) / 100
    stop = (r[2] or 0) / 100
    # Expectancy: P(2.5R) * 2.5 - P(stop) * 1, the rest are time-stops near 0
    exp_gross = hit * 2.5 - stop * 1.0
    exp_net = exp_gross - COST_R
    mfe = r[3] or 0
    mae = r[4] or 0
    flag = " *" if exp_net > 0.05 else ("  " if exp_net >= 0 else " X")
    print(f"  {label:<40} {r[0]:>9,} {r[1]:>6.1f}% {r[2]:>6.1f}% {exp_gross:>+7.3f}R {exp_net:>+7.3f}R{flag} {mfe:>5.2f}R {mae:>5.2f}R")

# ----------------------------------------------------------------------
# Triple Lock yearly consistency
# ----------------------------------------------------------------------
print("\n" + "=" * 75)
print("TRIPLE LOCK BY YEAR (n>=20)")
print("=" * 75)

rows = conn.execute("""
    SELECT YEAR(trade_date) AS yr, COUNT(*),
           AVG(hit_2_5R_10d)*100, AVG(stopped_10d)*100,
           AVG(hit_1R_10d_2x)*100,
           AVG((ct10-close)/close)*100
    FROM rr_live WHERE triple_lock=TRUE
    GROUP BY 1 ORDER BY 1
""").fetchall()
print(f"\n{'Year':>6} {'N':>7} {'2.5R%':>7} {'Stop%':>7} {'1R%':>6} {'Net R':>8} {'Avg10d':>9}")
print("-" * 60)
for yr, n, hit, stop, hit1, ret10 in rows:
    if n < 20:
        continue
    hit_p = (hit or 0) / 100
    stop_p = (stop or 0) / 100
    exp_net = hit_p * 2.5 - stop_p * 1.0 - COST_R
    print(f"{yr:>6} {n:>7,} {hit:>6.1f}% {stop:>6.1f}% {hit1:>5.1f}% {exp_net:>+7.3f}R {ret10:>+7.3f}%")

# ----------------------------------------------------------------------
# Target sensitivity — same 2*ATR stop, vary target multiple
# ----------------------------------------------------------------------
print("\n" + "=" * 75)
print("TARGET SENSITIVITY @ 2*ATR STOPS (Triple Lock universe, 10-day forward)")
print("=" * 75)
print("Question: average MFE10 ~1R suggests 2.5R target is too ambitious.")
print("Compute hit rates + expectancy at 1R, 1.5R, 2R, 2.5R targets.\n")

# Add per-target hit columns on the fly, restricted to triple_lock universe
# (avoid 1.6M-row scan for sensitivity sweep)
conn.execute("""
    CREATE TEMP TABLE rr_tl_targets AS
    SELECT *,
        CASE WHEN GREATEST(h1,h2,h3,h4,h5,h6,h7,h8,h9,h10) >= close + 2*atr20
              AND LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10) > close - 2*atr20
            THEN 1 ELSE 0 END AS hit_1R,
        CASE WHEN GREATEST(h1,h2,h3,h4,h5,h6,h7,h8,h9,h10) >= close + 3*atr20
              AND LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10) > close - 2*atr20
            THEN 1 ELSE 0 END AS hit_1_5R,
        CASE WHEN GREATEST(h1,h2,h3,h4,h5,h6,h7,h8,h9,h10) >= close + 4*atr20
              AND LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10) > close - 2*atr20
            THEN 1 ELSE 0 END AS hit_2R
    FROM rr_live
    WHERE triple_lock=TRUE
""")

print(f"{'Target':>7} {'Hit%':>7} {'Stop%':>7} {'Gross R':>9} {'Net R':>9}")
print("-" * 50)
for r_target, hit_col in [(1.0, "hit_1R"), (1.5, "hit_1_5R"), (2.0, "hit_2R"), (2.5, "hit_2_5R_10d")]:
    r = conn.execute(f"""
        SELECT COUNT(*),
               AVG({hit_col})*100,
               AVG(stopped_10d)*100
        FROM rr_tl_targets
    """).fetchone()
    n, hit, stop = r
    if n == 0:
        continue
    hit_p = (hit or 0) / 100
    stop_p = (stop or 0) / 100
    exp_gross = hit_p * r_target - stop_p * 1.0
    exp_net = exp_gross - COST_R
    flag = " *" if exp_net > 0.05 else ("  " if exp_net >= 0 else " X")
    print(f"  {r_target:>4.1f}R {hit:>6.1f}% {stop:>6.1f}% {exp_gross:>+7.3f}R {exp_net:>+7.3f}R{flag}")

# Also for the ALL-accumulation universe (sanity sweep)
print("\nSAME SWEEP @ ALL accumulation (n=1.65M) — control:")
print(f"{'Target':>7} {'Hit%':>7} {'Stop%':>7} {'Gross R':>9} {'Net R':>9}")
print("-" * 50)
for r_target, expr in [
    (1.0, "GREATEST(h1,h2,h3,h4,h5,h6,h7,h8,h9,h10) >= close + 2*atr20 AND LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10) > close - 2*atr20"),
    (1.5, "GREATEST(h1,h2,h3,h4,h5,h6,h7,h8,h9,h10) >= close + 3*atr20 AND LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10) > close - 2*atr20"),
    (2.0, "GREATEST(h1,h2,h3,h4,h5,h6,h7,h8,h9,h10) >= close + 4*atr20 AND LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10) > close - 2*atr20"),
    (2.5, "GREATEST(h1,h2,h3,h4,h5,h6,h7,h8,h9,h10) >= close + 5*atr20 AND LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10) > close - 2*atr20"),
]:
    r = conn.execute(f"""
        SELECT COUNT(*),
               AVG(CASE WHEN {expr} THEN 1 ELSE 0 END)*100,
               AVG(stopped_10d)*100
        FROM rr_live
    """).fetchone()
    n, hit, stop = r
    hit_p = (hit or 0) / 100
    stop_p = (stop or 0) / 100
    exp_gross = hit_p * r_target - stop_p * 1.0
    exp_net = exp_gross - COST_R
    flag = " *" if exp_net > 0.05 else ("  " if exp_net >= 0 else " X")
    print(f"  {r_target:>4.1f}R {hit:>6.1f}% {stop:>6.1f}% {exp_gross:>+7.3f}R {exp_net:>+7.3f}R{flag}")

print(f"\nDone in {time.time() - t0:.1f}s")
