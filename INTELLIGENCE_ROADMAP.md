# Quant-Bridge Intelligence System — Full Development Roadmap

**Project**: Institutional Intelligence + AI Signal System
**Status**: Foundation Complete. Intelligence Layer in planning.
**Last Updated**: 2026-02-19
**Author**: Claude Sonnet 4.6 (session context preserved here for continuity)

---

## What Is Already Built (Do Not Rebuild)

| Component | Location | Status |
|-----------|----------|--------|
| DuckDB Warehouse | `data/warehouse/sec_intel.duckdb` | ✅ Complete |
| 13F Institutional Data | `fact_13f_positions`, `agg_qoq_changes`, `agg_quarterly_holdings` | ✅ 19.9M rows, 2006–2025 |
| Form4 Insider Data | `fact_form4_transactions` | ✅ 1.84M rows |
| OHLCV Price Data | `fact_daily_prices` | ✅ 4.5M rows, 2020–2026, 11,100 tickers, source=massive_grouped |
| Sector Classification | `dim_issuer.sector` | ✅ 2,532 tickers classified via Polygon API + yfinance |
| QoQ Aggregation Engine | `signal_scanner/institutional_intel/reports/qoq_engine.py` | ✅ Sector propagation fixed |
| Kubera Reports Dashboard | `signal_scanner/dashboard/` | ✅ Running on port 8050 |
| Existing Signal Scanner | `signal_scanner/` (IBKR, GEX, FVG, options) | ✅ Day trading signals active |
| Paper Trading System | `signal_scanner/dashboard/` | ✅ Active |

**Key Config**: `signal_scanner/institutional_intel/config.py`
**Pipeline Entry**: `signal_scanner/institutional_intel/jobs/run_pipeline.py`
**Dashboard Entry**: `signal_scanner/main.py`

---

## Vision Statement

> We do not predict price. We track the **process that creates price**, identify which stage that process is in, and enter before the price movement begins.
>
> Institutional accumulation is a pipeline. Capital entering a stock today creates buying pressure for 2–6 quarters as the information cascade propagates through the market. We map this pipeline across three trading horizons: **Day Trading**, **Swing Trading**, and **Long Term** — for both Stocks and Options.
>
> Every score is data-backed. No hypothetical assumptions.

---

## Trading Horizons In Scope

| Horizon | Timeframe | Primary Data Used | Options Timeframe |
|---------|-----------|-------------------|-------------------|
| **Day Trading** | Intraday | Institutional phase as direction filter + IBKR flow + GEX + FVG | 0DTE / Weekly |
| **Swing Trading** | 2–8 weeks | Phase classification + Cascade timing + Price structure | 30–60 DTE |
| **Long Term** | 1–4 quarters | Full conviction score + Lag estimator + Macro cycle | LEAPS 6–18 months |

---

## System Architecture (Four Layers)

```
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 1 — MACRO INTELLIGENCE                                        │
│  Sector Rotation Clock → Which sectors are in institutional favor?   │
│  Economic Cycle Phase → Which factor tilts (growth/value/defensive)  │
├─────────────────────────────────────────────────────────────────────┤
│  LAYER 2 — STOCK SELECTION INTELLIGENCE                              │
│  Model 1: Accumulation Phase Classifier (7-phase pipeline)           │
│  Model 2: Lag Estimator (when does price respond?)                   │
│  Model 3: Copy-Cat Cascade Detector (2nd wave buying detection)      │
│  Model 4: Smart Money Divergence (price down, accumulation up)       │
├─────────────────────────────────────────────────────────────────────┤
│  LAYER 3 — CONVICTION CONFIRMATION                                   │
│  Model 5: Manager Quality Tier Scoring                               │
│  Model 6: Manager Concentration Signal (>3% portfolio allocation)    │
│  Model 7: Cluster Insider Intelligence (multi-insider same window)   │
│  Model 8: Conviction Cascade Score (6-dimensional unified score)     │
├─────────────────────────────────────────────────────────────────────┤
│  LAYER 4 — TRADING SIGNAL OUTPUT + RISK                              │
│  Model 9:  Distribution Early Warning (exit / short signal)          │
│  Model 10: Trading Horizon Signals (Day / Swing / Long + Options)    │
│  Model 11: Portfolio Intelligence Engine (sizing, correlation, hedge)│
└─────────────────────────────────────────────────────────────────────┘

UI LAYER (on top of all models):
  Screen A: Intelligence Command Center
  Screen B: Accumulation Radar (scatter plot)
  Screen C: Sector Rotation Clock
  Screen D: Individual Stock Deep Dive
  Screen E: Portfolio Builder
  Screen F: Ask Kubera (AI Stock Report — Claude API)
```

---

## Development Phases — Sequenced with Dependencies

---

### PHASE 1 — Intelligence Core
**Priority: HIGHEST. Everything else depends on this.**

---

