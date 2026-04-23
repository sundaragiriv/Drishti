# Master Build Prompt: Unified Trading Intelligence System

Note to Claude:
You are not building pages. You are building a decision system.
Do not optimize for speed of delivery over architectural truth.
If a phase is only partially complete, say so explicitly.
Codex will review every phase against the stated acceptance criteria.

## 0. Non-Negotiable Rules

1. No guessing.
2. No filler.
3. No skipped dependencies.
4. No vague "done".
5. Stop after each phase for Codex review.
6. Do not work on multiple major phases at once unless explicitly asked.
7. Do not claim an architectural separation exists if it is only partial.
8. UI consolidation must not flatten intelligence richness.
9. Keep rich evidence in the data model.
10. All important lists must be pre-ranked. Never expect the user to manually sort to discover the best ideas.

## 1. Final Product Vision

Final primary user-facing surfaces:

1. **Swing Snipers**
   - Main swing stock-ideas board.
   - Daily actionable swing ideas.
   - Built from slow institutional thesis, daily revalidation, pressure, options support, predictive enrichment, and interconnected-stock support.

2. **Predictive Intelligence**
   - One ranked list of stocks for next 3d / 5d.
   - True forward-looking ML surface.
   - Based on price history, regime, sector/theme, pressure, interconnected stocks, and later options features.

3. **Intraday ML**
   - Broad live intraday execution board.
   - VWAP_MR / FPB / ORB_V2 activity, entries, scores, and evidence.

4. **Intraday Snipers**
   - Elite narrow intraday bucket.
   - Separate from Intraday ML.
   - Separate attribution, evidence, and statistics.

5. **Options Board**
   - Derivative intelligence surface.
   - Best options expressions for stock ideas.
   - Contract-level intelligence.
   - OI / IV / skew / term-structure / liquidity context.

6. **P&L Ledger**
   - One truth for paper/manual/automated outcomes.
   - Every trade goes here.

7. **Intelligence Layer**
   - Research/context brain.
   - Deep reports and supporting evidence.

Convergence:
- Not a primary decision surface.
- Diagnostic/input only if retained.
- Do not let it compete with Swing Snipers or Predictive Intelligence.

## 2. Intelligence Layer Vision

The Intelligence Layer must contain:

1. **ISR**
   - Full stock drilldown.
   - Recommendation section.
   - Evidence stack.
   - Action panel.

2. **Institutional Report**
   - Conviction.
   - Manager quality.
   - Ownership shifts.
   - Accumulation/distribution behavior.
   - Fund clustering.

3. **Sector Rotation**
   - Where capital is flowing.
   - Sector leadership and laggards.
   - Multi-horizon rotation.

4. **Sector Strength**
   - Breadth.
   - Relative strength.
   - Participation quality.

5. **Top Stocks by Sector**
   - Strongest names within each sector/theme.

6. **Theme Tracker**
   - AI.
   - Semis.
   - Obesity.
   - Uranium.
   - Insurers.
   - Energy.
   - Shipping.
   - Any major theme cluster.

7. **Interconnected Stocks**
   - Leader/follower relationships.
   - Sympathy candidates.
   - Peer confirmation.
   - Cluster propagation.

8. **Market Regime**
   - Risk-on / risk-off.
   - Trend / chop.
   - Offensive / defensive.
   - Regime transitions.

9. **Pressure & Positioning**
   - Short pressure.
   - Dark pool.
   - Squeeze.
   - CTB.
   - OI concentration.
   - Later dealer-style positioning if available.

10. **Catalyst Monitor**
    - Insider activity.
    - Earnings proximity.
    - Filings.
    - Corporate actions.
    - Recent changes since thesis formed.

11. **Evidence Quality**
    - Data freshness.
    - Evidence strength.
    - Confidence.
    - Confirmation count.
    - Model/sample quality where relevant.

Top-level boards answer:
- What should I look at?
- What is best right now?
- Why is it here?

Intelligence Layer answers:
- Prove it.
- Explain it.
- Show supporting context.
- Show weakening context.
- Show freshness and risk.

## 3. User-Facing Language

Top-level swing/predictive idea boards should use simple operator language:

- Platinum
- Gold
- Silver
- Bronze
- Avoid

Important:
- This is a presentation layer.
- Do not replace internal status logic.

Internal logic still uses:
- ACTIVE
- RECONFIRMED
- STRETCHED
- MISSED
- INVALIDATED
- STALE

ISR and details must show the real underlying states and reasons.

## 4. Phase A - Local Intraday Data Plane

Objective:
Finish the live architecture so intraday strategies evaluate from a shared local bar store instead of directly from IBKR.

