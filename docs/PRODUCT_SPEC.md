# Quant-Bridge Product Specification
## Version 2.0 — "Sniper System"
**Last Updated:** 2026-03-04
**Status:** In Development

---

## 1. Vision

Transform Quant-Bridge from a broad signal scanner (many signals, low win rate) into a
precision sniper system (few signals, high edge). Every automated trade must have:
- Calibrated win rate >= 65%
- Minimum 2.5:1 reward-to-risk ratio
- Regime alignment (HMM confirms favorable market state)
- Clear source traceability

---

## 2. Navigation & Tab Structure

### Current (7 tabs — too much noise)
Intelligence | Stock Ideas | Options Ideas | Research | Live Scanner | Paper Trading | My Trades

### Target (4 tabs — decision-focused)

| Tab | Purpose | Key Components |
|-----|---------|----------------|
| **Intelligence** | Deep research + Regime analysis | ISR with tooltips, KPI cards, Regime Analysis section (state probabilities, history chart, strategy routing), Global search bar |
| **Sniper Board** | Consolidated ranked trade ideas | Single EV-ranked table (swing + options + AI), Regime filter toggle, Icons/badges not raw numbers, Click-to-expand detail, Click symbol → ISR |
| **Live Scanner** | Real-time intraday ML | Intraday regime chart (live), Active strategy highlighting, VWAP_MR / FPB / ORB_V2 signals with entry/stop/T1/T2 |
| **Performance** | Trade journal + analytics | Paper Trading + My Trades merged, Regime-stratified performance, Filter by strategy/source/regime |

### Global Elements (all tabs)
- **Regime Banner**: Persistent bar showing Daily + Intraday regime state with color coding
- **Global Search**: Type any symbol → navigates to ISR (replaces Research tab)

---

## 3. HMM Regime Detection

### 3.1 Architecture

Two independent HMM models operating at different timeframes:

#### Daily HMM (Swing Regime)
- **Library**: hmmlearn.GaussianHMM
- **States**: 5
- **Features**: log returns, range (H-L)/C, volume change, institutional_pressure (when available)
- **Bar size**: Daily bars from fact_daily_prices
- **Training**: Rolling 252-day window, refit weekly (Sunday night)
- **Scope**: Market-wide using broad market index (SPY or composite)
- **Purpose**: Gates all swing/idea entries

#### Intraday HMM (Day Trade Regime)
- **Library**: hmmlearn.GaussianHMM
- **States**: 5
- **Features**: 15-min returns, range, volume change, VWAP deviation
- **Bar size**: 15-minute bars from fact_intraday_bars
- **Training**: Rolling 20-day window, refit daily at 9:30 AM
- **Scope**: Per-instrument or market-wide (SPY)
- **Purpose**: Routes to specific intraday strategy

### 3.2 State Definitions

| State | Name | Characteristics | Swing Action | Intraday Action |
|-------|------|----------------|--------------|-----------------|
| 0 | Bull Trend | Low vol, positive returns, rising pressure | LONG entries allowed | FPB primary |
| 1 | Mean Reversion | Medium vol, range-bound | LONG with caution | VWAP_MR primary |
| 2 | Accumulation | Low vol, flat returns, pressure building | LONG (tight stops) | ORB_V2 if breakout |
| 3 | Distribution | Rising vol, fading returns | SHORT only | Reduced size |
| 4 | Crash/Panic | High vol, negative returns | NO TRADES — cash | NO TRADES — cash |

### 3.3 Regime Gating Rules

- **State 0, 1, 2**: LONG entries allowed (swing and intraday)
- **State 3**: SHORT entries only, LONG blocked
- **State 4**: ALL automated entries blocked — system goes to cash
- Every paper trade records `regime_at_entry` and `regime_at_exit`

### 3.4 Validation Requirements (before live deployment)

- Walk-forward backtest: train 252 days, test 63 days, slide forward
- Minimum 2 years out-of-sample coverage
- State 4 must capture >70% of drawdown periods
- Regime-filtered WR must exceed unfiltered WR by >= 5pp
- States must be stable across refit windows (no random shuffling)

---

## 4. Entry Thresholds (Sniper Criteria)

### 4.1 Swing / Idea Trades (via IdeaBridge)

| Parameter | Previous | New |
|-----------|----------|-----|
| Min conviction | 65 | 75 |
| Phase gate | EARLY/ACTIVE/LATE + EXPANSION | EARLY/ACTIVE/LATE only |
| Min R:R | 2.0 | 2.5 |
| Max open positions | 5 | 3 |
| Price above 200 SMA | Required (unless Triple Lock) | Required (unless Triple Lock) |
| Regime gate | None | State 0/1/2 only (State 3 = SHORT only, State 4 = blocked) |
| Daily cycle limit | 3 | 3 |

### 4.2 Scanner MTF (existing scanner trades)

