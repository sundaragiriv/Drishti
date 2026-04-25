"""Swing Trade R-Multiple Analysis — Using ACTUAL historical data.

Data sources:
  - fact_daily_prices: OHLCV + technicals computed in pandas
  - fact_form4_transactions: insider buy/sell signals
  - fact_insider_outcomes: pre-computed forward returns for insider buys
  - fact_13f_positions: institutional ownership (quarterly)

Holding periods: 10d, 20d, 30d
Stop = 1.5 * ATR(14), Targets = 1R, 2R, 3R
"""
import time
import numpy as np
import pandas as pd
import duckdb

t0 = time.time()
DB = "data/warehouse/sec_intel.duckdb"
conn = duckdb.connect(DB, read_only=True)

# ---------------------------------------------------------------
# 1. Load prices + compute technicals
# ---------------------------------------------------------------
print("Loading prices...")
prices = conn.execute("""
    SELECT ticker, trade_date, open, high, low, close, volume
    FROM fact_daily_prices
    WHERE trade_date >= '2018-01-01'
      AND close > 2.0 AND volume > 50000
    ORDER BY ticker, trade_date
""").fetchdf()
prices["trade_date"] = pd.to_datetime(prices["trade_date"])
print(f"  {len(prices):,} rows")

# ---------------------------------------------------------------
# 2. Load insider buy signals (Form4 purchases)
# ---------------------------------------------------------------
print("Loading Form4 insider buys...")
form4 = conn.execute("""
    SELECT ticker, transaction_date, insider_name, insider_role,
           shares, price AS txn_price, direction
    FROM fact_form4_transactions
    WHERE transaction_code = 'P'
      AND transaction_date >= '2018-01-01'
      AND price > 0 AND shares > 0
""").fetchdf()
form4["transaction_date"] = pd.to_datetime(form4["transaction_date"])
print(f"  {len(form4):,} insider buy transactions")

# Rolling insider activity per ticker per day
insider_daily = form4.groupby(["ticker", "transaction_date"]).agg(
    insider_buys=("insider_name", "count"),
    distinct_insiders=("insider_name", "nunique"),
    insider_dollar=("shares", lambda x: (x * form4.loc[x.index, "txn_price"]).sum()),
).reset_index()
insider_daily.rename(columns={"transaction_date": "trade_date"}, inplace=True)

# ---------------------------------------------------------------
# 3. Load 13F institutional ownership (quarterly snapshots)
# ---------------------------------------------------------------
print("Loading 13F institutional data...")
inst = conn.execute("""
    SELECT ticker, report_period,
           COUNT(DISTINCT manager_cik) AS inst_count,
           SUM(shares) AS inst_shares
    FROM fact_13f_positions
    WHERE ticker IS NOT NULL AND ticker != ''
      AND report_period >= '2018-01-01'
    GROUP BY ticker, report_period
""").fetchdf()
inst["report_period"] = pd.to_datetime(inst["report_period"])
print(f"  {len(inst):,} ticker-quarter combos")

# Compute QoQ change
inst = inst.sort_values(["ticker", "report_period"])
inst["prev_inst_count"] = inst.groupby("ticker")["inst_count"].shift(1)
inst["inst_change"] = inst["inst_count"] - inst["prev_inst_count"]
inst["inst_change_pct"] = inst["inst_change"] / inst["prev_inst_count"]
# Accumulation = 2+ quarters of rising institutional count
inst["inst_rising"] = (inst["inst_change"] > 0).astype(int)
inst["inst_streak"] = inst.groupby("ticker")["inst_rising"].transform(
    lambda x: x.groupby((x != x.shift()).cumsum()).cumsum()
)

conn.close()

# ---------------------------------------------------------------
# 4. Compute technicals on price data
# ---------------------------------------------------------------
print("Computing technicals...")
prices = prices.sort_values(["ticker", "trade_date"]).reset_index(drop=True)
g = prices.groupby("ticker")