Required architecture:
- One live intraday writer.
- Many strategy readers.
- Local store is source of truth.
- Broker is not the per-strategy data path.

Build:

### A1. Live Intraday Store
Use SQLite WAL mode.

Required tables:
- `session_universe`
- `live_intraday_bars`
- `live_symbol_status`
- `live_strategy_signals`
- `live_runtime_health`

Minimum fields:

`session_universe`
- session_date
- symbol
- tier
- source_eligibility
- open_position_flag
- added_reason
- added_at
- updated_at

`live_intraday_bars`
- symbol
- bar_ts
- open
- high
- low
- close
- volume
- fetch_ts
- source

Primary key:
- symbol + bar_ts

`live_symbol_status`
- symbol
- last_bar_ts
- bar_age_seconds
- is_stale
- last_fetch_status
- last_fetch_error
- last_fetch_at

`live_strategy_signals`
- strategy
- symbol
- signal_ts
- bar_ts_used
- freshness_state
- signal_type
- score
- percentile
- rationale
- status
- recommendation_source

`live_runtime_health`
- component
- heartbeat_ts
- cycles_completed
- errors
- lag_seconds
- notes

### A2. Bar Printer
- One process/thread only.
- Own IBKR connection if needed.
- Fetch bars for tracked symbols.
- Write to SQLite only.
- No strategy logic.
- No order logic.
- Stable cadence.
- Per-symbol freshness tracking.
- Tier-aware rotation.
- Health metrics.

### A3. Universe Builder
- Premarket universe creation.
- Tier 1 always tracked.
- Tier 2 controlled additions allowed.
- Open positions always included.

### A4. Strategy Engine
- VWAP_MR, FPB, ORB_V2 must read from SQLite only.
- No direct market-data fetch during evaluation.
- Stale bars must be detectable and enforced.
- Every decision must record exact bar timestamp used.

### A5. EOD Archiver
- Snapshot/move live session bars, signals, and health into DuckDB.
- Preserve replayability.
- No live dependence on DuckDB writes.

### A6. Architectural Truth
- If execution is still inside scanner logic, explicitly document that.
- Do not overstate execution decoupling.

Acceptance criteria:
- No live strategy fetches market data from IBKR.
- One bar printer is the only live market-data writer.
- Stale bars are visible and enforced.
- Live execution loop does not depend on DuckDB writes.
- Live session data can be archived and replayed.

Stop after Phase A.

## 5. Phase B - Swing Snipers as Living Board

Objective:
Make Swing Snipers a living swing board, not a frozen quarterly list.

Core architecture:

### Layer 1: Institutional Thesis
- Slow.
- Quarterly / filing-driven.
- Conviction.
- Accumulation phase.
- Institutional context.
- Do not recompute daily.

### Layer 2: Daily Trade Status
- ACTIVE
- RECONFIRMED
- STRETCHED
- MISSED
- INVALIDATED
- STALE

### Layer 3: Daily Execution Context
- Current price.
- Distance from thesis.
- Current entry zone.
- Stop.
- Targets.
- Current R:R.

Build:

### B1. Thesis Integrity
- Use first observable thesis date, not quarter-end date.
- Conviction remains the slow thesis score.

### B2. Daily Revalidation Engine
- Side-aware.
- Price-aware.
- Insider-aware.
- Short-pressure-aware.
- Dark-pool-aware.
- Squeeze-aware.
- Tracks why status changed.

### B3. Daily Execution Context Snapshot
- Recompute entry/stop/targets from current price context.
- Store actionability fields.

### B4. Ranking Model
Internal priority:
- RECONFIRMED
- ACTIVE
- STRETCHED
- STALE
- MISSED
- INVALIDATED

Within RECONFIRMED / ACTIVE:
- Current actionability first.
- Current R:R.
- Distance from valid entry.
- Freshness of reconfirmation.
- Thesis strength secondarily.

### B5. User-Facing Tier Mapping
Map internal status + quality to:
- Platinum
- Gold
- Silver
- Bronze
- Avoid

### B6. Board Fields
- symbol
- side
- user tier
- internal status
- thesis price
- current price
- distance from thesis
- conviction
- pressure
- source
- confirmation tags
- current R:R
- options expression available
- predictive 5d move (later)
- predictive confidence (later)
- interconnected support (later)

Acceptance criteria:
- Stale/missed ideas do not dominate top rows.
- Conviction is separate from actionability.
- Board is usable without manual sorting.
- User can see both thesis and current tradeability.

Stop after Phase B.

## 6. Phase C - Options Board as Real Derivatives Intelligence

Objective:
Build a real contract-level options intelligence system.

Do not fake options flow.
Use what data actually exists.

Build:

### C1. Per-Contract Persistence
Store:
- underlying
- contract symbol
- expiry
- strike
- call/put
- bid
- ask
- last
- midpoint
- volume
- open_interest
- IV
- Greeks if available
- snapshot timestamp
- liquidity/spread fields

### C2. Daily OI History
- Persist OI daily.
- Compute OI change.
- Detect new positioning.
- Detect concentration by strike / expiry.

### C3. IV / Skew / Term Structure
- ATM IV
- call-put skew
- near/far term spread
- IV percentile/rank proxy
- event premium / crush context

### C4. Options-Expression Engine
For top stock ideas:
- Recommend contract expressions by direction, liquidity, IV sanity, spread quality, expiry fit, delta target, and score.

### C5. Options Board UI Logic
Must show:
- Best expressions by underlying.
- Contract intelligence.
- OI concentration / walls.
- IV / skew context.
- Freshness/delay labels.

Acceptance criteria:
- Options data is contract-level.
- Top stock ideas map to concrete option expressions.
- No unsupported claims about live flow if delayed/sparse.

Stop after Phase C.

## 7. Phase D - Predictive Intelligence Implementation

Objective:
Build the real 3d / 5d forward-looking predictive layer.

This is not convergence.
This is not old quarterly ML scoring.
This is not entry quality.

### Step D1. Label Pipeline
Create:
- fwd_return_3d
- fwd_return_5d
- fwd_direction
- fwd_magnitude

Persist labels safely.

Stop after D1.

### Step D2. Feature Join Engine
Join per ticker-day:
- daily technicals
- institutional thesis/context
- insider activity
- short pressure / dark pool / squeeze
- regime
- sector rotation
- cross-asset context
- calendar features
- interaction terms

Hard rules:
- Point-in-time safe only.
- No leakage.
- Existing model outputs excluded from v1 unless proven frozen and OOS-safe.

Stop after D2.

### Step D3. Interconnected Stocks Feature Family
Build:
- leader/follower relationships
- sector/theme breadth
- peer momentum state
- lagged sympathy features
- relative move propagation
- cluster confirmation

This is part of the predictive feature set.

Stop after D3.

### Step D4. Model Training
Primary:
- LightGBM quantile regression for 3d/5d returns

Secondary:
- direction classifier

### Step D5. Calibration
- Platt scaling for direction probabilities
- Separate quantile coverage validation for prediction ranges

### Step D6. Validation Gate
Must pass:
- direction accuracy
- ECE
- IC
- top-decile Sharpe
- net-of-cost performance
- regime robustness
- rolling-window stability

If validation fails:
- Do not ship to UI.
- Document failure clearly.

Stop after D6.

### Step D7. Daily Scoring Pipeline
- Score eligible universe daily.
- Persist predictions.
- Persist model version and timestamp.

### Step D8. Predictive Intelligence Board
Show:
- predicted 3d / 5d move
- probability
- confidence
- range
- regime
- supporting context
- interconnected support
- options expression available

Also enrich Swing Snipers with:
- Pred 5d
- Conf

Acceptance criteria:
- Point-in-time safe.
- Validated before UI.
- Clearly distinct from convergence.
- Confidence and forecast range are honest.

## 8. Phase E - Massive Data Exploitation

Objective:
Use Massive Stocks Starter and Options Starter as delayed intelligence, historical analytics, predictive feature, and options-intelligence platforms.

Core role split:
- IBKR = execution / true live trigger path
- Massive = delayed intelligence / historical analytics / predictive feature platform / options intelligence platform

Do not use Massive Starter as primary real-time execution trigger because of 15-minute delay.

### E1. Stocks Starter
Use for:
- stock snapshots
- tracked-universe market-state context
- minute/day aggregate history
- second/minute aggregates where useful for research
- corporate actions
- reference data
- technical indicators for QA/prototyping only

Required outputs:
- snapshot pipeline for tracked universe
- minute/day stock historical store
- corporate actions ingestion
- reference-data hygiene
- predictive feature support
- replay/backtest support

### E2. Options Starter
Use for:
- per-contract chain snapshots
- daily OI history
- IV/skew/term-structure history
- minute/day delayed option aggregates
- delayed websocket/snapshot refresh for board intelligence

Required outputs:
- contract-level options store
- OI history
- IV/skew features
- options-expression scoring inputs
- delayed options analytics for Predictive Intelligence and ISR

### E3. Interconnected Stocks
Massive related-company / correlation / history data must feed:
- Interconnected Stocks intelligence
- predictive features
- Swing Snipers / ISR enrichment

### E4. Product Surface Feed Map
**Swing Snipers**
- sector/theme confirmation
- interconnected-stock support
- options enrichment

