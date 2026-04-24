# Quant-Bridge — Brutal Review

**Reviewer:** Claude (Opus 4.7), engineering + quant perspective
**Date:** 2026-04-23
**Scope:** Full repo at `Quant-Bridge-7d9249e2` (head commit `d46ab67`, branch `main`)
**Stance:** Blunt. You asked for it. I'll name files and lines; agree or rebut, don't take it personally.

---

## TL;DR

You've built an impressive **research platform** (~48K lines of Python, real DuckDB warehouse, IBKR live integration, 13F/Form 4 ingestion, HMM regime detection, half a dozen ML training scripts, a Dash dashboard with ~15 callback modules). It is **not a profitable trading system** today, and in its current shape it is unlikely to become one without cutting ~40% of what's there and doubling down on one actual edge.

The three things that will decide whether this ever makes money:

1. **You have one plausible edge (institutional + insider conviction → 45-day post-availability forward returns) buried under a pile of reactive technical-indicator scoring (`ConfluenceEngine`) that is undifferentiated from what every retail Dash project ships.** Cut the latter down to a sanity-check overlay and make the former the first-class product.
2. **The "GEX" module (`signal_scanner/core/gex_calculator.py:108-110`) is the classic retail-wrong formulation** — it assumes dealers are short all calls *and* short all puts, and computes Net GEX = CallGEX − PutGEX from open interest alone. Real dealer positioning depends on signed customer flow and is the difference between SpotGamma/SqueezeMetrics and a toy. **25% of every confluence score is being weighted on this number.** This is the single most important fix.
3. **There is exactly one test file** (`tests/test_trading_pipeline.py`, 7 tiers of smoke checks) across 48K LOC. Backtesters have no unit tests. The `_build_features*.py` / `_train_flow_predictor*.py` scripts at the repo root were never productionized — the V1 model is kept, V2 is kept, V3 is kept, and the live scanner loads `ml_signal_v2.pkl` and `flow_predictor_v*.pkl` from `data/models/` with no version pinning or registry. **You're flying a model you haven't validated out-of-sample in a disciplined way.**

Everything else is detail. The rest of this doc is the detail.

---

## 1. Repository Map

### 1.1 Actual architecture

Top-level is a mess, but the conceptual layering is this:

```
┌─ Data Ingestion ────────────────────────────────────────────────────────────┐
│  SEC EDGAR (13F, Form 4, 8-K)   signal_scanner/institutional_intel/ingest/  │
│  Polygon "Massive" (prices,      signal_scanner/institutional_intel/jobs/   │
│  options, short, news)             massive_loader.py, short_data_loader.py, │
│  IBKR (bars, option chains)         options_snapshot_loader.py, etc.        │
│  yfinance (CTB)                  → DuckDB: data/warehouse/sec_intel.duckdb  │
└─────────────────────────────────────────────────────────────────────────────┘
         │
┌─ Intelligence (EOD, quarterly) ─────────────────────────────────────────────┐
│  phase_classifier.py → accum_phase (EARLY/ACTIVE/LATE_ACCUM…DISTRIBUTION)    │
│  conviction_score.py → 0-100 composite (6 dims: phase, inst, insider, …)     │
│  squeeze_detector.py, short_conviction_engine.py, manager_quality.py,        │
│  cascade_detector.py, divergence_scanner.py, distribution_detector.py        │
│  ml_signal_v2.py (LightGBM), regime_hmm.py (3-state HMM on SPY)              │
│  insider_outcome_engine.py (1026 lines, Cohen-Malloy-Pomorski replication)   │
│  → writes intelligence_scores (DuckDB) + data/models/*.pkl                   │
└─────────────────────────────────────────────────────────────────────────────┘
         │
┌─ Live Data Plane (market hours) ────────────────────────────────────────────┐
│  IBKR → BarPrinter (own thread)    signal_scanner/core/bar_printer.py        │
│       → LiveBarStore (SQLite WAL)  signal_scanner/core/live_bar_store.py     │
│       → StrategyEngine (poll 60s)  signal_scanner/core/strategy_engine.py    │
│  Intraday strategies (read SQLite only, no IBKR data calls):                 │
│    VWAP_MR (1304 lines), FPB (1378), ORB_V2 (1365), Context Momentum (208)   │
│  Scanner/MTF Confluence (6-factor):                                          │
│    ConfluenceEngine          signal_scanner/core/confluence_engine.py        │
│    MultiSymbolScanner        signal_scanner/scanner/multi_symbol_scanner.py  │
└─────────────────────────────────────────────────────────────────────────────┘
         │
┌─ Execution / Paper ─────────────────────────────────────────────────────────┐
│  ExecutionConsumer (PENDING_EXECUTION → trade)                               │
│  PaperTrader (971 lines) — SIM only                                          │
│  OrderExecutor (562 lines) — IBKR bracket (parent LMT + TP LMT + SL STP)     │
│  IdeaBridge, IdeaLedger, IdeaRevalidator — cross-session idea persistence    │
└─────────────────────────────────────────────────────────────────────────────┘
         │
┌─ Dashboard (Dash) ──────────────────────────────────────────────────────────┐
│  15 callback modules: main, intelligence_v2, sniper, kubera, stock_report,   │
│    my_trades, reports, tradegpt (Claude API chat), intraday_ideas, …         │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 What actually runs end-to-end vs aspirational

| Capability | Runs? | Evidence |
|---|---|---|
| IBKR bar printer → SQLite → strategy engine | YES | `main.py:984-1047` wires it up cleanly; own clientId offset (`ib_cfg.client_id + 5`) is the right call. |
| Scanner MTF + ConfluenceEngine + PaperTrader | YES | `main.py:1155-1164` 60s cron. |
| VWAP_MR / FPB / ORB_V2 live scanners | PARTIALLY | Registered at startup (`main.py:1003-1031`) but according to `TODO.md:7-15` VWAP_MR *"has NEVER had a working snapshot in time"* until the 2026-03-12 fix. Live-track record is basically nothing. |
| IBKR bracket orders on paper account (port 7497) | YES | `order_executor.py:94-185` auto-enabled for all strategies when `ib_cfg.port == 7497` (`main.py:1179-1186`). |
| 13F / Form 4 / 8-K / short-interest daily refresh | YES | Scheduled in `main.py:559-927` with 10+ APScheduler cron jobs. |
| Intraday ML (VWAP_MR AUC 0.823, FPB 0.856, ORB_V2 0.731 per `TODO.md`) | YES (AUC claims) | Models load; AUC numbers are *per `TODO.md`*, not cross-validated in-repo. **I did not see the code that produced those AUCs.** |
| HMM regime detection on SPY | YES | `regime_hmm.py` 648 lines, writes daily regime. |
| `flow_predictor_v1/v2/v3.pkl` | LOADED, NOT USED | `data/models/*.pkl` exist; I could not find a live callsite that does `predict_proba` against them for execution. They appear to be research artefacts. |
| Dashboard "TradeGPT" Claude API chat | SHIPPED | `tradegpt_callbacks.py`. |
| Options `fact_options_flow` / dealer positioning | SCAFFOLDED | `options_flow_loader.py`, `options_snapshot_loader.py`, `options_bridge.py`. `TODO.md:191-195` explicitly marks OPTIONS TRADING as **PARKED — Infrastructure Not Ready**. |
| Dark pool from FINRA short-volume | IMPLEMENTED AS PROXY | `TODO.md:196`: *"Dark pool derivation from FINRA short volume is a proxy, not actual dark pool."* |
| Live capital | NO | All flows route to paper `port 7497`; no live mode wiring confirmed. |
| INSTITUTIONAL_INTEL_ARCHITECTURE Phases A–C (Ask Brahma, Signature Reports) | PHASE A+B DONE, PHASE C PARTIAL | DuckDB, QoQ engine, 19.9M 13F rows exist. Signature Reports dashboard mostly shipped. "Ask Brahma" = `tradegpt_callbacks.py`. |

### 1.3 Dead code / version-sprawl audit

Root-directory scripts are **research artefacts left in the repo root**. None of them is imported by live code — they read `data/warehouse/sec_intel.duckdb` directly and write `data/ml_training/*.parquet` or `data/models/*.pkl`.

| File | Lines | Status |
|---|---|---|
| `_build_features.py` | 182 | Superseded by v2. **DELETE** or move to `research/archive/`. |
| `_build_features_v2.py` | 329 | Current. Keep; move to `research/`. |
| `_train_flow_predictor.py` | 204 | v1 — superseded. **DELETE**. |
| `_train_flow_predictor_v2.py` | 227 | Superseded. **DELETE**. |
| `_train_flow_predictor_v3.py` | 246 | Current. Keep; move to `research/`. |
| `_rr_analysis.py` | 224 | One-shot expectancy table. Research artefact. Move. |
| `_swing_rr_analysis.py` | 435 | Same. Move. |
| `_intraday_strategy_analysis.py` | 445 | Same. Move. |
| `_reverse_analysis.py` | 280 | Same. Move. |
| `_enter_paper.py`, `_enter_paper_batch.py` | 194 | Ad-hoc CLI for manual trade entry. Either promote to `signal_scanner/cli/` with argparse + tests, or delete. |
| `market_brain.py`, `market_engine.py` | 128 | Original yfinance→Excel scripts from `Claude's Plan.txt` that have been entirely subsumed by the scanner. **DELETE**. `README.md` doesn't mention them. |
| `export_data.py` | 141 | Same era. Check and delete. |
| `TradingBridge.xlsx`, `vision.docx` | — | Non-code binary artefacts in repo root. Move to `docs/artifacts/`. |
| `improvementsfeb19.txt`, `Claude's Plan.txt` | — | Planning text. Move to `docs/`. |
| `start_scanner.bat` | — | Windows-only convenience. Leave in root or move to `scripts/`. |

**Also dead:** `_initial_scan()` and `run_scan_job()` in `main.py:435-446, 969-971` are both explicitly `return`ed with comments saying *"DISABLED — replaced by local data plane architecture (Phase A)."* They're still registered as APScheduler jobs at `main.py:973-979` and `459-468`. This is scheduler-as-dead-code — confusing, costs nothing but clarity. Rip them out.

**Verdict:** 9 root-level `_*.py` scripts, 2 `market_*.py`, and 2 docs that shouldn't be in root. Moving them to `research/` and `docs/` would clean up 90% of the visual clutter in one commit.

---

## 2. Code Quality Review

### 2.1 Coupling / abstractions

- `signal_scanner/main.py` is **1342 lines**. It imports `OrderExecutor`, `BarPrinter`, `StrategyEngine`, 4 live scanners, 15 dashboard callback modules, wires 15+ APScheduler jobs inline, does lazy imports inside function bodies, and toggles behavior based on `args.ibkr_live` and `ib_cfg.port == 7497`. This is a god-entry-point. There is no `App` / `Container` abstraction; every wiring decision is in one function. **Fix:** Factor into `app/scheduler.py`, `app/live_plane.py`, `app/intelligence_jobs.py`, `app/dashboard_wire.py`. Each about 200 lines.
- **Inline lazy imports everywhere** (`main.py:135`, `608`, `619`, `692`, `719`, `750`, etc.). This is symptom, not cause: it says circular imports or slow startup. Fix the real problem (probably DuckDB connection held on import).
- `signal_scanner/institutional_intel/` is a coherent submodule (its own `config.py`, `warehouse/db.py`, clean boundary) — keep. Everything outside that is tangled.
- `signal_scanner/paper/vwap_mr_live.py` (1304), `fpb_live.py` (1378), `orb_v2_live.py` (1365) duplicate a lot of scaffolding (daily context loading, qualifying-tickers logic, ML model loading, execution bridging). Extract a `BaseIntradayStrategy` with `load_daily_context`, `get_qualifying_tickers`, `evaluate_bar`, `record_signal`. You'll lose ~800 lines.

### 2.2 Configuration

- Single `ScannerConfig` dataclass with ~45 fields in `signal_scanner/config.py:28-137`. Reasonable. But many thresholds appear duplicated in strategy-specific code as hardcoded constants — look at `paper/fpb_live.py` and `paper/vwap_mr_live.py`. **Fix:** extract strategy-specific `VwapMRConfig`, `FpbConfig`, `OrbV2Config` dataclasses.
- `CONFLUENCE_WEIGHTS` + `REGIME_WEIGHTS` (`config.py:183-214`) are magic-number knobs with no grid-search justification in-repo. If you can't show me the backtest that produced these weights, they're just priors.
- No environment-based config loader. Secrets (`MASSIVE_API_KEY`, `ANTHROPIC_API_KEY`) are pulled from `os.environ` OR by parsing `.env` line-by-line in `options_snapshot_loader.py:37-45` — that's a home-rolled parser reimplemented in multiple jobs. **Fix:** use `pydantic-settings` or plain `python-dotenv`, one place.

### 2.3 Logging / error handling

- `loguru` everywhere — consistent. Good.
- `except Exception as e: logger.warning(...)` is the dominant error handler (I counted >40 occurrences in `main.py` alone). **This is the "swallow and continue" pattern** — your scheduler jobs can fail silently and you'll only know from grepping logs. At minimum, add a `record_skip(...)` call + a dashboard "Job Health" tile that shows last success timestamp per job.
- `TODO.md:126` explicitly asks for this: *"Every 5-min cycle, log: 'VWAP_MR: snapshot={n} tickers, window=OPEN/CLOSED, scanned={x}'."* Do it.

### 2.4 Security / credentials

- No hardcoded keys found in Python source. Good.
- `.gitignore` excludes `.env` (`~~/.gitignore:15`). Good. No `.env` committed.
- **BUT:** `.mcp.json` is modified in the working tree (per `git status`). Inspect it before any PR — MCP config files sometimes contain tokens.
- The home-rolled `.env` parser in `options_snapshot_loader.py:37-45` (naive `startswith("MASSIVE_API_KEY=")` split by `=`) will silently break on quoted values, spaces, comments, or multiline values. Low risk but noted.
- `requirements.txt` has `win10toast>=0.9` — this is **Windows-only**, breaks install on mac/linux unless pinned conditionally (`win10toast ; platform_system == "Windows"`). Your collaborators (and CI, if you get any) will hit this immediately.
- No `pyproject.toml` / `setup.py`. The package is *importable* only because the code uses `from signal_scanner...` and you run from repo root. There is no `pip install -e .`. **Fix:** add `pyproject.toml` with `[project]` metadata, tool-configs for `ruff`, `pytest`, `mypy`, and proper `[tool.setuptools.packages.find]`. Three hours of work, huge dev-experience win.

### 2.5 Test coverage

- **One** test file: `tests/test_trading_pipeline.py`. It's a pre-market **smoke harness** (7 tiers, read-only, DB/freshness/model-file checks). Useful but not a substitute for unit tests.
- `signal_scanner/tests/` directory exists but contents unknown (didn't dig). If it's empty or near-empty, that's another problem.
- **Zero unit tests** for: `ConfluenceEngine._score_*` functions (half-a-dozen gradient-scoring branches, all deterministic, trivial to test), `GEXCalculator._find_zero_gamma` (literal linear interpolation — a one-line test could catch sign-flip bugs), `swing_backtester._simulate_*`, `paper_trader._qualifies_for_swing`, `_position_size`, `OrderExecutor.place_bracket_order`.
- For a system that will manage capital: **this is dangerously undertested.** The risk is not "my tests fail" — it's "I refactor something, nothing fails, live trading silently mis-sizes for a week."

### 2.6 Packaging, deps

- `requirements.txt` has loose pins (`>=` everywhere, no upper bounds). `pandas-ta` + `numpy>=1.24` combo is notorious — you already know this, the README warns about it. Pin the bottom: `pandas-ta==0.3.14b`, `numpy>=1.24,<2.0`.
- No `dev-requirements.txt` or `[project.optional-dependencies]` for test tools (`pytest`, `ruff`, `mypy`).
- `ib_insync>=0.9.86` — `ib_insync` is in maintenance mode and deprecated in favor of `ib_async` (community fork) since 2023-2024. Not urgent but know it.

---

## 3. Data Pipeline Review

### 3.1 Sources

- **Price (daily):** Polygon "Grouped Daily" → `fact_daily_prices` (~4.5M rows 2020–2026 per `INTELLIGENCE_ROADMAP.md:18`). Rate-limited, robust.
- **Bars (intraday, live):** IBKR via `ib_insync` → `LiveBarStore` SQLite, also `fact_intraday_bars` in warehouse (5-min bars May 2024–Feb 2025 per `_intraday_strategy_analysis.py:16`). The intraday history is **~9 months** — not enough for a robust intraday backtest across regimes.
- **Options chain (live GEX):** IBKR chain → `GEXCalculator`. No historical option surface (see §4 on GEX).
- **13F:** SEC EDGAR → `fact_13f_positions` 19.9M rows. Parsed with `parsers/form13f_parser.py` (53 lines — suspiciously short for 13F parsing; verify it handles amendments, share vs principal CUSIPs, and put/call flags).
- **Form 4:** SEC EDGAR → `fact_form4_transactions` 1.84M rows.
- **8-K:** Daily refresh (`daily_8k_refresh.py`), classifies material events.
- **Short interest / short volume / CTB:** FINRA + yfinance (`short_data_loader.py`, 875 lines).
- **News sentiment:** Polygon.
- **Dark pool:** *Derived* from FINRA short-volume (proxy, not real — noted in `TODO.md:196`).

### 3.2 Storage

- DuckDB for the warehouse (`data/warehouse/sec_intel.duckdb`) — excellent choice for this workload.
- `.gitignore` correctly excludes it and all `data/raw/`, `data/processed/`, `data/warehouse/`, `*.parquet`. Good.
- SQLite (`signal_scanner/data/signals.db`) for live operational state with WAL mode. Fine for single-host.
- **Critical issue (documented in `TODO.md:FIX 4-5`):** DuckDB has single-writer locking and **multiple subprocesses race for the write lock during premarket**. The scanner runs in-process scheduler jobs, CLI scripts also open DuckDB. You've had to add `safe_duckdb_connect` and orphan-PID hunts. **This is an architectural smell.** Long-term fix: one DuckDB writer process (a "warehouse daemon") that accepts work over a queue, plus read-only connections everywhere else. Not urgent, but don't let it rot.

### 3.3 Survivorship bias / point-in-time

This is where the backtest credibility lives or dies.

- **Watchlists** (`signal_scanner/watchlists/sp500.txt`, 101 lines; `nasdaq100.txt`, 86) are **static snapshots of current membership**. Any backtest that uses them as the universe has survivorship bias. I don't think the primary backtesters do — they read `fact_daily_prices` directly. But the **live scanner uses `universe_master.txt` (2937 symbols)** which is presumably current. If you later use scanner watchlists as a backtest filter, you've introduced the bias without noticing.
- **Price history survivorship:** `fact_daily_prices` from Polygon "massive grouped daily" should include delisted tickers. *Verify this explicitly* by counting distinct tickers pre-2020 vs post-2024. If delistings are missing, your 2022 drawdown tests are lying to you.
- **Point-in-time 13F:** `_build_features_v2.py:33-44` correctly uses a **settled-quarter availability window** — filings become `avail_date` at +45 days after quarter-end (`Q1 → May 15`, `Q2 → Aug 15`, etc.) and `expire_date` the day before next `avail_date`. That's the right approach and I'm impressed this is done correctly. Same pattern in `_rr_analysis.py:17-27`. **Caveat:** 45 days is statutory deadline; many managers file earlier, some later. The +45 window is conservative (good). But if you later need higher frequency signals, you'd use actual `filed_at` per manager — you have `fact_13f_positions.filed_at` — use it.
- **Insider (Form 4):** Correct usage — Form 4 must be filed within 2 business days, so same-day availability is reasonable. `_build_features_v2.py:79-96` uses `transaction_date` rolling windows. **Subtle leak:** `transaction_date` can precede `filing_date` by up to 2 business days. You should key on `filed_at` to be strict point-in-time. On typical lags this will almost never matter, but the strict answer is "use filed_at."
- **Intelligence scores (`intelligence_scores` table):** `_build_features.py:15-41` uses `report_quarter` and derives an availability date. But `intelligence_scores` itself is *recomputed each EOD* with the current snapshot. If you score a stock using *today's* `ml_score_v2` value and the model was retrained last month, then joined with stock-days inside that quarter — **that's not point-in-time correct.** You're using a model trained on data from after the prediction date. `TODO.md:100-101` shows ML v2 re-scoring *across all tickers*. This is a subtle backtest leak specific to composite scores; fix by versioning the score table with `scored_at` and snapshotting.

### 3.4 Options / GEX data quality (toy vs. legit)

- **Live GEX** (`signal_scanner/core/gex_calculator.py`): See §4.1 — **this is the retail-wrong formulation** and 25% of your confluence score depends on it.
- **Historical GEX:** Does not exist in the warehouse. `options_snapshot_loader.py` is a framework, `fact_options_flow` is written daily, but you don't have dealer-positioning time series for backtesting. Anything that uses live GEX as a feature cannot be backtested.
- `TODO.md:191-197` is honest about this: *"Polygon v3/trades API requires premium ($199/mo) for real-time options flow."*

### 3.5 13F ingestion (stale data problem)

The right frame: 13F is **45-day-lagged quarterly ownership data**. Your `conviction_score` is therefore valid *at the time of filing availability* (45 days after quarter-end) and decays over the following ~75 trading days. You've modeled this correctly with `avail_date`/`expire_date` windows.

**What this means for strategy:**
- 13F is a **slow-moving edge** — it supports 1-week to 2-month swing/position holds, **not intraday or 1-3 day swings.** You cannot use it to predict today's intraday move.
- `run_post_q4_ingest.sh` + the daily 13F refresh (`daily_13f_refresh.py`) catch late filings into recent quarters. The freshness is fine; the *latency of the signal itself* is what constrains what you can do with it.
- **Your current design uses conviction_score to filter VWAP_MR / FPB / ORB_V2 entries (intraday)**. That's coherent — institutions-accumulated names as the *universe* to trade intraday patterns in is a reasonable filter. But don't confuse 13F with an intraday signal — it's a **quality filter**, not a timing signal.

---

## 4. Strategy / Quant Logic — the critical section

### 4.1 GEX calculation is wrong (the single biggest fix)

`signal_scanner/core/gex_calculator.py:108-114`:

```python
sign = 1.0 if is_call else -1.0
result["gex"] = (
    sign
    * df["gamma"].fillna(0)
    * df["openInterest"].fillna(0)
    * df["strike"] ** 2
    * 100
)
```

This computes `CallGEX = +gamma*OI*K²*100`, `PutGEX = -gamma*OI*K²*100`, and sums them. The comment at line 32-35 says *"Call GEX is positive (dealers long gamma from selling calls)"*. **That's exactly backwards for the premise.** If dealers **sold** calls, they are **short gamma**, not long gamma. More importantly, the formulation assumes dealers sold **all** calls and **all** puts. In reality:

- Dealer **inventory** is the sum of signed positions, and the *sign of the gamma contribution* from each contract depends on whether the dealer is net-long or net-short that contract, which depends on **signed customer flow** (which side of the market the customer was on).
- SqueezeMetrics / SpotGamma / Tier1Alpha infer dealer positioning from **trade-level flow data** (buy-initiated vs sell-initiated via NBBO lean, "at ask" vs "at bid"), not from open interest alone.
- OI-only formulations produce a number that *correlates* with real dealer gamma (because index dealers are on average net-short gamma) but **systematically mis-signs put-heavy or call-heavy days**, misplaces zero gamma, and routinely disagrees with real GEX by 30–60%.

**Impact on the system:** the GEX factor weights **25 out of 100 points in the default confluence score** (`config.py:183-190`) and **30 out of 100 in RISK_OFF regime** (`config.py:204-212`). A ~50% systematic error in the input becomes a 12-15 point error in the score, which decides BUY/SELL vs HOLD at your `paper_entry_min_score = 65.0` cutoff. **You are making enter/no-enter decisions partly on noise.**

**Fix options** (in priority order):
1. **Drop live-GEX from the scoring weight** until you have better data. Use it as a *visualization overlay only.* Reallocate the 25 points to `vwap_position` + `rsi_momentum` + a new `sector_rs` factor. Measure.
2. **Buy real GEX data** (SpotGamma ~$100-200/mo, SqueezeMetrics ~$200/mo, Tier1Alpha API). One monthly subscription, fix for the whole system. If your edge depends on dealer positioning, this is the cheapest fix money can buy.
3. **Compute proper GEX yourself** with signed flow: you'd need tick-level trades from Polygon ($199/mo Options Starter + trades). Big project. Not recommended given (2) exists.

### 4.2 Confluence score — classic technical-indicator soup

`ConfluenceEngine.score` (`confluence_engine.py:48-160`) is the heart of the scanner. It combines:

- SMA position (15 pts)
- **GEX positioning (25 pts)** ← the broken factor above
- RSI momentum (20 pts)
- Volume confirmation (15 pts)
- ADX trend strength (15 pts)
- VWAP position (10 pts)
- FVG bonus (up to +8)
- Prior-day H/L breakouts (bonus)
- Liquidity sweep/reclaim (bonus)

**Honest assessment:** Every one of these factors is computed by a hundred thousand retail quants. SMA200, RSI, ADX, Volume, VWAP are in every trading book ever written. None of them is an edge. The **combination** might have an edge, but only if the *weights* were learned from data. **You hand-picked the weights and hand-picked the regime-adaptive overrides.** That's an opinion, not an edge.

- There's no in-repo backtest of ConfluenceEngine as-a-whole that I could find. `_intraday_strategy_analysis.py` is ORB/VWAP/BB/RSI individually, not the confluence composite.
- The **FVG bonus** (+4 to +8) is applied *after* normalization (`confluence_engine.py:113-116`), meaning FVG in isolation can push a borderline 32 → 40 and flip a HOLD to a valid entry. There's no evidence in the repo this bonus is net-positive EV.
- **Regime-adaptive weights** (`config.py:193-214`) — you have 3 sets of hand-tuned weights for RISK_ON / NEUTRAL / RISK_OFF. The derivation is unreported. If you want regime-adaptivity, *learn the weights per regime* by fitting a simple logistic / LightGBM per regime on forward-return labels. You already have `_train_flow_predictor_v3.py` as a template.

**What to do:** ConfluenceEngine is fine **as a feature generator**. Stop using it as the *decision engine*. Feed its six sub-scores as features into the actual ML model (`flow_predictor_v3`) and let that model learn weights. Then the "confluence score" becomes *a calibrated probability*, not a magic 0-100 integer you threshold at 65.

### 4.3 Lookahead bias in `_build_features*`

Good news: I audited `_build_features_v2.py` carefully. The point-in-time logic looks correct:

- `avail_date` / `expire_date` windows for 13F accumulation (`:33-44`) — conservative 45-day lag, correct.
- `LAG()` used for features, `LEAD()` used for labels — correct separation.
- SPY context joined on `b.trade_date = spy.trade_date` (`:270`) — same-day SPY *return* is a feature, which is fine (it's computed from yesterday's close to today's close).
- Insider events joined on `b.trade_date = ie.transaction_date` with 5/10/30-day **trailing** windows (`:83-89`) — rolling windows are `RANGE BETWEEN INTERVAL N DAY PRECEDING AND CURRENT ROW`, correct.

**Subtle bug I flag but am not 100% sure on:** `spy_context` CTE at `:58-72` uses `PARTITION BY 1` in OVER clauses. That's a DuckDB idiom for "single partition". The SMAs at `:64-65` use `ROWS BETWEEN 49 PRECEDING AND CURRENT ROW`, so they use today's SPY close — that's fine **as a feature value at end-of-day**. But if this feature is joined to a stock row on the **same trade_date**, and your model is meant to predict the *next 3-day forward return starting tomorrow*, it's point-in-time OK. If instead you're imagining predictions computed *intraday using today's not-yet-closed SPY*, this is a subtle leak. Your labels are `LEAD(close, 3)` so the implicit assumption is "features are EOD close, trade at next open" — reasonable but **document it explicitly in the feature-matrix comments**, because at some point someone will try to use these features intraday and be surprised.

**Real concern:** Composite features like `ml_score_v2`, `conviction_score`, `squeeze_score` from `intelligence_scores` *may* be re-scored over history (§3.3). Re-scoring old quarters with a current-vintage model is point-in-time illegal. **Verify** that `intelligence_scores.computed_at` is stable per `(ticker, report_quarter)` and that you never overwrite past quarters when retraining models.

### 4.4 Backtest methodology (rigour check)

#### `_train_flow_predictor_v3.py` (the real ML model)

- Train/val/test = 2019–2023 / 2024 / 2025. Year-based temporal split — **correct for avoiding leakage at the boundary**, but with 2 years of validation+test that covers a bull market + some of a recovery. **Not robust across regimes** — no 2008/2009, no 2020 crash in test, no 2022 bear test.
- **No purged cross-validation.** Labels `label_3pct_3d` and `label_rr_2to1` use forward returns with 3/5-day windows; rows on consecutive trading days share return paths. This **biases validation AUC upward**. López de Prado's "Advances in Financial ML" Ch. 7 is the reference — you need Purged K-Fold with embargo. `docs/FAILED_RESEARCH_TRACK.md:122-131` shows *you know this* (*"v2 must implement purged k-fold cross-validation"*) but v3 still doesn't do it.
- **No transaction costs, no slippage** in the training objective or validation. AUC alone tells you nothing about tradability.
- **Universe = accumulation-phase stocks only**. Good — the conviction filter is doing work. But the in-sample class balance (+3%/3d hit rate ~20%, RR 2:1 ~10-15%) is **highly regime-dependent** — in 2022 hit rates collapse.
- **No turnover analysis, no capacity estimate, no Sharpe of the top-decile portfolio, no max drawdown.** The script prints precision tables at p99/p98/p95 — useful but not a backtest.

#### `_rr_analysis.py` and `_swing_rr_analysis.py`

- Compute hit rates for structural filters (below-200SMA + compressed + pullback etc.). This is an **expectancy exploration**, not a backtest. It tells you "this filter had historical expectancy of +0.3R on 2020-2025 data" — **no out-of-sample split, no walk-forward, no costs, no realistic position sizing.**
- **Look-ahead in `_rr_analysis.py`:** The conditions themselves (`compressed`, `pullback`) use *today's* values of ATR/return. If you backtest "enter today given today's pullback," entry price has to be today's close or tomorrow's open. The script implicitly assumes entry at today's close — that's fine *if you only act on EOD data*. Documented assumption, not a bug.

#### `_intraday_strategy_analysis.py`

- Uses `fact_intraday_features` (daily summaries of intraday action). Measures MFE/MAE distributions for ORB / VWAP MR / BB / RSI setups over **May 2024 – Feb 2025** (≈9 months of intraday data). **This is a single-regime sample.** Don't make strategy changes based on this alone.

#### `signal_scanner/institutional_intel/intelligence/swing_backtester.py` (1356 lines, the heavy one)

- Simulates 7 strategies (SQUEEZE, MEAN_REV, GAP_DRIFT, INSIDER_BREAKOUT, two filtered variants, CANDLE_REVERSAL) against forward bars. Writes `swing_backtest_results` table.
- Entry at next day's open (`_simulate_mean_rev:410-414`), stop at `entry - ATR(20)`, uses `_track_r_targets` to determine first touch of ±1R/2R. **Good methodology.**
- **Problems:**
  - No transaction costs modeled (no commission, no slippage, no half-spread).
  - No gap-risk handling — if a stock gaps below your stop overnight, you're not filled at the stop.
  - No position sizing — each entry is 1 unit. Portfolio-level Sharpe/drawdown is not reported from here.
  - No realistic capacity constraint.
  - Purged CV: not present.
- `orb_backtester.py` (979 lines) — did not read in detail; same structural warnings apply.

#### `strategy_backtester.py` (1267 lines), `ml_signal_v2.py` (766 lines)

- These are *present* but I did not read line-by-line. Based on the pattern of the others, expect similar structure with the same blind spots.

**Verdict on backtests:** You have **thorough but not rigorous** backtesting infrastructure. It would pass a casual read. It would fail a rigorous academic review (no purged CV, no costs, no regime tests, no out-of-sample walk-forward).

### 4.5 Flow predictor v3 — target leakage risk

Re-reading `_train_flow_predictor_v3.py`:

- Removes SPY features (`:40-42`) to force stock-specific learning — good.
- Adds interaction features (`:45-64`). These are fine — deterministic combinations of same-day features.
- `df["bull_regime"] = (df["spy_vs_sma200"] > 0)` at `:73` — **this uses an SPY feature they claimed to remove**. Verify that `bull_regime` is not in `all_features` — it is NOT (`:66`), it's used only for regime-conditional *analysis*, not as a feature. OK, safe.
- **My real concern:** `ml_score_v2` and `conviction_score` and `squeeze_score` are *used as features* (`_build_features_v2.py:100-118`). These are themselves ML outputs. If `ml_score_v2` was trained on forward returns that overlap with the v3 training set, **you have model-on-model leakage.** The v2 ml_score must have been trained strictly *before* the v3 training window, or v2's own CV embargo must exclude the v3 training dates. I don't see evidence this is verified.

---

## 5. Execution & Risk

### 5.1 Paper trading (`signal_scanner/paper/paper_trader.py`, 971 lines)

- Position sizing (`_position_size`, not shown but referenced): based on `paper_risk_per_trade_pct = 1.0` and `paper_leverage_per_trade = 15000.0` (max $15K notional per trade). `paper_min_notional_per_trade = 10000.0`. Capital $1M, max 30 positions. **Math check:** 30 × $15K = $450K max deployed = 45% max gross exposure. Reasonable. But 1% risk-per-trade * 30 open positions = **30% capital at risk concurrently if all stops hit simultaneously** — that's high for any correlated drawdown. Not catastrophic, but no portfolio-level VaR or correlation-aware sizing.
- Recent-loss cluster guard (`paper_trader.py:99-105`): skip if 3/5 recent trades on a symbol were losses. Decent anti-martingale guard.
- Multiple strategy tags (`NORMAL`, `DEFENSIVE`, `TRIPLE_LOCK`, `SWING`) with different entry thresholds (`ScannerConfig.paper_defensive_*`). Complex — worth a state-diagram comment.
- Flip-exit confirmation (`paper_flip_confirm_cycles = 3`): decent anti-whipsaw.

**What's missing:**
- **No daily/weekly drawdown kill-switch.** If the paper P&L draws down 5% in a day, nothing stops entries. In live trading with a broker-side circuit breaker this becomes non-optional.
- **No position-level sector/correlation limits.** You can end up with 20 long tech positions.
- **No max concurrent risk budget.** `paper_risk_per_trade_pct=1.0` * 30 positions = up to 30% account R. Should have a global R-cap (e.g., max 10% account R concurrently).
- **No per-strategy kill switch.** If VWAP_MR goes haywire, you can't disable just that strategy at runtime.

### 5.2 IBKR integration (`signal_scanner/core/order_executor.py`, 562 lines)

- Bracket order = parent LMT + TP LMT + SL STP, placed via `ib.bracketOrder(...)` (`:135-141`). Standard ib_insync pattern.
- **Order→Trade mapping** in memory (`_order_to_trade`, `_trade_orders`) + persisted to DB (`update_paper_trade_ibkr_orders`). Good.
- **Orphan gate** (`:81-92, 114-121`): if IBKR has a position you don't know about (e.g., left over from yesterday), new entries are blocked until you `acknowledge_orphans()`. **This is a good safety feature** and surprisingly rare in retail code.
- **Event handlers** for fills via `ib_insync` events (`_ensure_events`, not shown in detail). Correct pattern.
- **Reconciliation on startup** (`main.py:1205-1223`) queries IBKR positions and cross-checks. Good.
- **Fill price semantics:** the paper trader uses the signal's price as fill price. Live bracket uses LMT at `round(entry_price, 2)`. If the LMT doesn't fill (price moves past), **the TP and SL are submitted but never triggered because parent never fills** — `ib_insync`'s bracketOrder handles this correctly as an OCA group, but verify with a `TODO` test.
- **Partial fills:** not explicitly handled. If parent gets partial fill and is later cancelled, your SL/TP quantities may mismatch. Trace through `_handle_fill_event` (not read) to confirm.

### 5.3 Risk controls — client-side vs broker-side

- **Client-side:** max_open_positions=30, min_notional=$10K, late_entry_cutoff=15:30, position sizing by risk%, recent-loss cluster, orphan gate. Decent.
- **Broker-side (IBKR):** bracket order SL is the real safety net. Good.
- **Missing:** no daily P&L kill-switch, no global R-budget, no correlation limit, no sector concentration limit, no VIX-based derating, no open-order age cancellation.

### 5.4 Dashboard — monitoring live or backtests?

- `signal_scanner/dashboard/` has 15 callback modules. Reads from `signals.db` (SQLite, live) and `sec_intel.duckdb` (warehouse).
- `intelligence_callbacks_v2.py` reads `intelligence_scores` (daily-updated).
- `sniper_callbacks.py` reads live + intelligence ideas.
- **Both live trading and research views coexist.** That's fine, but the line between "this tile shows live paper P&L" and "this tile shows backtest expectancy" should be *visually* obvious. Test it with a naive user — the risk is self-deception ("my backtest says X, therefore live will do X").

---

## 6. Top 10 Concrete Gaps (ranked by impact)

| # | Gap | Why it matters | Specific fix |
|---|---|---|---|
| **1** | **GEX is computed from OI alone assuming dealers sold everything** (`core/gex_calculator.py:108`). 25% of confluence score weight is on this broken number. | You're filtering/ranking trades partly on noise. Every false positive at this layer costs a slippage+commission. | Either (a) buy SpotGamma/SqueezeMetrics real GEX data and replace, or (b) demote GEX to visualization-only and reallocate the 25 pts to sector RS + MTF alignment. Pick one this week. |
| **2** | **No purged K-Fold CV with embargo on ML training.** v1-v3 flow predictors and all backtesters use naive temporal splits with overlapping forward returns. | Validation AUCs are optimistically biased; live performance will be worse than backtest. | Implement purged CV per López de Prado Ch. 7. `docs/FAILED_RESEARCH_TRACK.md` says this is planned — actually do it before claiming any model is "AUC 0.82". |
| **3** | **No transaction costs, slippage, or gap-risk in any backtester.** `swing_backtester.py`, `_rr_analysis.py`, `_intraday_strategy_analysis.py`, `strategy_backtester.py` all assume perfect fills. | A strategy that shows +0.3R expectancy in a frictionless backtest often shows -0.1R after 10 bps roundtrip. | Add a `Cost` dataclass (commission, half_spread_bps, slippage_atr_frac) to every backtester. Charge on entry and exit. Re-run all existing analyses. Expect some "winners" to evaporate. |
| **4** | **Only 1 test file; zero unit tests on `ConfluenceEngine`, `GEXCalculator`, `PaperTrader._qualifies_for_swing`, `OrderExecutor` order construction.** | Every refactor risks silently mis-sizing / mis-ranking. Scale is coming — you can't ship changes confidently. | Add `tests/unit/` with one test file per module. Start with `confluence_engine` (trivial, deterministic), `gex_calculator._find_zero_gamma`, `paper_trader._position_size`. Aim for 80% coverage on decision-making code in 2 weeks. |
| **5** | **Composite feature leakage.** `ml_score_v2`, `conviction_score`, `squeeze_score` are used as features in v3 but may be retrained with forward data relative to v3's training window. | Classic model-on-model leakage — v3 test AUC could be inflated. | Stamp every intelligence score with `scored_at` and `model_version`. Only feed v3 a feature computed from data strictly *before* each row's `trade_date`. |
| **6** | **No daily kill-switch / max-drawdown / global-R-cap in paper_trader.** | In live trading, a strategy bug or a bad day can burn through half the account before you notice. | Add `DailyRiskManager`: if realized daily loss > 2% of equity, block new entries and flat any stale positions. Add global "max concurrent R at risk" = 8% cap. |
| **7** | **`main.py` is a 1342-line god function; 9 root-level `_*.py` research scripts pollute the root.** | New collaborators (or future-you) can't find what runs vs what doesn't. Dead `run_scan_job` / `_initial_scan` scheduler entries are landmines. | One-commit cleanup: move all `_*.py` to `research/`, delete v1 flow predictor / build_features, break `main.py` into `app/` submodules, add `pyproject.toml`. 1-2 days of work. |
| **8** | **Intraday backtest window is 9 months (May 2024 – Feb 2025).** Single regime. | Any conclusion drawn from `_intraday_strategy_analysis.py` is single-sample. | Ingest at least 3 years of intraday bars (IBKR allows 1-min bars ~1 year back, 5-min ~3 years; Polygon aggregates go further). Re-run. |
| **9** | **Scope sprawl in planning docs.** EDGE_ROADMAP + INTELLIGENCE_ROADMAP + INSTITUTIONAL_INTEL_ARCHITECTURE + TODO.md + Claude's Plan.txt + docs/*.md = ~70K words. | You cannot execute on all of this. Every plan that isn't done dilutes the plans that could be. | Delete INTELLIGENCE_ROADMAP's unstarted phases. Collapse to one `ROADMAP.md` with 3 sections: Now (this week), Soon (next month), Maybe (research). Everything else → Done archive or deleted. |
| **10** | **ConfluenceEngine hand-tuned weights + regime overrides have no backtest justification in-repo.** | You've made 16 numeric opinions (`CONFLUENCE_WEIGHTS` + 3 × `REGIME_WEIGHTS`) without showing the data. Weights drift = silent strategy drift. | Treat the 6 sub-factors as features, train a LightGBM per regime on forward returns (same target as flow_predictor), use calibrated probability as the score. `flow_predictor_v3` is the template. |

---

## 7. Monetization Roadmap

### 7.1 Honest assessment

You are one of maybe **100,000 people in the world** with a setup like this (DuckDB warehouse + IBKR + Python + a Dash dashboard + LightGBM models + 13F/insider data). What you have is **table stakes for serious retail quant**. Table stakes is not profitable by itself.

**Retail quant profitability base rate:** Of highly technical retail quants who build systems like this and trade them with real money, a fair estimate is that **~5-10% beat SPY after costs over 3+ years**, and **~1-2% generate Sharpe > 1 after costs**. The rest either break even, lose, or quietly stop. The survivorship bias in the "retail quant made it" stories is enormous.

**The specific failure modes** that bite projects at your stage:
- **Overfitting to backtest** — the model looks great in-sample, loses money live. You will not know it's overfitting until you lose 20-30% live. Purged CV + out-of-sample hold-out + paper-trade-first discipline are the only defenses.
- **Execution slippage** — especially on low-liquidity names. Your universe is Russell 3000-sized (2937 tickers). Lots of small caps with real slippage that a backtest doesn't model.
- **Regime collapse** — a model trained 2019-2023 (huge bull + brief 2022 bear) may not survive a real 2008-style drawdown or a flat 2015-2016-style year.
- **Infrastructure rot** — APIs change (Polygon did in 2024), IBKR connection drops, DuckDB locks hang the scanner before market open (already happened per `TODO.md`). Small failures compound.
- **Emotional scaling** — profitable at $10K, you scale to $100K, and now every drawdown is a mortgage payment. People override their system and blow up.

### 7.2 Your edge candidates (in order of plausibility)

Given what I see in the repo:

1. **Highest plausibility: 13F institutional accumulation → 1-3 month swing holds in accumulated names.** This is a *documented, academically-validated* factor (see "Shock-and-Awe" literature, Cohen-Malloy on insider effects, Koijen-Yogo on institutional demand). You have the data, you have the point-in-time correctness, you have conviction scoring. The `_rr_analysis.py` and `_train_flow_predictor_v3.py` outputs should be where you look first. **This is where you should concentrate.**
2. **Medium plausibility: Form 4 insider clusters** — 3+ insiders purchasing within 30 days on a non-routine basis. Cohen-Malloy-Pomorski confirms ~82 bps/month abnormal returns for opportunistic purchases. You've coded the `insider_outcome_engine.py` (1026 lines) for this. Package this as a standalone product.
3. **Medium plausibility: Earnings drift + institutional alignment.** Post-earnings-announcement drift (PEAD) is real but crowded. Institutional accumulation + earnings beat is a compound signal. You have 8-K events; you'd need earnings-beat sign. Not wired up.
4. **Your personal SAP/enterprise edge — undervalued and unique.** Alternative data from enterprise software telemetry (which you might credibly source or reason about) is what hedge funds pay millions for. If you can observe **ERP module adoption rates, SAP S/4HANA migration velocity, SaaS seat-count growth for specific public companies** through legitimate channels (paid alternative data, conference calls, anonymized partner data) — that's a *genuine* edge that nobody else replicating a LightGBM-on-13F has. **This is the most interesting lever and the least-explored in your current code.**
5. **Low plausibility: The confluence-scored intraday strategies (VWAP_MR, FPB, ORB_V2).** These are crowded, hard to model costs for, and dominated by better-capitalized HFTs. Treat them as *discipline practice*, not your profit center.
6. **Implausible: Dealer-positioning / GEX trading.** Without real GEX data (not OI-derived), you cannot compete with SpotGamma subscribers. Drop this as a strategy axis.

### 7.3 Capital and return expectations (realistic)

- **Capital floor:** $25-50K to meaningfully test. Below $10K, commissions and slippage eat everything. You can **paper trade** at any size but real fills matter.
- **Realistic Sharpe target for a diversified retail quant:** **0.8–1.3 net of costs** if you actually have an edge. **Sharpe > 1.5 claims from retail quants without real audit are almost always overfit.**
- **Realistic annual return on a 0.8 Sharpe strategy:** 8-15% with 10-15% vol. Not life-changing; beats SPY-minus-risk only if executed with discipline over years.
- **Realistic drawdown:** expect 15-25% drawdowns even on a good system. If you can't emotionally handle a 25% drawdown, size down until you can, or don't do this.

### 7.4 Phased plan

**Phase 1 — Fix foundation (2-4 weeks)**

Goal: one end-to-end clean pipeline you trust.

- Week 1: Repo hygiene. `pyproject.toml`, break up `main.py`, move root scripts to `research/`, delete v1 flow predictor + v1 build_features, delete `market_brain.py`/`market_engine.py`, consolidate planning docs.
- Week 1: **Decide on GEX.** Either subscribe to real data *or* strip GEX from scoring weight. No middle path.
- Week 2: Unit tests for `ConfluenceEngine`, `GEXCalculator`, `PaperTrader`, `OrderExecutor`. Target: 80% coverage on decision-making code.
- Week 2: **Add transaction costs and slippage** to every backtester. Re-run `_rr_analysis.py` / `swing_backtester.py`. Expect some strategies to die.
- Week 3: **Implement purged K-Fold CV** for `_train_flow_predictor_v3.py`. Retrain. Compare AUC to naive-CV — expect a 0.02-0.05 AUC drop. The dropped number is your real number.
- Week 3: Add daily drawdown kill-switch, global R-cap, sector concentration limit to `paper_trader.py`.
- Week 4: Audit composite features for retroactive retraining leaks. Stamp `scored_at` / `model_version`. Rebuild `_build_features_v2.py` to enforce strict point-in-time.

**Phase 2 — Validate edge (1-3 months paper)**

Goal: prove one strategy is profitable net-of-everything on out-of-sample data in paper.

- Pick **one** strategy to productionize. My recommendation: **Institutional Accumulation Swing (6-week hold)**, universe = `ACTIVE_ACCUM + LATE_ACCUM + conviction≥65 + filed within last 45 days`, entry on pullback to SMA20 with ATR stop. Simple. Testable. Academically grounded.
- Run this strategy in paper for **at least 2 months** without touching parameters. No fiddling. Measure: Sharpe, max DD, hit rate, avg R, turnover.
- In parallel: develop ONE other strategy candidate (e.g., Form 4 cluster-buy) *on paper only*, *with a fresh hold-out*.
- DO NOT: add new strategies, tune weights, modify cost assumptions mid-test.

**Phase 3 — Live small capital (3+ months, $10-25K)**

Goal: prove live execution matches paper.

- Live-trade the one validated strategy at position sizes **1/4** of what sizing formula says. You are testing fill quality and execution edge cases, not return.
- Compare live P&L vs the simultaneous paper run. **If live underperforms paper by >10%, your cost model is wrong** — fix before scaling.
- Keep a trade journal. Every entry: note what the system said, what you thought, what happened. Emotional discipline is the hardest edge.

**Phase 4 — Scale (only after Phase 3 shows live ≈ paper)**

- Scale the validated strategy to full sizing.
- Only then consider bringing a second strategy live.
- **Never** scale capital faster than the strategy's Sharpe justifies. At Sharpe 1.0, quadrupling capital is reasonable after 6 months of clean live performance; at Sharpe 0.5, wait a year.

### 7.5 What to CUT

Direct orders:

- **Delete:** `_build_features.py`, `_train_flow_predictor.py`, `_train_flow_predictor_v2.py`, `market_brain.py`, `market_engine.py`, `export_data.py`, `TradingBridge.xlsx`, `vision.docx`, `improvementsfeb19.txt`, `Claude's Plan.txt`.
- **Archive to `research/`:** all other root-level `_*.py` scripts.
- **Delete or rewrite:** `INTELLIGENCE_ROADMAP.md` (37K bytes — too aspirational). Replace with a 2-page `ROADMAP.md` containing only Phase 1 + Phase 2 above.
- **Delete or park:** TradeGPT dashboard chat (`tradegpt_callbacks.py`) — fun demo, zero trading value. Park behind a feature flag.
- **Park:** all dashboard tabs except "Sniper Board," "Intelligence," "My Trades," "Performance." Everything else is research UI.
- **Park:** 5 of the 7 swing strategies in `swing_backtester.py`. Keep `SQUEEZE` and `INSIDER_BREAKOUT` (the ones tied to your actual edge). The others are interesting research, not a product.
- **Park:** 13F-specific daily 8-K event classification, news sentiment, related stocks correlation. These are "later." Get the core working first.

The test: if deleting a feature wouldn't hurt your one validated strategy, delete it.

### 7.6 The hard truth

Most retail quant projects die in one of these ways, in order of frequency:

1. **"Almost there" forever.** You keep building. The system never trades live, because there's always one more feature to add, one more model to train, one more bug to fix. Your code grows, your bank account doesn't. **Your repo is at this stage right now.**
2. **Overfit to death, blow up on first real regime shift.** System shows Sharpe 2+ on backtest 2019-2024. Goes live in 2025. Breaks in 3 months. Loses 40%. Quits.
3. **Infrastructure fragility.** Scanner locks DuckDB. IBKR disconnects at 9:31 AM. Key API changes. Missed a Form 4 ingestion for a week. Small losses from execution errors accumulate faster than the edge generates.
4. **Scope sprawl to zero P&L.** Builds 12 strategies, validates 0, trades none.
5. **Emotional override.** System says short. You think the Fed is dovish. You skip the short. It would've been your best trade. You scale up your next good-feeling one. It loses.

You are currently at the intersection of **#1 and #4**. You have an extraordinary amount of code and almost no live track record. **Cut ruthlessly, pick one strategy, paper-trade it for 60 days without touching it, then graduate.**

---

## 8. Quick Wins (this week)

5-10 tactical changes you can ship in 7 days. Each is concrete.

1. **Delete dead scripts.** `rm _build_features.py _train_flow_predictor.py _train_flow_predictor_v2.py market_brain.py market_engine.py export_data.py TradingBridge.xlsx vision.docx improvementsfeb19.txt "Claude's Plan.txt"`. Commit. One-hour win in visual clarity.
2. **Add `pyproject.toml`.** `poetry init` or hand-written; include `[project]` with name/version, `ruff`/`pytest`/`mypy` sections, `platform-specific win10toast`. `pip install -e .` should Just Work on Mac, Linux, Windows. 2 hours.
3. **Demote GEX from scoring weight.** Edit `config.py:183-214` — set `gex_positioning: 0` in all three regime profiles; redistribute to `vwap_position: 15` (+5), `trend_strength: 20` (+5), and add `sector_rs: 15` as a new factor (you already have the code in `sector_rotation.py`). Visualize GEX on the dashboard; don't score on it. 2-4 hours.
4. **Add daily drawdown kill-switch** to `paper_trader.py`. If `SUM(realized_pnl WHERE date=today) / paper_starting_capital < -0.02`, block new entries. 30 lines, 1 hour. Add a dashboard banner.
5. **Add purged split to `_train_flow_predictor_v3.py`.** At minimum, drop the last 5 trading days of every train year from the training set (the "embargo" between train and val). 10 lines, 30 min. Retrain. You will see a small AUC drop — that is reality.
6. **Add transaction costs to `_rr_analysis.py`.** Subtract 10 bps (commission + half-spread + slippage) from every R-outcome. Rerun, publish the delta. Some "winners" will flip to losers. 30 minutes.
7. **Add unit tests for `ConfluenceEngine._score_sma`, `._score_rsi`, `._score_gex`, `._calc_rr`.** These are pure functions. 5 tests each, 2 hours total, catches future regressions forever.
8. **Write `ARCHITECTURE.md`** (one page) that is the *only* doc you keep. Tables: Live modules vs Research modules vs Deprecated. New joiner (or future-you) reads this first. Delete `INTELLIGENCE_ROADMAP.md`. 1 hour.
9. **Add `git hooks/pre-commit` with `ruff check`.** Your code is ~48K lines; ruff will find maybe 100 real issues in under 3 seconds. 15 minutes setup.
10. **Add a "what ran today" job status tile** to the dashboard, reading from APScheduler job listener events. For each of the ~15 scheduled jobs, show last success timestamp and last error (if any). Directly addresses `TODO.md:FIX 7`. 2-4 hours. Enormous ops win.

---

## Final word

You've done more engineering than most retail quants I've seen. You are drowning in your own ambition. Your code quality is above average; your research discipline is below what will make you money.

**Pick one strategy. Cut everything else. Prove it works for 60 days in paper. Then graduate.**

That's the path. Everything else is procrastination with good intentions.

— Claude
