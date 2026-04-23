-- ============================================================
-- CORE INGESTION TABLES
-- ============================================================

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id                  BIGINT PRIMARY KEY,
    source              TEXT NOT NULL,
    job_name            TEXT NOT NULL,
    started_at          TIMESTAMP NOT NULL,
    finished_at         TIMESTAMP,
    status              TEXT NOT NULL,
    rows_ingested       BIGINT DEFAULT 0,
    rows_failed         BIGINT DEFAULT 0,
    parser_version      TEXT,
    notes               TEXT
);

CREATE TABLE IF NOT EXISTS raw_file_manifest (
    accession_no        TEXT PRIMARY KEY,
    form_type           TEXT NOT NULL,
    cik                 TEXT,
    filing_date         DATE,
    source_url          TEXT,
    local_path          TEXT NOT NULL,
    sha256              TEXT,
    received_at         TIMESTAMP NOT NULL
);

-- ============================================================
-- DIMENSION TABLES
-- ============================================================

CREATE TABLE IF NOT EXISTS dim_manager_13f (
    manager_cik         TEXT PRIMARY KEY,
    manager_name        TEXT
);

CREATE TABLE IF NOT EXISTS dim_issuer (
    issuer_key          TEXT PRIMARY KEY,
    issuer_cik          TEXT,
    ticker              TEXT,
    cusip               TEXT,
    issuer_name         TEXT,
    sector              TEXT,
    industry            TEXT,
    mapping_confidence  TEXT DEFAULT 'LOW'
);

-- ============================================================
-- FACT TABLES — RAW FILINGS
-- ============================================================

CREATE TABLE IF NOT EXISTS fact_13f_positions (
    filing_accession_no TEXT NOT NULL,
    manager_cik         TEXT,
    manager_name        TEXT,
    report_period       DATE,
    filed_at            DATE,
    issuer_name         TEXT,
    cusip               TEXT,
    ticker              TEXT,
    class_title         TEXT,
    value_usd_thousands DOUBLE,
    shares              DOUBLE,
    put_call            TEXT,
    discretion          TEXT,
    source_path         TEXT,
    ingested_at         TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_form4_transactions (
    filing_accession_no TEXT NOT NULL,
    issuer_cik          TEXT,
    issuer_name         TEXT,
    ticker              TEXT,
    insider_name        TEXT,
    insider_role        TEXT,
    transaction_date    DATE,
    transaction_code    TEXT,
    direction           TEXT,
    shares              DOUBLE,
    price               DOUBLE,
    ownership_after     DOUBLE,
    source_path         TEXT,
    ingested_at         TIMESTAMP NOT NULL
);

-- ============================================================
-- AGGREGATED TABLES — QUARTERLY SNAPSHOTS (Kubera reports)
-- ============================================================

-- Per-ticker quarterly aggregate: total institutional count, shares, value
CREATE TABLE IF NOT EXISTS agg_quarterly_holdings (
    ticker              TEXT NOT NULL,
    report_quarter      TEXT NOT NULL,     -- e.g. '2025-Q3'
    inst_count          INTEGER DEFAULT 0, -- number of institutions holding
    total_shares        DOUBLE DEFAULT 0,
    total_value_usd_k   DOUBLE DEFAULT 0, -- value in thousands
    avg_shares_per_inst DOUBLE DEFAULT 0,
    sector              TEXT,
    computed_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, report_quarter)
);