**Predictive Intelligence**
- historical stock features
- historical options features
- sector/theme propagation
- interconnected-stock features

**Options Board**
- contract intelligence
- OI / IV / skew / term structure
- liquidity/spread quality

**Intelligence Layer**
- sector rotation
- theme tracking
- top stocks by sector
- related/interconnected stocks
- evidence freshness and data strength

**ISR**
- deeper stock/options/peer evidence
- recommendation support

### E5. Starter Limits
Be explicit:
- Delayed by 15 minutes.
- Not primary real-time trigger.
- Not true live options-flow trading.

### E6. Priority Order
Build in this order:
1. Per-contract options persistence
2. Daily OI history
3. IV/skew/term-structure features
4. Stock snapshot pipeline
5. Stock minute/day history ingestion
6. Corporate actions + reference-data hygiene
7. Interconnected-stocks feature layer
8. Wire Massive-derived features into Predictive Intelligence and ISR

Acceptance criteria:
- Massive is no longer just daily OHLCV utility.
- Options intelligence becomes richer.
- Predictive model gets stronger features.
- Delay/freshness always labeled.

Stop after Phase E.

## 9. Phase F - Surface Consolidation

Objective:
Make the system feel like one product.

Final surfaces:
1. Swing Snipers
2. Predictive Intelligence
3. Intraday ML
4. Intraday Snipers
5. Options Board
6. Intelligence Layer
7. ISR
8. P&L Ledger

Build:
- Unified navigation
- Consistent naming
- Consistent row actions
- Consistent metadata tags
- Consistent ranking logic
- Top-level surfaces simple
- ISR/intelligence deep

Acceptance criteria:
- No competing duplicate surfaces.
- No misleading AI labeling.
- Operator immediately knows where to go.

Stop after Phase F.

## 10. Phase G - ISR Recommendation System

Objective:
Make ISR immediately understandable and decision-oriented.

Build:

### G1. Recommendation Bar
User-facing verdict:
- Strong Buy
- Buy
- Watch
- Neutral
- Avoid
- Strong Avoid

Also show:
- Confidence: High / Medium / Low
- Horizon: Intraday / Swing 5D / Swing Thesis

### G2. Why Now Strip
3-5 strongest current reasons in plain language.

### G3. What Weakens It Strip
2-4 current risks in plain language.

### G4. Recommendation Scorecard
Components:
- Thesis Strength
- Current Setup Quality
- Predictive Edge
- Pressure / Positioning
- Sector / Theme Strength
- Interconnected Confirmation
- Options Quality
- Risk / Liquidity

### G5. Evidence Stack
- Ranked evidence
- Timestamps
- Freshness indicators
- Source-aware explanations

### G6. Action Panel
- Entry zone
- Stop
- Targets
- Current R:R
- Option expression
- If not actionable, say why clearly

Rules:
- Digestible in seconds at the top.
- Deep evidence below.
- Recommendation must be explainable.
- Show both support and risk.

Acceptance criteria:
- ISR gives one clear verdict quickly.
- Supporting reasons and risks are visible immediately.
- Action panel is concrete.

Stop after Phase G.

## 11. Phase H - Execution Decoupling and Hardening

Objective:
Finish the architecture so signal generation and execution are truly separate.

Build:
- Persisted signal queue/store
- Execution engine consumes signals separately
- Remove trade creation / order placement from scanner evaluation methods
- Isolate broker routing from signal generation
- Ensure routing failures do not stop strategies
- Add signal -> execution telemetry

Acceptance criteria:
- Strategies can run even if execution is unhealthy.
- No strategy method places orders inline.
- Signal lifecycle is inspectable.

Stop after Phase H.

## 12. Data / Product Richness Rule

Do not flatten the system into sterile tables.

Preserve and surface:
- institutional thesis
- insider context
- short pressure / dark pool / squeeze
- regime
- sector/theme context
- options context
- predictive context
- interconnected-stock context
- data freshness
- evidence strength
- confirmation count
- rationale fields

Every major surface should help answer:
- Why is this on top?
- Why now?
- What supports it?
- What weakens it?
- What is the risk?
- What next?

ISR remains the deep-intelligence layer.
Top-level boards remain curated action surfaces.

## 13. Delivery Format After Each Phase

Always deliver:
1. New files
2. Modified files
3. Exact architecture/product change summary
4. What is complete
5. What remains
6. Evidence/tests/logs
7. Risks/limitations
8. Stop for Codex review

Do not use vague completion language.

## 14. Build Order

Use this order exactly:

1. Phase A
2. Phase B
3. Phase C
4. Phase D
5. Phase E
6. Phase F
7. Phase G
8. Phase H

Begin with Phase A and stop for Codex review.
