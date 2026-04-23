"""REVERSE ANALYSIS: Start from outcomes, work backwards to find WHY.

Instead of: "Given features, predict returns"
We ask: "Stocks that moved +10% in 5 days — what was different about them?"
"""
import duckdb, time

t0 = time.time()
conn = duckdb.connect('data/warehouse/sec_intel.duckdb', read_only=True)

print("=" * 70)
print("REVERSE ANALYSIS: What was TRUE before big moves?")
print("=" * 70)

# Build classified dataset
conn.execute("""
    CREATE TEMP TABLE classified AS
    WITH universe AS (
        SELECT ticker, report_quarter, conviction_score, accum_phase,
               accum_phase_quarters,
               COALESCE(inst_f4_distinct_60d, 0) as f4_count,
               COALESCE(ml_score_v2, 0) as ml_score,
               COALESCE(squeeze_score, 0) as squeeze_score,
               COALESCE(insider_effect_score, 0) as insider_eff,
               COALESCE(institutional_pressure, 0) as inst_pressure,
               COALESCE(trend_score, 0) as trend_score,
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
        SELECT p.ticker, p.trade_date, p.open, p.high, p.low, p.close, p.volume,
               u.conviction_score, u.accum_phase, u.accum_phase_quarters,
               u.f4_count, u.ml_score, u.squeeze_score, u.insider_eff,
               u.inst_pressure, u.trend_score,
               LEAD(p.close, 3) OVER w as close_t3,
               LEAD(p.close, 5) OVER w as close_t5,
               GREATEST(LEAD(p.high,1) OVER w, LEAD(p.high,2) OVER w,
                        LEAD(p.high,3) OVER w, LEAD(p.high,4) OVER w,
                        LEAD(p.high,5) OVER w) as max_high_5d,
               LEAST(LEAD(p.low,1) OVER w, LEAD(p.low,2) OVER w,
                     LEAD(p.low,3) OVER w, LEAD(p.low,4) OVER w,
                     LEAD(p.low,5) OVER w) as min_low_5d,
               LAG(p.close,1) OVER w as prev_close,
               LAG(p.close,5) OVER w as close_5ago,
               LAG(p.close,20) OVER w as close_20ago,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) as sma10,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as sma20,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) as sma50,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) as sma200,
               AVG(p.volume) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) as vol_avg10,
               AVG(p.volume) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) as vol_avg50,
               AVG(p.high-p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as atr20,
               AVG(p.high-p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) as atr60,
               MAX(p.high) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as high20,
               MIN(p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as low20
        FROM fact_daily_prices p
        INNER JOIN universe u ON p.ticker = u.ticker
            AND p.trade_date >= u.avail_date AND p.trade_date <= u.expire_date
        WHERE p.close > 5 AND p.volume > 0
        WINDOW w AS (PARTITION BY p.ticker ORDER BY p.trade_date)
    )
    SELECT *,
        CASE
            WHEN (max_high_5d - close) / close >= 0.10 THEN 'BIG_WIN'
            WHEN (max_high_5d - close) / close >= 0.05 THEN 'WIN'
            WHEN (close - min_low_5d) / close >= 0.10 THEN 'BIG_LOSS'
            WHEN (close - min_low_5d) / close >= 0.05 THEN 'LOSS'
            ELSE 'FLAT'
        END as outcome,
        -- Computed features
        (close - sma20) / NULLIF(sma20, 0) as pct_sma20,
        (close - sma50) / NULLIF(sma50, 0) as pct_sma50,
        (close - sma200) / NULLIF(sma200, 0) as pct_sma200,
        CASE WHEN close > sma200 THEN 1 ELSE 0 END as above_200,
        CASE WHEN close > sma50 THEN 1 ELSE 0 END as above_50,
        CASE WHEN sma10 > sma20 AND sma20 > sma50 THEN 1 ELSE 0 END as ma_stacked,
        volume / NULLIF(vol_avg50, 0) as vol_ratio,
        vol_avg10 / NULLIF(vol_avg50, 0) as vol_trend,
        atr20 / NULLIF(atr60, 0) as atr_comp,
        atr20 / NULLIF(close, 0) as atr_pct,
        (close - low20) / NULLIF(high20 - low20, 0) as range_pos,
        (close - prev_close) / NULLIF(prev_close, 0) as ret_1d,
        (close - close_5ago) / NULLIF(close_5ago, 0) as ret_5d,
        (close - close_20ago) / NULLIF(close_20ago, 0) as ret_20d,
        (open - prev_close) / NULLIF(prev_close, 0) as gap
    FROM priced
    WHERE close_t5 IS NOT NULL AND sma200 IS NOT NULL
          AND max_high_5d IS NOT NULL AND min_low_5d IS NOT NULL
          AND atr60 IS NOT NULL AND close_20ago IS NOT NULL
""")

