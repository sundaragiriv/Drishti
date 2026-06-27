"""Intraday Strategy Analysis — Where did these strategies ACTUALLY make money?

Strategies tested:
  1. ORB (Opening Range Breakout) — breakout from first 15/30 min range
  2. VWAP Mean Reversion — buy/sell at VWAP deviations
  3. Bollinger Band — squeeze breakouts and mean reversion touches
  4. SMA Crossover — intraday EMA crosses
  5. RSI — oversold bounces and overbought fades

Approach: Not forward-testing fixed rules. Instead:
  - Identify all situations where each setup occurred
  - Measure what actually happened: MFE (max favorable), MAE (max adverse)
  - Find the REALISTIC target before the move reversed
  - Determine which conditions produced the best outcomes

Data: fact_intraday_bars (5-min bars, May 2024 - Feb 2025)
      fact_intraday_features (daily intraday summary)
"""
import time
import numpy as np
import pandas as pd
import duckdb

t0 = time.time()
DB = "data/warehouse/sec_intel.duckdb"
conn = duckdb.connect(DB, read_only=True)

# ---------------------------------------------------------------
# 1. Load intraday features (already computed daily summaries)
# ---------------------------------------------------------------
print("Loading intraday features...")
feats = conn.execute("""
    SELECT * FROM fact_intraday_features
    WHERE trade_date >= '2024-05-15'
""").fetchdf()
feats["trade_date"] = pd.to_datetime(feats["trade_date"])
print(f"  {len(feats):,} feature rows, {feats['ticker'].nunique()} tickers")

# ---------------------------------------------------------------
# 2. Load intraday bars for detailed analysis (RTH only: 9:30-16:00)
# ---------------------------------------------------------------
print("Loading intraday bars (RTH only)...")
bars = conn.execute("""
    SELECT ticker, bar_time, open, high, low, close, volume, vwap
    FROM fact_intraday_bars
    WHERE CAST(bar_time AS TIME) >= '09:30:00'
      AND CAST(bar_time AS TIME) < '16:00:00'
    ORDER BY ticker, bar_time
""").fetchdf()
bars["bar_time"] = pd.to_datetime(bars["bar_time"])
bars["trade_date"] = bars["bar_time"].dt.date
bars["bar_time_only"] = bars["bar_time"].dt.time
print(f"  {len(bars):,} RTH bars")

conn.close()

# ---------------------------------------------------------------
# 3. ORB (Opening Range Breakout) Analysis
# ---------------------------------------------------------------
print(f"\n{'='*100}")
print("  STRATEGY 1: OPENING RANGE BREAKOUT (ORB)")
print(f"{'='*100}")

# Use fact_intraday_features which has or_high, or_low, or_breakout, etc.
orb = feats[feats["or_high"].notna() & feats["or_low"].notna()].copy()
orb["or_size"] = orb["or_high"] - orb["or_low"]
orb["or_size_pct"] = orb["or_size"] / orb["open_930"]

# ORB long: price breaks above opening range high
# Result: how far did it go (eod_close vs entry at or_high)
orb_long = orb[orb["or_breakout"] == 1].copy()
orb_long["entry"] = orb_long["or_high"]
orb_long["stop"] = orb_long["or_low"]
orb_long["risk"] = orb_long["entry"] - orb_long["stop"]
orb_long["result_pct"] = (orb_long["eod_close"] - orb_long["entry"]) / orb_long["entry"] * 100
orb_long["result_R"] = (orb_long["eod_close"] - orb_long["entry"]) / orb_long["risk"]

# Max favorable: day_high - entry
orb_long["mfe_R"] = (orb_long["day_high"] - orb_long["entry"]) / orb_long["risk"]
# Max adverse: entry - day_low (after breakout)
orb_long["mae_R"] = np.maximum(0, (orb_long["entry"] - orb_long["day_low"]) / orb_long["risk"])

# Filter valid trades (risk > 0)
orb_long = orb_long[orb_long["risk"] > 0].copy()