# ATR(14)
prices["prev_close"] = g["close"].shift(1)
prices["tr"] = np.maximum(
    prices["high"] - prices["low"],
    np.maximum(
        (prices["high"] - prices["prev_close"]).abs(),
        (prices["low"] - prices["prev_close"]).abs()
    )
)
prices["atr14"] = g["tr"].transform(lambda x: x.rolling(14, min_periods=10).mean())

# SMAs
prices["sma20"] = g["close"].transform(lambda x: x.rolling(20).mean())
prices["sma50"] = g["close"].transform(lambda x: x.rolling(50).mean())
prices["sma200"] = g["close"].transform(lambda x: x.rolling(200, min_periods=150).mean())
prices["above_200"] = (prices["close"] > prices["sma200"]).astype(int)

# Pullback from SMA20
prices["pct_from_sma20"] = (prices["close"] - prices["sma20"]) / prices["sma20"]
prices["pullback_to_sma20"] = (prices["pct_from_sma20"].between(-0.05, 0.0)).astype(int)

# ATR compression
prices["atr50"] = g["tr"].transform(lambda x: x.rolling(50, min_periods=30).mean())
prices["atr_compressed"] = (prices["atr14"] < 0.85 * prices["atr50"]).astype(int)

# Volume trend
prices["vol_sma10"] = g["volume"].transform(lambda x: x.rolling(10).mean())
prices["vol_sma50"] = g["volume"].transform(lambda x: x.rolling(50).mean())
prices["vol_quiet"] = (prices["vol_sma10"] < 0.8 * prices["vol_sma50"]).astype(int)

# Momentum
prices["ret_20d_back"] = g["close"].transform(lambda x: x.pct_change(20))

# MA alignment (20 > 50 > 200)
prices["ma_aligned"] = ((prices["sma20"] > prices["sma50"]) & (prices["sma50"] > prices["sma200"])).astype(int)

# Forward max high / min low / close
for d in [10, 20, 30]:
    prices[f"max_high_{d}d"] = g["high"].transform(
        lambda x: x[::-1].rolling(d, min_periods=max(d-3, 5)).max()[::-1].shift(-1)
    )
    prices[f"min_low_{d}d"] = g["low"].transform(
        lambda x: x[::-1].rolling(d, min_periods=max(d-3, 5)).min()[::-1].shift(-1)
    )
    prices[f"close_{d}d"] = g["close"].transform(lambda x: x.shift(-d))

# Filter to 2019+ (need 2018 for lookback)
prices = prices[prices["trade_date"] >= "2019-01-01"].copy()
print(f"  {len(prices):,} rows after technicals")

# ---------------------------------------------------------------
# 5. Build 30-day rolling insider features
# ---------------------------------------------------------------
print("Building rolling insider features...")

# Merge same-day insider buys
df = prices.merge(insider_daily, on=["ticker", "trade_date"], how="left")
df["insider_buys"] = df["insider_buys"].fillna(0)
df["distinct_insiders"] = df["distinct_insiders"].fillna(0)
df["insider_dollar"] = df["insider_dollar"].fillna(0)

# 30-day rolling insider count using merge_asof won't work well, use a window approach
# Pre-compute: for each ticker-date, count distinct insiders in prior 30 days
print("  Computing 30d rolling insider clusters...")
form4_buys = form4[["ticker", "transaction_date", "insider_name"]].copy()
form4_buys = form4_buys.rename(columns={"transaction_date": "buy_date"})

# Create a cross between prices and form4 — only for tickers with buys
tickers_with_buys = form4_buys["ticker"].unique()
price_subset = df[df["ticker"].isin(tickers_with_buys)][["ticker", "trade_date"]].copy()

# More efficient: for each Form4 buy, mark all price dates within +30d
insider_events = form4_buys.merge(
    price_subset,
    on="ticker",
    how="inner"
)
insider_events = insider_events[
    (insider_events["trade_date"] >= insider_events["buy_date"]) &
    (insider_events["trade_date"] <= insider_events["buy_date"] + pd.Timedelta(days=30))
]
insider_30d = insider_events.groupby(["ticker", "trade_date"]).agg(
    insiders_30d=("insider_name", "nunique"),
    insider_txns_30d=("insider_name", "count"),
).reset_index()

