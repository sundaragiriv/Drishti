"""Deep RR analysis: ATR-based R-multiples, hit rates, expectancy."""
import duckdb, time
t0 = time.time()
conn = duckdb.connect('data/warehouse/sec_intel.duckdb', read_only=True)

print("=" * 70)
print("DEEP RR ANALYSIS: ATR-based R-multiples")
print("=" * 70)

conn.execute("""
    CREATE TEMP TABLE rr AS
    WITH universe AS (
        SELECT ticker, report_quarter, conviction_score, accum_phase,
               accum_phase_quarters,
               COALESCE(inst_f4_distinct_60d, 0) as f4_count,
               COALESCE(squeeze_score, 0) as squeeze_score,
               CASE
                   WHEN report_quarter LIKE '%-Q1' THEN CAST(LEFT(report_quarter,4)||'-05-15' AS DATE)
                   WHEN report_quarter LIKE '%-Q2' THEN CAST(LEFT(report_quarter,4)||'-08-15' AS DATE)
                   WHEN report_quarter LIKE '%-Q3' THEN CAST(LEFT(report_quarter,4)||'-11-15' AS DATE)
                   WHEN report_quarter LIKE '%-Q4' THEN CAST(CAST(CAST(LEFT(report_quarter,4) AS INT)+1 AS VARCHAR)||'-02-15' AS DATE)
               END as avail_date,
               CASE
                   WHEN report_quarter LIKE '%-Q1' THEN CAST(LEFT(report_quarter,4)||'-08-14' AS DATE)
                   WHEN report_quarter LIKE '%-Q2' THEN CAST(LEFT(report_quarter,4)||'-11-14' AS DATE)
                   WHEN report_quarter LIKE '%-Q3' THEN CAST(CAST(CAST(LEFT(report_quarter,4) AS INT)+1 AS VARCHAR)||'-02-14' AS DATE)
                   WHEN report_quarter LIKE '%-Q4' THEN CAST(CAST(CAST(LEFT(report_quarter,4) AS INT)+1 AS VARCHAR)||'-05-14' AS DATE)
               END as expire_date
        FROM intelligence_scores
        WHERE accum_phase IN ('EARLY_ACCUM','ACTIVE_ACCUM','LATE_ACCUM')
          AND report_quarter >= '2020-Q1' AND report_quarter <= '2025-Q3'
    ),
    priced AS (
        SELECT p.ticker, p.trade_date, p.close, p.high, p.low, p.open, p.volume,
               u.conviction_score, u.accum_phase, u.accum_phase_quarters,
               u.f4_count, u.squeeze_score,
               AVG(p.high-p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as atr20,
               AVG(p.high-p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) as atr60,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) as sma200,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) as sma50,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as sma20,
               AVG(p.volume) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) as va10,
               AVG(p.volume) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) as va50,
               MAX(p.high) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as high20,
               MIN(p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as low20,
               LAG(p.close,1) OVER w as pc1,
               LAG(p.close,5) OVER w as pc5,
               LEAD(p.high,1) OVER w as h1, LEAD(p.high,2) OVER w as h2,
               LEAD(p.high,3) OVER w as h3, LEAD(p.high,4) OVER w as h4,
               LEAD(p.high,5) OVER w as h5, LEAD(p.high,6) OVER w as h6,
               LEAD(p.high,7) OVER w as h7, LEAD(p.high,8) OVER w as h8,
               LEAD(p.high,9) OVER w as h9, LEAD(p.high,10) OVER w as h10,
               LEAD(p.low,1) OVER w as l1, LEAD(p.low,2) OVER w as l2,
               LEAD(p.low,3) OVER w as l3, LEAD(p.low,4) OVER w as l4,
               LEAD(p.low,5) OVER w as l5, LEAD(p.low,6) OVER w as l6,
               LEAD(p.low,7) OVER w as l7, LEAD(p.low,8) OVER w as l8,
               LEAD(p.low,9) OVER w as l9, LEAD(p.low,10) OVER w as l10,
               LEAD(p.close,5) OVER w as ct5, LEAD(p.close,10) OVER w as ct10
        FROM fact_daily_prices p
        INNER JOIN universe u ON p.ticker = u.ticker
            AND p.trade_date >= u.avail_date AND p.trade_date <= u.expire_date
        WHERE p.close > 5 AND p.volume > 0
        WINDOW w AS (PARTITION BY p.ticker ORDER BY p.trade_date)
    )
    SELECT *,
        CASE WHEN close < sma200 THEN 1 ELSE 0 END as below_200,
        CASE WHEN close > sma200 THEN 1 ELSE 0 END as above_200,
        CASE WHEN atr20/NULLIF(atr60,0) < 0.85 THEN 1 ELSE 0 END as compressed,
        (close - pc5)/NULLIF(pc5,0) as ret_5d,
        (close - low20)/NULLIF(high20 - low20, 0) as range_pos,
        va10/NULLIF(va50,0) as vol_trend,
        CASE WHEN sma20 > sma50 THEN 1 ELSE 0 END as sma20_gt_50,
        -- R-multiple targets (1R = 1 ATR stop)
        -- Hit 1R up before 1R down in 5 days
        CASE WHEN GREATEST(h1,h2,h3,h4,h5) >= close + atr20
              AND LEAST(l1,l2,l3,l4,l5) > close - atr20
            THEN 1 ELSE 0 END as hit_1R_5d,
        CASE WHEN GREATEST(h1,h2,h3,h4,h5) >= close + 2*atr20
              AND LEAST(l1,l2,l3,l4,l5) > close - atr20
            THEN 1 ELSE 0 END as hit_2R_5d,
        CASE WHEN GREATEST(h1,h2,h3,h4,h5,h6,h7,h8,h9,h10) >= close + atr20
              AND LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10) > close - atr20
            THEN 1 ELSE 0 END as hit_1R_10d,
        CASE WHEN GREATEST(h1,h2,h3,h4,h5,h6,h7,h8,h9,h10) >= close + 2*atr20
              AND LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10) > close - atr20
            THEN 1 ELSE 0 END as hit_2R_10d,
        CASE WHEN GREATEST(h1,h2,h3,h4,h5,h6,h7,h8,h9,h10) >= close + 3*atr20
              AND LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10) > close - atr20
            THEN 1 ELSE 0 END as hit_3R_10d,
        CASE WHEN LEAST(l1,l2,l3,l4,l5) <= close - atr20 THEN 1 ELSE 0 END as stopped_5d,
        CASE WHEN LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10) <= close - atr20 THEN 1 ELSE 0 END as stopped_10d,
        (GREATEST(h1,h2,h3,h4,h5) - close) / NULLIF(atr20, 0) as mfe_5d,
        (close - LEAST(l1,l2,l3,l4,l5)) / NULLIF(atr20, 0) as mae_5d,
        (GREATEST(h1,h2,h3,h4,h5,h6,h7,h8,h9,h10) - close) / NULLIF(atr20, 0) as mfe_10d,
        (close - LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10)) / NULLIF(atr20, 0) as mae_10d
    FROM priced
    WHERE h10 IS NOT NULL AND l10 IS NOT NULL AND atr20 > 0
          AND sma200 IS NOT NULL AND atr60 IS NOT NULL AND pc5 IS NOT NULL
""")