# Distribution
print("\nOutcome Distribution:")
rows = conn.execute("""
    SELECT outcome, COUNT(*) as n FROM classified GROUP BY outcome
    ORDER BY CASE outcome WHEN 'BIG_WIN' THEN 1 WHEN 'WIN' THEN 2 WHEN 'FLAT' THEN 3 WHEN 'LOSS' THEN 4 WHEN 'BIG_LOSS' THEN 5 END
""").fetchall()
total = sum(r[1] for r in rows)
for r in rows:
    print(f"  {r[0]:<10} {r[1]:>8,} ({r[1]/total*100:.1f}%)")

# THE KEY ANALYSIS: Compare BIG_WIN vs FLAT vs BIG_LOSS
print("\n" + "=" * 70)
print("FACTOR COMPARISON: BIG_WIN (+10% in 5d) vs FLAT vs BIG_LOSS (-10%)")
print("=" * 70)

factors = [
    ("conviction_score", "Conviction", "avg"),
    ("accum_phase_quarters", "Accum Quarters", "avg"),
    ("f4_count", "F4 Insiders (60d)", "avg"),
    ("ml_score", "ML Score", "avg"),
    ("squeeze_score", "Squeeze Score", "avg"),
    ("insider_eff", "Insider Effect", "avg"),
    ("inst_pressure", "Inst Pressure", "avg"),
    ("trend_score", "Trend Score", "avg"),
    ("above_200", "% Above 200SMA", "avg_pct"),
    ("above_50", "% Above 50SMA", "avg_pct"),
    ("ma_stacked", "% MA Stacked", "avg_pct"),
    ("pct_sma20", "Dist from SMA20", "avg_pct"),
    ("pct_sma50", "Dist from SMA50", "avg_pct"),
    ("pct_sma200", "Dist from SMA200", "avg_pct"),
    ("vol_ratio", "Volume Ratio (vs 50d)", "avg"),
    ("vol_trend", "Vol Trend (10d/50d)", "avg"),
    ("atr_comp", "ATR Compression (20/60)", "avg"),
    ("atr_pct", "ATR as % of Price", "avg_pct"),
    ("range_pos", "Range Position (20d)", "avg"),
    ("ret_1d", "1-Day Return", "avg_pct"),
    ("ret_5d", "5-Day Return", "avg_pct"),
    ("ret_20d", "20-Day Return", "avg_pct"),
    ("gap", "Gap (today)", "avg_pct"),
]

print(f"\n{'Factor':<25} {'BIG_WIN':>10} {'WIN':>10} {'FLAT':>10} {'LOSS':>10} {'BIG_LOSS':>10} {'Edge':>10}")
print("-" * 85)

for col, label, fmt in factors:
    vals = conn.execute(f"""
        SELECT outcome, AVG({col}) as v
        FROM classified
        WHERE outcome IN ('BIG_WIN','WIN','FLAT','LOSS','BIG_LOSS')
        GROUP BY outcome
        ORDER BY CASE outcome WHEN 'BIG_WIN' THEN 1 WHEN 'WIN' THEN 2 WHEN 'FLAT' THEN 3 WHEN 'LOSS' THEN 4 WHEN 'BIG_LOSS' THEN 5 END
    """).fetchall()

    val_map = {r[0]: r[1] for r in vals}
    bw = val_map.get('BIG_WIN', 0) or 0
    w = val_map.get('WIN', 0) or 0
    f = val_map.get('FLAT', 0) or 0
    l = val_map.get('LOSS', 0) or 0
    bl = val_map.get('BIG_LOSS', 0) or 0

    # Edge = how much BIG_WIN differs from BIG_LOSS (normalized)
    edge = bw - bl

    if fmt == "avg_pct":
        print(f"{label:<25} {bw*100:>+9.2f}% {w*100:>+9.2f}% {f*100:>+9.2f}% {l*100:>+9.2f}% {bl*100:>+9.2f}% {edge*100:>+9.2f}%")
    else:
        print(f"{label:<25} {bw:>10.2f} {w:>10.2f} {f:>10.2f} {l:>10.2f} {bl:>10.2f} {edge:>+10.2f}")