| Parameter | Previous | New |
|-----------|----------|-----|
| Conviction gate | None | >= 70 |
| Phase gate | None | Accum phases only |
| 200 SMA gate | None | Required |
| Regime gate | None | State 0/1/2 only |

### 4.3 Intraday ML (VWAP_MR / FPB / ORB_V2)

| Parameter | Previous | New |
|-----------|----------|-----|
| ML probability | Strategy default | Unchanged (model handles it) |
| Regime gate | None | HMM routes to active strategy |
| Min R:R | 2.0 | 2.0 (intraday stays at 2R) |
| State 4 behavior | Trades allowed | ALL entries blocked |

### 4.4 Triple Lock (unchanged — proven edge)

```
conviction > 70 AND ml_v2 > 70 AND f4_distinct_insiders_60d >= 1
AND accum_phase IN (EARLY_ACCUM, ACTIVE_ACCUM, LATE_ACCUM)
```
Historical: 59.8% WR (n=132). Auto-entry, bypasses most gates except State 4.

---

## 5. UI Design Principles

### 5.1 Data Density Tiers

| Location | Density | Approach |
|----------|---------|----------|
| ISR (Intelligence) | Full | All fields visible, hover tooltips on every metric |
| Regime Analysis | High | Probability charts, history, state transitions |
| Sniper Board | Low | Icons, badges, colors. 3-second decision |
| Live Scanner | Medium | Regime + ML gauge + entry/stop/targets |
| Performance | Medium | Charts and filterable stats |

### 5.2 Visual Language

- **Sentiment**: ▲ green (bull), ▼ red (bear), — gray (neutral)
- **Signal strength**: ●●●●● (5 independent signals agree) to ●○○○○ (1 signal)
- **Regime badges**: Color-coded text — green "TRENDING", blue "MEAN-REV", yellow "ACCUMULATING", orange "DISTRIBUTING", red "CRASH"
- **Phase badges**: "Accumulating" / "Distributing" / "Dormant" (plain text, no numbers)
- **Pressure**: Visual gauge bar (red→yellow→green) on ISR only
- **R:R**: Numeric with color (green >= 2.5, yellow 2.0-2.5, red < 2.0)

### 5.3 Tooltip Requirements (ISR only)

Every metric on ISR gets a hover tooltip explaining:
- What the metric measures
- How it's calculated (1 sentence)
- What "good" vs "bad" looks like
- Example: "ML Score (55.4): Machine learning confidence percentile. Ranked 0-100 across all tickers. >70 = top 30%, model sees favorable pattern."

---

## 6. Sniper Board Specification

### 6.1 Unified Trade Ideas Table

Single ranked table combining all idea sources, sorted by EV Score descending.

**Columns:**
| Column | Type | Description |
|--------|------|-------------|
| Rank | # | Position by EV score |
| Symbol | Link | Click → ISR |
| Current Price | $ | Live or latest close |
| Side | Icon | ▲ LONG / ▼ SHORT |
| Signal Strength | Visual | ●●●○○ (how many independent signals agree) |
| Regime | Badge | Current daily HMM state, color-coded |
| Entry | $ | Suggested entry price/zone |
| Stop | $ | Stop loss level |
| T1 | $ | Target 1 (minimum 2.5R) |
| T2 | $ | Target 2 (stretch target) |
| R:R | Ratio | Reward-to-risk, color-coded |
| Source | Badge | SWING / TRIPLE_LOCK / SQUEEZE / OPTIONS / AI |
| EV Score | # | Expected value ranking |

### 6.2 Filters

- **Regime toggle**: "Regime-Aligned Only" (default) vs "Show All"
- **Side filter**: All / LONG / SHORT
- **Source filter**: All / Swing / Triple Lock / Squeeze / Options
- **Timeframe**: Swing / Intraday / Options

### 6.3 Row Expansion (click to expand)

Shows detail row: Conviction, ML Score, Phase, Pressure, Trend, Insider Effect,
Squeeze Score, Options Flow sentiment — for users who want to dig deeper.

### 6.4 Entry Gate (what appears here)

Only ideas that pass ALL of:
- Calibrated WR >= 65% (from expectancy_calibration)
- R:R >= 2.5 (swing) or >= 2.0 (intraday)
- Regime state 0/1/2 for LONG, state 3 for SHORT
- Current price + stop + T1 + T2 all present

---

## 7. ML Models — Current & Planned

### 7.1 Current Models

| Model | Type | AUC | Status | Purpose |
|-------|------|-----|--------|---------|
| Swing ML v2 | LightGBM | 0.560 | Active | Swing stock ranking |
| VWAP_MR | LightGBM | 0.823 | Active | Intraday mean reversion |
| FPB | LightGBM | 0.856 | Active | First pullback breakout |
| ORB_V2 | LightGBM | 0.731 | Active | Opening range breakout |

### 7.2 Planned Models (Priority Order)

