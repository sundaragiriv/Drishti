# Quant-Bridge Release Notes

User-visible changes, dated. Newest on top. Each entry: what changed,
why, and the user-facing impact (config, behavior, dashboard).

For deep technical commits see `git log`. For research artefacts see
`docs/*.md`.

---

## 2026-04-27 (night) — F1-B: structural features (YZ + Hurst) ship modest lift

- **YZ + Hurst features added** to v3_5d. Honest purged 5-fold CV mean
  AUC: **0.5563 ±0.0024** vs F1-A baseline 0.5510 ±0.0044.
  +0.005 AUC improvement, std cut nearly in half.
- **yz_overnight_share at rank #5 in feature importance** — gap-risk
  awareness is a genuinely productive feature for our 5-day swing target.
  Hypothesis confirmed.
- All 7 structural features in top-20 importance. yz_vs_atr_ratio_14
  at #8 (validates the orthogonality test).
- **Lyapunov + Rough Vol explicitly REJECTED** (final, not deferred).
  YZ + Hurst = our structural feature set going forward.
- Decision rule mislabeled "SHELVE" — base-rate shift between train pool
  (21% hit) and 90-day holdout (9.5% hit) invalidated the Brier
  comparison. Real verdict: WASH-leaning-POSITIVE. See
  `docs/structural_features_eval_2026-04-28.md`.
- Live deployment still deferred (needs runtime feature pipeline).
- Memory: added `feedback_research_script_guards.md` after a smoke-test
  import side-effect crashed the EOD pipeline's Massive Enrichment step.

## 2026-04-27 (mid-day) — Objective question + Top 10 hero panel

- **Dashboard now answers ONE question prominently:** "What stocks will
  make me money today?" Replaces "Swing Snipers / EV-ranked trade ideas"
  technical title with the user-perspective question that everything in
  the system is built to answer.
- **Today's Top 10 hero panel** added above the full ranked table.
  Shows the actionable shortlist with side / R:R / conviction / status.
  When no qualifying setups exist, the summary says **"Sit out — no
  qualifying setups today"** — that's a valid and honest answer.
- Also saved the objective question to memory as a project rule so
  future features are evaluated against it.

## 2026-04-27 (mid-day) — Restored main scan loops (CRITICAL FIX)

- **Bug discovered:** `run_execution_scan` and `run_research_scan` were
  defined in `main.py` but **never registered with the scheduler**.
  IdeaBridge had been silently broken for weeks — Triple Lock / Swing /
  PEAD never fired any trades because no scan was calling
  `multi_symbol_scanner.scan_symbols()`.
- **Fix:** Both scans now scheduled on 900s interval. Initial scan
  fires at startup so we don't wait 15 min for first cycle.