#### Task 1.1: New Database Table — `intelligence_scores`

**File to modify**: `signal_scanner/institutional_intel/warehouse/schema.sql`

Add table:
```sql
CREATE TABLE IF NOT EXISTS intelligence_scores (
    ticker TEXT NOT NULL,
    report_quarter TEXT NOT NULL,           -- e.g. '2025-Q3'
    computed_at TIMESTAMP NOT NULL,

    -- Phase Classification (Model 1)
    accum_phase TEXT,                       -- DORMANT | EARLY_ACCUM | ACTIVE_ACCUM | LATE_ACCUM | EXPANSION | DISTRIBUTION | DECLINE
    accum_phase_quarters INTEGER,           -- how many quarters in current phase
    accum_strength_score DOUBLE,            -- 0-100, strength of accumulation signal

    -- Lag Estimation (Model 2)
    expected_impact_quarters INTEGER,       -- estimated quarters until price responds
    lag_confidence TEXT,                    -- HIGH | MEDIUM | LOW
    lag_rationale TEXT,                     -- human-readable reason

    -- Cascade Detection (Model 3)
    cascade_stage INTEGER,                  -- 0=none, 1=early, 2=active, 3=late
    new_initiations_count INTEGER,          -- new managers entering this quarter
    copycat_score DOUBLE,                   -- 0-100

    -- Smart Money Divergence (Model 4)
    divergence_active BOOLEAN,             -- price falling but accumulation rising
    divergence_magnitude DOUBLE,           -- |price_change_pct| + accum_change_pct

    -- Manager Quality (Model 5)
    tier1_manager_count INTEGER,           -- top-20 AUM holders
    tier2_manager_count INTEGER,           -- top-100 AUM holders
    manager_quality_score DOUBLE,          -- weighted by tier

    -- Concentration Signal (Model 6)
    max_manager_concentration DOUBLE,      -- highest % allocation by any single manager
    concentrated_managers_count INTEGER,   -- count of managers with >3% allocation

    -- Insider Intelligence (Model 7)
    insider_cluster_detected BOOLEAN,      -- 3+ insiders buying within 30 days
    insider_net_buy_count INTEGER,         -- net buys (buys - sells) last 90 days
    ceo_cfo_buying BOOLEAN,               -- CEO or CFO specifically buying
    insider_score DOUBLE,                  -- 0-100

    -- Conviction Cascade Score (Model 8)
    conviction_score DOUBLE,              -- 0-100, master score (6 dimensions)
    conviction_breakdown TEXT,            -- JSON string of dimension scores

    -- Distribution Warning (Model 9)
    distribution_warning BOOLEAN,
    distribution_severity TEXT,           -- MILD | MODERATE | SEVERE

    -- Trading Horizon Signals (Model 10)
    swing_signal TEXT,                    -- BUY | WATCH | AVOID | SHORT
    swing_entry_zone TEXT,               -- price range description
    swing_target TEXT,                   -- target price or % gain
    swing_stop TEXT,                     -- stop loss price or %
    swing_options_suggestion TEXT,       -- e.g. "Buy 45-DTE call, strike X"

    longterm_signal TEXT,                -- BUY | ACCUMULATE | HOLD | REDUCE | EXIT
    longterm_thesis TEXT,               -- 1-2 sentence reasoning
    longterm_target_quarter TEXT,       -- e.g. '2026-Q2'
    longterm_options_suggestion TEXT,   -- e.g. "LEAPS Jan 2027 call"

    PRIMARY KEY (ticker, report_quarter)
);
```

---

#### Task 1.2: Model 1 — Accumulation Phase Classifier

**New file**: `signal_scanner/institutional_intel/intelligence/phase_classifier.py`

**Logic** (rule-based first, ML refinement later):

```
Phase: DORMANT
  Rule: inst_count < 5 AND inst_count_change <= 0 for 2+ quarters
  AND avg_price_change_pct is flat (< 5% abs)

Phase: EARLY_ACCUM
  Rule: inst_count_change > 0 for 1-2 consecutive quarters
  AND new_initiations > 0 (managers entering for first time)
  AND avg_price_change_pct < 10% (price not yet responding)
  AND total_shares increasing

Phase: ACTIVE_ACCUM
  Rule: inst_count_change > 0 for 3+ consecutive quarters
  AND total_shares increasing
  AND avg_price_change_pct < 20% (price lagging accumulation)
  → THIS IS THE PRIMARY BUY ZONE

Phase: LATE_ACCUM
  Rule: inst_count growth SLOWING (positive but decelerating)
  AND avg_price_change_pct 15-40% (price starting to respond)
  AND volume expanding

Phase: EXPANSION
  Rule: inst_count growth near zero or slightly positive
  AND avg_price_change_pct > 30%
  AND volume high
  → Institutions reached target. Retail is driving now.

Phase: DISTRIBUTION
  Rule: inst_count_change < 0 for 1+ quarters
  AND avg_price_change_pct still positive (retail still buying)
  → Smart money exiting into retail strength

Phase: DECLINE
  Rule: inst_count_change < 0 for 2+ quarters
  AND avg_price_change_pct < -10%
  → Full distribution complete, price falling
```