| # | Model | Type | Data Source | Purpose |
|---|-------|------|-------------|---------|
| 1 | Daily HMM Regime | GaussianHMM (5 states) | fact_daily_prices | Gate swing entries, regime detection |
| 2 | Intraday HMM Regime | GaussianHMM (5 states) | fact_intraday_bars (15m) | Route intraday strategies |
| 3 | Swing ML v3 | LightGBM | + pressure, trend, insider, squeeze, regime | Replace v2 with richer features |
| 4 | Options Flow ML | LightGBM | fact_options_flow + intelligence | Predict 2-week returns from flow |
| 5 | Insider Timing ML | LightGBM | fact_insider_outcomes + intelligence | Which insider buys work |
| 6 | Retrain Intraday | LightGBM | + HMM regime as feature | Boost VWAP_MR/FPB/ORB_V2 AUCs |

---

## 8. Performance Tab Specification

### 8.1 Merged View (Paper Trading + My Trades)

**Sub-tabs:**
- **Open Positions**: Live P&L, current price, distance to stop/T1/T2, regime at entry
- **Closed Trades**: Full journal with entry/exit, P&L, regime, source, notes
- **Analytics**: Win rate, expectancy, profit factor, Sharpe — filterable

### 8.2 Regime-Stratified Analytics

Filter performance by:
- Regime at entry (State 0-4)
- Strategy/source
- Time period
- Side (LONG/SHORT)

Key insight: "What's my WR when trading with the regime vs against it?"

---

## 9. Data Pipeline (Unchanged)

No changes to data ingestion. All existing pipelines remain as-is:
- fact_daily_prices (Polygon)
- fact_13f_positions (SEC EDGAR)
- fact_form4_transactions (SEC EDGAR)
- fact_options_flow (Polygon)
- fact_short_volume / fact_short_interest (FINRA)
- fact_form8k_events (SEC EDGAR)
- fact_cost_to_borrow (yfinance)
- fact_news_sentiment (Polygon)

Intelligence pipeline stages 6a-6j + ML scoring remain unchanged.
HMM is an additional intelligence layer, not a replacement.

---

## 10. Implementation Phases

### Phase 1: Sniper Backend (Completed — Mar 4-5 2026)
- [x] Fix institutional_pressure (was 0.0 everywhere)
- [x] Fix triple_lock phase filter (add EARLY_ACCUM)
- [x] IdeaBridge auto paper trading
- [x] Daily HMM model build + walk-forward validation (Sharpe 3.47 OOS)
- [x] Tighten entry thresholds (conv>=75, R:R>=2.5, max 3 positions)
- [x] Wire HMM regime gate (IdeaBridge + EOD pipeline)

### Phase 2: UI Consolidation (Completed — Mar 5 2026)
- [x] 7 tabs → 4 tabs navigation (Intelligence, Sniper Board, Live Scanner, Performance)
- [x] Sniper Board (consolidated EV-ranked ideas table)
- [x] Global search bar (replace Research tab, in navbar)
- [x] Regime banner (HMM-powered, persistent across tabs)
- [x] Performance tab (merged Paper Trading + My Trades, regime-stratified analytics)
- [ ] Visual simplification (icons, badges, gauges) — partial

### Phase 3: ML Expansion (Target: Mar 9-15 2026)
- [ ] Intraday HMM regime model
- [ ] Swing ML v3 retrain with new features
- [ ] ISR tooltips on every metric
- [ ] Regime Analysis section in Intelligence tab

### Phase 4: Advanced ML (Target: Mar 16+ 2026)
- [ ] Options Flow ML
- [ ] Insider Timing ML
- [ ] Retrain intraday models with HMM regime feature
- [ ] Performance tab regime-stratified analytics (breakdown table done, charts pending)

---

## Appendix A: Key File Locations

| Component | Path |
|-----------|------|
| Intelligence pipeline | signal_scanner/institutional_intel/intelligence/ |
| Paper trading | signal_scanner/paper/ |
| Dashboard layouts | signal_scanner/dashboard/layouts/ |
| Dashboard callbacks | signal_scanner/dashboard/ |
| Intraday ML models | data/warehouse/models/ |
| Scanner | signal_scanner/scanner/ |
| Config | signal_scanner/config.py |
| EOD pipeline | signal_scanner/institutional_intel/jobs/run_eod_pipeline.py |
| DuckDB warehouse | data/warehouse/sec_intel.duckdb |
| SQLite signals DB | signal_scanner/data/signals.db |

## Appendix B: Intelligence Score Column Names

Actual column names in `intelligence_scores` table:
- ML: `ml_score_v2`, `triple_lock`, `inst_f4_distinct_60d`, `price_momentum_90d`, `price_above_200sma`
- Insider: `insider_effect_score`, `insider_hist_win_rate`, `insider_hist_alpha`, `trend_score`, `institutional_pressure`
- Short: `squeeze_score`, `short_squeeze_score`, `short_volume_ratio_trend`
- Phase: `accum_phase`, `conviction_score`, `swing_signal`, `data_quality_score`