if len(orb_long) > 50:
    print(f"\n  ORB LONG Breakouts: {len(orb_long):,} signals")
    print(f"  Avg result: {orb_long['result_pct'].mean():+.2f}% | Median: {orb_long['result_pct'].median():+.2f}%")
    print(f"  Avg MFE:    {orb_long['mfe_R'].mean():.2f}R | Avg MAE: {orb_long['mae_R'].mean():.2f}R")
    print(f"  Win rate (close > entry): {(orb_long['result_R'] > 0).mean()*100:.1f}%")

    print(f"\n  {'R Target':>10} {'Hit%':>7} {'N':>7} {'Avg MFE':>8} {'Avg EOD R':>10}")
    for target_r in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]:
        hit = (orb_long["mfe_R"] >= target_r).mean()
        n = (orb_long["mfe_R"] >= target_r).sum()
        avg_mfe_hit = orb_long[orb_long["mfe_R"] >= target_r]["mfe_R"].mean() if n > 0 else 0
        avg_eod = orb_long[orb_long["mfe_R"] >= target_r]["result_R"].mean() if n > 0 else 0
        print(f"  {target_r:>8.1f}R {hit*100:>6.1f}% {n:>7,} {avg_mfe_hit:>7.2f}R {avg_eod:>+9.2f}R")

    # Conditions that improve ORB
    print(f"\n  ORB LONG by OR Size:")
    print(f"  {'OR Size':>15} {'N':>7} {'Win%':>6} {'1R Hit':>7} {'2R Hit':>7} {'Avg R':>7}")
    for label, lo, hi in [("Tight <0.5%", 0, 0.005), ("Medium 0.5-1%", 0.005, 0.01),
                           ("Wide 1-2%", 0.01, 0.02), ("Very wide >2%", 0.02, 1.0)]:
        sub = orb_long[(orb_long["or_size_pct"] >= lo) & (orb_long["or_size_pct"] < hi)]
        if len(sub) < 20:
            continue
        wr = (sub["result_R"] > 0).mean()
        h1 = (sub["mfe_R"] >= 1.0).mean()
        h2 = (sub["mfe_R"] >= 2.0).mean()
        ar = sub["result_R"].mean()
        print(f"  {label:>15} {len(sub):>7,} {wr*100:>5.1f}% {h1*100:>6.1f}% {h2*100:>6.1f}% {ar:>+6.2f}")

    # Volume ratio impact
    print(f"\n  ORB LONG by Volume Ratio (OR vol vs 20d avg):")
    print(f"  {'Vol Ratio':>15} {'N':>7} {'Win%':>6} {'1R Hit':>7} {'2R Hit':>7} {'Avg R':>7}")
    for label, lo, hi in [("<1x (quiet)", 0, 1.0), ("1-2x", 1.0, 2.0),
                           ("2-3x (active)", 2.0, 3.0), (">3x (surge)", 3.0, 100)]:
        sub = orb_long[(orb_long["volume_ratio"] >= lo) & (orb_long["volume_ratio"] < hi)]
        if len(sub) < 20:
            continue
        wr = (sub["result_R"] > 0).mean()
        h1 = (sub["mfe_R"] >= 1.0).mean()
        h2 = (sub["mfe_R"] >= 2.0).mean()
        ar = sub["result_R"].mean()
        print(f"  {label:>15} {len(sub):>7,} {wr*100:>5.1f}% {h1*100:>6.1f}% {h2*100:>6.1f}% {ar:>+6.2f}")

    # Gap impact
    print(f"\n  ORB LONG by Gap:")
    print(f"  {'Gap':>15} {'N':>7} {'Win%':>6} {'1R Hit':>7} {'2R Hit':>7} {'Avg R':>7}")
    for label, lo, hi in [("Gap down <-1%", -100, -0.01), ("Small gap -1 to 0", -0.01, 0),
                           ("Small gap 0 to 1%", 0, 0.01), ("Gap up 1-3%", 0.01, 0.03),
                           ("Big gap >3%", 0.03, 100)]:
        sub = orb_long[(orb_long["gap_pct"] >= lo) & (orb_long["gap_pct"] < hi)]
        if len(sub) < 20:
            continue
        wr = (sub["result_R"] > 0).mean()
        h1 = (sub["mfe_R"] >= 1.0).mean()
        h2 = (sub["mfe_R"] >= 2.0).mean()
        ar = sub["result_R"].mean()
        print(f"  {label:>15} {len(sub):>7,} {wr*100:>5.1f}% {h1*100:>6.1f}% {h2*100:>6.1f}% {ar:>+6.2f}")