ct = conn.execute("SELECT COUNT(*) FROM rr").fetchone()[0]
print(f"\nTotal: {ct:,} stock-days in accumulation (2020-2025)")

# ---------------------------------------------------------------
# RR analysis by condition
# ---------------------------------------------------------------
conditions = [
    ("ALL accumulation stocks", "1=1"),
    ("--- BELOW 200SMA THESIS ---", None),
    ("Below 200SMA", "below_200 = 1"),
    ("  + Pullback (5d ret < -2%)", "below_200 = 1 AND ret_5d < -0.02"),
    ("  + ATR compressed", "below_200 = 1 AND compressed = 1"),
    ("  + Compressed + Pullback", "below_200 = 1 AND compressed = 1 AND ret_5d < -0.02"),
    ("  + Comp + PB + Near lows", "below_200 = 1 AND compressed = 1 AND ret_5d < -0.02 AND range_pos < 0.3"),
    ("  + Conv>=65 + Comp + PB", "conviction_score >= 65 AND below_200 = 1 AND compressed = 1 AND ret_5d < -0.02"),
    ("  + Conv>=65 + Comp + Near lows", "conviction_score >= 65 AND below_200 = 1 AND compressed = 1 AND range_pos < 0.3"),
    ("  + Insider + Comp + PB", "f4_count >= 1 AND below_200 = 1 AND compressed = 1 AND ret_5d < -0.02"),
    ("  + Insider + Below200 + PB", "f4_count >= 1 AND below_200 = 1 AND ret_5d < -0.02"),
    ("--- ABOVE 200SMA THESIS ---", None),
    ("Above 200SMA", "above_200 = 1"),
    ("  + SMA aligned + Pullback", "above_200 = 1 AND sma20_gt_50 = 1 AND ret_5d < -0.02"),
    ("  + SMA aligned + PB + Quiet", "above_200 = 1 AND sma20_gt_50 = 1 AND ret_5d < -0.02 AND vol_trend < 0.9"),
    ("  + Conv>=65 + Aligned + PB", "conviction_score >= 65 AND above_200 = 1 AND sma20_gt_50 = 1 AND ret_5d < -0.02"),
    ("--- EARLY ACCUM THESIS ---", None),
    ("EARLY_ACCUM only", "accum_phase = 'EARLY_ACCUM'"),
    ("  + Below 200 + PB", "accum_phase = 'EARLY_ACCUM' AND below_200 = 1 AND ret_5d < -0.02"),
    ("  + Conv>=60 + Below200 + PB", "accum_phase = 'EARLY_ACCUM' AND conviction_score >= 60 AND below_200 = 1 AND ret_5d < -0.02"),
]

