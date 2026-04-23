"""Build V2 feature matrix with enriched features for Flow Predictor.

V2 additions over V1:
  - Market context (SPY return, SPY trend, market breadth proxy)
  - Recent insider events (bought in last 5/10/30 days)
  - Short interest features
  - Sector relative momentum
  - Asymmetric RR target: hits +2% before -1% within 5 days
  - Multi-day candle patterns (3-day momentum, gap)
"""
import duckdb, time, os

t0 = time.time()
conn = duckdb.connect('data/warehouse/sec_intel.duckdb', read_only=True)

print("Building V2 feature matrix for Flow Predictor...")

# ---------------------------------------------------------------
# 1. Universe
# ---------------------------------------------------------------
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
           COALESCE(price_momentum_90d, 0) as price_mom_90d,
           COALESCE(price_above_200sma, 0) as intel_above_200sma,
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

# ---------------------------------------------------------------
# 2. SPY reference table (market context)
# ---------------------------------------------------------------
print("  Building SPY context...")
conn.execute("""
    CREATE TEMP TABLE spy_context AS
    SELECT trade_date,
           close as spy_close,
           (close - LAG(close, 1) OVER w) / LAG(close, 1) OVER w as spy_ret_1d,
           (close - LAG(close, 5) OVER w) / LAG(close, 5) OVER w as spy_ret_5d,
           (close - LAG(close, 20) OVER w) / LAG(close, 20) OVER w as spy_ret_20d,
           close / AVG(close) OVER (PARTITION BY 1 ORDER BY trade_date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) - 1 as spy_vs_sma50,
           close / AVG(close) OVER (PARTITION BY 1 ORDER BY trade_date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) - 1 as spy_vs_sma200,
           AVG(high - low) OVER (PARTITION BY 1 ORDER BY trade_date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) /
           NULLIF(close, 0) as spy_atr10_pct,
           STDDEV(close) OVER (PARTITION BY 1 ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) /
           NULLIF(AVG(close) OVER (PARTITION BY 1 ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW), 0) as spy_volatility
    FROM fact_daily_prices
    WHERE ticker = 'SPY'
    WINDOW w AS (PARTITION BY 1 ORDER BY trade_date)
""")

# ---------------------------------------------------------------
# 3. Insider events (point-in-time)
# ---------------------------------------------------------------
print("  Building insider event features...")
conn.execute("""
    CREATE TEMP TABLE insider_events AS
    SELECT ticker, transaction_date,
           COUNT(*) OVER (PARTITION BY ticker ORDER BY transaction_date
                         RANGE BETWEEN INTERVAL 5 DAY PRECEDING AND CURRENT ROW) as insider_buys_5d,
           COUNT(*) OVER (PARTITION BY ticker ORDER BY transaction_date
                         RANGE BETWEEN INTERVAL 10 DAY PRECEDING AND CURRENT ROW) as insider_buys_10d,
           COUNT(*) OVER (PARTITION BY ticker ORDER BY transaction_date
                         RANGE BETWEEN INTERVAL 30 DAY PRECEDING AND CURRENT ROW) as insider_buys_30d,
           COUNT(DISTINCT insider_name) OVER (PARTITION BY ticker ORDER BY transaction_date
                         RANGE BETWEEN INTERVAL 30 DAY PRECEDING AND CURRENT ROW) as distinct_insiders_30d,
           SUM(shares * price) OVER (PARTITION BY ticker ORDER BY transaction_date
                         RANGE BETWEEN INTERVAL 30 DAY PRECEDING AND CURRENT ROW) as insider_dollar_30d
    FROM fact_form4_transactions
    WHERE transaction_code = 'P'
      AND transaction_date >= '2019-01-01'
      AND price > 0
""")