# DEEPER: What accum phase produces the most big wins?
print("\n" + "=" * 70)
print("BY ACCUMULATION PHASE")
print("=" * 70)
rows = conn.execute("""
    SELECT accum_phase, outcome, COUNT(*) as n
    FROM classified
    WHERE outcome IN ('BIG_WIN','WIN','FLAT','LOSS','BIG_LOSS')
    GROUP BY accum_phase, outcome
""").fetchall()

phase_totals = {}
phase_outcomes = {}
for r in rows:
    phase = r[0]
    outcome = r[1]
    n = r[2]
    phase_totals[phase] = phase_totals.get(phase, 0) + n
    phase_outcomes[(phase, outcome)] = n

print(f"\n{'Phase':<15} {'Total':>8} {'BIG_WIN%':>9} {'WIN%':>9} {'FLAT%':>9} {'LOSS%':>9} {'BIG_LOSS%':>10} {'Win/Loss':>9}")
for phase in ['EARLY_ACCUM', 'ACTIVE_ACCUM', 'LATE_ACCUM']:
    t = phase_totals.get(phase, 1)
    bw = phase_outcomes.get((phase, 'BIG_WIN'), 0) / t * 100
    w = phase_outcomes.get((phase, 'WIN'), 0) / t * 100
    f = phase_outcomes.get((phase, 'FLAT'), 0) / t * 100
    l = phase_outcomes.get((phase, 'LOSS'), 0) / t * 100
    bl = phase_outcomes.get((phase, 'BIG_LOSS'), 0) / t * 100
    ratio = (bw + w) / max(l + bl, 0.01)
    print(f"{phase:<15} {t:>8,} {bw:>8.1f}% {w:>8.1f}% {f:>8.1f}% {l:>8.1f}% {bl:>9.1f}% {ratio:>8.2f}")


# CRITICAL: What specific conditions produce BIG_WIN with MINIMAL BIG_LOSS?
print("\n" + "=" * 70)
print("CONFLUENCE ANALYSIS: What combos produce BIG_WIN without BIG_LOSS?")
print("=" * 70)

# Test various condition combos
combos = conn.execute("""
    SELECT
        CASE WHEN above_200 = 1 THEN 'Y' ELSE 'N' END as above200,
        CASE WHEN ma_stacked = 1 THEN 'Y' ELSE 'N' END as ma_stack,
        CASE WHEN atr_comp < 0.85 THEN 'COMP' ELSE 'NORM' END as atr_state,
        CASE WHEN vol_trend < 0.9 THEN 'QUIET' ELSE 'LOUD' END as vol_state,
        CASE WHEN f4_count >= 1 THEN 'YES' ELSE 'NO' END as has_insider,
        CASE WHEN ret_5d < -0.02 THEN 'PULLBACK' WHEN ret_5d > 0.02 THEN 'MOMO' ELSE 'FLAT' END as momentum,
        COUNT(*) as n,
        SUM(CASE WHEN outcome = 'BIG_WIN' THEN 1 ELSE 0 END) as big_wins,
        SUM(CASE WHEN outcome IN ('BIG_WIN','WIN') THEN 1 ELSE 0 END) as all_wins,
        SUM(CASE WHEN outcome IN ('LOSS','BIG_LOSS') THEN 1 ELSE 0 END) as all_losses,
        SUM(CASE WHEN outcome = 'BIG_LOSS' THEN 1 ELSE 0 END) as big_losses,
        AVG((max_high_5d - close)/close) * 100 as avg_max_up,
        AVG((close - min_low_5d)/close) * 100 as avg_max_dn
    FROM classified
    GROUP BY 1,2,3,4,5,6
    HAVING COUNT(*) >= 200
    ORDER BY SUM(CASE WHEN outcome IN ('BIG_WIN','WIN') THEN 1 ELSE 0 END) * 1.0 / COUNT(*) DESC
    LIMIT 30
""").fetchall()