-- Quarter-over-quarter diff for each ticker
CREATE TABLE IF NOT EXISTS agg_qoq_changes (
    ticker              TEXT NOT NULL,
    current_quarter     TEXT NOT NULL,     -- e.g. '2025-Q3'
    prior_quarter       TEXT NOT NULL,     -- e.g. '2025-Q2'
    -- Count changes
    inst_count_current  INTEGER DEFAULT 0,
    inst_count_prior    INTEGER DEFAULT 0,
    inst_count_change   INTEGER DEFAULT 0,
    inst_count_change_pct DOUBLE,
    -- Shares changes
    shares_current      DOUBLE DEFAULT 0,
    shares_prior        DOUBLE DEFAULT 0,
    shares_change       DOUBLE DEFAULT 0,
    shares_change_pct   DOUBLE,
    -- Value changes
    value_current_usd_k DOUBLE DEFAULT 0,
    value_prior_usd_k   DOUBLE DEFAULT 0,
    value_change_usd_k  DOUBLE DEFAULT 0,
    value_change_pct    DOUBLE,
    -- Streak tracking (consecutive quarters of increase)
    count_up_streak     INTEGER DEFAULT 0,
    shares_up_streak    INTEGER DEFAULT 0,
    value_up_streak     INTEGER DEFAULT 0,
    sector              TEXT,
    computed_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, current_quarter)
);

-- Sector-level quarterly aggregation
CREATE TABLE IF NOT EXISTS agg_sector_quarterly (
    sector              TEXT NOT NULL,
    report_quarter      TEXT NOT NULL,
    total_inst_count    INTEGER DEFAULT 0,
    total_shares        DOUBLE DEFAULT 0,
    total_value_usd_k   DOUBLE DEFAULT 0,
    ticker_count        INTEGER DEFAULT 0,
    computed_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (sector, report_quarter)
);

-- ============================================================
-- REPORT OUTPUT TABLES — Pre-computed Kubera reports
-- ============================================================

-- Platinum Report: composite-scored ranking
CREATE TABLE IF NOT EXISTS report_platinum (
    report_quarter      TEXT NOT NULL,
    rank_position       INTEGER NOT NULL,
    ticker              TEXT NOT NULL,
    company_name        TEXT,
    sector              TEXT,
    composite_score     DOUBLE NOT NULL,
    -- Component scores (0-100 each)
    accumulation_score  DOUBLE DEFAULT 0,
    volume_growth_score DOUBLE DEFAULT 0,
    stability_score     DOUBLE DEFAULT 0,
    -- Raw metrics
    inst_count_current  INTEGER,
    inst_count_prior    INTEGER,
    shares_change_pct   DOUBLE,
    value_change_pct    DOUBLE,
    count_change_pct    DOUBLE,
    computed_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (report_quarter, ticker)
);

-- Kubera Diamonds: various filters applied to QoQ changes
CREATE TABLE IF NOT EXISTS report_kubera_diamonds (
    report_quarter      TEXT NOT NULL,
    diamond_type        TEXT NOT NULL,     -- 'SHARES_UPTREND', 'PRICE_VOLUME', 'CSAPV'
    ticker              TEXT NOT NULL,
    company_name        TEXT,
    sector              TEXT,
    shares_change_pct   DOUBLE,
    value_change_pct    DOUBLE,
    count_change_pct    DOUBLE,
    price_change_pct    DOUBLE,           -- requires IBKR price data
    volume_change_pct   DOUBLE,           -- requires IBKR volume data
    csapv_aligned       BOOLEAN DEFAULT FALSE,
    computed_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (report_quarter, diamond_type, ticker)
);

-- Institutional exit tracking
CREATE TABLE IF NOT EXISTS report_institutional_exits (
    report_quarter      TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    company_name        TEXT,
    sector              TEXT,
    inst_count_current  INTEGER,
    inst_count_prior    INTEGER,
    inst_count_change   INTEGER,
    shares_change_pct   DOUBLE,
    value_change_pct    DOUBLE,
    exit_severity       TEXT,             -- 'PARTIAL', 'SIGNIFICANT', 'MASS_EXIT'
    computed_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (report_quarter, ticker)
);

-- ============================================================
-- MARKET DATA TABLES — Price/volume from Massive.com
-- ============================================================

CREATE TABLE IF NOT EXISTS fact_daily_prices (
    ticker              TEXT NOT NULL,
    trade_date          DATE NOT NULL,
    open                DOUBLE,
    high                DOUBLE,
    low                 DOUBLE,
    close               DOUBLE,
    volume              BIGINT,
    vwap                DOUBLE,
    transactions        INTEGER,
    source              TEXT,             -- 'massive_rest' or 'massive_s3'
    ingested_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, trade_date)
);