- Dashboard appeared empty pre-bounce because the data was correct but
  no entries were being made. Confirmed by user pushback ("i cant see
  anything yet, pretty empty").

## 2026-04-27 (pre-market) — F1-A + F2 honest result, Monday-ready

- **F1-A: v3 retrained on `label_hit_1R_5d`** (5-day window, same R-frame).
  Honest purged 5-fold CV mean AUC: **0.5510 ±0.0044** (n=1.7M).
  Folds: 0.546 / 0.546 / 0.554 / 0.557 / 0.553.
  Compared to F1's label_hit_1R_10d (AUC 0.513): **+0.038 lift**.
  This is the right label for our actual 5-day-max-hold production frame.
  See `docs/f1a_v3_retrain_5d_2026-04-27.md`.
- **F2: Isotonic calibration applied** to v3_1R_5d. Calibrator saved
  alongside the model in `data/models/flow_predictor_v3_1R_5d.pkl`.
  Finding: v3_5d has **rank-skill but its probabilities cluster around
  the base rate (0.22)**. Top decile ≈ 30% hit, bottom decile ≈ 15%.
  Useful as a ranker, not as a confidence gauge.
- **Live v3_5d ranking deferred.** Integrating into IdeaBridge requires
  a runtime feature pipeline (mirror of `_build_features_v2.py` over
  live DuckDB+SQLite state) — 1-2 day project. Model artifact + calibrator
  saved for that integration next session.
- **Pre-market data refresh running in background** — EOD pipeline to
  catch weekend Form 4 / 8-K filings + refit HMM with Friday's data.
- **Dashboard bounced** for Monday open at http://127.0.0.1:8050.

## 2026-04-26 (late) — PEAD strategy live + probability calibration ready

- **PEAD (Post-Earnings Announcement Drift) strategy shipped** as a
  new IdeaBridge source. When an 8-K with `has_earnings=True` lands on
  a name in our accumulation universe AND the stock gapped >=3% on
  filing day, we trade the drift over 10 days at the 1R/2*ATR frame.
  Source tag: `PEAD_DRIFT_LONG` / `PEAD_DRIFT_SHORT`.
  Academic foundation: Bernard-Thomas 1989, 50+ years of replication.
  Caveat: our 8-K table only goes back 2026-01-08 so historical backtest
  impossible until SEC EDGAR bulk-backfill — paper-trade-and-measure mode.
- **Isotonic probability calibration** module added
  (`probability_calibration.py`). Wraps sklearn `IsotonicRegression`
  with sample-size guards + a calibration-report helper. Will be
  applied to v3 outputs once F1 retraining completes.
- **F1 (v3 retrain on `label_hit_1R_10d`) running in background**
  — will follow with its own release note + report.

## 2026-04-26 (late evening) — Intraday sub-tabs added

- **Intraday section now has visible sub-tabs**: "Confluence (Sniper)"
  and "ML (VWAP_MR / FPB / ORB_V2)". Clicking either switches the
  hidden ls-tabs controller behind the scenes — no architecture change,
  just exposed the existing routing as user-clickable.
- Section title simplified to "Intraday" with new desc covering both
  sub-strategies.

## 2026-04-26 (evening) — Navbar rename + P&L Ledger cleared

- **Navbar renamed** for clarity. "Swing Snipers" / "Snipers" was
  ambiguous (two near-identical labels with similar icons). New nav:
  **Swing (Multi-Day)** | **Intraday** | **Options** | **Forecast**
  (disabled placeholder) | **Intelligence** | **P&L Ledger**.
  Underlying nav-IDs preserved so callbacks keep working — only
  labels and one icon changed.
- **P&L Ledger cleared.** All 103 closed paper trades archived to
  `paper_trades_archive_20260426_191035` (recoverable). `paper_trades`
  table empty for fresh start Monday.
- **Dashboard bounced** to pick up rename.

## 2026-04-26 — Catalyst gate killed (negative result), dashboard bounce

- **Catalyst gate disabled.** Tier 1 historical backtest on 3,255 stock-days
  showed the gate *hurts* every population with statistical power
  (ALL accum −0.10 R, conv≥65 −0.09 R, ML v2 ≥90 −0.53 R per trade).
  Both A/B cohorts now enter regardless. Catalyst metadata still
  captured for future research. See
  `docs/catalyst_gate_dead_2026-04-26.md`.
- **Catalyst experiment scaffold shipped earlier today** then
  immediately disabled — discipline working. Cohort + flag columns
  still live on `paper_trades`.
- **Dashboard bounced** to pick up the day's changes. http://127.0.0.1:8050.

## 2026-04-25 — 1R target shipped, releases page started

- **IdeaBridge target: 2.5R → 1R.** Live-config backtest at 2*ATR stops
  showed 2.5R hits only 9.2% of the time on Triple Lock vs 38.6% at 1R.
  Net expectancy flipped from −0.131 R/trade to +0.024 R/trade. New
  config knobs: `paper_idea_target_r_multiple` (default 1.0),
  `paper_idea_stretch_target_r_multiple` (default 1.5). See
  `docs/live_config_expectancy_2026-04-25.md`.

## 2026-04-24 — Saturday review-fixes session (commit 2183b86)

Closed 7 of 10 items from the brutal review (`docs/Quant-Bridge-Review.md`):

- **GEX demoted to visualization-only.** 25 pts of confluence scoring
  weight redistributed to rsi_momentum / trend_strength / vwap_position.
  Reason: OI-only formulation systematically mis-signs dealer
  positioning vs real signed-flow GEX, never validated. See
  `signal_scanner/config.py CONFLUENCE_WEIGHTS / REGIME_WEIGHTS`.
- **Daily-drawdown kill-switch** (2% of starting capital) +
  **global R-cap** (8% concurrent at-risk). New config knobs in
  `paper_trader.py`. Trips at NY-midnight rollover.
- **Purged K-Fold CV with embargo** for v3 ML training. Honest AUC:
  3pct_3d = 0.626 (was 0.65), rr_2to1 = 0.578 (was 0.60).
  See `docs/purged_cv_v3_2026-04-24.md`.
- **Cost-aware backtests.** New `Cost` dataclass (1bp commission +
  2bp half-spread + 0.05 ATR slippage per side). Wired into
  `swing_backtester.py` and `research/rr_analysis.py`. See
  `docs/backtest_cost_deltas_2026-04-24.md`.
- **Provenance stamping.** New columns on `intelligence_scores`:
  `ml_score_v2_version` + `ml_score_v2_scored_at`. Prevents v2-into-v3
  composite-feature leakage on retrains.
- **Repo hygiene.** 11 root `_*.py` scripts moved to `research/`,
  6 dead files deleted, planning docs archived to `docs/archive/`.
  `pyproject.toml` added with ruff/pytest/mypy config. `main.py`
  trimmed 1342 → 1300 lines (dead `_initial_scan` / `run_scan_job` removed).
- **47 new unit tests.** GEXCalculator math, ConfluenceEngine
  scorers, PaperTrader kill-switch + position sizing, PurgedKFold
  splitter. Suite: 1 → 91 → 100 → 112.

## 2026-04-23 — EOD backfill + brutal review committed

- **Brutal external review** of the repo committed for posterity.
  See `docs/Quant-Bridge-Review.md`.
- **Full EOD backfill ran 5h** — pulled CTB, shorts (14d), Form 4 (7d),
  1,048 Q1 2026 13F filings, 8-K events, dark pool, options snapshot,
  intelligence pipeline (Q4 2025 rescored), squeeze scores, ML v2 score,
  HMM regime refit. Dashboard live again at http://127.0.0.1:8050.