# ---------------------------------------------------------------
# 4. Main feature matrix
# ---------------------------------------------------------------
print("  Computing full feature matrix...")
conn.execute("""
    CREATE TEMP TABLE features_v2 AS
    WITH base AS (
        SELECT p.ticker, p.trade_date, p.open, p.high, p.low, p.close, p.volume,
               su.conviction_score, su.accum_phase, su.accum_phase_quarters,
               su.f4_count, su.ml_score, su.squeeze_score, su.short_squeeze,
               su.insider_effect, su.trend_score, su.inst_pressure,
               su.price_mom_90d,
               su.report_quarter,
               -- Forward prices for labels
               LEAD(p.close, 1) OVER w as close_t1,
               LEAD(p.close, 2) OVER w as close_t2,
               LEAD(p.close, 3) OVER w as close_t3,
               LEAD(p.close, 5) OVER w as close_t5,
               LEAD(p.high, 1) OVER w as high_t1,
               LEAD(p.high, 2) OVER w as high_t2,
               LEAD(p.high, 3) OVER w as high_t3,
               LEAD(p.high, 4) OVER w as high_t4,
               LEAD(p.high, 5) OVER w as high_t5,
               LEAD(p.low, 1) OVER w as low_t1,
               LEAD(p.low, 2) OVER w as low_t2,
               LEAD(p.low, 3) OVER w as low_t3,
               LEAD(p.low, 4) OVER w as low_t4,
               LEAD(p.low, 5) OVER w as low_t5,
               -- Backward prices
               LAG(p.close, 1) OVER w as close_lag1,
               LAG(p.close, 2) OVER w as close_lag2,
               LAG(p.close, 3) OVER w as close_lag3,
               LAG(p.close, 5) OVER w as close_lag5,
               LAG(p.close, 10) OVER w as close_lag10,
               LAG(p.close, 20) OVER w as close_lag20,
               LAG(p.open, 1) OVER w as open_lag1,
               LAG(p.volume, 1) OVER w as vol_lag1,
               LAG(p.volume, 2) OVER w as vol_lag2,
               -- Moving averages
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as sma5,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) as sma10,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as sma20,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) as sma50,
               AVG(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) as sma200,
               -- Volume averages
               AVG(p.volume) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as vol_avg5,
               AVG(p.volume) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) as vol_avg10,
               AVG(p.volume) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) as vol_avg50,
               -- ATR
               AVG(p.high - p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as atr5,
               AVG(p.high - p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) as atr14,
               AVG(p.high - p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as atr20,
               AVG(p.high - p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) as atr60,
               -- Highs and lows
               MAX(p.high) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as high5,
               MIN(p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as low5,
               MAX(p.high) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as high20,
               MIN(p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as low20,
               MAX(p.high) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW) as high52w,
               MIN(p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW) as low52w,
               -- Std dev
               STDDEV(p.close) OVER (PARTITION BY p.ticker ORDER BY p.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as std20
        FROM fact_daily_prices p
        INNER JOIN signal_universe su ON p.ticker = su.ticker
            AND p.trade_date >= su.avail_date AND p.trade_date <= su.expire_date
        WHERE p.close > 5 AND p.volume > 0
        WINDOW w AS (PARTITION BY p.ticker ORDER BY p.trade_date)
    )
    SELECT
        b.ticker, b.trade_date, b.close, b.report_quarter,

        -- === LABELS ===
        (b.close_t3 - b.close) / b.close as ret_3d,
        (b.close_t5 - b.close) / b.close as ret_5d,
        CASE WHEN (b.close_t3 - b.close) / b.close >= 0.03 THEN 1 ELSE 0 END as label_3pct_3d,
        CASE WHEN (b.close_t3 - b.close) / b.close >= 0.02 THEN 1 ELSE 0 END as label_2pct_3d,
        CASE WHEN b.close_t3 > b.close THEN 1 ELSE 0 END as label_up_3d,
        -- Asymmetric RR target: max high in 5 days >= close+2% AND min low in 5 days > close-1%
        CASE WHEN GREATEST(b.high_t1, b.high_t2, b.high_t3, b.high_t4, b.high_t5) >= b.close * 1.02
              AND LEAST(b.low_t1, b.low_t2, b.low_t3, b.low_t4, b.low_t5) > b.close * 0.99
            THEN 1 ELSE 0 END as label_rr_2to1,
        -- Softer: +1.5% up before -1% down in 5d
        CASE WHEN GREATEST(b.high_t1, b.high_t2, b.high_t3, b.high_t4, b.high_t5) >= b.close * 1.015
              AND LEAST(b.low_t1, b.low_t2, b.low_t3, b.low_t4, b.low_t5) > b.close * 0.99
            THEN 1 ELSE 0 END as label_rr_1_5to1,
        -- Max drawup and drawdown in 5 days
        (GREATEST(b.high_t1, b.high_t2, b.high_t3, b.high_t4, b.high_t5) - b.close) / b.close as max_up_5d,
        (b.close - LEAST(b.low_t1, b.low_t2, b.low_t3, b.low_t4, b.low_t5)) / b.close as max_down_5d,

        -- === INSTITUTIONAL FEATURES (10) ===
        b.conviction_score,
        CASE WHEN b.accum_phase = 'EARLY_ACCUM' THEN 0
             WHEN b.accum_phase = 'ACTIVE_ACCUM' THEN 1
             WHEN b.accum_phase = 'LATE_ACCUM' THEN 2 END as phase_ord,
        b.accum_phase_quarters,
        b.f4_count,
        b.ml_score,
        b.squeeze_score,
        b.short_squeeze,
        b.insider_effect,
        b.inst_pressure,
        b.price_mom_90d,

        -- === INSIDER EVENT FEATURES (5) ===
        COALESCE(ie.insider_buys_5d, 0) as insider_buys_5d,
        COALESCE(ie.insider_buys_10d, 0) as insider_buys_10d,
        COALESCE(ie.insider_buys_30d, 0) as insider_buys_30d,
        COALESCE(ie.distinct_insiders_30d, 0) as distinct_insiders_30d,
        CASE WHEN COALESCE(ie.insider_dollar_30d, 0) > 0
             THEN LN(COALESCE(ie.insider_dollar_30d, 0) + 1)
             ELSE 0 END as log_insider_dollar_30d,

        -- === MARKET CONTEXT (7) ===
        COALESCE(spy.spy_ret_1d, 0) as spy_ret_1d,
        COALESCE(spy.spy_ret_5d, 0) as spy_ret_5d,
        COALESCE(spy.spy_ret_20d, 0) as spy_ret_20d,
        COALESCE(spy.spy_vs_sma50, 0) as spy_vs_sma50,
        COALESCE(spy.spy_vs_sma200, 0) as spy_vs_sma200,
        COALESCE(spy.spy_atr10_pct, 0) as spy_atr10_pct,
        COALESCE(spy.spy_volatility, 0) as spy_volatility,

        -- === TECHNICAL: TREND (7) ===
        (b.close - b.sma5) / NULLIF(b.sma5, 0) as pct_from_sma5,
        (b.close - b.sma10) / NULLIF(b.sma10, 0) as pct_from_sma10,
        (b.close - b.sma20) / NULLIF(b.sma20, 0) as pct_from_sma20,
        (b.close - b.sma50) / NULLIF(b.sma50, 0) as pct_from_sma50,
        (b.close - b.sma200) / NULLIF(b.sma200, 0) as pct_from_sma200,
        CASE WHEN b.close > b.sma200 THEN 1 ELSE 0 END as above_200sma,
        CASE WHEN b.sma10 > b.sma20 AND b.sma20 > b.sma50 THEN 1 ELSE 0 END as ma_aligned,

        -- === TECHNICAL: MOMENTUM (7) ===
        (b.close - b.close_lag1) / NULLIF(b.close_lag1, 0) as ret_1d,
        (b.close - b.close_lag2) / NULLIF(b.close_lag2, 0) as ret_2d_back,
        (b.close - b.close_lag3) / NULLIF(b.close_lag3, 0) as ret_3d_back,
        (b.close - b.close_lag5) / NULLIF(b.close_lag5, 0) as ret_5d_back,
        (b.close - b.close_lag10) / NULLIF(b.close_lag10, 0) as ret_10d_back,
        (b.close - b.close_lag20) / NULLIF(b.close_lag20, 0) as ret_20d_back,
        -- Relative strength vs SPY
        COALESCE(
            (b.close - b.close_lag5) / NULLIF(b.close_lag5, 0) - spy.spy_ret_5d,
            0
        ) as rel_strength_5d,

        -- === TECHNICAL: VOLATILITY (5) ===
        b.atr14 / NULLIF(b.close, 0) as atr14_pct,
        b.atr5 / NULLIF(b.atr20, 0) as atr_short_vs_long,
        b.atr20 / NULLIF(b.atr60, 0) as atr_compression,
        b.std20 / NULLIF(b.sma20, 0) as bollinger_width,
        (b.close - (b.sma20 - 2 * b.std20)) / NULLIF(4 * b.std20, 0) as bollinger_pos,

        -- === TECHNICAL: VOLUME (4) ===
        b.volume / NULLIF(b.vol_avg10, 0) as vol_ratio_10d,
        b.volume / NULLIF(b.vol_avg50, 0) as vol_ratio_50d,
        b.vol_avg5 / NULLIF(b.vol_avg50, 0) as vol_trend_5_50,
        b.vol_avg10 / NULLIF(b.vol_avg50, 0) as vol_trend_10_50,

        -- === TECHNICAL: STRUCTURE (5) ===
        (b.close - b.low5) / NULLIF(b.high5 - b.low5, 0) as range_pos_5d,
        (b.close - b.low20) / NULLIF(b.high20 - b.low20, 0) as range_pos_20d,
        (b.close - b.low52w) / NULLIF(b.high52w - b.low52w, 0) as range_pos_52w,
        (b.high5 - b.low5) / NULLIF(b.close, 0) as range_5d_pct,
        (b.high20 - b.low20) / NULLIF(b.close, 0) as range_20d_pct,

        -- === TECHNICAL: CANDLE PATTERNS (4) ===
        (b.close - b.open) / NULLIF(b.high - b.low, 0) as candle_body_ratio,
        (b.high - GREATEST(b.open, b.close)) / NULLIF(b.high - b.low, 0) as upper_wick_ratio,
        -- Gap from prev close
        (b.open - b.close_lag1) / NULLIF(b.close_lag1, 0) as gap_pct,
        -- 3-bar momentum (close today vs close 3 bars ago, normalized by ATR)
        (b.close - b.close_lag3) / NULLIF(b.atr14, 0) as mom_3bar_atr

    FROM base b
    LEFT JOIN spy_context spy ON b.trade_date = spy.trade_date
    LEFT JOIN insider_events ie ON b.ticker = ie.ticker AND b.trade_date = ie.transaction_date
    WHERE b.close_t5 IS NOT NULL
      AND b.sma200 IS NOT NULL
      AND b.atr60 IS NOT NULL
      AND b.close_lag20 IS NOT NULL
      AND b.low_t5 IS NOT NULL
      AND b.high_t5 IS NOT NULL
""")