**Input tables**: `agg_qoq_changes`, `agg_quarterly_holdings`
**Output table**: `intelligence_scores.accum_phase`
**Key function**: `classify_phases(conn, quarter) -> dict[ticker, phase]`

---

#### Task 1.3: Model 2 — Lag Estimator

**Add to**: `signal_scanner/institutional_intel/intelligence/phase_classifier.py`

**Logic**:

Lag is determined by these factors (each reduces or increases lag):

```python
def estimate_lag(ticker_data: dict) -> tuple[int, str, str]:
    """Returns (expected_quarters, confidence, rationale)"""

    lag = 3  # baseline

    # Market cap adjustment (larger = slower response)
    if market_cap > 100_000_000_000:   lag += 1   # mega cap
    elif market_cap < 2_000_000_000:   lag -= 1   # small cap (more impact per share)

    # Accumulation velocity (faster = shorter lag)
    if inst_count_growth_rate > 20%:   lag -= 1
    if inst_count_growth_rate < 5%:    lag += 1

    # Insider confirmation (they know the catalyst)
    if insider_cluster_detected:       lag -= 1

    # Short interest (squeeze accelerates impact)
    if short_interest_ratio > 15%:     lag -= 1

    # Cascade stage (2nd wave = closer to move)
    if cascade_stage >= 2:             lag -= 1

    return max(1, min(lag, 6)), confidence, rationale
```

---

#### Task 1.4: New Module Init

**New file**: `signal_scanner/institutional_intel/intelligence/__init__.py`
Empty init to make it a package.

---

#### Task 1.5: Wire Phase Classifier into Pipeline

**File to modify**: `signal_scanner/institutional_intel/jobs/run_pipeline.py`

Add new stage `intelligence` that runs after `aggregate`:
```python
elif args.stage in ("intelligence", "all"):
    from signal_scanner.institutional_intel.intelligence.phase_classifier import run_phase_classification
    run_phase_classification(conn)
```

---

#### Task 1.6: Accumulation Radar UI — Screen B

**New file**: `signal_scanner/dashboard/layouts/intelligence_view.py`

Scatter plot using `plotly.graph_objects.Scatter`:
- X-axis: `accum_strength_score` (0-100)
- Y-axis: `avg_price_change_pct` (from agg_qoq_changes)
- Color: phase (EARLY=green, ACTIVE=bright green, LATE=yellow, EXPANSION=orange, DISTRIBUTION=red)
- Size: `conviction_score`
- Hover: ticker, company, sector, phase, conviction, lag estimate
- Click: opens Individual Stock Deep Dive (Screen D)

**Quadrant annotations**:
- Bottom-right: "BUY ZONE — Strong accumulation, price not yet responded"
- Top-right: "EXPANSION — Move already happened"
- Top-left: "DISTRIBUTION — Smart money exiting"
- Bottom-left: "DORMANT — No activity"

---

### PHASE 1.5 — Backtesting Framework
**Run this before Phase 2. Validates Phase 1 before building further.**

---

#### Task 1.7: Backtest Engine

**New file**: `signal_scanner/institutional_intel/intelligence/backtest.py`

**Core principle**: Only use data visible at `filing_date + 45 days` (the moment the 13F became public). This is the realistic entry point. Never use quarter-end prices as entry — that's look-ahead bias.

**Logic**:
```python
def run_phase_backtest(conn, train_quarters: list[str], holdout_quarters: list[str]):
    """
    For each quarter in train range:
      1. Run phase classifier using ONLY data available through that quarter
      2. Record phase + conviction for each ticker at entry (filing_date + 45 days)
      3. Compute forward returns at 30d, 60d, 90d, 180d from entry date
      4. Compute SPY return over same windows (benchmark)
      5. Record alpha = stock_return - spy_return

    Segments:
      - By phase (EARLY_ACCUM vs ACTIVE_ACCUM vs LATE_ACCUM)
      - By conviction score bucket (0-40, 40-60, 60-80, 80-100)
      - By insider confirmation (cluster detected vs not)
      - By cascade stage (0, 1, 2, 3)
      - By manager tier (Tier-1 present vs not)
    """
```