-- ============================================================
-- INTELLIGENCE LAYER — Phase classification + conviction scores
-- ============================================================

CREATE TABLE IF NOT EXISTS intelligence_scores (
    ticker                      TEXT NOT NULL,
    report_quarter              TEXT NOT NULL,       -- e.g. '2025-Q3'
    computed_at                 TIMESTAMP NOT NULL,

    -- Phase Classification (Model 1)
    accum_phase                 TEXT,                -- DORMANT|EARLY_ACCUM|ACTIVE_ACCUM|LATE_ACCUM|EXPANSION|DISTRIBUTION|DECLINE
    accum_phase_quarters        INTEGER DEFAULT 0,   -- consecutive quarters in current phase
    accum_strength_score        DOUBLE DEFAULT 0,    -- 0-100

    -- Lag Estimation (Model 2)
    expected_impact_quarters    INTEGER DEFAULT 2,
    lag_confidence              TEXT DEFAULT 'LOW',  -- HIGH|MEDIUM|LOW
    lag_rationale               TEXT,

    -- Cascade Detection (Model 3)
    cascade_stage               INTEGER DEFAULT 0,   -- 0=none 1=early 2=active 3=late
    new_initiations_count       INTEGER DEFAULT 0,
    copycat_score               DOUBLE DEFAULT 0,

    -- Smart Money Divergence (Model 4)
    divergence_active           BOOLEAN DEFAULT FALSE,
    divergence_magnitude        DOUBLE DEFAULT 0,

    -- Manager Quality (Model 5)
    tier1_manager_count         INTEGER DEFAULT 0,
    tier2_manager_count         INTEGER DEFAULT 0,
    manager_quality_score       DOUBLE DEFAULT 0,

    -- Concentration Signal (Model 6)
    max_manager_concentration   DOUBLE DEFAULT 0,
    concentrated_managers_count INTEGER DEFAULT 0,

    -- Insider Intelligence (Model 7)
    insider_cluster_detected    BOOLEAN DEFAULT FALSE,
    insider_net_buy_count       INTEGER DEFAULT 0,
    ceo_cfo_buying              BOOLEAN DEFAULT FALSE,
    insider_score               DOUBLE DEFAULT 0,

    -- Conviction Cascade Score (Model 8)
    conviction_score            DOUBLE DEFAULT 0,    -- 0-100 master score
    conviction_breakdown        TEXT,                -- JSON

    -- Distribution Warning (Model 9)
    distribution_warning        BOOLEAN DEFAULT FALSE,
    distribution_severity       TEXT,                -- MILD|MODERATE|SEVERE

    -- Trading Signals (Model 10)
    day_bias                    TEXT DEFAULT 'NEUTRAL', -- LONG_ONLY|SHORT_ONLY|NEUTRAL
    swing_signal                TEXT,                -- BUY|WATCH|AVOID|SHORT
    swing_entry_zone            TEXT,
    swing_target                TEXT,
    swing_stop                  TEXT,
    swing_options_suggestion    TEXT,
    longterm_signal             TEXT,                -- BUY|ACCUMULATE|HOLD|REDUCE|EXIT
    longterm_thesis             TEXT,
    longterm_target_quarter     TEXT,
    longterm_options_suggestion TEXT,

    PRIMARY KEY (ticker, report_quarter)
);

-- Manager quality tiers (built once from 13F AUM)
CREATE TABLE IF NOT EXISTS dim_manager_tiers (
    manager_cik         TEXT PRIMARY KEY,
    manager_name        TEXT,
    total_aum_k         DOUBLE,
    tier                INTEGER    -- 1=top-20, 2=top-100, 3=rest
);