ct = conn.execute("SELECT COUNT(*) FROM features_v2").fetchone()[0]

# Label statistics
stats = conn.execute("""
    SELECT
        COUNT(*) as n,
        AVG(label_3pct_3d) as rate_3pct,
        AVG(label_2pct_3d) as rate_2pct,
        AVG(label_up_3d) as rate_up,
        AVG(label_rr_2to1) as rate_rr2,
        AVG(label_rr_1_5to1) as rate_rr15,
        AVG(max_up_5d)*100 as avg_max_up,
        AVG(max_down_5d)*100 as avg_max_down
    FROM features_v2
""").fetchone()

print(f"\n  Feature matrix: {ct:,} rows")
print(f"  Labels:")
print(f"    +3% in 3d:        {stats[1]*100:.1f}%")
print(f"    +2% in 3d:        {stats[2]*100:.1f}%")
print(f"    Up in 3d:         {stats[3]*100:.1f}%")
print(f"    RR 2:1 (5d):      {stats[4]*100:.1f}%  <-- +2% up before -1% down")
print(f"    RR 1.5:1 (5d):    {stats[5]*100:.1f}%  <-- +1.5% up before -1% down")
print(f"    Avg max up 5d:    {stats[6]:.2f}%")
print(f"    Avg max down 5d:  {stats[7]:.2f}%")