print(f"\n{'>200':>4} {'MA':>3} {'ATR':>5} {'Vol':>6} {'Ins':>4} {'Mom':>8} | {'N':>6} {'BgW%':>6} {'Win%':>6} {'Loss%':>6} {'BgL%':>6} | {'W/L':>5} {'AvgUp':>6} {'AvgDn':>6}")
print("-" * 100)
for r in combos:
    win_pct = r[8]/r[6]*100
    loss_pct = r[9]/r[6]*100
    bw_pct = r[7]/r[6]*100
    bl_pct = r[10]/r[6]*100
    wl = (r[8]) / max(r[9], 1)
    print(f"{r[0]:>4} {r[1]:>3} {r[2]:>5} {r[3]:>6} {r[4]:>4} {r[5]:>8} | "
          f"{r[6]:>6,} {bw_pct:>5.1f}% {win_pct:>5.1f}% {loss_pct:>5.1f}% {bl_pct:>5.1f}% | "
          f"{wl:>5.2f} {r[11]:>5.1f}% {r[12]:>5.1f}%")


# Now check: adding conviction as a filter
print("\n" + "=" * 70)
print("HIGH CONVICTION COMBOS (conv >= 65)")
print("=" * 70)

combos2 = conn.execute("""
    SELECT
        CASE WHEN above_200 = 1 THEN 'Y' ELSE 'N' END as above200,
        CASE WHEN atr_comp < 0.85 THEN 'COMP' ELSE 'NORM' END as atr_state,
        CASE WHEN f4_count >= 1 THEN 'INS' ELSE 'no' END as has_insider,
        CASE WHEN ret_5d < -0.02 THEN 'PB' WHEN ret_5d > 0.02 THEN 'MO' ELSE 'FL' END as mom,
        CASE WHEN range_pos > 0.7 THEN 'HI' WHEN range_pos < 0.3 THEN 'LO' ELSE 'MD' END as rng,
        COUNT(*) as n,
        SUM(CASE WHEN outcome IN ('BIG_WIN','WIN') THEN 1 ELSE 0 END)*100.0/COUNT(*) as win_rate,
        SUM(CASE WHEN outcome IN ('LOSS','BIG_LOSS') THEN 1 ELSE 0 END)*100.0/COUNT(*) as loss_rate,
        AVG((max_high_5d - close)/close) * 100 as avg_up,
        AVG((close - min_low_5d)/close) * 100 as avg_dn,
        AVG((close_t5 - close)/close) * 100 as avg_ret_5d
    FROM classified
    WHERE conviction_score >= 65
    GROUP BY 1,2,3,4,5
    HAVING COUNT(*) >= 100
    ORDER BY SUM(CASE WHEN outcome IN ('BIG_WIN','WIN') THEN 1 ELSE 0 END)*1.0/COUNT(*) DESC
    LIMIT 25
""").fetchall()

print(f"\n{'>200':>4} {'ATR':>5} {'Ins':>4} {'Mom':>3} {'Rng':>4} | {'N':>6} {'Win%':>6} {'Loss%':>6} {'AvgUp':>6} {'AvgDn':>6} {'Ret5d':>7}")
print("-" * 70)
for r in combos2:
    print(f"{r[0]:>4} {r[1]:>5} {r[2]:>4} {r[3]:>3} {r[4]:>4} | "
          f"{r[5]:>6,} {r[6]:>5.1f}% {r[7]:>5.1f}% {r[8]:>5.1f}% {r[9]:>5.1f}% {r[10]:>+6.2f}%")

conn.close()
print(f"\nDone in {time.time()-t0:.1f}s")