# ---------------------------------------------------------------
# 4. VWAP Mean Reversion Analysis
# ---------------------------------------------------------------
print(f"\n{'='*100}")
print("  STRATEGY 2: VWAP MEAN REVERSION")
print(f"{'='*100}")

# Analyze: when price deviates from VWAP, how often does it revert?
vwap = feats[feats["max_vwap_dev_below"].notna()].copy()

# Long setup: price drops below VWAP by X%
# Result: does it come back to VWAP (or above)?
print(f"\n  VWAP Deviation Analysis (N={len(vwap):,}):")
print(f"  {'Max Dev Below':>15} {'N':>7} {'Reverted%':>10} {'Close>VWAP%':>12} {'AvgEODvVWAP':>12}")
for label, lo, hi in [("0.5-1%", 0.005, 0.01), ("1-2%", 0.01, 0.02),
                       ("2-3%", 0.02, 0.03), ("3-5%", 0.03, 0.05), (">5%", 0.05, 1.0)]:
    sub = vwap[(vwap["max_vwap_dev_below"] >= lo) & (vwap["max_vwap_dev_below"] < hi)]
    if len(sub) < 20:
        continue
    reverted = (sub["eod_vs_vwap_pct"] >= -0.001).mean()  # close at or above VWAP
    above = (sub["eod_vs_vwap_pct"] > 0).mean()
    avg_eod = sub["eod_vs_vwap_pct"].mean() * 100
    print(f"  {label:>15} {len(sub):>7,} {reverted*100:>9.1f}% {above*100:>11.1f}% {avg_eod:>+11.2f}%")

# VWAP cross count — how tradeable is the cross?
print(f"\n  VWAP Cross Count distribution:")
print(f"  {'Crosses':>10} {'N':>7} {'CloseAboveVWAP%':>16} {'AvgEODvOpen%':>13}")
for lo, hi in [(0, 3), (3, 6), (6, 10), (10, 20), (20, 100)]:
    sub = vwap[(vwap["vwap_cross_count"] >= lo) & (vwap["vwap_cross_count"] < hi)]
    if len(sub) < 20:
        continue
    above = (sub["eod_vs_vwap_pct"] > 0).mean()
    eod_open = sub["eod_vs_open_pct"].mean() * 100
    print(f"  {lo}-{hi:>3}      {len(sub):>7,} {above*100:>15.1f}% {eod_open:>+12.2f}%")

# Condition: VWAP MR works better with conviction/squeeze
print(f"\n  VWAP MR by Conviction Score (entry when price < VWAP by >1%):")
mr_setup = vwap[vwap["max_vwap_dev_below"] >= 0.01].copy()
print(f"  {'Conviction':>15} {'N':>7} {'Reverted%':>10} {'Close>VWAP%':>12} {'AvgEOD%':>8}")
for label, lo, hi in [("0-30", 0, 30), ("30-50", 30, 50), ("50-65", 50, 65),
                       ("65-80", 65, 80), ("80+", 80, 101)]:
    sub = mr_setup[(mr_setup["conviction_score"] >= lo) & (mr_setup["conviction_score"] < hi)]
    if len(sub) < 20:
        continue
    reverted = (sub["eod_vs_vwap_pct"] >= -0.001).mean()
    above = (sub["eod_vs_vwap_pct"] > 0).mean()
    eod = sub["eod_vs_open_pct"].mean() * 100
    print(f"  {label:>15} {len(sub):>7,} {reverted*100:>9.1f}% {above*100:>11.1f}% {eod:>+7.2f}%")

# ---------------------------------------------------------------
# 5. RSI Analysis
# ---------------------------------------------------------------
print(f"\n{'='*100}")
print("  STRATEGY 3: RSI OVERSOLD/OVERBOUGHT")
print(f"{'='*100}")