df = df.merge(insider_30d, on=["ticker", "trade_date"], how="left")
df["insiders_30d"] = df["insiders_30d"].fillna(0)
df["insider_txns_30d"] = df["insider_txns_30d"].fillna(0)

# ---------------------------------------------------------------
# 6. Map quarterly 13F data to daily prices
# ---------------------------------------------------------------
print("Mapping institutional data to daily...")
# Map quarterly 13F to daily via quarter assignment
inst_quarters = inst[["ticker", "report_period", "inst_count", "inst_change", "inst_change_pct", "inst_streak"]].copy()

# Assign each price date to the most recent completed quarter
# Q1 (Mar 31) data available ~May, Q2 (Jun 30) ~Aug, Q3 (Sep 30) ~Nov, Q4 (Dec 31) ~Feb
# Use the quarter ending before the trade_date minus ~45 day filing lag
def assign_quarter(trade_date):
    # Which quarter's data would be available by this date?
    # 13F filed ~45 days after quarter end
    ref = trade_date - pd.Timedelta(days=45)
    q = ref.quarter
    y = ref.year
    qend = pd.Timestamp(year=y, month=q*3, day={1:31, 2:30, 3:30, 4:31}[q])
    return qend

df["mapped_quarter"] = df["trade_date"].apply(assign_quarter)
inst_quarters = inst_quarters.rename(columns={"report_period": "mapped_quarter"})
df = df.merge(inst_quarters, on=["ticker", "mapped_quarter"], how="left")
df.drop(columns=["mapped_quarter"], inplace=True)
df["inst_count"] = df["inst_count"].fillna(0)
df["inst_change"] = df["inst_change"].fillna(0)
df["inst_streak"] = df["inst_streak"].fillna(0)
df["inst_accumulating"] = (df["inst_streak"] >= 2).astype(int)  # 2+ quarters of rising

df["year"] = df["trade_date"].dt.year

# ---------------------------------------------------------------
# 7. R-multiple calculations
# ---------------------------------------------------------------
print("Computing R-multiples...")
df = df[df["atr14"] > 0].copy()
df["R_unit"] = 1.5 * df["atr14"]

for d in [10, 20, 30]:
    h = f"{d}d"
    df[f"mfe_{h}"] = (df[f"max_high_{h}"] - df["close"]) / df["R_unit"]
    df[f"mae_{h}"] = (df["close"] - df[f"min_low_{h}"]) / df["R_unit"]
    df[f"hit_1R_{h}"] = (df[f"mfe_{h}"] >= 1.0).astype(int)
    df[f"hit_2R_{h}"] = (df[f"mfe_{h}"] >= 2.0).astype(int)
    df[f"hit_3R_{h}"] = (df[f"mfe_{h}"] >= 3.0).astype(int)
    df[f"stopped_{h}"] = (df[f"mae_{h}"] >= 1.0).astype(int)
    df[f"ret_pct_{h}"] = np.where(
        df[f"close_{h}"].notna(),
        (df[f"close_{h}"] - df["close"]) / df["close"],
        np.nan
    )

print(f"  Final dataset: {len(df):,} rows")