**Output table**: `backtest_results`
```sql
CREATE TABLE IF NOT EXISTS backtest_results (
    ticker TEXT,
    signal_quarter TEXT,          -- quarter when signal was generated
    entry_date DATE,              -- filing_date + 45 days (public signal date)
    entry_price DOUBLE,
    accum_phase TEXT,
    conviction_score DOUBLE,
    cascade_stage INTEGER,
    insider_confirmed BOOLEAN,
    tier1_present BOOLEAN,

    -- Forward returns
    return_30d  DOUBLE,           -- (price_30d - entry) / entry * 100
    return_60d  DOUBLE,
    return_90d  DOUBLE,
    return_180d DOUBLE,

    -- SPY benchmark returns (same window)
    spy_return_30d  DOUBLE,
    spy_return_60d  DOUBLE,
    spy_return_90d  DOUBLE,
    spy_return_180d DOUBLE,

    -- Alpha
    alpha_30d  DOUBLE,            -- return_30d - spy_return_30d
    alpha_60d  DOUBLE,
    alpha_90d  DOUBLE,
    alpha_180d DOUBLE,

    -- Was lag estimate accurate?
    actual_peak_quarter INTEGER,  -- how many quarters until max return
    estimated_lag_quarters INTEGER,

    PRIMARY KEY (ticker, signal_quarter)
);
```

**Backtest report function**:
```python
def print_backtest_summary(conn):
    """Print win rates, average alpha, hit rates by segment."""
    # Win rate by phase
    # Average alpha by conviction bucket
    # Lag accuracy: estimated vs actual
    # Best performing signal combinations
```

**Train/Holdout split**:
- Train: 2020-Q2 → 2023-Q4 (14 quarters)
- Validation: 2024-Q1 → 2024-Q3 (3 quarters, out-of-sample)
- Holdout (never touch until live): 2024-Q4 → present

**CLI**:
```bash
python -m signal_scanner.institutional_intel.intelligence.backtest --run
python -m signal_scanner.institutional_intel.intelligence.backtest --summary
```

**What we're validating**:
1. Does ACTIVE_ACCUM outperform EARLY_ACCUM? (phase ordering correctness)
2. Does higher conviction = higher alpha? (score calibration)
3. Does insider confirmation improve win rate? (feature value)
4. Are lag estimates accurate? (timing model correctness)
5. Does the system generate meaningful alpha vs SPY? (overall edge)

If the backtest shows conviction_score has NO correlation with forward returns → we adjust the weights before showing users anything.

---

### PHASE 2 — Conviction Layer

**Depends on**: Phase 1 + Phase 1.5 (backtest validates assumptions before building further)

---

#### Task 2.1: Model 3 — Copy-Cat Cascade Detector

**Add to**: `signal_scanner/institutional_intel/intelligence/cascade_detector.py`

**Logic**:
```sql
-- For each ticker-quarter, count managers who had 0 shares prior quarter
-- but now have > 0 shares (pure new initiations, not additions)
SELECT
    ticker,
    report_quarter,
    COUNT(*) AS new_initiations,
    SUM(shares_current) AS new_initiation_shares
FROM agg_qoq_changes
WHERE shares_prior = 0 AND shares_current > 0
GROUP BY ticker, report_quarter
```

Track new_initiations over consecutive quarters:
- Stage 0: 0 new initiations
- Stage 1: 1-2 new managers this quarter
- Stage 2: 3-5 new managers (cascade accelerating)
- Stage 3: 6+ new managers (consensus forming, probably late)

---

#### Task 2.2: Model 4 — Smart Money Divergence Scanner

**Add to**: `signal_scanner/institutional_intel/intelligence/divergence_scanner.py`

```sql
SELECT
    q.ticker,
    q.current_quarter,
    q.shares_change_pct,
    q.inst_count_change,
    p.avg_price_change_pct,
    -- Divergence: accumulation rising, price falling
    CASE WHEN q.shares_change_pct > 10 AND p.avg_price_change_pct < -5
         THEN TRUE ELSE FALSE END AS divergence_active,
    ABS(p.avg_price_change_pct) + q.shares_change_pct AS divergence_magnitude
FROM agg_qoq_changes q
JOIN agg_quarterly_holdings p ON q.ticker = p.ticker AND q.current_quarter = p.report_quarter
WHERE q.shares_change_pct > 10 AND p.avg_price_change_pct < -5
ORDER BY divergence_magnitude DESC
```

---

#### Task 2.3: Model 5 — Manager Quality Tier Classification

**New file**: `signal_scanner/institutional_intel/intelligence/manager_quality.py`

Build a tier table of manager CIKs ranked by total AUM (computed from `fact_13f_positions` total value):
```sql
CREATE TABLE IF NOT EXISTS dim_manager_tiers AS
SELECT
    manager_cik,
    manager_name,
    SUM(value_usd_thousands) AS total_aum_k,
    NTILE(3) OVER (ORDER BY SUM(value_usd_thousands) DESC) AS tier_raw,
    CASE
        WHEN RANK() OVER (ORDER BY SUM(value_usd_thousands) DESC) <= 20 THEN 1  -- BlackRock, Vanguard, etc.
        WHEN RANK() OVER (ORDER BY SUM(value_usd_thousands) DESC) <= 100 THEN 2
        ELSE 3
    END AS tier
FROM fact_13f_positions
GROUP BY manager_cik, manager_name
```

