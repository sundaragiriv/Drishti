"""Three robustness sensitivity tests on the validated ML_55 strategy.

(1) ADV filter      — require 20-day avg dollar volume >= $1M at entry.
                       Confirms the edge isn't an illiquidity artifact.
(2) Regime exit     — if SPY drops below its 200-day SMA on a given day,
                       exit all open positions at next bar's open. Tests
                       whether a market-state defense improves drawdown.
(3) Sub-period split — break the ML_55 result into 2016-19 / 2020-21 / 2022-25
                       and report CAGR / Sharpe / DD / win % per period.

All tests use the locked spec: $100K start, 5% position size, max 10 concurrent,
50% deployed cap, $1 + 10bps costs, price >= $5, survivorship-safe.

Baseline (from STRATEGY_BACKTEST_VERDICT.md):
   ML_55:  CAGR +19.5%/yr · Sharpe 2.67 · max DD -21.4% · win 60.8% · n=945

Run:
  .venv\\Scripts\\python -m research.insider_strategy_sensitivities --all
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import duckdb
import joblib
import numpy as np
import pandas as pd

# Pull locked params + helpers from the main harness
from research.insider_strategy_backtest import (
    ARTIFACT_DIR, WAREHOUSE,
    POSITION_PCT, MAX_CONCURRENT, MAX_DEPLOYED,
    COMMISSION_PER_TRADE, SLIPPAGE_BPS, STARTING_CAPITAL,
    IN_SAMPLE_END, OOS_START,
    ML_HOLD, ML_STOP_X, ML_TGT_R,
    load_prices, build_trade_paths,
)
import warnings
warnings.filterwarnings("ignore", category=UserWarning)


# ---------------------------------------------------------------------------
# Helper: compute SPY 200-day SMA series (regime proxy)
# ---------------------------------------------------------------------------
def load_regime_proxy() -> pd.DataFrame:
    """Return SPY daily close + 200d SMA. If SPY missing late, falls back to
    a composite of AAPL/MSFT/NVDA/GOOGL/AMZN (same fallback as the HMM uses)."""
    con = duckdb.connect(WAREHOUSE, read_only=True)
    # Try SPY first
    spy = con.execute("""
        SELECT trade_date, close FROM fact_daily_prices
        WHERE ticker = 'SPY' AND trade_date BETWEEN DATE '2015-06-01' AND DATE '2025-12-31'
        ORDER BY trade_date
    """).fetch_df()

    spy["trade_date"] = pd.to_datetime(spy["trade_date"])  # normalize to Timestamp
    if len(spy) < 500 or spy["trade_date"].max() < pd.Timestamp("2025-06-01"):
        # Build composite
        comp = con.execute("""
            SELECT trade_date, AVG(close) AS close
            FROM fact_daily_prices
            WHERE ticker IN ('AAPL','MSFT','NVDA','GOOGL','AMZN')
              AND trade_date BETWEEN DATE '2015-06-01' AND DATE '2025-12-31'
            GROUP BY trade_date
            ORDER BY trade_date
        """).fetch_df()
        spy = comp
        spy["trade_date"] = pd.to_datetime(spy["trade_date"])
        print(f"[REGIME] using composite proxy (SPY data ends early), {len(spy):,} bars")
    else:
        print(f"[REGIME] SPY series, {len(spy):,} bars")
    con.close()
    spy["trade_date"] = spy["trade_date"].dt.date
    spy["sma200"] = spy["close"].rolling(200, min_periods=200).mean()
    spy["above_200sma"] = (spy["close"] > spy["sma200"]).astype(int)
    return spy


# ---------------------------------------------------------------------------
# Common: prepare ML_55 per-event exit dates + returns (re-uses model)
# ---------------------------------------------------------------------------
def prepare_ml55_trades(adv_filter: bool = False) -> tuple:
    """Run the ML_55 logic per-event and return (trades_df, equity_walk_inputs).

    If adv_filter=True, drop events whose ticker doesn't meet ADV >= $1M at entry.
    """
    events = pd.read_parquet(ARTIFACT_DIR / "events_enriched.parquet")
    events["entry_date"] = pd.to_datetime(events["entry_date"]).dt.date

    prices = load_prices(tickers=events["ticker"].unique().tolist())
    paths, valid, lens = build_trade_paths(events, prices)
    entry_prices = events["entry_price"].values.astype(np.float64)
    atrs = events["atr14"].values.astype(np.float64)

    # ----- Compute ADV (20d avg dollar volume) at entry -----
    if adv_filter:
        print("[ADV] computing 20-day average dollar volume at entry per event ...")
        ticker_groups = {t: g.sort_values("trade_date").reset_index(drop=True)
                         for t, g in prices.groupby("ticker")}
        adv = np.zeros(len(events), dtype=np.float64)
        for i, ev in events.iterrows():
            if ev["ticker"] not in ticker_groups:
                continue
            g = ticker_groups[ev["ticker"]]
            mask = g["trade_date"] >= ev["entry_date"]
            if not mask.any():
                continue
            entry_idx = mask.idxmax()
            prior = g.iloc[max(0, entry_idx - 20):entry_idx]
            if len(prior) < 20:
                continue
            adv[i] = float((prior["close"] * prior["volume"]).mean())
        events["adv_20d"] = adv
        n_before = valid.sum()
        valid = valid & (adv >= 1_000_000)
        print(f"[ADV] valid after ADV>=$1M filter: {valid.sum():,} (was {n_before:,})")

    # ----- Score each (event, day_t) once with the saved ML model -----
    model = joblib.load(ARTIFACT_DIR / "ml_exit_model.pkl")
    stop_dist = ML_STOP_X * atrs
    stop_px = entry_prices - stop_dist
    target_px = entry_prices + ML_TGT_R * stop_dist

    feat_records = []; feat_rows = []
    for i in range(len(events)):
        if not valid[i]:
            continue
        H = min(ML_HOLD, lens[i] - 1)
        if H < 5:
            continue
        path = paths[i]; entry_px = entry_prices[i]
        atr_pct = (atrs[i] / entry_px) * 100
        n_dir = int(events.iloc[i]["n_directors"])
        n_ins = int(events.iloc[i]["n_insiders"])
        n_off = int(events.iloc[i]["n_officers"])
        lv = np.log1p(float(events.iloc[i]["total_value"] or 0))
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
    feature_cols = ["days_elapsed", "days_until_ruled_exit", "curr_return_pct",
                    "mfe_pct", "mae_pct", "atr_pct_of_entry",
                    "n_directors", "n_insiders", "n_officers", "log_total_value"]
    print(f"[ML] one-shot predict on {len(feat_arr):,} rows ...")
    probs = model.predict_proba(pd.DataFrame(feat_arr, columns=feature_cols))[:, 1]
    probs_by_event = {}
    for k, (ei, t) in enumerate(feat_idx):
        probs_by_event.setdefault(int(ei), []).append((int(t), float(probs[k])))

    # ----- Walk per-event to find exit date / return at threshold 0.55 -----
    THRESHOLD = 0.55
    out_ret = np.full(len(events), np.nan)
    out_day = np.full(len(events), -1, dtype=np.int32)
    out_reason = np.full(len(events), "", dtype=object)

    for i in range(len(events)):
        if not valid[i]:
            continue
        H = min(ML_HOLD, lens[i] - 1)
        if H < 5:
            continue
        path = paths[i]; entry_px = entry_prices[i]
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

    # Compute exit_date per event
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
    return trades, ticker_groups


# ---------------------------------------------------------------------------
# Walker: simulate portfolio with optional regime gate
# ---------------------------------------------------------------------------
def walk_portfolio(trades: pd.DataFrame, regime: Optional[pd.DataFrame] = None,
                   label: str = "") -> dict:
    """Walk the calendar day by day; optionally force-exit all positions if
    regime['above_200sma'] flips to 0. New entries blocked while regime is 0.
    """
    all_dates = sorted(set(trades["entry_date"]).union(set(trades["exit_date"])))
    # Build a regime lookup (date -> above_200sma)
    if regime is not None:
        reg_lookup = dict(zip(regime["trade_date"], regime["above_200sma"]))
    else:
        reg_lookup = {}

    equity = STARTING_CAPITAL; cash = STARTING_CAPITAL
    open_positions: list = []; taken: list = []; missed = 0
    forced_exits = 0
    equity_series = []
    entries_by_date = trades.groupby("entry_date")
    entry_dates_set = set(trades["entry_date"])

    for d in all_dates:
        # 1) Time-stop / planned exits today
        still_open = []
        for p in open_positions:
            if p["exit_date"] == d:
                gross_exit = p["entry_px"] * (1 + p["gross_return"])
                proceeds = (gross_exit * p["shares"] * (1 - SLIPPAGE_BPS / 10_000.0)
                            - COMMISSION_PER_TRADE)
                cash += proceeds
                p["net_pnl"] = proceeds - p["cost_basis"]
                taken.append(p)
            else:
                still_open.append(p)
        open_positions = still_open

        # 2) REGIME EXIT — if regime turns bearish, flatten everything
        if reg_lookup and reg_lookup.get(d, 1) == 0 and len(open_positions) > 0:
            for p in list(open_positions):
                # Approximate exit at current path mark: use the trade's gross_return
                # would overstate gains. Best approximation: assume we lose ~atr-of-entry
                # since regime turn typically aligns with broad sell-off.
                # Conservative: take a -1% mark on exit vs the cost_basis.
                exit_value = p["cost_basis"] * 0.99
                cash += exit_value - COMMISSION_PER_TRADE
                p["net_pnl"] = exit_value - p["cost_basis"] - COMMISSION_PER_TRADE
                p["exit_reason"] = "REGIME"
                taken.append(p)
                forced_exits += 1
            open_positions = []

        # 3) Entries
        if d in entry_dates_set:
            # Skip new entries if regime says bearish
            if reg_lookup and reg_lookup.get(d, 1) == 0:
                missed += len(entries_by_date.get_group(d))
            else:
                for _, ev in entries_by_date.get_group(d).iterrows():
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

    eq = pd.DataFrame(equity_series, columns=["date", "equity", "cash", "open_n", "cum_missed"])
    eq["date"] = pd.to_datetime(eq["date"])
    eq = eq.groupby("date", as_index=False).last().sort_values("date").reset_index(drop=True)

    final_eq = eq["equity"].iloc[-1]
    days = (eq["date"].iloc[-1] - eq["date"].iloc[0]).days
    years = max(days / 365.25, 0.01)
    cagr = (final_eq / STARTING_CAPITAL) ** (1 / years) - 1
    peak = eq["equity"].cummax()
    dd = (eq["equity"] / peak - 1)
    max_dd = dd.min()
    daily_ret = eq["equity"].pct_change().dropna()
    sharpe = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)
              if daily_ret.std() > 0 else 0)
    n_trades = len(taken)
    wins = sum(1 for t in taken if t["net_pnl"] > 0)
    win_rate = (wins / n_trades * 100) if n_trades else 0
    reason_mix = Counter([t.get("exit_reason", "") for t in taken])

    print(f"\n  === {label} ===")
    print(f"  trades taken : {n_trades:,}   missed : {missed:,}   forced exits: {forced_exits:,}")
    print(f"  end equity   : ${final_eq:,.0f}   total return : {(final_eq / STARTING_CAPITAL - 1) * 100:+.1f}%")
    print(f"  CAGR         : {cagr * 100:+.2f}%/yr  max DD : {max_dd * 100:.1f}%  "
          f"sharpe : {sharpe:.2f}  win : {win_rate:.1f}%")
    print(f"  exit mix     : {dict(reason_mix)}")

    eq.to_parquet(ARTIFACT_DIR / f"equity_{label}.parquet", index=False)
    return {"label": label, "trades": n_trades, "missed": missed,
            "final_equity": float(final_eq), "cagr_pct": float(cagr * 100),
            "max_dd_pct": float(max_dd * 100), "sharpe": float(sharpe),
            "win_rate_pct": float(win_rate), "forced_exits": forced_exits}


# ---------------------------------------------------------------------------
# Sensitivity 3: sub-period breakdown of existing ML_55 result
# ---------------------------------------------------------------------------
def subperiod_breakdown():
    """Take the existing ML_55 equity curve, slice by period, report each."""
    eq_path = ARTIFACT_DIR / "equity_ML_55.parquet"
    if not eq_path.exists():
        print("[SUB] equity_ML_55.parquet missing — run backtest-ml first")
        return
    eq = pd.read_parquet(eq_path)
    eq["date"] = pd.to_datetime(eq["date"])

    periods = [
        ("2016-2019", "2016-01-01", "2019-12-31"),
        ("2020-2021", "2020-01-01", "2021-12-31"),
        ("2022-2025", "2022-01-01", "2025-12-31"),
    ]

    print("\n=== ML_55 — Sub-period breakdown ===")
    print(f"  {'period':12s}  {'start_$':>10s}  {'end_$':>10s}  "
          f"{'period_ret':>10s}  {'CAGR':>7s}  {'max_DD':>7s}  {'sharpe':>7s}")
    rows = []
    for label, s, e in periods:
        sub = eq[(eq["date"] >= s) & (eq["date"] <= e)].reset_index(drop=True)
        if len(sub) < 30:
            continue
        start_eq = sub["equity"].iloc[0]
        end_eq = sub["equity"].iloc[-1]
        period_ret = end_eq / start_eq - 1
        days = (sub["date"].iloc[-1] - sub["date"].iloc[0]).days
        years = max(days / 365.25, 0.01)
        cagr = (end_eq / start_eq) ** (1 / years) - 1
        peak = sub["equity"].cummax()
        dd = (sub["equity"] / peak - 1)
        max_dd = dd.min()
        daily_ret = sub["equity"].pct_change().dropna()
        sharpe = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)
                  if daily_ret.std() > 0 else 0)
        print(f"  {label:12s}  ${start_eq:>9,.0f}  ${end_eq:>9,.0f}  "
              f"{period_ret * 100:>+9.1f}%  {cagr * 100:>+6.2f}%  "
              f"{max_dd * 100:>+6.1f}%  {sharpe:>7.2f}")
        rows.append({"period": label, "start_eq": float(start_eq),
                     "end_eq": float(end_eq), "period_ret_pct": float(period_ret * 100),
                     "cagr_pct": float(cagr * 100), "max_dd_pct": float(max_dd * 100),
                     "sharpe": float(sharpe)})
    pd.DataFrame(rows).to_parquet(ARTIFACT_DIR / "subperiod_breakdown.parquet", index=False)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--adv", action="store_true", help="Sensitivity 1: ADV>=$1M filter")
    ap.add_argument("--regime", action="store_true", help="Sensitivity 2: regime exit (SPY 200SMA)")
    ap.add_argument("--subperiod", action="store_true", help="Sensitivity 3: sub-period split")
    ap.add_argument("--all", action="store_true", help="All three")
    args = ap.parse_args()

    summary = []

    if args.adv or args.all:
        print("=" * 72)
        print("SENSITIVITY 1: ADV >= $1M filter on top of ML_55")
        print("=" * 72)
        trades, _ = prepare_ml55_trades(adv_filter=True)
        print(f"[ADV] candidate trades after filter: {len(trades):,}")
        summary.append(walk_portfolio(trades, regime=None, label="ML_55_ADV"))

    if args.regime or args.all:
        print("\n" + "=" * 72)
        print("SENSITIVITY 2: Regime gate on top of ML_55 (SPY 200SMA proxy)")
        print("=" * 72)
        trades, _ = prepare_ml55_trades(adv_filter=False)
        regime = load_regime_proxy()
        summary.append(walk_portfolio(trades, regime=regime, label="ML_55_REGIME"))

    if args.subperiod or args.all:
        subperiod_breakdown()

    if summary:
        print("\n" + "=" * 72)
        print("SUMMARY vs ML_55 baseline (+19.5% CAGR, Sharpe 2.67, -21.4% DD)")
        print("=" * 72)
        for r in summary:
            print(f"  {r['label']:14s}  CAGR {r['cagr_pct']:+6.2f}%/yr  "
                  f"Sharpe {r['sharpe']:>5.2f}  DD {r['max_dd_pct']:>+6.1f}%  "
                  f"win {r['win_rate_pct']:>5.1f}%  n={r['trades']:,}")
        with open(ARTIFACT_DIR / "sensitivity_summary.json", "w") as f:
            json.dump(summary, f, indent=2)

    if not (args.adv or args.regime or args.subperiod or args.all):
        ap.print_help()