# ---------------------------------------------------------------
# 8. Define conditions
# ---------------------------------------------------------------
conditions = {
    "Base (all)":                 pd.Series(True, index=df.index),
    # Insider signals
    "Insider buy (same day)":     df["insider_buys"] > 0,
    "Insider 1+ (30d)":          df["insiders_30d"] >= 1,
    "Insider 2+ cluster":        df["insiders_30d"] >= 2,
    "Insider 3+ cluster":        df["insiders_30d"] >= 3,
    # Institutional
    "Inst accumulating (2Q+)":   df["inst_accumulating"] == 1,
    "Inst count>=50":            df["inst_count"] >= 50,
    "Inst rising + Insider":     (df["inst_accumulating"] == 1) & (df["insiders_30d"] >= 1),
    "Inst rising + Insider 2+":  (df["inst_accumulating"] == 1) & (df["insiders_30d"] >= 2),
    # Technical
    "Above 200SMA":              df["above_200"] == 1,
    "Below 200SMA":              df["above_200"] == 0,
    "ATR compressed":            df["atr_compressed"] == 1,
    "Pullback to SMA20":         df["pullback_to_sma20"] == 1,
    "MA aligned (20>50>200)":    df["ma_aligned"] == 1,
    "Vol quiet":                 df["vol_quiet"] == 1,
    # Confluence combos
    "Insider + Above200":        (df["insiders_30d"] >= 1) & (df["above_200"] == 1),
    "Insider + Below200":        (df["insiders_30d"] >= 1) & (df["above_200"] == 0),
    "Insider + Compressed":      (df["insiders_30d"] >= 1) & (df["atr_compressed"] == 1),
    "Insider + Pullback20":      (df["insiders_30d"] >= 1) & (df["pullback_to_sma20"] == 1),
    "Insider + MA aligned":      (df["insiders_30d"] >= 1) & (df["ma_aligned"] == 1),
    "Insider2+ + Above200":      (df["insiders_30d"] >= 2) & (df["above_200"] == 1),
    "Insider2+ + Below200":      (df["insiders_30d"] >= 2) & (df["above_200"] == 0),
    "Insider2+ + Compressed":    (df["insiders_30d"] >= 2) & (df["atr_compressed"] == 1),
    "Insider2+ + Pullback20":    (df["insiders_30d"] >= 2) & (df["pullback_to_sma20"] == 1),
    "Insider2+ + MA aligned":    (df["insiders_30d"] >= 2) & (df["ma_aligned"] == 1),
    # Triple confluence
    "Ins + Above200 + Comp":     (df["insiders_30d"] >= 1) & (df["above_200"] == 1) & (df["atr_compressed"] == 1),
    "Ins + Above200 + Pullback": (df["insiders_30d"] >= 1) & (df["above_200"] == 1) & (df["pullback_to_sma20"] == 1),
    "Ins + Above200 + MA align": (df["insiders_30d"] >= 1) & (df["above_200"] == 1) & (df["ma_aligned"] == 1),
    "Ins + Below200 + Comp":     (df["insiders_30d"] >= 1) & (df["above_200"] == 0) & (df["atr_compressed"] == 1),
    "Ins + Below200 + Pullback": (df["insiders_30d"] >= 1) & (df["above_200"] == 0) & (df["pullback_to_sma20"] == 1),
    "Ins2 + Above200 + Comp":   (df["insiders_30d"] >= 2) & (df["above_200"] == 1) & (df["atr_compressed"] == 1),
    "Ins2 + Above200 + PB":     (df["insiders_30d"] >= 2) & (df["above_200"] == 1) & (df["pullback_to_sma20"] == 1),
    "Ins2 + Below200 + Comp":   (df["insiders_30d"] >= 2) & (df["above_200"] == 0) & (df["atr_compressed"] == 1),
    # Institutional + insider + technical
    "InstAcc + Ins + Above200":  (df["inst_accumulating"] == 1) & (df["insiders_30d"] >= 1) & (df["above_200"] == 1),
    "InstAcc + Ins + Comp":      (df["inst_accumulating"] == 1) & (df["insiders_30d"] >= 1) & (df["atr_compressed"] == 1),
    "InstAcc + Ins + PB":        (df["inst_accumulating"] == 1) & (df["insiders_30d"] >= 1) & (df["pullback_to_sma20"] == 1),
    "InstAcc + Ins + MA align":  (df["inst_accumulating"] == 1) & (df["insiders_30d"] >= 1) & (df["ma_aligned"] == 1),
    "InstAcc + Ins2 + Above200": (df["inst_accumulating"] == 1) & (df["insiders_30d"] >= 2) & (df["above_200"] == 1),
    # Quad confluence
    "Ins + InstAcc + Ab200 + Comp": (df["insiders_30d"] >= 1) & (df["inst_accumulating"] == 1) & (df["above_200"] == 1) & (df["atr_compressed"] == 1),
    "Ins + InstAcc + Ab200 + PB":   (df["insiders_30d"] >= 1) & (df["inst_accumulating"] == 1) & (df["above_200"] == 1) & (df["pullback_to_sma20"] == 1),
    "Ins + InstAcc + Ab200 + MA":   (df["insiders_30d"] >= 1) & (df["inst_accumulating"] == 1) & (df["above_200"] == 1) & (df["ma_aligned"] == 1),
}