---

#### Task 2.4: Model 6 — Manager Concentration Signal

**Add to**: `signal_scanner/institutional_intel/intelligence/manager_quality.py`

For each (ticker, quarter), compute:
```sql
-- What % of each manager's total portfolio is this one stock?
SELECT
    p.ticker,
    p.quarter_label,
    p.manager_cik,
    p.value_usd_thousands / m.total_aum_k AS concentration_pct
FROM fact_13f_positions p
JOIN dim_manager_tiers m ON p.manager_cik = m.manager_cik
WHERE p.value_usd_thousands / m.total_aum_k > 0.03  -- >3% allocation = high conviction
```

---

#### Task 2.5: Model 7 — Cluster Insider Intelligence

**New file**: `signal_scanner/institutional_intel/intelligence/insider_intelligence.py`

```sql
-- Detect cluster buying: 3+ insiders buying same stock within 30 days
SELECT
    ticker,
    DATE_TRUNC('month', transaction_date) AS month,
    COUNT(DISTINCT insider_cik) AS unique_buyers,
    SUM(shares) AS total_insider_shares,
    bool_or(relationship LIKE '%Chief Executive%' OR relationship LIKE '%CEO%') AS ceo_buying,
    bool_or(relationship LIKE '%Chief Financial%' OR relationship LIKE '%CFO%') AS cfo_buying
FROM fact_form4_transactions
WHERE transaction_type IN ('P', 'A')  -- Purchase or Award
  AND transaction_date >= CURRENT_DATE - INTERVAL '90 days'
GROUP BY ticker, DATE_TRUNC('month', transaction_date)
HAVING COUNT(DISTINCT insider_cik) >= 3
```

---

#### Task 2.6: Model 8 — Conviction Cascade Score (Master Score)

**New file**: `signal_scanner/institutional_intel/intelligence/conviction_score.py`

6-dimensional scoring, each 0-100, weighted average:

```python
conviction_score = (
    accum_depth_score    * 0.25  # Phase + streak length + accum_strength
  + cascade_score        * 0.20  # New initiations + cascade stage
  + manager_quality_score* 0.15  # Tier-1/2 manager presence + concentration
  + insider_score        * 0.20  # Cluster detection + CEO/CFO + net buys
  + sector_tailwind_score* 0.10  # Is sector in inflow trend?
  + lag_opportunity_score* 0.10  # Early in lag = high score, late = low score
)
```

**Output stored in** `intelligence_scores.conviction_score` and `conviction_breakdown` (JSON).

---

### PHASE 3 — Macro Intelligence Layer

**Depends on**: Phase 1 complete (Phase 2 not required)

---

#### Task 3.1: Model 5 (Macro) — Sector Rotation Clock

**New file**: `signal_scanner/institutional_intel/intelligence/sector_rotation.py`

Compute net institutional flows per sector per quarter:
```sql
SELECT
    sector,
    report_quarter,
    SUM(value_current_usd_k) AS total_value_k,
    SUM(value_current_usd_k) - SUM(value_prior_usd_k) AS net_flow_k,
    (SUM(value_current_usd_k) - SUM(value_prior_usd_k)) / NULLIF(SUM(value_prior_usd_k), 0) * 100 AS flow_pct,
    SUM(inst_count_current) - SUM(inst_count_prior) AS net_inst_count_change
FROM agg_qoq_changes
WHERE sector IS NOT NULL
GROUP BY sector, report_quarter
ORDER BY report_quarter DESC, flow_pct DESC
```

Map sector flows to economic cycle phase:
```python
CYCLE_MAP = {
    "early_recovery": ["Technology", "Consumer Discretionary", "Financials"],
    "mid_expansion":  ["Industrials", "Energy", "Materials"],
    "late_cycle":     ["Energy", "Healthcare", "Consumer Staples"],
    "defensive":      ["Healthcare", "Utilities", "Consumer Staples"],
}
```

Store result in `agg_sector_rotation` table (new).

#### Task 3.2: Sector Rotation Clock UI — Screen C

Visual: Circular clock face with 11 sectors at positions around the clock.
Color coding: Green (inflow 2+ quarters), Yellow (neutral), Red (outflow).
Arrow: pointing to current estimated cycle position.
Built in: Plotly polar chart or custom SVG in Dash.

---

### PHASE 4 — Trading Signal Output Layer

**Depends on**: Phase 1 + Phase 2 complete

---

#### Task 4.1: Model 9 — Distribution Early Warning

**Add to**: `signal_scanner/institutional_intel/intelligence/distribution_detector.py`

```
MILD: inst_count_change < 0 for 1 quarter AND price still +10%+ from accum average
MODERATE: inst_count_change < 0 for 2 quarters AND new_initiations = 0
SEVERE: inst_count_change < -20% AND price near peak AND volume increasing on down days
```