-- Sector rotation aggregation (for Sector Clock)
CREATE TABLE IF NOT EXISTS agg_sector_rotation (
    sector              TEXT NOT NULL,
    report_quarter      TEXT NOT NULL,
    total_value_k       DOUBLE DEFAULT 0,
    net_flow_k          DOUBLE DEFAULT 0,
    flow_pct            DOUBLE DEFAULT 0,
    net_inst_count_change INTEGER DEFAULT 0,
    ticker_count        INTEGER DEFAULT 0,
    inflow_streak       INTEGER DEFAULT 0,   -- consecutive quarters of net inflow
    computed_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (sector, report_quarter)
);

-- Backtest results table
CREATE TABLE IF NOT EXISTS backtest_results (
    ticker                  TEXT NOT NULL,
    signal_quarter          TEXT NOT NULL,
    entry_date              DATE,
    entry_price             DOUBLE,
    accum_phase             TEXT,
    conviction_score        DOUBLE,
    cascade_stage           INTEGER,
    insider_confirmed       BOOLEAN,
    tier1_present           BOOLEAN,
    -- Forward returns
    return_30d              DOUBLE,
    return_60d              DOUBLE,
    return_90d              DOUBLE,
    return_180d             DOUBLE,
    -- SPY benchmark
    spy_return_30d          DOUBLE,
    spy_return_60d          DOUBLE,
    spy_return_90d          DOUBLE,
    spy_return_180d         DOUBLE,
    -- Alpha (stock - spy)
    alpha_30d               DOUBLE,
    alpha_60d               DOUBLE,
    alpha_90d               DOUBLE,
    alpha_180d              DOUBLE,
    -- Lag accuracy
    estimated_lag_quarters  INTEGER,
    actual_peak_quarter     INTEGER,
    computed_at             TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, signal_quarter)
);

-- ============================================================
-- INSIDER OUTCOME ENGINE — Historical pattern analysis
-- ============================================================

-- Individual insider transaction outcomes with forward returns
CREATE TABLE IF NOT EXISTS fact_insider_outcomes (
    ticker              TEXT NOT NULL,
    transaction_date    DATE NOT NULL,
    insider_name        TEXT NOT NULL,
    insider_role        TEXT,
    role_category       TEXT,             -- CEO|CFO|COO|DIRECTOR|VP|10PCT_OWNER|OTHER
    transaction_code    TEXT,
    shares              DOUBLE,
    txn_price           DOUBLE,
    dollar_value        DOUBLE,
    size_category       TEXT,             -- SMALL|MEDIUM|LARGE|MEGA
    is_routine          BOOLEAN DEFAULT FALSE,  -- Cohen-Malloy-Pomorski classification
    entry_close         DOUBLE,
    close_t5            DOUBLE,           -- 5 trading days (1 week)
    close_t21           DOUBLE,           -- 21 trading days (1 month)
    close_t63           DOUBLE,           -- 63 trading days (3 months)
    close_t126          DOUBLE,           -- 126 trading days (6 months)
    return_5d           DOUBLE,
    return_30d          DOUBLE,
    return_90d          DOUBLE,
    return_180d         DOUBLE,
    spy_return_5d       DOUBLE,
    spy_return_30d      DOUBLE,
    spy_return_90d      DOUBLE,
    spy_return_180d     DOUBLE,
    alpha_5d            DOUBLE,
    alpha_30d           DOUBLE,
    alpha_90d           DOUBLE,
    alpha_180d          DOUBLE,
    computed_at         TIMESTAMP NOT NULL
);