rsi = feats[feats["rsi_14_min"].notna()].copy()

# RSI oversold bounce: min RSI < 30, then recovery
print(f"\n  RSI Min Levels (intraday RSI reached this low):")
print(f"  {'RSI Min':>12} {'N':>7} {'CloseAboveOpen%':>16} {'AvgEOD%':>8} {'CloseNearHOD%':>14}")
for label, lo, hi in [("<20 (extreme)", 0, 20), ("20-25", 20, 25), ("25-30", 25, 30),
                       ("30-35", 30, 35), ("35-40", 35, 40)]:
    sub = rsi[(rsi["rsi_14_min"] >= lo) & (rsi["rsi_14_min"] < hi)]
    if len(sub) < 20:
        continue
    above_open = (sub["eod_vs_open_pct"] > 0).mean()
    avg_eod = sub["eod_vs_open_pct"].mean() * 100
    near_hod = sub["eod_close_near_hod"].mean() * 100
    print(f"  {label:>12} {len(sub):>7,} {above_open*100:>15.1f}% {avg_eod:>+7.2f}% {near_hod:>13.1f}%")

# RSI overbought fade: max RSI > 70
print(f"\n  RSI Max Levels (intraday RSI reached this high):")
print(f"  {'RSI Max':>12} {'N':>7} {'CloseBelowOpen%':>16} {'AvgEOD%':>8} {'CloseNearLOD%':>14}")
for label, lo, hi in [("60-65", 60, 65), ("65-70", 65, 70), ("70-75", 70, 75),
                       ("75-80", 75, 80), (">80 (extreme)", 80, 101)]:
    sub = rsi[(rsi["rsi_14_max"] >= lo) & (rsi["rsi_14_max"] < hi)]
    if len(sub) < 20:
        continue
    below_open = (sub["eod_vs_open_pct"] < 0).mean()
    avg_eod = sub["eod_vs_open_pct"].mean() * 100
    near_lod = sub["eod_close_near_lod"].mean() * 100
    print(f"  {label:>12} {len(sub):>7,} {below_open*100:>15.1f}% {avg_eod:>+7.2f}% {near_lod:>13.1f}%")

# ---------------------------------------------------------------
# 6. Intraday bar-level analysis for VWAP MR
# ---------------------------------------------------------------
print(f"\n{'='*100}")
print("  STRATEGY 4: BAR-LEVEL VWAP MEAN REVERSION (5-min bars)")
print(f"{'='*100}")

# For each day, find bars where price is significantly below VWAP
# Then track what happened next (recovery to VWAP)
print("  Computing bar-level VWAP deviations...")

# Filter to bars with VWAP
bars_vwap = bars[bars["vwap"].notna() & (bars["vwap"] > 0)].copy()
bars_vwap["dev_from_vwap"] = (bars_vwap["close"] - bars_vwap["vwap"]) / bars_vwap["vwap"]

# For each ticker-day, compute running VWAP deviation stats
# Focus on setups where price drops 1%+ below VWAP between 10:00-14:00
from datetime import time as dtime
setup_mask = (
    (bars_vwap["dev_from_vwap"] <= -0.01) &
    (bars_vwap["bar_time_only"] >= dtime(10, 0)) &
    (bars_vwap["bar_time_only"] <= dtime(14, 0))
)
mr_bars = bars_vwap[setup_mask].copy()
print(f"  {len(mr_bars):,} bars with price >= 1% below VWAP (10:00-14:00)")