# ---------------------------------------------------------------
# 9. Print results
# ---------------------------------------------------------------
for d in [10, 20, 30]:
    h = f"{d}d"
    print(f"\n{'='*115}")
    print(f"  HOLDING: {d} DAYS  |  Stop = 1.5xATR  |  Long entries only")
    print(f"{'='*115}")
    print(f"  {'Setup':<38} {'N':>9} {'1R%':>6} {'2R%':>6} {'3R%':>6} {'Stop%':>6} {'Exp':>7} {'AvgPct':>8}")
    print(f"  {'-'*98}")

    results = []
    for name, mask in conditions.items():
        sub = df[mask].dropna(subset=[f"hit_2R_{h}", f"stopped_{h}", f"ret_pct_{h}"])
        if len(sub) < 50:
            continue
        n = len(sub)
        h1 = sub[f"hit_1R_{h}"].mean()
        h2 = sub[f"hit_2R_{h}"].mean()
        h3 = sub[f"hit_3R_{h}"].mean()
        st = sub[f"stopped_{h}"].mean()
        exp = h2 * 2.0 - st * 1.0
        ap = sub[f"ret_pct_{h}"].mean() * 100
        results.append((name, n, h1, h2, h3, st, exp, ap))
        print(f"  {name:<38} {n:>9,} {h1*100:>5.1f}% {h2*100:>5.1f}% {h3*100:>5.1f}% {st*100:>5.1f}% {exp:>+6.3f} {ap:>+7.2f}%")

    # Highlight best expectancy
    if results:
        best = max(results, key=lambda x: x[6])  # highest expectancy
        print(f"\n  BEST EXPECTANCY: {best[0]} -> Exp={best[6]:+.3f}, 2R={best[3]*100:.1f}%, N={best[1]:,}")

# ---------------------------------------------------------------
# 10. Yearly consistency for top setups at 20d
# ---------------------------------------------------------------
print(f"\n{'='*115}")
print("  YEARLY CONSISTENCY — 20d Holding Period")
print(f"{'='*115}")

# Find setups with positive expectancy and N>=100 at 20d
good_setups = []
for name, mask in conditions.items():
    sub = df[mask].dropna(subset=["hit_2R_20d", "stopped_20d", "ret_pct_20d"])
    if len(sub) < 100:
        continue
    h2 = sub["hit_2R_20d"].mean()
    st = sub["stopped_20d"].mean()
    exp = h2 * 2.0 - st * 1.0
    if exp > 0.05:  # positive expectancy threshold
        good_setups.append((name, exp, len(sub)))

good_setups.sort(key=lambda x: -x[1])

for name, overall_exp, overall_n in good_setups[:12]:
    mask = conditions[name]
    sub = df[mask].dropna(subset=["hit_2R_20d", "stopped_20d", "ret_pct_20d"])

    print(f"\n  --- {name} (N={overall_n:,}, Exp={overall_exp:+.3f}) ---")
    print(f"  {'Year':>6} {'N':>7} {'1R%':>6} {'2R%':>6} {'3R%':>6} {'Stop%':>6} {'Exp':>7} {'AvgPct':>8}")

    years_positive = 0
    years_total = 0
    for year in sorted(sub["year"].unique()):
        if year < 2019:
            continue
        ys = sub[sub["year"] == year]
        if len(ys) < 10:
            continue
        h2 = ys["hit_2R_20d"].mean()
        st = ys["stopped_20d"].mean()
        exp = h2 * 2.0 - st * 1.0
        ap = ys["ret_pct_20d"].mean() * 100
        years_total += 1
        if exp > 0:
            years_positive += 1
        print(f"  {year:>6} {len(ys):>7,} {ys['hit_1R_20d'].mean()*100:>5.1f}% "
              f"{h2*100:>5.1f}% {ys['hit_3R_20d'].mean()*100:>5.1f}% "
              f"{st*100:>5.1f}% {exp:>+6.3f} {ap:>+7.2f}%")
    if years_total > 0:
        print(f"  Consistency: {years_positive}/{years_total} years positive ({years_positive/years_total*100:.0f}%)")