This powers the EXIT signal for long positions and the SHORT signal setup.

---

#### Task 4.2: Model 10 — Trading Horizon Signal Generator

**New file**: `signal_scanner/institutional_intel/intelligence/trading_signals.py`

For each ticker with a conviction score, generate structured signals:

**Long Term Signal Logic**:
```python
if phase in ("EARLY_ACCUM", "ACTIVE_ACCUM") and conviction_score >= 60:
    signal = "BUY"
    entry = "Current price or scale in over 2-3 weeks"
    target = f"Q{current_quarter + lag_quarters} based on historical phase expansion"
    options = f"LEAPS: Buy {lag_quarters * 3}-month call, 10-15% OTM"

elif phase == "LATE_ACCUM" and conviction_score >= 70:
    signal = "ACCUMULATE"

elif phase == "DISTRIBUTION" and distribution_warning:
    signal = "REDUCE" or "EXIT"
    options = "Protective puts or covered calls"
```

**Swing Signal Logic**:
```python
if phase in ("EARLY_ACCUM", "ACTIVE_ACCUM") and cascade_stage >= 1 and price_constructive:
    signal = "BUY"
    entry = "Near 20-day MA or recent consolidation base"
    target = "+15 to +25% from entry"
    stop = "-7 to -10% from entry"
    options = f"Buy 45-DTE call, strike at current price or slight OTM"
```

**Day Trading Filter** (integrate with existing signal scanner):
```python
# The existing IBKR scanner already generates day signals.
# Add institutional direction as a FILTER — don't day-trade against the phase.
if phase in ("DISTRIBUTION", "DECLINE"):
    day_signal_bias = "SHORT_ONLY"  # only take short day setups
elif phase in ("EARLY_ACCUM", "ACTIVE_ACCUM"):
    day_signal_bias = "LONG_ONLY"   # only take long day setups
else:
    day_signal_bias = "NEUTRAL"     # take both
```

**File to modify**: `signal_scanner/scanner.py` — add `institutional_phase_filter()` that gates day signals.

---

### PHASE 5 — Ask Kubera (AI Report Engine)

**Depends on**: Phase 1 + Phase 2 + Phase 4 complete
**Requires**: Anthropic API key (`ANTHROPIC_API_KEY` in `.env`)

---

#### Task 5.1: Data Aggregation Engine

**New file**: `signal_scanner/institutional_intel/intelligence/kubera_context.py`

Function: `build_stock_context(ticker: str, conn) -> dict`

Pulls ALL available data for a ticker into a structured dict:
```python
{
  "ticker": "NVDA",
  "company": "NVIDIA Corporation",
  "sector": "Technology",
  "current_phase": "ACTIVE_ACCUM",
  "phase_quarters": 3,
  "conviction_score": 87,
  "conviction_breakdown": {...},
  "lag_estimate": "1-2 quarters",
  "lag_confidence": "HIGH",
  "institutional_summary": {
    "current_holders": 342,
    "prior_holders": 298,
    "shares_change_pct": 12.4,
    "value_usd_millions": 8_420,
    "tier1_holders": 15,
    "new_initiations_this_quarter": 8,
    "cascade_stage": 2,
    "top_5_managers": [...],
    "max_concentration": 4.2
  },
  "insider_summary": {
    "cluster_detected": True,
    "recent_buys": 4,
    "recent_sells": 1,
    "ceo_buying": True,
    "cfo_buying": False,
    "net_insider_value_usd": 2_400_000
  },
  "price_summary": {
    "current_price": 142.50,
    "avg_price_last_quarter": 128.30,
    "price_change_pct_qoq": 11.1,
    "avg_volume": 42_000_000,
    "52w_high": 165.00,
    "52w_low": 87.00,
    "pct_from_52w_high": -13.6
  },
  "sector_context": {
    "sector_flow_trend": "INFLOW",
    "sector_flow_consecutive_quarters": 3,
    "sector_cycle_position": "early_expansion"
  },
  "distribution_warning": False,
  "trading_signals": {
    "day_bias": "LONG_ONLY",
    "swing_signal": "BUY",
    "swing_entry": "138-142",
    "swing_target": "162",
    "swing_stop": "129",
    "swing_options": "Buy 45-DTE 145C",
    "longterm_signal": "BUY",
    "longterm_thesis_data": "...",
    "longterm_target_quarter": "2026-Q2",
    "longterm_options": "LEAPS Jan 2027 150C"
  }
}
```

---

#### Task 5.2: Claude API Integration

**New file**: `signal_scanner/institutional_intel/intelligence/kubera_engine.py`