-- Statistical profiles per ticker/role/size
CREATE TABLE IF NOT EXISTS agg_insider_patterns (
    ticker              TEXT NOT NULL,
    pattern_type        TEXT NOT NULL,     -- ALL|OPPORTUNISTIC|ROLE|SIZE
    role_category       TEXT DEFAULT 'ALL',
    size_category       TEXT DEFAULT 'ALL',
    sample_count        INTEGER DEFAULT 0,
    win_rate_5d         DOUBLE,
    win_rate_30d        DOUBLE,
    win_rate_90d        DOUBLE,
    win_rate_180d       DOUBLE,
    alpha_win_5d        DOUBLE,
    alpha_win_30d       DOUBLE,
    alpha_win_90d       DOUBLE,
    alpha_win_180d      DOUBLE,
    mean_return_5d      DOUBLE,
    mean_return_30d     DOUBLE,
    mean_return_90d     DOUBLE,
    mean_return_180d    DOUBLE,
    mean_alpha_5d       DOUBLE,
    mean_alpha_30d      DOUBLE,
    mean_alpha_90d      DOUBLE,
    mean_alpha_180d     DOUBLE,
    median_return_90d   DOUBLE,
    insider_effect_score DOUBLE DEFAULT 0, -- 0-100 composite
    computed_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, pattern_type, role_category, size_category)
);

-- ============================================================
-- SHORT INTEREST & SHORT VOLUME — Polygon/Massive.com
-- ============================================================

-- Bi-monthly FINRA short interest (total outstanding shorts)
CREATE TABLE IF NOT EXISTS fact_short_interest (
    ticker              TEXT NOT NULL,
    settlement_date     DATE NOT NULL,
    short_interest      BIGINT,           -- total shares sold short
    avg_daily_volume    BIGINT,
    days_to_cover       DOUBLE,           -- short_interest / avg_daily_volume
    source              TEXT,
    ingested_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, settlement_date)
);

-- Daily FINRA short sale volume
CREATE TABLE IF NOT EXISTS fact_short_volume (
    ticker              TEXT NOT NULL,
    trade_date          DATE NOT NULL,
    short_volume        BIGINT,           -- total short sale volume that day
    total_volume        BIGINT,           -- total reported volume
    short_volume_ratio  DOUBLE,           -- short_volume / total_volume * 100
    exempt_volume       BIGINT,           -- short-sale-exempt volume
    non_exempt_volume   BIGINT,           -- non-exempt short volume
    source              TEXT,
    ingested_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, trade_date)
);

-- Daily aggregated dark pool (off-exchange) volume
CREATE TABLE IF NOT EXISTS fact_dark_pool_daily (
    ticker              TEXT NOT NULL,
    trade_date          DATE NOT NULL,
    dark_pool_volume    BIGINT DEFAULT 0, -- total off-exchange volume (exchange=4)
    dark_pool_trades    INTEGER DEFAULT 0,-- count of off-exchange trades
    dark_pool_vwap      DOUBLE,           -- volume-weighted average price
    total_volume        BIGINT DEFAULT 0, -- total volume (all exchanges)
    dark_pool_pct       DOUBLE DEFAULT 0, -- dark_pool_volume / total_volume * 100
    source              TEXT,
    ingested_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, trade_date)
);

-- Cost-to-borrow / securities lending data
CREATE TABLE IF NOT EXISTS fact_cost_to_borrow (
    ticker              TEXT NOT NULL,
    report_date         DATE NOT NULL,
    fee_rate            DOUBLE,           -- annualized borrow fee %
    available_shares    BIGINT,           -- shares available to borrow
    utilization_pct     DOUBLE,           -- % of lendable shares on loan
    source              TEXT,
    ingested_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, report_date)
);

-- ============================================================
-- OPTIONS FLOW — Polygon v3/snapshot/options EOD aggregate
-- ============================================================

-- Daily aggregate options flow per underlying ticker
CREATE TABLE IF NOT EXISTS fact_options_flow (
    ticker              TEXT NOT NULL,
    snapshot_date       DATE NOT NULL,
    call_volume         BIGINT DEFAULT 0,     -- total call volume
    put_volume          BIGINT DEFAULT 0,     -- total put volume
    call_oi             BIGINT DEFAULT 0,     -- total call open interest
    put_oi              BIGINT DEFAULT 0,     -- total put open interest
    put_call_ratio_vol  DOUBLE,               -- put_volume / call_volume
    put_call_ratio_oi   DOUBLE,               -- put_oi / call_oi
    avg_call_iv         DOUBLE,               -- avg implied volatility (calls)
    avg_put_iv          DOUBLE,               -- avg implied volatility (puts)
    max_call_oi_strike  DOUBLE,               -- strike with highest call OI (gamma wall)
    max_put_oi_strike   DOUBLE,               -- strike with highest put OI (put wall)
    unusual_call_flag   BOOLEAN DEFAULT FALSE,-- call vol > 3x avg
    unusual_put_flag    BOOLEAN DEFAULT FALSE,-- put vol > 3x avg
    source              TEXT,
    ingested_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, snapshot_date)
);