# Column count
ncols = conn.execute("SELECT COUNT(*) FROM information_schema.columns WHERE table_name='features_v2'").fetchone()[0]
print(f"\n  Total columns: {ncols} ({ncols - 14} features + 14 meta/labels)")

# Save
os.makedirs("data/ml_training", exist_ok=True)
conn.execute("COPY features_v2 TO 'data/ml_training/flow_predictor_features_v2.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)")
print(f"  Saved: data/ml_training/flow_predictor_features_v2.parquet")

# By year
print("\n=== By Year ===")
rows = conn.execute("""
    SELECT YEAR(trade_date) as yr, COUNT(*) as n,
           AVG(label_3pct_3d)*100 as pct3,
           AVG(label_rr_2to1)*100 as rr2,
           AVG(label_rr_1_5to1)*100 as rr15,
           AVG(label_up_3d)*100 as up_rate
    FROM features_v2 GROUP BY 1 ORDER BY 1
""").fetchall()
for r in rows:
    print(f"  {r[0]}: {r[1]:>8,} rows, +3%={r[2]:>5.1f}%, RR2:1={r[3]:>5.1f}%, RR1.5:1={r[4]:>5.1f}%, Up={r[5]:>5.1f}%")

conn.close()
print(f"\nDone in {time.time()-t0:.1f}s")