```python
import anthropic

SYSTEM_PROMPT = """You are Kubera, a senior financial analyst with 20+ years of experience
at top-tier institutions including BlackRock and Goldman Sachs. You specialize in reading
institutional flows, insider behavior, and translating data into actionable trading recommendations.

You receive structured data about a stock and produce a comprehensive analysis.
You are direct, specific, and data-backed. You never use vague language.
You provide specific price levels, specific timeframes, and clear reasoning grounded only in the data provided.
You consider three trading horizons: Day Trading, Swing Trading (2-8 weeks), and Long Term (1-4 quarters).
For each horizon, you recommend specific options strategies where applicable."""

def generate_kubera_report(context: dict) -> str:
    client = anthropic.Anthropic()

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"""Analyze this stock and produce your full Kubera Report:

{json.dumps(context, indent=2)}

Structure your response as:
1. EXECUTIVE SUMMARY (2-3 sentences, the headline thesis)
2. INSTITUTIONAL INTELLIGENCE (what the 13F data tells us, specific numbers)
3. INSIDER INTELLIGENCE (what insider behavior confirms or contradicts)
4. PHASE ANALYSIS (current phase, what it means, expected timeline)
5. DAY TRADING (bias direction, key levels to watch today, options if applicable)
6. SWING TRADING (signal, entry zone, target, stop, options strategy with specific strike/expiry)
7. LONG TERM (signal, thesis in 2-3 sentences, target quarter, LEAPS suggestion if applicable)
8. RISK FACTORS (what would invalidate this thesis)
9. VERDICT (one word: BUY / WATCH / AVOID / SHORT — plus confidence %)"""
        }]
    )

    return message.content[0].text
```

**Config addition needed** in `.env`:
```
ANTHROPIC_API_KEY=your_key_here
```

---

#### Task 5.3: Ask Kubera UI — Screen F

**New file**: `signal_scanner/dashboard/layouts/kubera_view.py`

Layout:
```
┌─────────────────────────────────────────────────────────┐
│  ASK KUBERA                                              │
│  ┌─────────────────────────────────────────────────┐    │
│  │  Enter Ticker: [_______] [Analyze]               │    │
│  └─────────────────────────────────────────────────┘    │
├─────────────────────────────────────────────────────────┤
│  VERDICT: BUY ● 87% Confidence                          │
│  Phase: ACTIVE ACCUMULATION (Q3)  Lag: 1-2 Quarters     │
│  Conviction: 87/100  ████████░░                         │
├─────────────────────────────────────────────────────────┤
│  EXECUTIVE SUMMARY                                       │
│  [AI generated text]                                     │
├───────────────┬─────────────────┬───────────────────────┤
│  DAY TRADING  │  SWING TRADING  │  LONG TERM            │
│  Bias: LONG   │  Signal: BUY    │  Signal: BUY          │
│  Key: $142 S  │  Entry: 138-142 │  Target: Q2 2026      │
│  Key: $148 R  │  Target: $162   │  Thesis: [AI text]    │
│  0DTE: 145C   │  Stop: $129     │  LEAPS: Jan27 150C    │
│               │  45-DTE: 145C   │                       │
├───────────────┴─────────────────┴───────────────────────┤
│  INSTITUTIONAL INTELLIGENCE     INSIDER INTELLIGENCE     │
│  342 holders (+44 QoQ)          Cluster Buy Detected ✓  │
│  Cascade Stage: 2               CEO Buying ✓            │
│  8 New Initiations              Net: +$2.4M insider     │
│  15 Tier-1 managers             4 buys / 1 sell (90d)   │
├─────────────────────────────────────────────────────────┤
│  RISK FACTORS                                            │
│  [AI generated text]                                     │
└─────────────────────────────────────────────────────────┘
```

**New callback file**: `signal_scanner/dashboard/kubera_callbacks.py`
Register in `signal_scanner/main.py`

---

### PHASE 6 — Full UI Integration

**Depends on**: All phases above complete

---

#### Task 6.1: Intelligence Command Center — Screen A

**New tab** in existing dashboard (`signal_scanner/dashboard/layouts/reports_view.py` or new file).

Top bar: Macro regime classification + current cycle phase.
Main table: Top 20 conviction setups ranked by `conviction_score DESC` where `accum_phase IN ('EARLY_ACCUM', 'ACTIVE_ACCUM')`.
Columns: Rank, Ticker, Company, Sector, Phase, Conviction, Lag Estimate, Swing Signal, Long Signal, Insider Alert.
Bottom section: Distribution warnings (potential shorts/exits).

#### Task 6.2: Individual Stock Deep Dive — Screen D

Triggered by clicking a ticker anywhere in the system.
Shows:
- Accumulation timeline chart (inst_count + shares per quarter, bar chart)
- Insider buy/sell overlay (scatter on same chart)
- Price chart with accumulation phases color-coded in background
- Phase badge + conviction score breakdown
- Quick links to Ask Kubera for that ticker

#### Task 6.3: Portfolio Builder — Screen E

