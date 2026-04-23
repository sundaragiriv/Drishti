"""Build feature matrix for Flow Predictor ML model.

Universe: Stocks in institutional accumulation (EARLY/ACTIVE/LATE_ACCUM)
Features: ~30 technical + institutional hybrid features
Labels: 3-day and 5-day forward returns
"""
import duckdb, time, os

t0 = time.time()
conn = duckdb.connect('data/warehouse/sec_intel.duckdb', read_only=True)

print("Building feature matrix for Flow Predictor...")

# Step 1: Universe - stock-quarters in accumulation
conn.execute("""
    CREATE TEMP TABLE signal_universe AS
    SELECT ticker, report_quarter, conviction_score, accum_phase, accum_phase_quarters,
           COALESCE(inst_f4_distinct_60d, 0) as f4_count,
           COALESCE(ml_score_v2, 0) as ml_score,
           COALESCE(squeeze_score, 0) as squeeze_score,
           COALESCE(short_squeeze_score, 0) as short_squeeze,
           COALESCE(insider_effect_score, 0) as insider_effect,
           COALESCE(trend_score, 0) as trend_score,
           COALESCE(institutional_pressure, 0) as inst_pressure,
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
      AND report_quarter >= '2019-Q1'
      AND report_quarter <= '2025-Q3'
""")

ct = conn.execute("SELECT COUNT(*) FROM signal_universe").fetchone()[0]
print(f"  Universe: {ct:,} stock-quarters")

# Step 2: Join prices, compute features + labels
print("  Computing technical features + labels...")
conn.execute("""
    CREATE TEMP TABLE features AS
    WITH base AS (
        SELECT p.ticker, p.trade_date, p.open, p.high, p.low, p.close, p.volume,
               su.conviction_score, su.accum_phase, su.accum_phase_quarters,
               su.f4_count, su.ml_score, su.squeeze_score, su.short_squeeze,
               su.insider_effect, su.trend_score, su.inst_pressure,
               su.report_quarter,
               -- Forward returns (labels)
               LEAD(p.close, 1) OVER w as close_t1,
               LEAD(p.close, 2) OVER w as close_t2,
               LEAD(p.close, 3) OVER w as close_t3,
               LEAD(p.close, 5) OVER w as close_t5,
               -- Backward prices for features
               LAG(p.close, 1) OVER w as close_lag1,
               LAG(p.close, 5) OVER w as close_lag5,
               LAG(p.close, 10) OVER w as close_lag10,
               LAG(p.close, 20) OVER w as close_lag20,
               LAG(p.volume, 1) OVER w as vol_lag1,
               -- Moving averages
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) as sma10,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as sma20,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) as sma50,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) as sma200,
               -- Volume averages
               AVG(p.volume) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) as vol_avg10,
               AVG(p.volume) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) as vol_avg50,
               -- ATR
               AVG(p.high - p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) as atr14,
               AVG(p.high - p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as atr20,
               AVG(p.high - p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) as atr60,
               -- Highs and lows
               MAX(p.high) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as high20,
               MIN(p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as low20,
               MAX(p.high) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW) as high52w,
               MIN(p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW) as low52w,
               -- Std dev for Bollinger
               STDDEV(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as std20
        FROM fact_daily_prices p
        INNER JOIN signal_universe su ON p.ticker = su.ticker
            AND p.trade_date >= su.avail_date AND p.trade_date <= su.expire_date
        WHERE p.close > 5 AND p.volume > 0
        WINDOW w AS (PARTITION BY p.ticker ORDER BY p.trade_date)
    )
    SELECT
        ticker, trade_date, close, report_quarter,
        -- === LABELS ===
        (close_t3 - close) / close as ret_3d,
        (close_t5 - close) / close as ret_5d,
        CASE WHEN (close_t3 - close) / close >= 0.03 THEN 1 ELSE 0 END as label_3pct_3d,
        CASE WHEN (close_t3 - close) / close >= 0.02 THEN 1 ELSE 0 END as label_2pct_3d,
        CASE WHEN close_t3 > close THEN 1 ELSE 0 END as label_up_3d,

        -- === INSTITUTIONAL FEATURES (9) ===
        conviction_score,
        CASE WHEN accum_phase = 'EARLY_ACCUM' THEN 0
             WHEN accum_phase = 'ACTIVE_ACCUM' THEN 1
             WHEN accum_phase = 'LATE_ACCUM' THEN 2 END as phase_ord,
        accum_phase_quarters,
        f4_count,
        ml_score,
        squeeze_score,
        short_squeeze,
        insider_effect,
        inst_pressure,

        -- === TECHNICAL FEATURES (22) ===
        -- Trend position
        (close - sma10) / NULLIF(sma10, 0) as pct_from_sma10,
        (close - sma20) / NULLIF(sma20, 0) as pct_from_sma20,
        (close - sma50) / NULLIF(sma50, 0) as pct_from_sma50,
        (close - sma200) / NULLIF(sma200, 0) as pct_from_sma200,
        CASE WHEN close > sma200 THEN 1 ELSE 0 END as above_200sma,
        CASE WHEN sma10 > sma20 AND sma20 > sma50 THEN 1 ELSE 0 END as ma_aligned,

        -- Momentum
        (close - close_lag1) / NULLIF(close_lag1, 0) as ret_1d,
        (close - close_lag5) / NULLIF(close_lag5, 0) as ret_5d_back,
        (close - close_lag10) / NULLIF(close_lag10, 0) as ret_10d_back,
        (close - close_lag20) / NULLIF(close_lag20, 0) as ret_20d_back,

        -- Volatility
        atr14 / NULLIF(close, 0) as atr14_pct,
        atr20 / NULLIF(atr60, 0) as atr_compression,
        std20 / NULLIF(sma20, 0) as bollinger_width,
        (close - (sma20 - 2 * std20)) / NULLIF(4 * std20, 0) as bollinger_pos,

        -- Volume
        volume / NULLIF(vol_avg10, 0) as vol_ratio_10d,
        volume / NULLIF(vol_avg50, 0) as vol_ratio_50d,
        vol_avg10 / NULLIF(vol_avg50, 0) as vol_trend,

        -- Range / Structure
        (close - low20) / NULLIF(high20 - low20, 0) as range_position_20d,
        (close - low52w) / NULLIF(high52w - low52w, 0) as range_position_52w,
        (high20 - low20) / NULLIF(close, 0) as range_20d_pct,

        -- Candle pattern (today's bar)
        (close - open) / NULLIF(high - low, 0) as candle_body_ratio,
        (high - GREATEST(open, close)) / NULLIF(high - low, 0) as upper_wick_ratio

    FROM base
    WHERE close_t3 IS NOT NULL
      AND sma200 IS NOT NULL
      AND atr60 IS NOT NULL
      AND close_lag20 IS NOT NULL
""")

