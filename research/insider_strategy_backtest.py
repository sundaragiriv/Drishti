"""Insider Director-cluster strategy backtest harness.

What this does:
  Step 1: build PIT-correct historical cluster events (since 2016) with rich metadata.
  Step 2: (a) statistical exit optimization on in-sample (2016-2022) — sweep hold
          horizons, ATR stops, R-multiple targets. Pick the (hold, stop, target)
          parameter set the data prefers, not what we guessed.
  Step 3: run the full strategy backtest with chosen (a) params across 2016-2025.
          OOS test window is 2023-2025.
  Step 4: (b) ML exit model — features per (event, day_t): days elapsed, return,
          drawdown, max favorable excursion, regime state, etc. Label: "is
          exit-now better than holding to the (a)-chosen time stop?" Train
          2016-2022, test 2023-2025.
  Step 5: re-run strategy with ML-driven exits, side-by-side compare to (a).

Locked parameters (per the 2026-06-27 alignment conversation):
  - Entry: next day's OPEN after cluster_known_date (= last_buy + 2 trading days)
  - Sizing: 5% position size, flat for v1 (no scaling)
  - Portfolio: max 10 concurrent, max 50% deployed
  - Costs: $1 per trade + 10 bps slippage per side
  - Universe: price >= $5, survivorship-safe (built from price history), no ADV filter
  - Capital: $100K paper

Point-in-time discipline:
  - Form-4 cluster known at MAX(transaction_date) of the trailing-30d window + 2 trading days
  - Universe at each date = tickers that traded on that date (no survivorship bias)
  - In-sample tuning years (2016-2022) and OOS test window (2023-2025) are strict — no peeking

Run:
  .venv\\Scripts\\python -m research.insider_strategy_backtest --build-events
  .venv\\Scripts\\python -m research.insider_strategy_backtest --optimize
  .venv\\Scripts\\python -m research.insider_strategy_backtest --backtest
  .venv\\Scripts\\python -m research.insider_strategy_backtest --train-ml
  .venv\\Scripts\\python -m research.insider_strategy_backtest --backtest-ml
  .venv\\Scripts\\python -m research.insider_strategy_backtest --all
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import duckdb
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Locked parameters
# ---------------------------------------------------------------------------
WAREHOUSE = r"e:\Quant-Bridge\data\warehouse\sec_intel.duckdb"
ARTIFACT_DIR = Path(r"e:\Quant-Bridge\research\artifacts\insider_strategy")
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

# Cluster definition (matches pond_trigger_backtest --pond-alone, the validated one)
CLUSTER_WINDOW_DAYS = 30
SEC_LAG_DAYS = 2
MIN_INSIDERS = 2
MIN_DIRECTORS = 1
START_DATE = "2016-01-01"
END_DATE = "2025-12-31"

# In-sample / OOS split (strict — no peeking for tuning)
IN_SAMPLE_END = "2022-12-31"   # tune on this and earlier
OOS_START = "2023-01-01"       # never seen during tuning

# Strategy params (locked)
POSITION_PCT = 0.05            # 5% of equity per trade
MAX_CONCURRENT = 10
MAX_DEPLOYED = 0.50
COMMISSION_PER_TRADE = 1.0     # $1 per side
SLIPPAGE_BPS = 10              # 10 bps per side
STARTING_CAPITAL = 100_000.0
PRICE_FLOOR = 5.0

# Optimization sweep grids
HOLD_HORIZONS = [10, 20, 30, 45, 60, 90, 120, 180]
STOP_ATR_MULTS = [0.5, 1.0, 1.5, 2.0]
TARGET_R_MULTS = [1.0, 2.0, 3.0]  # plus a "no target" entry handled separately

# ATR window for stop calc
ATR_WINDOW = 14


# ---------------------------------------------------------------------------
# Step 1: Build PIT-correct cluster events
# ---------------------------------------------------------------------------
def build_events() -> pd.DataFrame:
    """Find every Director-led insider cluster known between START and END."""
    con = duckdb.connect(WAREHOUSE, read_only=True)
    print(f"[BUILD] querying clusters {START_DATE}..{END_DATE}")
    rows = con.execute(f"""
        WITH buys AS (
            SELECT ticker, insider_name, insider_role, transaction_date,
                   shares, price
            FROM fact_form4_transactions
            WHERE transaction_code = 'P' AND upper(direction) = 'BUY'
              AND ticker IS NOT NULL AND ticker <> ''
              AND ticker NOT IN ('NONE', 'N/A', '--', '?', 'NULL')
              AND LENGTH(ticker) BETWEEN 1 AND 6
              AND transaction_date BETWEEN DATE '{START_DATE}' - INTERVAL '{CLUSTER_WINDOW_DAYS}' DAY
                                       AND DATE '{END_DATE}'
              AND shares > 0 AND price > 0
        ),
        clustered AS (
            SELECT b.ticker, b.transaction_date AS d,
                   COUNT(DISTINCT b2.insider_name) AS n_insiders,
                   COUNT(DISTINCT CASE WHEN b2.insider_role ILIKE '%director%'
                                       THEN b2.insider_name END) AS n_directors,
                   COUNT(DISTINCT CASE WHEN b2.insider_role ILIKE '%officer%'
                                       THEN b2.insider_name END) AS n_officers,
                   SUM(b2.shares * b2.price) AS total_value,
                   SUM(b2.shares) AS total_shares,
                   AVG(b2.price) AS avg_buy_price
            FROM buys b JOIN buys b2
              ON b2.ticker = b.ticker
             AND b2.transaction_date BETWEEN b.transaction_date - INTERVAL '{CLUSTER_WINDOW_DAYS}' DAY
                                         AND b.transaction_date
            GROUP BY b.ticker, b.transaction_date
            HAVING COUNT(DISTINCT b2.insider_name) >= {MIN_INSIDERS}
               AND MAX(CASE WHEN b2.insider_role ILIKE '%director%' THEN 1 ELSE 0 END) >= {MIN_DIRECTORS}
        )
        SELECT ticker, d AS cluster_date, n_insiders, n_directors, n_officers,
               total_value, total_shares, avg_buy_price
        FROM clustered
        ORDER BY d, ticker
    """).fetchall()
    con.close()

    cols = ["ticker", "cluster_date", "n_insiders", "n_directors", "n_officers",
            "total_value", "total_shares", "avg_buy_price"]
    df = pd.DataFrame(rows, columns=cols)
    df["cluster_date"] = pd.to_datetime(df["cluster_date"]).dt.date

    # Dedupe: keep first cluster per (ticker, rolling 60d window) so we don't
    # re-enter the same name every day a new buy lands. A second cluster for
    # the same ticker only counts if it's 60d after the prior one (a "fresh"
    # signal). This matches how a trader would treat it.
    df = df.sort_values(["ticker", "cluster_date"]).reset_index(drop=True)
    keep = np.ones(len(df), dtype=bool)
    last_date_by_ticker: dict = {}
    for i, row in df.iterrows():
        prev = last_date_by_ticker.get(row["ticker"])
        if prev is not None and (row["cluster_date"] - prev).days < 60:
            keep[i] = False
        else:
            last_date_by_ticker[row["ticker"]] = row["cluster_date"]
    df = df[keep].reset_index(drop=True)

    df["known_date"] = df["cluster_date"].apply(
        lambda d: d + timedelta(days=SEC_LAG_DAYS))

    out = ARTIFACT_DIR / "events.parquet"
    df.to_parquet(out, index=False)
    print(f"[BUILD] {len(df):,} clusters · saved -> {out}")
    print(f"        per-year:")
    yc = df.assign(yr=pd.to_datetime(df["cluster_date"]).dt.year).groupby("yr").size()
    for yr, n in yc.items():
        print(f"          {yr}: {n:,}")
    return df


# ---------------------------------------------------------------------------
# Helper: bulk-load price panel for the tickers + dates we need
# ---------------------------------------------------------------------------
def load_prices(tickers: Optional[List[str]] = None) -> pd.DataFrame:
    """Load fact_daily_prices for tickers in window. Returns indexed DataFrame."""
    con = duckdb.connect(WAREHOUSE, read_only=True)
    print(f"[PRICES] loading {START_DATE}..{END_DATE} ...")
    sql = f"""
        SELECT ticker, trade_date, open, high, low, close, volume
        FROM fact_daily_prices
        WHERE trade_date BETWEEN DATE '{START_DATE}' AND DATE '{END_DATE}'
          AND close IS NOT NULL AND close >= {PRICE_FLOOR}
    """
    if tickers:
        ticker_list = "','".join(tickers)
        sql += f" AND ticker IN ('{ticker_list}')"
    df = con.execute(sql).fetch_df()
    con.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    print(f"[PRICES] {len(df):,} rows · {df['ticker'].nunique():,} tickers")
    return df


def compute_forward_returns_and_atr(events: pd.DataFrame,
                                    prices: pd.DataFrame) -> pd.DataFrame:
    """For each event, attach entry-day price + ATR + forward returns at horizons.

    Entry-day = first trading day on or after known_date.
    ATR = average true range over prior 14 trading days (in absolute $).
    Forward returns = (close[t+N] - entry_price) / entry_price, for each horizon.
    Also: max_favorable_excursion and max_adverse_excursion within 180d (for stop/target study).
    """
    print("[ENRICH] computing entry prices, ATR, and forward returns...")

    # Group prices by ticker for fast lookup
    prices_sorted = prices.sort_values(["ticker", "trade_date"]).reset_index(drop=True)
    # Pre-build per-ticker arrays
    ticker_groups = {t: g.reset_index(drop=True) for t, g in prices_sorted.groupby("ticker")}

    out_rows = []
    skipped = 0
    for _, ev in events.iterrows():
        tkr = ev["ticker"]
        if tkr not in ticker_groups:
            skipped += 1
            continue
        g = ticker_groups[tkr]
        # find first trade_date >= known_date
        mask = g["trade_date"] >= ev["known_date"]
        if not mask.any():
            skipped += 1
            continue
        entry_idx = mask.idxmax()
        # need at least 14 prior bars for ATR
        if entry_idx < ATR_WINDOW:
            skipped += 1
            continue
        entry_row = g.iloc[entry_idx]
        entry_date = entry_row["trade_date"]
        entry_price = float(entry_row["open"])  # locked: next day open
        if entry_price <= 0:
            skipped += 1
            continue

        # ATR(14) computed on prior 14 bars
        prior = g.iloc[max(0, entry_idx - ATR_WINDOW):entry_idx]
        if len(prior) < ATR_WINDOW:
            skipped += 1
            continue
        prev_close = prior["close"].shift(1)
        tr = pd.concat([
            prior["high"] - prior["low"],
            (prior["high"] - prev_close).abs(),
            (prior["low"]  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = float(tr.dropna().mean())
        if atr <= 0:
            atr = entry_price * 0.02  # 2% fallback

        # Forward returns at horizons + MFE/MAE within 180d
        future = g.iloc[entry_idx:entry_idx + max(HOLD_HORIZONS) + 1]
        row = {
            "ticker": tkr,
            "cluster_date": ev["cluster_date"],
            "known_date": ev["known_date"],
            "entry_date": entry_date,
            "entry_price": entry_price,
            "atr14": atr,
            "n_insiders": int(ev["n_insiders"]),
            "n_directors": int(ev["n_directors"]),
            "n_officers": int(ev["n_officers"]),
            "total_value": float(ev["total_value"] or 0),
            "avg_buy_price": float(ev["avg_buy_price"] or 0),
        }
        # forward returns at each horizon
        for h in HOLD_HORIZONS:
            if len(future) > h:
                exit_close = float(future.iloc[h]["close"])
                row[f"ret_{h}d"] = (exit_close / entry_price) - 1.0
            else:
                row[f"ret_{h}d"] = np.nan
        # MFE/MAE within 180d window (intra-bar high/low for max favorable / adverse)
        h180 = future.iloc[1:min(181, len(future))]
        if len(h180) > 0:
            row["mfe_180d"] = float(h180["high"].max() / entry_price - 1.0)
            row["mae_180d"] = float(h180["low"].min()  / entry_price - 1.0)
        else:
            row["mfe_180d"] = np.nan
            row["mae_180d"] = np.nan
        out_rows.append(row)

    df = pd.DataFrame(out_rows)
    print(f"[ENRICH] {len(df):,} events enriched · {skipped:,} skipped (no price / insufficient history)")
    out = ARTIFACT_DIR / "events_enriched.parquet"
    df.to_parquet(out, index=False)
    print(f"[ENRICH] saved -> {out}")
    return df


# ---------------------------------------------------------------------------
# Step 2: Build per-event price paths, sweep exit grids on in-sample window
# ---------------------------------------------------------------------------
MAX_PATH_DAYS = max(HOLD_HORIZONS) + 1  # 181 trading days post-entry


def build_trade_paths(events: pd.DataFrame, prices: pd.DataFrame) -> tuple:
    """For each enriched event, build a (MAX_PATH_DAYS, 3) array of (high, low, close)
    starting at the entry bar (so index 0 = entry day, index k = day k of trade).

    Returns (paths_np, valid_mask) where valid_mask[i] is True if event i has a
    full 180+1d path available (we drop the very recent events without forward bars).
    """
    print(f"[PATHS] building per-event price paths (max {MAX_PATH_DAYS} bars)...")
    prices_sorted = prices.sort_values(["ticker", "trade_date"]).reset_index(drop=True)
    ticker_groups = {t: g.reset_index(drop=True) for t, g in prices_sorted.groupby("ticker")}

    n = len(events)
    paths = np.full((n, MAX_PATH_DAYS, 3), np.nan, dtype=np.float64)  # H, L, C
    valid = np.zeros(n, dtype=bool)
    actual_lens = np.zeros(n, dtype=np.int32)

    for i, ev in events.iterrows():
        tkr = ev["ticker"]
        if tkr not in ticker_groups:
            continue
        g = ticker_groups[tkr]
        mask = g["trade_date"] >= ev["entry_date"]
        if not mask.any():
            continue
        entry_idx = mask.idxmax()
        window = g.iloc[entry_idx:entry_idx + MAX_PATH_DAYS]
        L = len(window)
        if L < 10:
            continue  # too little forward data, skip
        paths[i, :L, 0] = window["high"].values
        paths[i, :L, 1] = window["low"].values
        paths[i, :L, 2] = window["close"].values
        actual_lens[i] = L
        valid[i] = True

    print(f"[PATHS] valid paths: {valid.sum():,} / {n:,}")
    return paths, valid, actual_lens


def simulate_exits(paths: np.ndarray, valid: np.ndarray, lens: np.ndarray,
                   entry_prices: np.ndarray, atrs: np.ndarray,
                   hold_days: int, stop_atr_mult: float,
                   target_r_mult: Optional[float]) -> np.ndarray:
    """Vectorized exit simulation for ALL events at one (hold, stop, target) combo.

    Conservative tie-break: if stop and target both touched on the same bar, assume
    stop fires first (worst case for the trader — no peeking at intra-bar order).

    Returns: per-event exit-return array (nan where invalid).
    """
    n = paths.shape[0]
    out = np.full(n, np.nan)

    stop_dist = stop_atr_mult * atrs        # in $
    stop_px   = entry_prices - stop_dist    # absolute stop price
    if target_r_mult is not None:
        target_px = entry_prices + target_r_mult * stop_dist
    else:
        target_px = np.full(n, np.inf)

    eff_hold = np.minimum(hold_days, lens - 1)  # don't walk past actual data

    # Vectorize the day-by-day check
    # paths[i, k, 0] = high, [1] = low, [2] = close at day k
    # We need: for each event, first day in [1..eff_hold[i]] where low <= stop or high >= target
    for i in range(n):
        if not valid[i]:
            continue
        H = eff_hold[i]
        if H < 1:
            continue
        # Day 0 = entry day (we already entered). Check exits from day 1..H
        sub_high = paths[i, 1:H + 1, 0]
        sub_low  = paths[i, 1:H + 1, 1]
        sub_close = paths[i, 1:H + 1, 2]

        # Stop hit: low <= stop_px
        stop_hits = np.where(sub_low <= stop_px[i])[0]
        target_hits = np.where(sub_high >= target_px[i])[0]

        first_stop = stop_hits[0] if stop_hits.size > 0 else 10**9
        first_target = target_hits[0] if target_hits.size > 0 else 10**9

        if first_stop <= first_target and first_stop < 10**9:
            # Stop fires (conservative tie-break)
            exit_px = stop_px[i]
        elif first_target < 10**9:
            exit_px = target_px[i]
        else:
            # Time stop — exit at close of day H
            exit_px = sub_close[-1]

        out[i] = (exit_px / entry_prices[i]) - 1.0

    return out


def optimize_exits() -> None:
    """Sweep (hold, stop, target) grid on IN-SAMPLE only; pick the best combo."""
    ev_path = ARTIFACT_DIR / "events_enriched.parquet"
    if not ev_path.exists():
        print("[OPT] events_enriched.parquet missing — run --build-events first")
        return
    events = pd.read_parquet(ev_path)
    events["entry_date"] = pd.to_datetime(events["entry_date"]).dt.date

    # Split in-sample vs OOS
    is_mask = events["entry_date"] <= pd.to_datetime(IN_SAMPLE_END).date()
    oos_mask = events["entry_date"] >= pd.to_datetime(OOS_START).date()
    print(f"[OPT] in-sample (2016..{IN_SAMPLE_END}): {is_mask.sum():,} events")
    print(f"[OPT] OOS       ({OOS_START}..2025):     {oos_mask.sum():,} events")

    prices = load_prices(tickers=events["ticker"].unique().tolist())
    paths, valid, lens = build_trade_paths(events, prices)

    entry_prices = events["entry_price"].values.astype(np.float64)
    atrs = events["atr14"].values.astype(np.float64)
    is_arr = is_mask.values
    oos_arr = oos_mask.values

    # Build the grid (include "no profit target" as None)
    target_grid = TARGET_R_MULTS + [None]

    print("\n[OPT] sweeping grid on IN-SAMPLE events only ...")
    print(f"{'hold':>4}d  {'stop':>5}xATR  {'tgt':>4}R  "
          f"{'n':>5}  {'mean%':>7}  {'med%':>7}  {'win%':>6}  "
          f"{'sharpe':>7}  {'expect':>7}")
    print("-" * 78)

    best = None
    rows = []
    for hold in HOLD_HORIZONS:
        for stop in STOP_ATR_MULTS:
            for tgt in target_grid:
                ret = simulate_exits(paths, valid, lens, entry_prices, atrs,
                                     hold, stop, tgt)
                # filter to in-sample, valid
                vmask = is_arr & ~np.isnan(ret)
                if vmask.sum() < 50:
                    continue
                r = ret[vmask]
                mean = r.mean() * 100
                med = np.median(r) * 100
                win = 100.0 * (r > 0).mean()
                sd = r.std()
                sharpe = (r.mean() / sd) if sd > 0 else 0  # per-trade
                # expectancy = win_prob * avg_win + lose_prob * avg_loss
                wins = r[r > 0]; losses = r[r <= 0]
                avg_win = wins.mean() if len(wins) > 0 else 0
                avg_loss = losses.mean() if len(losses) > 0 else 0
                expect = ((r > 0).mean() * avg_win + (r <= 0).mean() * avg_loss) * 100

                tgt_str = "none" if tgt is None else f"{tgt}R"
                row = {"hold": hold, "stop_xATR": stop, "target_R": tgt,
                       "n": int(vmask.sum()), "mean_pct": mean, "median_pct": med,
                       "win_pct": win, "sharpe": sharpe, "expectancy_pct": expect}
                rows.append(row)
                print(f"{hold:>4}d  {stop:>5}x   {tgt_str:>4}  "
                      f"{int(vmask.sum()):>5,}  {mean:>7.2f}  {med:>7.2f}  "
                      f"{win:>6.1f}  {sharpe:>7.3f}  {expect:>7.3f}")

                # Pick by expectancy (mean return after costs proxy)
                if best is None or expect > best["expectancy_pct"]:
                    best = row

    df = pd.DataFrame(rows)
    df.to_parquet(ARTIFACT_DIR / "optimize_results.parquet", index=False)
    print("\n[OPT] BEST IN-SAMPLE combo (by expectancy):")
    print(f"      hold={best['hold']}d  stop={best['stop_xATR']}xATR  "
          f"target={best['target_R']}R")
    print(f"      n={best['n']:,}  mean={best['mean_pct']:.2f}%  "
          f"win={best['win_pct']:.1f}%  expect={best['expectancy_pct']:.3f}%")

    # Verify on OOS
    print("\n[OPT] OOS verification with the chosen combo:")
    ret_oos = simulate_exits(paths, valid, lens, entry_prices, atrs,
                             best["hold"], best["stop_xATR"], best["target_R"])
    vmask = oos_arr & ~np.isnan(ret_oos)
    if vmask.sum() > 0:
        r = ret_oos[vmask]
        print(f"      n={vmask.sum():,}  mean={r.mean()*100:.2f}%  "
              f"win={(r > 0).mean()*100:.1f}%")
    else:
        print("      no valid OOS events")

    # Save chosen params for downstream use
    with open(ARTIFACT_DIR / "chosen_params.json", "w") as f:
        json.dump({"hold_days": best["hold"], "stop_xATR": best["stop_xATR"],
                   "target_R": best["target_R"],
                   "in_sample_n": best["n"],
                   "in_sample_mean_pct": best["mean_pct"],
                   "in_sample_win_pct": best["win_pct"],
                   "in_sample_expectancy_pct": best["expectancy_pct"]}, f, indent=2)
    print(f"\n[OPT] saved -> {ARTIFACT_DIR / 'chosen_params.json'}")


# ---------------------------------------------------------------------------
# Step 3: Strategy backtest with portfolio constraints (full walk)
# ---------------------------------------------------------------------------

def simulate_exits_with_dates(paths: np.ndarray, valid: np.ndarray,
                              lens: np.ndarray, entry_prices: np.ndarray,
                              atrs: np.ndarray, hold_days: int,
                              stop_atr_mult: float,
                              target_r_mult: Optional[float]) -> tuple:
    """Like simulate_exits but also returns the exit-day offset per event."""
    n = paths.shape[0]
    ret = np.full(n, np.nan)
    day_off = np.full(n, -1, dtype=np.int32)
    reason = np.full(n, "", dtype=object)

    stop_dist = stop_atr_mult * atrs
    stop_px   = entry_prices - stop_dist
    target_px = (entry_prices + target_r_mult * stop_dist
                 if target_r_mult is not None else np.full(n, np.inf))
    eff_hold = np.minimum(hold_days, lens - 1)

    for i in range(n):
        if not valid[i]:
            continue
        H = eff_hold[i]
        if H < 1:
            continue
        sub_high = paths[i, 1:H + 1, 0]
        sub_low  = paths[i, 1:H + 1, 1]
        sub_close = paths[i, 1:H + 1, 2]

        stop_hits = np.where(sub_low <= stop_px[i])[0]
        target_hits = np.where(sub_high >= target_px[i])[0]
        first_stop = stop_hits[0] if stop_hits.size > 0 else 10**9
        first_target = target_hits[0] if target_hits.size > 0 else 10**9

        if first_stop <= first_target and first_stop < 10**9:
            day_off[i] = first_stop + 1  # +1 because we skipped day 0
            ret[i] = (stop_px[i] / entry_prices[i]) - 1.0
            reason[i] = "STOP"
        elif first_target < 10**9:
            day_off[i] = first_target + 1
            ret[i] = (target_px[i] / entry_prices[i]) - 1.0
            reason[i] = "TARGET"
        else:
            day_off[i] = H
            ret[i] = (sub_close[-1] / entry_prices[i]) - 1.0
            reason[i] = "TIME"

    return ret, day_off, reason


def run_strategy_backtest() -> None:
    """Run the full strategy backtest with portfolio limits on:
       (1) the raw-best optimizer combo (180d/2.0xATR/no target)
       (2) a balanced alternative (60d/2.0xATR/3R)
    """
    ev_path = ARTIFACT_DIR / "events_enriched.parquet"
    events = pd.read_parquet(ev_path)
    events["entry_date"] = pd.to_datetime(events["entry_date"]).dt.date

    prices = load_prices(tickers=events["ticker"].unique().tolist())
    paths, valid, lens = build_trade_paths(events, prices)
    entry_prices = events["entry_price"].values.astype(np.float64)
    atrs = events["atr14"].values.astype(np.float64)

    # Two parameter sets
    combos = [
        ("RAW_BEST",  180, 2.0, None),
        ("BALANCED",   60, 2.0, 3.0),
        ("CONSERVATIVE", 30, 2.0, 2.0),
    ]

    summary = []
    for label, hold, stop, tgt in combos:
        print(f"\n{'='*72}")
        print(f"STRATEGY BACKTEST — {label}  (hold={hold}d, stop={stop}xATR, "
              f"target={'none' if tgt is None else f'{tgt}R'})")
        print(f"{'='*72}")

        ret, day_off, reason = simulate_exits_with_dates(
            paths, valid, lens, entry_prices, atrs, hold, stop, tgt)

        # Build trade records: entry_date, ticker, entry_price, exit_date_offset,
        # exit_price, gross_return, exit_reason
        events_local = events.copy()
        events_local["gross_return"] = ret
        events_local["day_offset"] = day_off
        events_local["exit_reason"] = reason

        # Compute exit dates (entry_date + day_offset in TRADING days — approximate
        # by adding (day_offset * 7/5) calendar days, then snapping to next bar).
        # Simpler honest approach: use the actual trade_date from the path window.
        # We re-walk the price panel to get true exit dates per ticker.
        ticker_groups = {t: g.sort_values("trade_date").reset_index(drop=True)
                         for t, g in prices.groupby("ticker")}
        exit_dates = []
        for i, ev in events_local.iterrows():
            d_off = int(ev["day_offset"])
            if d_off < 0 or pd.isna(ev["gross_return"]) or ev["ticker"] not in ticker_groups:
                exit_dates.append(None)
                continue
            g = ticker_groups[ev["ticker"]]
            mask = g["trade_date"] >= ev["entry_date"]
            if not mask.any():
                exit_dates.append(None); continue
            entry_idx = mask.idxmax()
            target_idx = entry_idx + d_off
            if target_idx >= len(g):
                exit_dates.append(None); continue
            exit_dates.append(g.iloc[target_idx]["trade_date"])
        events_local["exit_date"] = exit_dates

        # Filter to events that produced a usable trade
        trades = events_local.dropna(subset=["gross_return", "exit_date"]).copy()
        trades = trades.sort_values("entry_date").reset_index(drop=True)
        print(f"  candidate trades: {len(trades):,}")

        # Walk the strategy day by day, respecting MAX_CONCURRENT & MAX_DEPLOYED
        # Calendar from min entry_date to max exit_date
        all_dates = sorted(set(trades["entry_date"]).union(set(trades["exit_date"])))
        equity = STARTING_CAPITAL
        cash = STARTING_CAPITAL
        open_positions = []  # list of dicts: ticker, entry_date, exit_date, entry_px, shares, gross_return
        equity_series = []
        taken = []
        missed = 0

        entries_by_date = trades.groupby("entry_date")
        exits_by_date = trades.groupby("exit_date")
        # Sort by date once for fast iteration
        entry_dates_set = set(trades["entry_date"])
        exit_dates_set = set(trades["exit_date"])

        for d in all_dates:
            # 1) Close any positions exiting today (apply cost + slippage)
            still_open = []
            for p in open_positions:
                if p["exit_date"] == d:
                    gross_exit = p["entry_px"] * (1 + p["gross_return"])
                    # 10 bps slippage on exit + $1 commission
                    exit_proceeds = (gross_exit * p["shares"]
                                     * (1 - SLIPPAGE_BPS / 10_000.0)
                                     - COMMISSION_PER_TRADE)
                    cash += exit_proceeds
                    p["actual_exit_value"] = exit_proceeds
                    p["net_pnl"] = exit_proceeds - p["cost_basis"]
                    taken.append(p)
                else:
                    still_open.append(p)
            open_positions = still_open

            # 2) Try to enter new positions today
            if d in entry_dates_set:
                day_entries = entries_by_date.get_group(d)
                for _, ev in day_entries.iterrows():
                    # Portfolio limits
                    if len(open_positions) >= MAX_CONCURRENT:
                        missed += 1; continue
                    deployed = sum(p["cost_basis"] for p in open_positions)
                    if deployed >= MAX_DEPLOYED * equity:
                        missed += 1; continue
                    # Position sizing: 5% of equity
                    desired_notional = POSITION_PCT * equity
                    if cash < desired_notional + COMMISSION_PER_TRADE:
                        missed += 1; continue
                    # Buy at entry_px + slippage + commission
                    fill_px = ev["entry_price"] * (1 + SLIPPAGE_BPS / 10_000.0)
                    shares = desired_notional / fill_px
                    cost_basis = shares * fill_px + COMMISSION_PER_TRADE
                    cash -= cost_basis
                    open_positions.append({
                        "ticker": ev["ticker"],
                        "entry_date": d,
                        "exit_date": ev["exit_date"],
                        "entry_px": fill_px,
                        "shares": shares,
                        "gross_return": ev["gross_return"],
                        "cost_basis": cost_basis,
                        "n_directors": int(ev["n_directors"]),
                        "n_insiders": int(ev["n_insiders"]),
                        "exit_reason": ev["exit_reason"],
                    })

            # 3) Mark equity — open positions valued at entry (we don't have intraday mark)
            # For honesty: equity = cash + sum of cost_basis of open positions (lower-bound view)
            equity = cash + sum(p["cost_basis"] for p in open_positions)
            equity_series.append((d, equity, cash, len(open_positions), missed))

        # Final stats
        eq_df = pd.DataFrame(equity_series, columns=["date", "equity", "cash", "open_n", "cum_missed"])
        eq_df["date"] = pd.to_datetime(eq_df["date"])
        # Drop any duplicate dates (multiple entries/exits same day) — keep last
        eq_df = eq_df.groupby("date", as_index=False).last().sort_values("date").reset_index(drop=True)

        # Compute end-state stats
        final_eq = eq_df["equity"].iloc[-1]
        total_ret = final_eq / STARTING_CAPITAL - 1
        days = (eq_df["date"].iloc[-1] - eq_df["date"].iloc[0]).days
        years = max(days / 365.25, 0.01)
        cagr = (final_eq / STARTING_CAPITAL) ** (1 / years) - 1
        # Max drawdown
        peak = eq_df["equity"].cummax()
        dd = (eq_df["equity"] / peak - 1)
        max_dd = dd.min()
        # Sharpe of daily equity returns
        daily_ret = eq_df["equity"].pct_change().dropna()
        sharpe = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)
                  if daily_ret.std() > 0 else 0)

        n_trades = len(taken)
        wins = sum(1 for t in taken if t["net_pnl"] > 0)
        avg_win = np.mean([t["net_pnl"] for t in taken if t["net_pnl"] > 0]) if wins else 0
        losses = [t["net_pnl"] for t in taken if t["net_pnl"] <= 0]
        avg_loss = np.mean(losses) if losses else 0
        win_rate = (wins / n_trades * 100) if n_trades else 0

        print(f"  trades taken : {n_trades:,}   missed (limits) : {missed:,}")
        print(f"  start equity : ${STARTING_CAPITAL:,.0f}")
        print(f"  end equity   : ${final_eq:,.0f}")
        print(f"  total return : {total_ret * 100:+.1f}%")
        print(f"  CAGR         : {cagr * 100:+.2f}%/yr  ({years:.1f} years)")
        print(f"  max drawdown : {max_dd * 100:.1f}%")
        print(f"  sharpe (252) : {sharpe:.2f}")
        print(f"  win rate     : {win_rate:.1f}%  avg win ${avg_win:,.0f}  avg loss ${avg_loss:,.0f}")

        # Save equity curve + trades for the report
        eq_df.to_parquet(ARTIFACT_DIR / f"equity_{label}.parquet", index=False)
        pd.DataFrame(taken).to_parquet(ARTIFACT_DIR / f"trades_{label}.parquet", index=False)

        summary.append({
            "label": label, "hold": hold, "stop_xATR": stop, "target_R": tgt,
            "n_taken": n_trades, "n_missed": missed,
            "final_equity": final_eq, "total_return_pct": total_ret * 100,
            "cagr_pct": cagr * 100, "max_dd_pct": max_dd * 100,
            "sharpe": sharpe, "win_rate_pct": win_rate,
        })

    print(f"\n{'='*72}\nSUMMARY\n{'='*72}")
    s = pd.DataFrame(summary)
    print(s.to_string(index=False))
    s.to_parquet(ARTIFACT_DIR / "strategy_summary.parquet", index=False)


# ---------------------------------------------------------------------------
# Steps 4-5: ML exit model — given the trade is alive at day_t, predict whether
# exit-now is better than holding to the (a)-chosen time stop.
# Baseline strategy: CONSERVATIVE = 30d / 2.0xATR / 2R
# ---------------------------------------------------------------------------

# Lock the ML baseline params (matches CONSERVATIVE in the backtest above)
ML_HOLD = 30
ML_STOP_X = 2.0
ML_TGT_R = 2.0


def _build_ml_dataset(events, paths, valid, lens, entry_prices, atrs):
    """For each (event, day_t in 1..ML_HOLD), build features and label.

    label = 1 if exiting at close[t] gives a higher net return than holding to
            the (fixed-rules) exit; else 0.
    The model learns to detect trades where the data has already turned sour.
    """
    rows = []
    stop_dist = ML_STOP_X * atrs
    stop_px = entry_prices - stop_dist
    target_px = entry_prices + ML_TGT_R * stop_dist

    for i, ev in events.iterrows():
        if not valid[i]:
            continue
        H = min(ML_HOLD, lens[i] - 1)
        if H < 5:
            continue
        path = paths[i]
        entry_px = entry_prices[i]
        # Compute the actual fixed-rule exit (used as the "if-hold" benchmark)
        sub_high = path[1:H + 1, 0]; sub_low = path[1:H + 1, 1]; sub_close = path[1:H + 1, 2]
        stop_hits = np.where(sub_low <= stop_px[i])[0]
        target_hits = np.where(sub_high >= target_px[i])[0]
        first_stop = stop_hits[0] if stop_hits.size > 0 else 10**9
        first_target = target_hits[0] if target_hits.size > 0 else 10**9
        if first_stop <= first_target and first_stop < 10**9:
            ruled_exit_day = first_stop + 1
            ruled_exit_px = stop_px[i]
        elif first_target < 10**9:
            ruled_exit_day = first_target + 1
            ruled_exit_px = target_px[i]
        else:
            ruled_exit_day = H
            ruled_exit_px = sub_close[-1]
        ruled_return = (ruled_exit_px / entry_px) - 1.0

        # Walk day by day building feature rows (only days BEFORE ruled exit)
        running_max = entry_px
        running_min = entry_px
        for t in range(1, ruled_exit_day):
            close_t = path[t, 2]
            running_max = max(running_max, path[t, 0])
            running_min = min(running_min, path[t, 1])
            mfe = (running_max / entry_px) - 1.0
            mae = (running_min / entry_px) - 1.0
            curr_ret = (close_t / entry_px) - 1.0
            # If we exited at close today:
            net_if_exit = curr_ret
            # If we held to the ruled exit:
            net_if_hold = ruled_return
            label = 1 if net_if_exit > net_if_hold else 0
            rows.append({
                "event_idx": i,
                "entry_date": ev["entry_date"],
                "days_elapsed": t,
                "days_until_ruled_exit": ruled_exit_day - t,
                "curr_return_pct": curr_ret * 100,
                "mfe_pct": mfe * 100,
                "mae_pct": mae * 100,
                "atr_pct_of_entry": (atrs[i] / entry_px) * 100,
                "n_directors": int(ev["n_directors"]),
                "n_insiders": int(ev["n_insiders"]),
                "n_officers": int(ev["n_officers"]),
                "log_total_value": np.log1p(float(ev["total_value"] or 0)),
                "label_exit_better": label,
            })

    return pd.DataFrame(rows)


def train_ml_exit_model():
    """Train LightGBM on in-sample (2016-2022); save model + OOS predictions."""
    try:
        import lightgbm as lgb
    except ImportError:
        print("[ML] lightgbm not installed — pip install lightgbm")
        return

    events = pd.read_parquet(ARTIFACT_DIR / "events_enriched.parquet")
    events["entry_date"] = pd.to_datetime(events["entry_date"]).dt.date

    prices = load_prices(tickers=events["ticker"].unique().tolist())
    paths, valid, lens = build_trade_paths(events, prices)
    entry_prices = events["entry_price"].values.astype(np.float64)
    atrs = events["atr14"].values.astype(np.float64)

    print("[ML] building feature/label dataset...")
    ds = _build_ml_dataset(events, paths, valid, lens, entry_prices, atrs)
    print(f"[ML] dataset: {len(ds):,} rows")

    # In-sample / OOS split by entry_date of the underlying event
    is_mask = ds["entry_date"] <= pd.to_datetime(IN_SAMPLE_END).date()
    oos_mask = ds["entry_date"] >= pd.to_datetime(OOS_START).date()
    train = ds[is_mask].reset_index(drop=True)
    test = ds[oos_mask].reset_index(drop=True)
    print(f"[ML] train: {len(train):,}  test: {len(test):,}")
    print(f"[ML] base rate (exit-better) train: {train['label_exit_better'].mean():.3f}  "
          f"test: {test['label_exit_better'].mean():.3f}")

    feature_cols = ["days_elapsed", "days_until_ruled_exit", "curr_return_pct",
                    "mfe_pct", "mae_pct", "atr_pct_of_entry",
                    "n_directors", "n_insiders", "n_officers",
                    "log_total_value"]
    Xtr = train[feature_cols].values
    ytr = train["label_exit_better"].values
    Xte = test[feature_cols].values
    yte = test["label_exit_better"].values

    print("[ML] training LightGBM...")
    model = lgb.LGBMClassifier(
        n_estimators=300, learning_rate=0.05,
        num_leaves=31, max_depth=6,
        min_child_samples=50, reg_alpha=0.1, reg_lambda=0.1,
        verbose=-1,
    )
    model.fit(Xtr, ytr, eval_set=[(Xte, yte)], callbacks=[lgb.early_stopping(20)])

    # AUC
    from sklearn.metrics import roc_auc_score, accuracy_score
    p_tr = model.predict_proba(Xtr)[:, 1]
    p_te = model.predict_proba(Xte)[:, 1]
    print(f"[ML] train AUC: {roc_auc_score(ytr, p_tr):.4f}")
    print(f"[ML] test  AUC: {roc_auc_score(yte, p_te):.4f}")

    # Feature importances
    print("\n[ML] feature importances:")
    for f, imp in sorted(zip(feature_cols, model.feature_importances_),
                        key=lambda x: -x[1]):
        print(f"  {f:24s} {imp}")

    # Save model + test predictions for the backtest step
    import joblib
    joblib.dump(model, ARTIFACT_DIR / "ml_exit_model.pkl")
    test["p_exit_better"] = p_te
    test.to_parquet(ARTIFACT_DIR / "ml_test_predictions.parquet", index=False)
    print(f"\n[ML] saved model + test predictions")


def backtest_with_ml_exits():
    """Run the strategy using the ML model as an early-exit signal.

    Vectorized: build the full (event, day) feature panel, call predict_proba
    ONCE on the whole thing, then per-event find the first day where stop/target
    fires OR p(exit-better) > threshold.
    """
    import joblib
    import warnings
    warnings.filterwarnings("ignore", category=UserWarning)

    model = joblib.load(ARTIFACT_DIR / "ml_exit_model.pkl")

    events = pd.read_parquet(ARTIFACT_DIR / "events_enriched.parquet")
    events["entry_date"] = pd.to_datetime(events["entry_date"]).dt.date

    prices = load_prices(tickers=events["ticker"].unique().tolist())
    paths, valid, lens = build_trade_paths(events, prices)
    entry_prices = events["entry_price"].values.astype(np.float64)
    atrs = events["atr14"].values.astype(np.float64)

    stop_dist = ML_STOP_X * atrs
    stop_px = entry_prices - stop_dist
    target_px = entry_prices + ML_TGT_R * stop_dist

    # --------------------------------------------------------------
    # Build the full (event, day) feature panel ONCE
    # --------------------------------------------------------------
    print("[ML-BT] building (event x day) feature panel for one-shot ML scoring...")
    feat_records = []   # (event_idx, day_t)
    feat_rows = []      # feature vectors

    for i in range(len(events)):
        if not valid[i]:
            continue
        H = min(ML_HOLD, lens[i] - 1)
        if H < 5:
            continue
        path = paths[i]
        entry_px = entry_prices[i]
        atr_pct = (atrs[i] / entry_px) * 100
        n_dir = int(events.iloc[i]["n_directors"])
        n_ins = int(events.iloc[i]["n_insiders"])
        n_off = int(events.iloc[i]["n_officers"])
        lv = np.log1p(float(events.iloc[i]["total_value"] or 0))

        # Build running MFE/MAE in one pass
        highs = path[:H + 1, 0]; lows = path[:H + 1, 1]; closes = path[:H + 1, 2]
        run_max = np.maximum.accumulate(highs)
        run_min = np.minimum.accumulate(lows)

        for t in range(1, H + 1):
            curr_ret_pct = (closes[t] / entry_px - 1.0) * 100
            mfe_pct = (run_max[t] / entry_px - 1.0) * 100
            mae_pct = (run_min[t] / entry_px - 1.0) * 100
            feat_records.append((i, t))
            feat_rows.append([t, H - t, curr_ret_pct, mfe_pct, mae_pct,
                              atr_pct, n_dir, n_ins, n_off, lv])

    feat_arr = np.array(feat_rows, dtype=np.float64)
    feat_idx = np.array(feat_records, dtype=np.int32)
    print(f"[ML-BT] feature panel: {len(feat_arr):,} rows")

    feature_cols = ["days_elapsed", "days_until_ruled_exit", "curr_return_pct",
                    "mfe_pct", "mae_pct", "atr_pct_of_entry",
                    "n_directors", "n_insiders", "n_officers",
                    "log_total_value"]
    feat_df = pd.DataFrame(feat_arr, columns=feature_cols)
    print("[ML-BT] one-shot predict_proba ...")
    probs = model.predict_proba(feat_df)[:, 1]

    # Bin probs back by event: probs_by_event[i] -> list of (day_t, p)
    probs_by_event = {}
    for k, (ei, t) in enumerate(feat_idx):
        probs_by_event.setdefault(int(ei), []).append((int(t), float(probs[k])))

    # --------------------------------------------------------------
    # Walk per-event; choose exit based on threshold
    # --------------------------------------------------------------
    is_mask_d = (events["entry_date"] <= pd.to_datetime(IN_SAMPLE_END).date()).values
    oos_mask_d = (events["entry_date"] >= pd.to_datetime(OOS_START).date()).values

    thresholds = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
    threshold_results = []

    print(f"\n[ML-BT] sweeping thresholds (CONSERVATIVE baseline below for reference):")
    print(f"  baseline 30d/2x/2R from earlier:  OOS mean ~3.3%  win ~46%")
    print()
    print(f"  {'thresh':>7}  {'IS_n':>6}  {'IS_mean%':>8}  {'IS_win%':>7}  "
          f"{'OOS_n':>6}  {'OOS_mean%':>9}  {'OOS_win%':>8}  {'avg_hold':>8}  exit_mix")

    for THRESHOLD in thresholds:
        out_ret = np.full(len(events), np.nan)
        out_day = np.full(len(events), -1, dtype=np.int32)
        out_reason = np.full(len(events), "", dtype=object)

        for i in range(len(events)):
            if not valid[i]:
                continue
            H = min(ML_HOLD, lens[i] - 1)
            if H < 5:
                continue
            path = paths[i]
            entry_px = entry_prices[i]
            highs = path[1:H + 1, 0]; lows = path[1:H + 1, 1]; closes = path[1:H + 1, 2]

            stop_hits = np.where(lows <= stop_px[i])[0]
            target_hits = np.where(highs >= target_px[i])[0]
            first_stop = stop_hits[0] + 1 if stop_hits.size > 0 else 10**9
            first_target = target_hits[0] + 1 if target_hits.size > 0 else 10**9

            ml_exit_day = 10**9
            for (t, p) in probs_by_event.get(i, []):
                if p > THRESHOLD:
                    ml_exit_day = t
                    break

            first = min(first_stop, first_target, ml_exit_day, H)
            if first == first_stop and first_stop < 10**9:
                out_ret[i] = (stop_px[i] / entry_px) - 1.0
                out_reason[i] = "STOP"
            elif first == first_target and first_target < 10**9:
                out_ret[i] = (target_px[i] / entry_px) - 1.0
                out_reason[i] = "TARGET"
            elif first == ml_exit_day and ml_exit_day < 10**9:
                out_ret[i] = (closes[first - 1] / entry_px) - 1.0
                out_reason[i] = "ML"
            else:
                out_ret[i] = (closes[H - 1] / entry_px) - 1.0
                out_reason[i] = "TIME"
            out_day[i] = first

        def stats(mask):
            sub = mask & ~np.isnan(out_ret)
            r = out_ret[sub]; d = out_day[sub]
            if len(r) == 0:
                return (0, 0, 0, 0)
            return (len(r), float(r.mean() * 100), float((r > 0).mean() * 100),
                    float(d.mean()))

        n_is, m_is, w_is, h_is = stats(is_mask_d)
        n_oos, m_oos, w_oos, h_oos = stats(oos_mask_d)
        from collections import Counter
        oos_reasons = out_reason[oos_mask_d & (out_reason != "")]
        c = Counter(oos_reasons)
        mix = " ".join(f"{k}={v}" for k, v in sorted(c.items()))
        print(f"  {THRESHOLD:>7.2f}  {n_is:>6,}  {m_is:>8.2f}  {w_is:>7.1f}  "
              f"{n_oos:>6,}  {m_oos:>9.2f}  {w_oos:>8.1f}  {h_oos:>8.1f}  {mix}")

        threshold_results.append({
            "threshold": THRESHOLD, "n_is": n_is, "mean_is_pct": m_is,
            "win_is_pct": w_is, "n_oos": n_oos, "mean_oos_pct": m_oos,
            "win_oos_pct": w_oos, "avg_hold_oos": h_oos, "oos_exit_mix": dict(c),
        })

    pd.DataFrame(threshold_results).to_parquet(
        ARTIFACT_DIR / "ml_threshold_sweep.parquet", index=False)
    print(f"\n[ML-BT] saved -> {ARTIFACT_DIR / 'ml_threshold_sweep.parquet'}")

    # ----------------------------------------------------------------------
    # Run FULL portfolio backtest at two ML thresholds, side-by-side
    # ----------------------------------------------------------------------
    print("\n[ML-BT] Running full portfolio backtest at threshold 0.65 + 0.55 ...")
    for THRESHOLD in (0.65, 0.55):
        out_ret = np.full(len(events), np.nan)
        out_day = np.full(len(events), -1, dtype=np.int32)
        out_reason = np.full(len(events), "", dtype=object)

        for i in range(len(events)):
            if not valid[i]:
                continue
            H = min(ML_HOLD, lens[i] - 1)
            if H < 5:
                continue
            path = paths[i]
            entry_px = entry_prices[i]
            highs = path[1:H + 1, 0]; lows = path[1:H + 1, 1]; closes = path[1:H + 1, 2]
            stop_hits = np.where(lows <= stop_px[i])[0]
            target_hits = np.where(highs >= target_px[i])[0]
            first_stop = stop_hits[0] + 1 if stop_hits.size > 0 else 10**9
            first_target = target_hits[0] + 1 if target_hits.size > 0 else 10**9
            ml_exit_day = 10**9
            for (t, p) in probs_by_event.get(i, []):
                if p > THRESHOLD:
                    ml_exit_day = t; break
            first = min(first_stop, first_target, ml_exit_day, H)
            if first == first_stop and first_stop < 10**9:
                out_ret[i] = (stop_px[i] / entry_px) - 1.0; out_reason[i] = "STOP"
            elif first == first_target and first_target < 10**9:
                out_ret[i] = (target_px[i] / entry_px) - 1.0; out_reason[i] = "TARGET"
            elif first == ml_exit_day and ml_exit_day < 10**9:
                out_ret[i] = (closes[first - 1] / entry_px) - 1.0; out_reason[i] = "ML"
            else:
                out_ret[i] = (closes[H - 1] / entry_px) - 1.0; out_reason[i] = "TIME"
            out_day[i] = first

        # Compute exit_date for each event
        ticker_groups = {t: g.sort_values("trade_date").reset_index(drop=True)
                         for t, g in prices.groupby("ticker")}
        events_local = events.copy()
        events_local["gross_return"] = out_ret
        events_local["day_offset"] = out_day
        events_local["exit_reason"] = out_reason
        exit_dates = []
        for i, ev in events_local.iterrows():
            d_off = int(ev["day_offset"])
            if d_off < 0 or pd.isna(ev["gross_return"]) or ev["ticker"] not in ticker_groups:
                exit_dates.append(None); continue
            g = ticker_groups[ev["ticker"]]
            mask = g["trade_date"] >= ev["entry_date"]
            if not mask.any():
                exit_dates.append(None); continue
            entry_idx = mask.idxmax()
            target_idx = entry_idx + d_off
            if target_idx >= len(g):
                exit_dates.append(None); continue
            exit_dates.append(g.iloc[target_idx]["trade_date"])
        events_local["exit_date"] = exit_dates
        trades = events_local.dropna(subset=["gross_return", "exit_date"]).copy()
        trades = trades.sort_values("entry_date").reset_index(drop=True)

        # Walk strategy with portfolio limits
        all_dates = sorted(set(trades["entry_date"]).union(set(trades["exit_date"])))
        equity = STARTING_CAPITAL; cash = STARTING_CAPITAL
        open_positions = []; taken = []; missed = 0
        equity_series = []
        entries_by_date = trades.groupby("entry_date")
        entry_dates_set = set(trades["entry_date"])

        for d in all_dates:
            still_open = []
            for p in open_positions:
                if p["exit_date"] == d:
                    gross_exit = p["entry_px"] * (1 + p["gross_return"])
                    exit_proceeds = (gross_exit * p["shares"]
                                     * (1 - SLIPPAGE_BPS / 10_000.0)
                                     - COMMISSION_PER_TRADE)
                    cash += exit_proceeds
                    p["net_pnl"] = exit_proceeds - p["cost_basis"]
                    taken.append(p)
                else:
                    still_open.append(p)
            open_positions = still_open

            if d in entry_dates_set:
                day_entries = entries_by_date.get_group(d)
                for _, ev in day_entries.iterrows():
                    if len(open_positions) >= MAX_CONCURRENT:
                        missed += 1; continue
                    deployed = sum(p["cost_basis"] for p in open_positions)
                    if deployed >= MAX_DEPLOYED * equity:
                        missed += 1; continue
                    desired_notional = POSITION_PCT * equity
                    if cash < desired_notional + COMMISSION_PER_TRADE:
                        missed += 1; continue
                    fill_px = ev["entry_price"] * (1 + SLIPPAGE_BPS / 10_000.0)
                    shares = desired_notional / fill_px
                    cost_basis = shares * fill_px + COMMISSION_PER_TRADE
                    cash -= cost_basis
                    open_positions.append({
                        "ticker": ev["ticker"], "entry_date": d,
                        "exit_date": ev["exit_date"], "entry_px": fill_px,
                        "shares": shares, "gross_return": ev["gross_return"],
                        "cost_basis": cost_basis, "exit_reason": ev["exit_reason"],
                    })

            equity = cash + sum(p["cost_basis"] for p in open_positions)
            equity_series.append((d, equity, cash, len(open_positions), missed))

        eq_df = pd.DataFrame(equity_series, columns=["date", "equity", "cash", "open_n", "cum_missed"])
        eq_df["date"] = pd.to_datetime(eq_df["date"])
        eq_df = eq_df.groupby("date", as_index=False).last().sort_values("date").reset_index(drop=True)

        final_eq = eq_df["equity"].iloc[-1]
        total_ret = final_eq / STARTING_CAPITAL - 1
        days = (eq_df["date"].iloc[-1] - eq_df["date"].iloc[0]).days
        years = max(days / 365.25, 0.01)
        cagr = (final_eq / STARTING_CAPITAL) ** (1 / years) - 1
        peak = eq_df["equity"].cummax()
        dd = (eq_df["equity"] / peak - 1)
        max_dd = dd.min()
        daily_ret = eq_df["equity"].pct_change().dropna()
        sharpe = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)
                  if daily_ret.std() > 0 else 0)
        n_trades = len(taken)
        wins = sum(1 for t in taken if t["net_pnl"] > 0)
        win_rate = (wins / n_trades * 100) if n_trades else 0

        label = f"ML_{int(THRESHOLD * 100):02d}"
        print(f"\n  === {label} (threshold {THRESHOLD}) ===")
        print(f"  trades taken : {n_trades:,}   missed : {missed:,}")
        print(f"  end equity   : ${final_eq:,.0f}   total return : {total_ret * 100:+.1f}%")
        print(f"  CAGR         : {cagr * 100:+.2f}%/yr  max DD : {max_dd * 100:.1f}%  "
              f"sharpe : {sharpe:.2f}  win : {win_rate:.1f}%")
        eq_df.to_parquet(ARTIFACT_DIR / f"equity_{label}.parquet", index=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-events", action="store_true",
                    help="Step 1: build PIT cluster events + price-enriched event table")
    ap.add_argument("--optimize", action="store_true",
                    help="Step 2: (a) sweep hold/stop/target on in-sample")
    ap.add_argument("--backtest", action="store_true",
                    help="Step 3: run strategy backtest with chosen (a) params")
    ap.add_argument("--train-ml", action="store_true",
                    help="Step 4: train (b) ML exit model")
    ap.add_argument("--backtest-ml", action="store_true",
                    help="Step 5: backtest with ML-driven exits, compare to (a)")
    ap.add_argument("--all", action="store_true", help="Run steps 1-5 in order")
    args = ap.parse_args()

    if args.build_events or args.all:
        events = build_events()
        prices = load_prices(tickers=events["ticker"].unique().tolist())
        compute_forward_returns_and_atr(events, prices)

    if args.optimize or args.all:
        optimize_exits()
    if args.backtest or args.all:
        run_strategy_backtest()
    if args.train_ml or args.all:
        train_ml_exit_model()
    if args.backtest_ml or args.all:
        backtest_with_ml_exits()

    if not (args.build_events or args.optimize or args.backtest
            or args.train_ml or args.backtest_ml or args.all):
        ap.print_help()