# For each setup bar, find max recovery in next 12 bars (1 hour)
# and max further drop
if len(mr_bars) > 1000:
    # Sample approach: take first occurrence per ticker-day
    mr_first = mr_bars.groupby(["ticker", "trade_date"]).first().reset_index()
    print(f"  {len(mr_first):,} unique ticker-day setups")

    # Merge with daily features to get EOD outcome
    mr_first["trade_date_dt"] = pd.to_datetime(mr_first["trade_date"])
    mr_first = mr_first.merge(
        feats[["ticker", "trade_date", "eod_close", "eod_vs_vwap_pct", "day_high", "day_low",
               "conviction_score", "squeeze_score", "accum_phase"]],
        left_on=["ticker", "trade_date_dt"],
        right_on=["ticker", "trade_date"],
        how="left",
        suffixes=("", "_feat")
    )

    mr_first["entry"] = mr_first["close"]
    mr_first["vwap_target"] = mr_first["vwap"]
    mr_first["risk_pct"] = -mr_first["dev_from_vwap"]  # how far below VWAP

    # Did it recover to VWAP by EOD?
    mr_first["recovered"] = (mr_first["eod_close"] >= mr_first["vwap"]).astype(int)
    # Did it recover past entry?
    mr_first["profitable"] = (mr_first["eod_close"] > mr_first["entry"]).astype(int)
    # Max upside: day_high - entry
    mr_first["max_up_pct"] = (mr_first["day_high"] - mr_first["entry"]) / mr_first["entry"] * 100

    print(f"\n  VWAP MR Long (entry when >=1% below VWAP, 10:00-14:00):")
    print(f"  Total: {len(mr_first):,} setups")
    print(f"  Recovered to VWAP by EOD: {mr_first['recovered'].mean()*100:.1f}%")
    print(f"  Profitable by EOD: {mr_first['profitable'].mean()*100:.1f}%")
    print(f"  Avg max upside: {mr_first['max_up_pct'].mean():.2f}%")

    # By deviation size
    print(f"\n  By Deviation Size:")
    print(f"  {'Dev Below VWAP':>15} {'N':>7} {'Recovered%':>10} {'Profitable%':>12} {'AvgMaxUp%':>10}")
    for label, lo, hi in [("1-1.5%", 0.01, 0.015), ("1.5-2%", 0.015, 0.02),
                           ("2-3%", 0.02, 0.03), ("3-5%", 0.03, 0.05), (">5%", 0.05, 1.0)]:
        sub = mr_first[(mr_first["risk_pct"] >= lo) & (mr_first["risk_pct"] < hi)]
        if len(sub) < 20:
            continue
        rec = sub["recovered"].mean()
        prof = sub["profitable"].mean()
        maxup = sub["max_up_pct"].mean()
        print(f"  {label:>15} {len(sub):>7,} {rec*100:>9.1f}% {prof*100:>11.1f}% {maxup:>9.2f}%")

    # By time of entry
    print(f"\n  By Entry Time:")
    print(f"  {'Time':>12} {'N':>7} {'Recovered%':>10} {'Profitable%':>12} {'AvgMaxUp%':>10}")
    mr_first["entry_hour"] = pd.to_datetime(mr_first["bar_time"]).dt.hour
    for hour in [10, 11, 12, 13, 14]:
        sub = mr_first[mr_first["entry_hour"] == hour]
        if len(sub) < 20:
            continue
        rec = sub["recovered"].mean()
        prof = sub["profitable"].mean()
        maxup = sub["max_up_pct"].mean()
        print(f"  {hour}:00-{hour}:59 {len(sub):>7,} {rec*100:>9.1f}% {prof*100:>11.1f}% {maxup:>9.2f}%")

    # By conviction
    print(f"\n  By Conviction Score:")
    print(f"  {'Conviction':>12} {'N':>7} {'Recovered%':>10} {'Profitable%':>12} {'AvgMaxUp%':>10}")
    for label, lo, hi in [("0-30", 0, 30), ("30-50", 30, 50), ("50-65", 50, 65),
                           ("65-80", 65, 80), ("80+", 80, 101)]:
        sub = mr_first[(mr_first["conviction_score"] >= lo) & (mr_first["conviction_score"] < hi)]
        if len(sub) < 20:
            continue
        rec = sub["recovered"].mean()
        prof = sub["profitable"].mean()
        maxup = sub["max_up_pct"].mean()
        print(f"  {label:>12} {len(sub):>7,} {rec*100:>9.1f}% {prof*100:>11.1f}% {maxup:>9.2f}%")