# ---------------------------------------------------------------
# 11. Stop distance optimization for best setup
# ---------------------------------------------------------------
print(f"\n{'='*115}")
print("  STOP DISTANCE OPTIMIZATION")
print(f"{'='*115}")

# Test on top 3 setups
for setup_name in [x[0] for x in good_setups[:3]]:
    mask = conditions[setup_name]
    sub = df[mask].dropna(subset=["max_high_20d", "min_low_20d", "close_20d"]).copy()
    if len(sub) < 50:
        continue

    print(f"\n  --- {setup_name} (N={len(sub):,}) ---")
    print(f"  {'Stop':>12} {'2R hit%':>8} {'3R hit%':>8} {'Stopped%':>9} {'Exp(2R)':>8} {'Exp(3R)':>8}")

    for sm in [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]:
        R = sm * sub["atr14"]
        h2 = (sub["max_high_20d"] >= sub["close"] + 2 * R).mean()
        h3 = (sub["max_high_20d"] >= sub["close"] + 3 * R).mean()
        st = (sub["min_low_20d"] <= sub["close"] - R).mean()
        exp2 = h2 * 2.0 - st * 1.0
        exp3 = h3 * 3.0 - st * 1.0
        print(f"  {sm:>8.2f}x ATR {h2*100:>7.1f}% {h3*100:>7.1f}% {st*100:>8.1f}% {exp2:>+7.3f} {exp3:>+7.3f}")

# ---------------------------------------------------------------
# 12. Best setups summary
# ---------------------------------------------------------------
print(f"\n{'='*115}")
print("  TOP SWING SETUPS RANKED BY EXPECTANCY (20d, 1.5xATR stop)")
print(f"{'='*115}")

all_results_20d = []
for name, mask in conditions.items():
    sub = df[mask].dropna(subset=["hit_2R_20d", "stopped_20d", "ret_pct_20d"])
    if len(sub) < 50:
        continue
    h1 = sub["hit_1R_20d"].mean()
    h2 = sub["hit_2R_20d"].mean()
    h3 = sub["hit_3R_20d"].mean()
    st = sub["stopped_20d"].mean()
    exp = h2 * 2.0 - st * 1.0
    ap = sub["ret_pct_20d"].mean() * 100
    # yearly consistency
    yp = 0
    yt = 0
    for year in sub["year"].unique():
        if year < 2019: continue
        ys = sub[sub["year"] == year]
        if len(ys) < 10: continue
        yt += 1
        if ys["hit_2R_20d"].mean() * 2 - ys["stopped_20d"].mean() > 0:
            yp += 1
    consistency = yp / yt * 100 if yt > 0 else 0
    all_results_20d.append((name, len(sub), h1, h2, h3, st, exp, ap, consistency))

all_results_20d.sort(key=lambda x: -x[6])
print(f"  {'Rank':>4} {'Setup':<38} {'N':>9} {'2R%':>6} {'3R%':>6} {'Stop%':>6} {'Exp':>7} {'Avg%':>7} {'YrCon':>6}")
for i, (name, n, h1, h2, h3, st, exp, ap, con) in enumerate(all_results_20d[:20], 1):
    marker = " ***" if exp > 0.15 and con >= 70 else ""
    print(f"  {i:>4} {name:<38} {n:>9,} {h2*100:>5.1f}% {h3*100:>5.1f}% {st*100:>5.1f}% {exp:>+6.3f} {ap:>+6.2f}% {con:>5.0f}%{marker}")

print(f"\n  *** = Exp > +0.15 AND yearly consistency >= 70%")
print(f"\nDone in {time.time()-t0:.1f}s")
