"""
Codex v7.2 Swing Benchmark Replica
===================================
Cross-validates Codex v7.2 swing benchmark results using identical parameters.

Data: fact_daily_prices (2023-2024, with 2022 lookback for SMA200)
Setups: FIB_RSI2_3DOWN, HOLY_GRAIL, FIB_3DOWN, PULLBACK_3DOWN
Execution: next-day open entry, 2*ATR stop, first-hit engine (stop wins on tie)

Optimized: vectorized feature computation and numpy-based trade evaluation.
"""

import os
import sys
import datetime
import numpy as np
import pandas as pd
import duckdb

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'warehouse', 'sec_intel.duckdb')
ARTIFACT_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'artifacts', 'v72_swing_benchmark_replica')

# --- Parameters (matching Codex v7.2 exactly) ---
STOP_ATR_MULT = 2.0
TARGET_R_GRID = [1.0, 1.5]
HOLD_DAYS_GRID = [5, 10, 15, 20, 30]
YEARS = [2023, 2024]


# --- Vectorized feature computation ---

def compute_features_vectorized(group_df: pd.DataFrame) -> pd.DataFrame:
    """Compute all features for a single ticker. Input must be sorted by trade_date."""
    n = len(group_df)
    close = group_df['close'].values.astype(np.float64)
    high = group_df['high'].values.astype(np.float64)
    low = group_df['low'].values.astype(np.float64)

    # SMA 50, 200
    sma50 = pd.Series(close).rolling(50, min_periods=50).mean().values
    sma200 = pd.Series(close).rolling(200, min_periods=200).mean().values

    # ATR 20 (SMA-based)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    prev_close = np.roll(close, 1)
    tr[1:] = np.maximum(high[1:] - low[1:],
                        np.maximum(np.abs(high[1:] - prev_close[1:]),
                                   np.abs(low[1:] - prev_close[1:])))
    atr20 = pd.Series(tr).rolling(20, min_periods=20).mean().values

    # RSI(2) - Wilder's smoothing
    rsi2 = np.full(n, np.nan)
    if n >= 3:
        delta = np.diff(close)
        gain = np.where(delta > 0, delta, 0.0)
        loss = np.where(delta < 0, -delta, 0.0)
        avg_g = np.mean(gain[:2])
        avg_l = np.mean(loss[:2])
        rsi2[2] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
        for i in range(2, len(delta)):
            avg_g = (avg_g * 1 + gain[i]) / 2
            avg_l = (avg_l * 1 + loss[i]) / 2
            rsi2[i + 1] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)

    # ADX(14) - Wilder's smoothing
    adx14 = np.full(n, np.nan)
    if n >= 30:
        up_move = np.diff(high)
        down_move = -np.diff(low)
        pdm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        mdm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        tr_dm = np.empty(n - 1)
        for i in range(n - 1):
            tr_dm[i] = max(high[i+1] - low[i+1], abs(high[i+1] - close[i]), abs(low[i+1] - close[i]))

        p = 14
        atr_s = np.sum(tr_dm[:p])
        pdm_s = np.sum(pdm[:p])
        mdm_s = np.sum(mdm[:p])
        dx_arr = np.zeros(n - 1)

        for i in range(p - 1, n - 1):
            if i == p - 1:
                atr_s = np.sum(tr_dm[:p])
                pdm_s = np.sum(pdm[:p])
                mdm_s = np.sum(mdm[:p])
            else:
                atr_s = atr_s - atr_s / p + tr_dm[i]
                pdm_s = pdm_s - pdm_s / p + pdm[i]
                mdm_s = mdm_s - mdm_s / p + mdm[i]
            if atr_s > 0:
                pdi = 100.0 * pdm_s / atr_s
                mdi = 100.0 * mdm_s / atr_s
                denom = pdi + mdi
                if denom > 0:
                    dx_arr[i] = 100.0 * abs(pdi - mdi) / denom

        adx_start = 2 * p - 1
        if adx_start < n - 1:
            adx_val = np.mean(dx_arr[p-1:adx_start])
            adx14[adx_start + 1] = adx_val
            for i in range(adx_start, n - 2):
                adx_val = (adx_val * (p - 1) + dx_arr[i + 1]) / p
                adx14[i + 2] = adx_val

    # Consecutive down days
    consec_down = np.zeros(n, dtype=np.int32)
    for i in range(1, n):
        if close[i] < close[i-1]:
            consec_down[i] = consec_down[i-1] + 1

    # Derived
    with np.errstate(divide='ignore', invalid='ignore'):
        pv50 = np.where(sma50 > 0, (close - sma50) / sma50 * 100.0, np.nan)
        pv200 = np.where(sma200 > 0, (close - sma200) / sma200 * 100.0, np.nan)

    group_df = group_df.copy()
    group_df['sma_200'] = sma200
    group_df['atr_20'] = atr20
    group_df['rsi_2'] = rsi2
    group_df['adx_14'] = adx14
    group_df['consecutive_down_days'] = consec_down
    group_df['price_vs_sma50_pct'] = pv50
    group_df['rsi2_below_10'] = rsi2 < 10

    return group_df