# ---------------------------------------------------------------
# 7. Bollinger Band Analysis from 5-min bars
# ---------------------------------------------------------------
print(f"\n{'='*100}")
print("  STRATEGY 5: BOLLINGER BAND ANALYSIS (from daily features)")
print(f"{'='*100}")

# Use OR range vs ATR as a proxy for compression (similar to BB squeeze)
bb = feats[feats["or_range_vs_atr"].notna()].copy()

print(f"\n  OR Range vs ATR (squeeze proxy):")
print(f"  {'OR/ATR':>12} {'N':>7} {'Breakout%':>10} {'AvgEODvOpen%':>13} {'CloseNearHOD%':>14}")
for label, lo, hi in [("<0.3 (squeeze)", 0, 0.3), ("0.3-0.5", 0.3, 0.5),
                       ("0.5-0.8", 0.5, 0.8), ("0.8-1.2 (normal)", 0.8, 1.2),
                       (">1.2 (wide)", 1.2, 10)]:
    sub = bb[(bb["or_range_vs_atr"] >= lo) & (bb["or_range_vs_atr"] < hi)]
    if len(sub) < 20:
        continue
    breakout = sub["or_breakout"].mean() if "or_breakout" in sub.columns else 0
    eod_open = sub["eod_vs_open_pct"].mean() * 100
    near_hod = sub["eod_close_near_hod"].mean() * 100
    print(f"  {label:>12} {len(sub):>7,} {breakout*100:>9.1f}% {eod_open:>+12.2f}% {near_hod:>13.1f}%")

# Consolidation bars (intraday compression)
print(f"\n  Consolidation Bars (tight range periods):")
print(f"  {'Consol Bars':>12} {'N':>7} {'Breakout%':>10} {'AvgEODvOpen%':>13}")
for lo, hi in [(0, 5), (5, 10), (10, 15), (15, 25), (25, 100)]:
    sub = bb[(bb["consolidation_bars"] >= lo) & (bb["consolidation_bars"] < hi)]
    if len(sub) < 20:
        continue
    breakout = sub["or_breakout"].mean()
    eod_open = sub["eod_vs_open_pct"].mean() * 100
    print(f"  {lo:>3}-{hi:>3}      {len(sub):>7,} {breakout*100:>9.1f}% {eod_open:>+12.2f}%")

# ---------------------------------------------------------------
# 8. Combined strategy analysis: What ACTUALLY works intraday?
# ---------------------------------------------------------------
print(f"\n{'='*100}")
print("  COMBINED: WHAT CONDITIONS PRODUCE THE BEST INTRADAY OUTCOMES?")
print(f"{'='*100}")

combo = feats[feats["eod_vs_open_pct"].notna()].copy()

combos = {
    "Base (all)":                      pd.Series(True, index=combo.index),
    "Gap up + ORB breakout":           (combo["gap_pct"] > 0) & (combo["or_breakout"] == 1),
    "Gap down + ORB breakout":         (combo["gap_pct"] < 0) & (combo["or_breakout"] == 1),
    "Gap up + high vol":               (combo["gap_pct"] > 0.01) & (combo["volume_ratio"] > 2),
    "Squeeze OR + breakout":           (combo["or_range_vs_atr"] < 0.5) & (combo["or_breakout"] == 1),
    "Low RSI + high conv":             (combo["rsi_14_min"] < 30) & (combo["conviction_score"] >= 50),
    "VWAP dev >1.5% + conv>=50":       (combo["max_vwap_dev_below"] >= 0.015) & (combo["conviction_score"] >= 50),
    "High vol + squeeze":              (combo["volume_ratio"] > 2) & (combo["squeeze_score"] >= 50),
    "ORB break + high vol + gap up":   (combo["or_breakout"] == 1) & (combo["volume_ratio"] > 1.5) & (combo["gap_pct"] > 0),
    "Accum + ORB break":              (combo["accum_phase"].isin(["EARLY_ACCUM", "ACTIVE_ACCUM", "LATE_ACCUM"])) & (combo["or_breakout"] == 1),
    "Accum + VWAP revert":            (combo["accum_phase"].isin(["EARLY_ACCUM", "ACTIVE_ACCUM", "LATE_ACCUM"])) & (combo["max_vwap_dev_below"] >= 0.01),
    "Conv>=65 + ORB + vol>1.5":        (combo["conviction_score"] >= 65) & (combo["or_breakout"] == 1) & (combo["volume_ratio"] > 1.5),
    "Vol spike + low RSI":             (combo["volume_ratio"] > 2) & (combo["rsi_14_min"] < 35),
    "Tight OR + gap down + vol":       (combo["or_range_vs_atr"] < 0.5) & (combo["gap_pct"] < -0.005) & (combo["volume_ratio"] > 1.5),
    "Hammer candles + low RSI":        (combo["candle_hammer_count_5m"] >= 2) & (combo["rsi_14_min"] < 35),
    "Vol climax reversal":             combo["volume_climax_reversal"] == 1,
    "Engulfing bull near VWAP":        (combo["candle_engulf_bull_count_5m"] >= 1) & (combo["candle_reversal_near_vwap"] >= 1),
    "Vol spike near VWAP":            (combo["volume_spike_near_vwap"] >= 1),
}