ct = conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
pos3 = conn.execute("SELECT COUNT(*) FROM features WHERE label_3pct_3d = 1").fetchone()[0]
pos2 = conn.execute("SELECT COUNT(*) FROM features WHERE label_2pct_3d = 1").fetchone()[0]
posup = conn.execute("SELECT COUNT(*) FROM features WHERE label_up_3d = 1").fetchone()[0]
print(f"  Feature matrix: {ct:,} rows")
print(f"  +3% in 3d: {pos3:,} ({pos3/ct*100:.1f}%)")
print(f"  +2% in 3d: {pos2:,} ({pos2/ct*100:.1f}%)")
print(f"  Up in 3d:  {posup:,} ({posup/ct*100:.1f}%)")

# Save
os.makedirs("data/ml_training", exist_ok=True)
conn.execute("COPY features TO 'data/ml_training/flow_predictor_features.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)")
print(f"\n  Saved: data/ml_training/flow_predictor_features.parquet")

# Stats by year
print("\n=== By Year ===")
rows = conn.execute("""
    SELECT YEAR(trade_date) as yr, COUNT(*) as n,
           AVG(label_3pct_3d)*100 as pct3, AVG(label_up_3d)*100 as up_rate,
           AVG(ret_3d)*100 as avg_ret
    FROM features GROUP BY 1 ORDER BY 1
""").fetchall()
for r in rows:
    print(f"  {r[0]}: {r[1]:>8,} rows, +3% rate={r[2]:>5.1f}%, up rate={r[3]:>5.1f}%, avg ret={r[4]:>6.3f}%")

conn.close()
print(f"\nDone in {time.time()-t0:.1f}s")