# --- Vectorized setup detection ---

def detect_setups_vectorized(df: pd.DataFrame) -> pd.DataFrame:
    """Detect all 4 setups using vectorized boolean masks. Returns signals DataFrame."""
    # Base mask: close > sma_200, valid features
    valid = (~np.isnan(df['sma_200'].values)) & (~np.isnan(df['price_vs_sma50_pct'].values))
    above_200 = df['close'].values > df['sma_200'].values
    base = valid & above_200

    pv50 = df['price_vs_sma50_pct'].values
    cd = df['consecutive_down_days'].values
    rsi_ok = df['rsi2_below_10'].values | (df['rsi_2'].values < 10)
    adx = df['adx_14'].values
    adx_ok = (~np.isnan(adx)) & (adx >= 25)

    fib_zone = (pv50 >= -3.0) & (pv50 <= -1.0)
    pullback_zone = (pv50 >= -3.0) & (pv50 <= 0.0)
    cd3 = cd >= 3

    setups = {
        'FIB_RSI2_3DOWN': base & fib_zone & cd3 & rsi_ok,
        'HOLY_GRAIL': base & pullback_zone & adx_ok & rsi_ok,
        'FIB_3DOWN': base & fib_zone & cd3,
        'PULLBACK_3DOWN': base & pullback_zone & cd3,
    }

    rows = []
    for name, mask in setups.items():
        subset = df[mask][['ticker', 'trade_date', 'close', 'atr_20']].copy()
        subset['setup'] = name
        rows.append(subset)

    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


# --- Numpy-based first-hit engine ---

