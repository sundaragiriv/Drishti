# Signal Command Center — V2 Enrichment Pipeline

## Status: COMPLETE

## Overview
Major upgrade from screener to signal generator. One row per symbol (MTF aggregated),
ATR-based stops/targets, gradient scoring, market regime awareness, and enriched columns.

---

## Phase 1: Config + Technical Analyzer Upgrades
- [x] config.py — Add ATR period, gradient thresholds, R:R minimum, VWAP flag, regime params
- [x] technical_analyzer.py — Add ATR-14, VWAP, RSI slope, ADX slope, prior day levels

## Phase 2: Market Regime Module
- [x] core/market_regime.py — NEW file: SPY trend + VIX check = RISK_ON / RISK_OFF / NEUTRAL

## Phase 3: Confluence Engine Rewrite
- [x] confluence_engine.py — Gradient scoring, ATR-based stops (1.5x ATR), scaled targets (T1/T2/T3), R:R gate (min 1.5:1), distance-to-level

## Phase 4: Signal Ranker — MTF Aggregation
- [x] signal_ranker.py — One row per symbol, MTF agreement (3/3, 2/3, 1/3), best TF for entry, dominant signal from higher TF, relative strength vs SPY

## Phase 5: Scanner Pipeline Updates
- [x] multi_symbol_scanner.py — Signal persistence tracking, market regime pass-through, session time tag, earnings proximity check, new result dict fields

## Phase 6: Database Schema
- [x] database/models.py — New columns: atr, vwap, rsi_slope, adx_slope, mtf_agreement, market_regime, relative_strength, signal_age, session_time, rr_ratio, distance_to_resistance_pct, distance_to_support_pct, prior_day_high, prior_day_low
- [x] database/db_manager.py — Updated upsert, new query for MTF aggregated view

## Phase 7: Dashboard
- [x] dashboard/layouts/main_view.py — Tooltips, new columns, market regime banner, column config dropdown, timezone fix
- [x] dashboard/layouts/detail_view.py — Updated trade params with ATR, scaled targets, regime context
- [x] dashboard/callbacks.py — Format new fields, MTF mode toggle, regime banner update

## Phase 8: Verification
- [x] Delete old DB, run --scan-once --no-ibkr, validate all new columns populated
- [x] 15 raw signals -> 5 MTF rows, 0 errors, all enrichments verified
- [ ] Start dashboard, verify table renders with MTF view (user manual step)

---

## New Columns (Final Table — MTF Aggregated View)
| Column | Source | Description |
|--------|--------|-------------|
| Symbol | scanner | Ticker |
| Signal | confluence | LONG/SHORT/NEUTRAL (from highest TF) |
| Rec | confluence | BUY/SELL/HOLD |
| Score | confluence | 0-100 gradient score |
| MTF | ranker | 3/3, 2/3, 1/3 agreement |
| Price | tech | Current price |
| Trend | tech | UP/DOWN/SIDE |
| R:R | confluence | Risk:Reward ratio |
| Stop | confluence | ATR-based stop loss |
| T1 | confluence | Target 1 (1R) |
| T2 | confluence | Target 2 (2R / gamma wall) |
| RSI | tech | Value + arrow (slope) |
| ADX | tech | Value + arrow (slope) |
| VWAP | tech | Above/Below VWAP |
| GEX | gex | Above/Below ZG + distance% |
| Vol | tech | Volume ratio |
| RS | scanner | Relative strength vs SPY |
| Regime | regime | Market risk on/off |
| Sector | watchlist | Sector name |
| Age | scanner | Signal persistence count |
| Updated | scanner | Local time HH:MM:SS |

## Key Architecture Changes
- Scanner still runs per-symbol per-timeframe internally
- NEW: signal_ranker.aggregate_mtf() collapses to one row per symbol BEFORE dashboard display
- Higher TF (1h) sets bias direction, lower TF (5m) provides entry timing
- Market regime fetched ONCE per scan cycle (SPY + VIX), passed to all symbols
- Signal persistence tracked via in-memory dict in scanner (symbol -> consecutive_count)
- R:R < 1.5 signals are demoted to HOLD regardless of score