print(f"\n  {'Setup':<38} {'N':>7} {'Win%':>6} {'AvgRet%':>8} {'MedianRet%':>10} {'NearHOD%':>9} {'NearLOD%':>9}")
print(f"  {'-'*100}")

results = []
for name, mask in combos.items():
    sub = combo[mask]
    if len(sub) < 30:
        continue
    wr = (sub["eod_vs_open_pct"] > 0).mean()
    avg = sub["eod_vs_open_pct"].mean() * 100
    med = sub["eod_vs_open_pct"].median() * 100
    hod = sub["eod_close_near_hod"].mean() * 100
    lod = sub["eod_close_near_lod"].mean() * 100
    results.append((name, len(sub), wr, avg, med, hod, lod))
    print(f"  {name:<38} {len(sub):>7,} {wr*100:>5.1f}% {avg:>+7.2f}% {med:>+9.2f}% {hod:>8.1f}% {lod:>8.1f}%")

# Sort by win rate
results.sort(key=lambda x: -x[2])
print(f"\n  TOP 5 BY WIN RATE:")
for name, n, wr, avg, med, hod, lod in results[:5]:
    print(f"  {name:<38} Win={wr*100:.1f}%, Avg={avg:+.2f}%, N={n}")

# Sort by avg return
results.sort(key=lambda x: -x[3])
print(f"\n  TOP 5 BY AVG RETURN:")
for name, n, wr, avg, med, hod, lod in results[:5]:
    print(f"  {name:<38} Avg={avg:+.2f}%, Win={wr*100:.1f}%, N={n}")

# ---------------------------------------------------------------
# 9. Realistic target analysis
# ---------------------------------------------------------------
print(f"\n{'='*100}")
print("  REALISTIC INTRADAY TARGETS — Before the move reverses")
print(f"{'='*100}")

# Using first_30min_range as R-unit for intraday
intra_r = feats[feats["first_30min_range_pct"].notna() & (feats["first_30min_range_pct"] > 0)].copy()
intra_r["intra_R"] = intra_r["first_30min_range_pct"]  # using 30min range as 1R

# Day range in R-units
intra_r["day_range_R"] = intra_r["day_range"] / (intra_r["open_930"] * intra_r["intra_R"])
intra_r["eod_R"] = intra_r["eod_vs_open_pct"] / intra_r["intra_R"]

print(f"\n  Distribution of day range (in opening-range R-units):")
print(f"  Mean day range:  {intra_r['day_range_R'].mean():.2f}R")
print(f"  Median:          {intra_r['day_range_R'].median():.2f}R")
print(f"  75th pctl:       {intra_r['day_range_R'].quantile(0.75):.2f}R")
print(f"  90th pctl:       {intra_r['day_range_R'].quantile(0.90):.2f}R")

print(f"\n  Probability of reaching X R-units from open:")
for r_target in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]:
    pct = (intra_r["day_range_R"] >= r_target).mean()
    print(f"  {r_target:.1f}R: {pct*100:.1f}%")

print(f"\nDone in {time.time()-t0:.1f}s")