print(f"\n{'Condition':<43} {'N':>7} {'1R/5d':>7} {'2R/5d':>7} {'1R/10d':>7} {'2R/10d':>7} {'3R/10d':>7} {'Stp5d':>7} {'Stp10d':>7} {'MFE10':>6} {'MAE10':>6}")
print("-" * 130)

for label, where in conditions:
    if where is None:
        print(f"\n{label}")
        continue
    r = conn.execute(f"""
        SELECT COUNT(*),
               AVG(hit_1R_5d)*100, AVG(hit_2R_5d)*100,
               AVG(hit_1R_10d)*100, AVG(hit_2R_10d)*100, AVG(hit_3R_10d)*100,
               AVG(stopped_5d)*100, AVG(stopped_10d)*100,
               AVG(mfe_10d), AVG(mae_10d)
        FROM rr WHERE {where}
    """).fetchone()
    if r[0] < 20:
        print(f"{label:<43} {r[0]:>7,} (too few)")
        continue
    print(f"{label:<43} {r[0]:>7,} {r[1]:>6.1f}% {r[2]:>6.1f}% {r[3]:>6.1f}% {r[4]:>6.1f}% {r[5]:>6.1f}% {r[6]:>6.1f}% {r[7]:>6.1f}% {r[8]:>5.2f}R {r[9]:>5.2f}R")

# ---------------------------------------------------------------
# EXPECTANCY
# ---------------------------------------------------------------
print("\n" + "=" * 70)
print("EXPECTANCY PER TRADE (targeting 2R with 1R stop)")
print("=" * 70)

best_combos = [
    ("ALL accumulation", "1=1"),
    ("Below 200 + Comp + PB", "below_200 = 1 AND compressed = 1 AND ret_5d < -0.02"),
    ("Conv>=65 + Below200 + Comp + PB", "conviction_score >= 65 AND below_200 = 1 AND compressed = 1 AND ret_5d < -0.02"),
    ("Insider + Below200 + PB", "f4_count >= 1 AND below_200 = 1 AND ret_5d < -0.02"),
    ("EARLY + Below200 + PB", "accum_phase = 'EARLY_ACCUM' AND below_200 = 1 AND ret_5d < -0.02"),
    ("Above200 + Aligned + PB", "above_200 = 1 AND sma20_gt_50 = 1 AND ret_5d < -0.02"),
    ("Conv>=65 + Above200 + Aligned + PB", "conviction_score >= 65 AND above_200 = 1 AND sma20_gt_50 = 1 AND ret_5d < -0.02"),
]