Drag from conviction setups into portfolio.
Real-time:
- Sector concentration pie
- Phase concentration (don't overload one phase)
- Position sizing via simplified Kelly: `edge * conviction_score / 100`
- Correlation warning if >3 stocks same sector same phase

#### Task 6.4: Navigation Updates

**File to modify**: `signal_scanner/dashboard/layouts/main_view.py`
Add new tabs: "Intelligence", "Brahma", "Sector Clock"
Existing tabs: keep as-is.

---

### PHASE 7 — ML Enhancement (Future, After Phases 1-6)

**Depends on**: 6+ months of intelligence_scores data accumulated

---

#### Task 7.1: XGBoost Phase-Return Model

Train on: `intelligence_scores` features → forward 90-day return (from `fact_daily_prices`)
Goal: Learn which phase + conviction combinations actually predict returns in our specific dataset
Replace rule-based conviction weights with learned weights

#### Task 7.2: Feature Importance Analysis

Run SHAP values to understand which signals actually matter
Feed back into conviction score weighting
This tells us if insider cluster buying matters more than Tier-1 manager presence, etc.

---

## File Creation Summary

| New File | Purpose | Phase |
|----------|---------|-------|
| `intelligence/__init__.py` | Package init | 1 |
| `intelligence/phase_classifier.py` | Model 1 + Model 2 | 1 |
| `intelligence/cascade_detector.py` | Model 3 | 2 |
| `intelligence/divergence_scanner.py` | Model 4 | 2 |
| `intelligence/manager_quality.py` | Models 5 + 6 | 2 |
| `intelligence/insider_intelligence.py` | Model 7 | 2 |
| `intelligence/conviction_score.py` | Model 8 | 2 |
| `intelligence/sector_rotation.py` | Sector Rotation Clock | 3 |
| `intelligence/distribution_detector.py` | Model 9 | 4 |
| `intelligence/trading_signals.py` | Model 10 | 4 |
| `intelligence/kubera_context.py` | Data aggregation for Ask Kubera | 5 |
| `intelligence/kubera_engine.py` | Claude API integration | 5 |
| `dashboard/layouts/intelligence_view.py` | Screens A + B + D | 6 |
| `dashboard/layouts/kubera_view.py` | Screen F | 5 |
| `dashboard/kubera_callbacks.py` | Ask Kubera UI callbacks | 5 |

| Modified File | Change | Phase |
|---------------|--------|-------|
| `warehouse/schema.sql` | Add `intelligence_scores` table | 1 |
| `warehouse/db.py` | Migration for new table | 1 |
| `jobs/run_pipeline.py` | Add `intelligence` stage | 1 |
| `scanner.py` | Add institutional phase filter for day trades | 4 |
| `dashboard/layouts/main_view.py` | Add new nav tabs | 6 |
| `main.py` | Register brahma callbacks | 5 |

---

## Environment Variables Required

```bash
# Already in .env:
MASSIVE_API_KEY=...
SEC_USER_AGENT=...

# New — required for Ask Kubera:
ANTHROPIC_API_KEY=...
```

---

## Quick Start for New Developer / Future Session

```bash
# 1. Verify existing data is intact
python -c "
import duckdb
c = duckdb.connect('data/warehouse/sec_intel.duckdb', read_only=True)
print(c.execute('SELECT COUNT(*) FROM fact_13f_positions').fetchone())
print(c.execute('SELECT COUNT(*) FROM fact_daily_prices').fetchone())
print(c.execute('SELECT COUNT(*) FROM fact_form4_transactions').fetchone())
"

# 2. Start with Phase 1 — create intelligence table + phase classifier
# Create: signal_scanner/institutional_intel/intelligence/__init__.py
# Create: signal_scanner/institutional_intel/intelligence/phase_classifier.py
# Modify: signal_scanner/institutional_intel/warehouse/schema.sql (add intelligence_scores)

# 3. Run the new intelligence stage
python -m signal_scanner.institutional_intel.jobs.run_pipeline --stage intelligence

# 4. Verify
python -c "
import duckdb
c = duckdb.connect('data/warehouse/sec_intel.duckdb', read_only=True)
print(c.execute('SELECT accum_phase, COUNT(*) FROM intelligence_scores GROUP BY accum_phase').fetchdf())
"

# 5. Launch dashboard to verify Accumulation Radar renders
python -m signal_scanner.main --debug
```

---

## Design Principles (Non-Negotiable)

1. **Every score is data-backed.** No scores without a traceable SQL/Python calculation.
2. **No hypothetical assumptions.** If we can't prove it from the data, we don't show it.
3. **Three horizons always.** Every analysis surfaces Day / Swing / Long Term.
4. **Options always considered.** Each horizon includes an options suggestion where applicable.
5. **Phase first, everything else second.** The accumulation phase gates all other signals.
6. **Ask Kubera unifies everything.** One click = one report = one decision.

---

*This document is the source of truth for the Intelligence System build. Start with Phase 1, Task 1.1.*