-- ============================================================
-- NEWS SENTIMENT — Polygon v2/reference/news
-- ============================================================

-- Per-article per-ticker sentiment from Polygon news insights
CREATE TABLE IF NOT EXISTS fact_news_sentiment (
    news_id             TEXT NOT NULL,        -- Polygon article ID
    ticker              TEXT NOT NULL,        -- affected ticker
    published_at        TIMESTAMP,
    title               TEXT,
    sentiment           TEXT,                 -- positive / negative / neutral
    sentiment_score     SMALLINT,             -- +1 / -1 / 0
    sentiment_reasoning TEXT,
    author              TEXT,
    article_url         TEXT,
    publisher           TEXT,
    source              TEXT DEFAULT 'polygon',
    ingested_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (news_id, ticker)
);

-- ============================================================
-- RELATED STOCKS & LEAD-LAG
-- ============================================================

-- Related company pairs from Polygon v1/related-companies
CREATE TABLE IF NOT EXISTS dim_related_companies (
    ticker              TEXT NOT NULL,
    related_ticker      TEXT NOT NULL,
    relationship_type   TEXT DEFAULT 'polygon_related',
    source              TEXT,
    ingested_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, related_ticker)
);

-- Rolling correlation between ticker pairs (lead-lag signal)
CREATE TABLE IF NOT EXISTS fact_stock_correlations (
    ticker_a            TEXT NOT NULL,
    ticker_b            TEXT NOT NULL,
    lookback_days       INTEGER NOT NULL,
    correlation         DOUBLE,               -- Pearson -1 to +1
    granger_p_value     DOUBLE,               -- A Granger-causes B (p-value)
    computed_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker_a, ticker_b, lookback_days)
);

-- ============================================================
-- FORM 8-K MATERIAL EVENTS — SEC EDGAR daily refresh
-- ============================================================

-- One row per 8-K filing; classified by item type for fast signal lookup.
-- Populated by jobs/daily_8k_refresh.py (runs EOD ~6:45 PM ET weekdays).
--
-- 8-K items tracked:
--   1.01  Material Definitive Agreement (M&A / deals)
--   1.05  Cybersecurity Incidents
--   2.01  Completion of Acquisition
--   2.02  Results of Operations (earnings)
--   2.04  Triggering Events on Debt
--   5.02  Director / Officer Changes
--   8.01  Other Events
CREATE TABLE IF NOT EXISTS fact_form8k_events (
    filing_accession_no TEXT NOT NULL PRIMARY KEY,
    filer_cik           TEXT,                 -- SEC CIK (no leading zeros)
    company_name        TEXT,
    ticker              TEXT,                 -- best-effort from dim_issuer join
    filed_date          DATE,                 -- FILED AS OF DATE from SGML
    report_date         DATE,                 -- CONFORMED PERIOD OF REPORT
    event_items         TEXT,                 -- comma-separated codes e.g. "2.02,9.01"
    has_earnings        BOOLEAN DEFAULT FALSE, -- item 2.02
    has_acquisition     BOOLEAN DEFAULT FALSE, -- item 1.01 or 2.01
    has_officer_change  BOOLEAN DEFAULT FALSE, -- item 5.02
    has_cyber_incident  BOOLEAN DEFAULT FALSE, -- item 1.05
    source_url          TEXT,
    ingested_at         TIMESTAMP NOT NULL
);