print(f"\n{'Setup':<40} {'N':>7} {'2R Hit':>7} {'Stopped':>8} {'Expect':>8} {'Avg5d':>8} {'Avg10d':>8}")
print("-" * 85)

for label, where in best_combos:
    r = conn.execute(f"""
        SELECT COUNT(*),
               AVG(hit_2R_10d)*100,
               AVG(stopped_10d)*100,
               AVG((ct5-close)/close)*100,
               AVG((ct10-close)/close)*100
        FROM rr WHERE {where}
    """).fetchone()
    if r[0] < 20:
        continue
    hit = r[1]/100
    stop = r[2]/100
    exp = hit * 2 - stop * 1
    print(f"{label:<40} {r[0]:>7,} {r[1]:>6.1f}% {r[2]:>7.1f}% {exp:>+7.3f}R {r[3]:>+7.3f}% {r[4]:>+7.3f}%")

# ---------------------------------------------------------------
# YEARLY CONSISTENCY
# ---------------------------------------------------------------
print("\n" + "=" * 70)
print("YEARLY: Below 200 + Compressed + Pullback")
print("=" * 70)
rows = conn.execute("""
    SELECT YEAR(trade_date), COUNT(*),
           AVG(hit_1R_5d)*100, AVG(hit_2R_5d)*100,
           AVG(hit_1R_10d)*100, AVG(hit_2R_10d)*100,
           AVG(stopped_5d)*100, AVG(stopped_10d)*100,
           AVG((ct5-close)/close)*100
    FROM rr
    WHERE below_200 = 1 AND compressed = 1 AND ret_5d < -0.02
    GROUP BY 1 ORDER BY 1
""").fetchall()
print(f"  {'Year':>5} {'N':>7} {'1R/5d':>7} {'2R/5d':>7} {'1R/10d':>7} {'2R/10d':>7} {'Stp5d':>7} {'Stp10d':>7} {'Ret5d':>8}")
for r in rows:
    print(f"  {r[0]:>5} {r[1]:>7,} {r[2]:>6.1f}% {r[3]:>6.1f}% {r[4]:>6.1f}% {r[5]:>6.1f}% {r[6]:>6.1f}% {r[7]:>6.1f}% {r[8]:>+7.3f}%")

print("\n" + "=" * 70)
print("YEARLY: Conv>=65 + Above200 + SMA Aligned + Pullback")
print("=" * 70)
rows = conn.execute("""
    SELECT YEAR(trade_date), COUNT(*),
           AVG(hit_1R_5d)*100, AVG(hit_2R_5d)*100,
           AVG(hit_1R_10d)*100, AVG(hit_2R_10d)*100,
           AVG(stopped_5d)*100, AVG(stopped_10d)*100,
           AVG((ct5-close)/close)*100
    FROM rr
    WHERE conviction_score >= 65 AND above_200 = 1 AND sma20_gt_50 = 1 AND ret_5d < -0.02
    GROUP BY 1 ORDER BY 1
""").fetchall()
print(f"  {'Year':>5} {'N':>7} {'1R/5d':>7} {'2R/5d':>7} {'1R/10d':>7} {'2R/10d':>7} {'Stp5d':>7} {'Stp10d':>7} {'Ret5d':>8}")
for r in rows:
    print(f"  {r[0]:>5} {r[1]:>7,} {r[2]:>6.1f}% {r[3]:>6.1f}% {r[4]:>6.1f}% {r[5]:>6.1f}% {r[6]:>6.1f}% {r[7]:>6.1f}% {r[8]:>+7.3f}%")

conn.close()
print(f"\nDone in {time.time()-t0:.1f}s")
