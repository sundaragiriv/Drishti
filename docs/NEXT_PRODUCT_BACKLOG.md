# Next Product Backlog

Product-expansion work. Not bugs. Not cleanup. New value.

## Priority 1: Daily Operator Questions

The system must explicitly answer every day:

| Question | Current Answer | Gap |
|----------|---------------|-----|
| What intraday stocks do I play? | Intraday ML tab shows strategies | No ranked "today's top intraday" list |
| What are my sniper swing trades? | Swing Snipers (Platinum/Gold) | Working — needs live data validation |
| What stocks may move in next 2/3/5 days? | Not answered | Predictive model failed — blocked |
| Why couldn't I trade anything today? | Not answered | No diagnostic surface |
| What is my P&L and how do I improve? | P&L Ledger exists | Needs improvement analytics |

**Build**: Surfaces that answer questions 1, 4, and 5 directly.

## Priority 2: Daily Operator Brief

Unified daily summary surface showing:
- Top 5 intraday plays (from strategy engine evaluations)
- Top 5 swing snipers (from Swing Snipers Platinum/Gold)
- Predictive watchlist placeholder (empty until v2 model passes)
- Why-no-trade diagnostics (from signal lifecycle data)
- Today's P&L snapshot (from P&L Ledger)
- Biggest mistakes / improvement suggestions (from closed trade analysis)

**Where**: New tab or top-of-page summary on Swing Snipers.

## Priority 3: Why-No-Trade Diagnostics

First-class diagnostic surface explaining why no trades fired.

Possible outputs from `live_strategy_signals`:
- `EVALUATED_NO_SETUP` — setup conditions not met (X tickers checked)
- `STALE_SKIP` — bars were stale (Y tickers skipped)
- `NO_BARS` — no bar data available (Z tickers)
- `EVAL_ERROR` — evaluation failed (with error messages)
- No qualifying tickers (empty universe)
- Regime blocked entries (CRASH/DISTRIBUTION)
- Position limits reached
- IBKR disconnected

**Source**: Already persisted in `live_strategy_signals` by the strategy engine.
Just needs a UI surface to present it.

## Priority 4: Mean Reversion Intelligence

Add to ISR and Intelligence Layer:
- Mean reversion section: is this stock oversold in an uptrend? Extended? Neutral?
- RSI-based verdicts: "Oversold in Uptrend" / "Extended Above Resistance" / "Neutral Range"
- Market-level mean reversion: sector breadth compression/expansion
- Sector mean reversion report: which sectors are stretched vs compressed

**Data**: RSI, BB width, price vs SMA, sector breadth — all already in fact_swing_features.

## Priority 5: Rich Buy-Only Intelligence Summaries

For Platinum / Gold / Strong Buy names ONLY:
- What the company does (from dim_issuer + Polygon ticker details)
- What drives it (sector/theme context)
- What changed recently (insider buys, 8-K events, conviction shift)
- Why it's a buy now (from ISR recommendation engine)
- Key risk (from "What Weakens It" strip)

**Where**: ISR detail panel, below recommendation bar.
**Rule**: Do not generate for every stock. Only top-tier names.

## Priority 6: Stock-Side Massive Enrichment Surfaced

Current state:
- `fact_stock_snapshots` (11,925 rows) — warehouse only
- `fact_corporate_actions` (1,637 rows) — warehouse only

Next step:
- Show recent corporate actions in ISR (splits, dividends near earnings)
- Use snapshot data for market-state context in daily brief
- Feed corporate actions proximity as predictive feature (v2)

## Priority 7: Intelligence Layer Expansion

Potential new reports/tabs:
- **Theme Tracker**: AI, semis, obesity, uranium, energy, shipping clusters
- **Interconnected Stocks**: leader/follower visualization, sympathy candidates
- **Pressure & Positioning**: short pressure + dark pool dashboard by ticker
- **Catalyst Monitor**: insider activity + earnings + filings timeline
- **Evidence Quality**: data freshness + confidence per ticker
- **Top Stocks by Sector**: strongest names by conviction within each sector
- **Sector Strength**: breadth, relative strength, participation quality

**Data**: Most already exists in DuckDB. Needs UI surfaces.

## Priority 8: Predictive Intelligence v2

**Status**: Research only. No UI integration.

Only restart when:
1. v2 research hypothesis is defined (see FAILED_RESEARCH_TRACK.md)
2. Training data addresses regime diversity
3. Feature plan addresses sparsity
4. Validation gate is preserved unchanged
5. No production promises before gate passes

## Working Rules
- Prioritize around the 5 daily operator questions
- Build Why-No-Trade and Daily Brief first (highest operator value)
- Do not restart Predictive v2 until research is ready
- Keep backlogs separate from cleanup and research tracks