def evaluate_trades_batch(signals_df: pd.DataFrame, prices_df: pd.DataFrame) -> pd.DataFrame:
    """Evaluate all trades using numpy arrays for speed.

    For each signal, look up forward bars and run first-hit engine.
    """
    # Build per-ticker arrays for fast lookup
    print("    Building price arrays...")
    ticker_arrays = {}
    for ticker, tdf in prices_df.groupby('ticker'):
        tdf = tdf.sort_values('trade_date').reset_index(drop=True)
        ticker_arrays[ticker] = {
            'dates': tdf['trade_date'].values,
            'open': tdf['open'].values.astype(np.float64),
            'high': tdf['high'].values.astype(np.float64),
            'low': tdf['low'].values.astype(np.float64),
            'close': tdf['close'].values.astype(np.float64),
        }

    max_hold = max(HOLD_DAYS_GRID)
    results = []
    total = len(signals_df)
    report_every = max(total // 10, 1)

    for idx in range(total):
        if idx % report_every == 0:
            print(f"    {idx:,}/{total:,} signals evaluated...")

        row = signals_df.iloc[idx]
        ticker = row['ticker']
        signal_date = row['trade_date']
        atr20 = row['atr_20']
        setup = row['setup']

        if np.isnan(atr20) or atr20 <= 0:
            continue

        ta = ticker_arrays.get(ticker)
        if ta is None:
            continue

        # Find index of signal_date
        dates = ta['dates']
        sig_idx_arr = np.where(dates == signal_date)[0]
        if len(sig_idx_arr) == 0:
            continue
        sig_idx = sig_idx_arr[0]

        # Need at least 1 bar after signal for entry + max_hold bars for evaluation
        if sig_idx + 1 + max_hold > len(dates):
            # Still try with available bars
            if sig_idx + 2 > len(dates):
                continue

        entry_idx = sig_idx + 1
        entry_price = ta['open'][entry_idx]
        if np.isnan(entry_price) or entry_price <= 0:
            continue

        # Forward bars from entry day onward
        end_idx = min(entry_idx + max_hold, len(dates))
        fwd_high = ta['high'][entry_idx:end_idx]
        fwd_low = ta['low'][entry_idx:end_idx]
        fwd_close = ta['close'][entry_idx:end_idx]

        year = pd.Timestamp(signal_date).year

        stop_dist = STOP_ATR_MULT * atr20
        stop_price = entry_price - stop_dist
        r_unit = stop_dist

        for target_r in TARGET_R_GRID:
            target_price = entry_price + target_r * stop_dist

            # MFE for max hold window
            for hold_days in HOLD_DAYS_GRID:
                hd = min(hold_days, len(fwd_high))
                if hd == 0:
                    continue

                h = fwd_high[:hd]
                l = fwd_low[:hd]
                c = fwd_close[:hd]

                # MFE hit (ignore stop ordering)
                mfe_hit = np.max(h) >= target_price

                # First-hit engine
                win = False
                exit_r = 0.0

                stop_bars = l <= stop_price
                target_bars = h >= target_price

                # Find first stop and first target
                stop_indices = np.where(stop_bars)[0]
                target_indices = np.where(target_bars)[0]

                first_stop = stop_indices[0] if len(stop_indices) > 0 else hd + 1
                first_target = target_indices[0] if len(target_indices) > 0 else hd + 1

                if first_stop <= first_target:
                    # Stop hit first (or same bar = stop wins)
                    exit_r = (stop_price - entry_price) / r_unit
                    win = False
                elif first_target < first_stop:
                    # Target hit first
                    exit_r = (target_price - entry_price) / r_unit
                    win = True
                else:
                    # Neither hit: exit at last close
                    exit_r = (c[-1] - entry_price) / r_unit
                    win = False

                results.append((
                    ticker, signal_date, year, setup, target_r, hold_days,
                    entry_price, atr20, win, mfe_hit, exit_r,
                ))

    print(f"    Total evaluations: {len(results):,}")
    return pd.DataFrame(results, columns=[
        'ticker', 'trade_date', 'year', 'setup', 'target_r', 'hold_days',
        'entry_price', 'atr_20', 'win', 'mfe_hit', 'exit_r',
    ])


# --- Main ---

def main():
    t0 = datetime.datetime.now()
    print("=" * 70)
    print("Codex v7.2 Swing Benchmark Replica")
    print(f"Started: {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    db_path = os.path.abspath(DB_PATH)
    print(f"\nDB: {db_path}")
    conn = duckdb.connect(db_path, read_only=True)

    # Step 1: Load daily prices (2022 lookback for SMA200)
    print("\n[1/5] Loading daily prices (2022-01-01 to 2024-12-31)...")
    prices_df = conn.execute("""
        SELECT ticker, trade_date, open, high, low, close, volume
        FROM fact_daily_prices
        WHERE trade_date >= '2022-01-01' AND trade_date <= '2024-12-31'
        ORDER BY ticker, trade_date
    """).fetchdf()
    n_tickers = prices_df['ticker'].nunique()
    print(f"  Loaded {len(prices_df):,} daily bars, {n_tickers:,} tickers")
    conn.close()

    # Step 2: Compute features per ticker
    print("\n[2/5] Computing features (SMA200, ATR20, RSI2, ADX14, consec_down)...")
    all_features = []
    tickers = prices_df['ticker'].unique()
    progress_interval = max(len(tickers) // 10, 1)

    for idx, ticker in enumerate(tickers):
        if idx % progress_interval == 0:
            elapsed = (datetime.datetime.now() - t0).total_seconds()
            print(f"  {idx:,}/{len(tickers):,} tickers ({elapsed:.0f}s elapsed)...")
        tdf = prices_df[prices_df['ticker'] == ticker].copy()
        if len(tdf) < 220:
            continue
        tdf = tdf.sort_values('trade_date').reset_index(drop=True)
        featured = compute_features_vectorized(tdf)
        # Keep only 2023-2024
        mask = (featured['trade_date'] >= pd.Timestamp('2023-01-01')) & \
               (featured['trade_date'] <= pd.Timestamp('2024-12-31'))
        window = featured[mask]
        if len(window) > 0:
            all_features.append(window)

    features_df = pd.concat(all_features, ignore_index=True)
    print(f"  Features: {len(features_df):,} rows, {features_df['ticker'].nunique():,} tickers")

    # Step 3: Detect setups (vectorized)
    print("\n[3/5] Detecting setups (vectorized)...")
    signals_df = detect_setups_vectorized(features_df)
    print(f"  Signals detected:")
    for name in ['FIB_RSI2_3DOWN', 'HOLY_GRAIL', 'FIB_3DOWN', 'PULLBACK_3DOWN']:
        n = len(signals_df[signals_df['setup'] == name])
        print(f"    {name}: {n:,}")
    print(f"  Total: {len(signals_df):,}")

    # Step 4: Evaluate trades
    print("\n[4/5] Evaluating trades (numpy first-hit engine)...")
    results_df = evaluate_trades_batch(signals_df, prices_df)

    # Step 5: Summaries
    print("\n[5/5] Computing summaries...")

    # A) Grid summary
    print("\n" + "=" * 70)
    print("A) GRID SUMMARY")
    print("=" * 70)

    grid_summary = results_df.groupby(['setup', 'target_r', 'hold_days']).agg(
        n=('win', 'count'),
        first_hit_win_rate=('win', 'mean'),
        mfe_hit_rate=('mfe_hit', 'mean'),
        expectancy_r=('exit_r', 'mean'),
    ).reset_index()

    for setup_name in ['FIB_RSI2_3DOWN', 'HOLY_GRAIL', 'FIB_3DOWN', 'PULLBACK_3DOWN']:
        subset = grid_summary[grid_summary['setup'] == setup_name]
        print(f"\n  --- {setup_name} ---")
        print(f"  {'target_r':>8} {'hold':>5} {'n':>6} {'WR':>8} {'MFE_WR':>8} {'E[R]':>10}")
        for _, r in subset.iterrows():
            print(f"  {r['target_r']:>8.1f} {r['hold_days']:>5} {int(r['n']):>6} "
                  f"{r['first_hit_win_rate']:>8.4f} {r['mfe_hit_rate']:>8.4f} {r['expectancy_r']:>10.6f}")

    # B) Canonical rows
    print("\n" + "=" * 70)
    print("B) CANONICAL ROWS (best WR per setup+target_r, tie-break: higher expectancy)")
    print("=" * 70)

    canonical = grid_summary.sort_values(
        ['setup', 'target_r', 'first_hit_win_rate', 'expectancy_r'],
        ascending=[True, True, False, False]
    ).groupby(['setup', 'target_r']).first().reset_index()

    print(f"\n  {'setup':<20} {'tgt':>4} {'hold':>5} {'n':>6} {'WR':>10} {'MFE_WR':>10} {'E[R]':>10}")
    for _, r in canonical.iterrows():
        print(f"  {r['setup']:<20} {r['target_r']:>4.1f} {int(r['hold_days']):>5} {int(r['n']):>6} "
              f"{r['first_hit_win_rate']:>10.6f} {r['mfe_hit_rate']:>10.6f} {r['expectancy_r']:>10.6f}")

    # C) Year breakdown
    print("\n" + "=" * 70)
    print("C) YEAR BREAKDOWN (canonical rows)")
    print("=" * 70)

    for _, can in canonical.iterrows():
        setup = can['setup']
        target_r = can['target_r']
        hold = can['hold_days']
        subset = results_df[(results_df['setup'] == setup) &
                           (results_df['target_r'] == target_r) &
                           (results_df['hold_days'] == hold)]
        yearly = subset.groupby('year').agg(
            n=('win', 'count'),
            wr=('win', 'mean'),
            mfe_wr=('mfe_hit', 'mean'),
            exp_r=('exit_r', 'mean'),
        ).reset_index()
        print(f"\n  {setup} @ {target_r}R, hold {int(hold)}:")
        for _, yr in yearly.iterrows():
            print(f"    {int(yr['year'])}: n={int(yr['n']):,}, WR={yr['wr']:.6f}, MFE_WR={yr['mfe_wr']:.6f}, E[R]={yr['exp_r']:.6f}")

    # D) Delta table vs Codex
    print("\n" + "=" * 70)
    print("D) DELTA TABLE vs CODEX EXPECTED VALUES")
    print("=" * 70)

    codex_expected = [
        ('FIB_RSI2_3DOWN', 1.0, 30, 3135, 0.540989, 0.677512, 0.112139),
        ('FIB_RSI2_3DOWN', 1.5, 30, 3135, 0.414035, 0.521850, 0.140689),
        ('HOLY_GRAIL',     1.0, 30, 1623, 0.545903, 0.663586, 0.134302),
        ('HOLY_GRAIL',     1.5, 30, 1623, 0.412816, 0.497843, 0.181907),
        ('FIB_3DOWN',      1.0, 30, 4118, 0.537640, 0.674356, 0.109017),
        ('FIB_3DOWN',      1.5, 30, 4118, 0.414522, 0.523069, 0.144616),
        ('PULLBACK_3DOWN', 1.0, 30, 6277, 0.536243, 0.673570, 0.104793),
        ('PULLBACK_3DOWN', 1.5, 30, 6277, 0.414529, 0.523339, 0.141576),
    ]

    header = (f"  {'setup':<20} {'tgt':>4} {'hold':>5} | "
              f"{'n_cdx':>6} {'n_ours':>6} {'dn':>6} | "
              f"{'WR_cdx':>8} {'WR_ours':>8} {'dWR':>7} {'':>3} | "
              f"{'MFE_cdx':>8} {'MFE_ours':>8} {'dMFE':>7} | "
              f"{'ER_cdx':>8} {'ER_ours':>8} {'dER':>7}")
    print(f"\n{header}")
    print("  " + "-" * 130)

    for (setup, tgt, hold, n_codex, wr_codex, mfe_codex, er_codex) in codex_expected:
        match = grid_summary[(grid_summary['setup'] == setup) &
                            (grid_summary['target_r'] == tgt) &
                            (grid_summary['hold_days'] == hold)]
        if len(match) == 0:
            print(f"  {setup:<20} {tgt:>4.1f} {hold:>5} | {n_codex:>6} {'N/A':>6} {'N/A':>6} | N/A")
            continue
        r = match.iloc[0]
        n_ours = int(r['n'])
        wr_ours = r['first_hit_win_rate']
        mfe_ours = r['mfe_hit_rate']
        er_ours = r['expectancy_r']
        dn = n_ours - n_codex
        dwr = wr_ours - wr_codex
        dmfe = mfe_ours - mfe_codex
        der = er_ours - er_codex

        wr_flag = "OK" if abs(dwr) <= 0.005 else "!!!"
        print(f"  {setup:<20} {tgt:>4.1f} {hold:>5} | "
              f"{n_codex:>6} {n_ours:>6} {dn:>+6} | "
              f"{wr_codex:>8.6f} {wr_ours:>8.6f} {dwr:>+7.4f} {wr_flag:>3} | "
              f"{mfe_codex:>8.6f} {mfe_ours:>8.6f} {dmfe:>+7.4f} | "
              f"{er_codex:>8.6f} {er_ours:>8.6f} {der:>+7.4f}")

    # Save artifacts
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(ARTIFACT_DIR, ts)
    os.makedirs(os.path.join(out_dir, 'validation'), exist_ok=True)

    grid_summary.to_parquet(os.path.join(out_dir, 'validation', 'summary_grid_v72.parquet'), index=False)
    canonical.to_parquet(os.path.join(out_dir, 'validation', 'summary_canonical_v72.parquet'), index=False)

    elapsed = (datetime.datetime.now() - t0).total_seconds()
    print(f"\n  Artifacts saved to: {out_dir}")
    print(f"  Total runtime: {elapsed:.0f}s")

    # Assumption notes
    print("\n" + "=" * 70)
    print("ASSUMPTION NOTES")
    print("=" * 70)
    print("""
  1. ATR: SMA-based 20-period (not Wilder's EMA). Matches our swing_feature_engine.
     If Codex uses Wilder's smoothing for ATR, stop distances will differ slightly.
  2. Entry: open[t+1] where t=signal day. Identical to Codex spec.
  3. Bar precedence: stop wins on same-bar tie. Identical to Codex spec.
  4. RSI(2): Wilder's smoothing. Standard implementation.
  5. ADX(14): Wilder's smoothing on +DI/-DI/DX. Standard 14-period.
  6. close > sma_200: strict greater-than.
  7. price_vs_sma50_pct BETWEEN: inclusive both ends (>= AND <=).
  8. Data: fact_daily_prices from Polygon (split-adjusted). Features computed
     from raw prices for full 2023-2024 coverage.
  9. No slippage, no commissions.
  10. Universe: all tickers with >= 220 daily bars in 2022-2024 window.
    """)


if __name__ == '__main__':
    main()
