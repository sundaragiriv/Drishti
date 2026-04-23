"""SQLite schema definitions for Signal Command Center V2."""

CREATE_SIGNALS_TABLE = """
CREATE TABLE IF NOT EXISTS signals (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                  TEXT    NOT NULL,
    timestamp               TEXT    NOT NULL,
    timeframe               TEXT    NOT NULL,
    score                   INTEGER NOT NULL,
    signal                  TEXT    NOT NULL,
    price                   REAL,
    sma_200                 REAL,
    sma_50                  REAL,
    price_vs_sma            TEXT,
    price_vs_sma_pct        REAL,
    zero_gamma_level        REAL,
    gamma_wall_up           REAL,
    gamma_wall_down         REAL,
    gex_status              TEXT,
    rsi                     REAL,
    rsi_slope               REAL,
    adx                     REAL,
    adx_slope               REAL,
    atr                     REAL,
    volume_ratio            REAL,
    vwap                    REAL,
    vwap_status             TEXT,
    trend_direction         TEXT,
    recommendation          TEXT,
    stop_loss               REAL,
    target_1                REAL,
    target_2                REAL,
    rr_ratio                REAL,
    trade_conditions        TEXT,
    distance_to_resistance_pct REAL,
    distance_to_support_pct    REAL,
    prior_day_high          REAL,
    prior_day_low           REAL,
    prior_day_close         REAL,
    relative_strength       REAL,
    market_regime           TEXT,
    signal_age              INTEGER DEFAULT 1,
    session_time            TEXT,
    sector                  TEXT,
    last_updated            TEXT    NOT NULL,
    UNIQUE(symbol, timeframe, timestamp)
);
"""

CREATE_SCAN_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS scan_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_start      TEXT    NOT NULL,
    scan_end        TEXT    NOT NULL,
    symbols_scanned INTEGER,
    signals_found   INTEGER,
    errors          INTEGER DEFAULT 0,
    data_source     TEXT,
    duration_seconds REAL,
    scan_type       TEXT
);
"""

CREATE_PAPER_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at               TEXT    NOT NULL,
    closed_at               TEXT,
    symbol                  TEXT    NOT NULL,
    side                    TEXT    NOT NULL,   -- LONG or SHORT
    entry_price             REAL    NOT NULL,
    exit_price              REAL,
    quantity                INTEGER NOT NULL,
    notional                REAL    NOT NULL,
    stop_loss               REAL,
    target_1                REAL,
    target_2                REAL,
    status                  TEXT    NOT NULL,   -- OPEN or CLOSED
    exit_reason             TEXT,
    recommendation_source   TEXT,
    instrument_type         TEXT    DEFAULT 'STOCK',
    option_type             TEXT,
    option_expiry           TEXT,
    option_strike           REAL,
    entry_signal            TEXT,
    entry_score             REAL,
    entry_rr_ratio          REAL,
    entry_market_regime     TEXT,
    entry_gex_status        TEXT,
    entry_session_time      TEXT,
    entry_trade_conditions  TEXT,
    realized_pnl            REAL    DEFAULT 0,
    realized_pnl_pct        REAL    DEFAULT 0,
    fees                    REAL    DEFAULT 0,
    created_ts              TEXT    NOT NULL
);
"""

CREATE_OPTION_SETUPS_TABLE = """
CREATE TABLE IF NOT EXISTS option_setups (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT    NOT NULL,
    option_type         TEXT    NOT NULL,   -- CALL or PUT
    expiry_date         TEXT    NOT NULL,
    strike              REAL    NOT NULL,
    underlying_price    REAL    NOT NULL,
    recommendation      TEXT    NOT NULL,   -- BUY or SELL from stock model
    signal              TEXT    NOT NULL,   -- LONG or SHORT
    score               REAL    NOT NULL,
    rr_ratio            REAL,
    market_regime       TEXT,
    gex_status          TEXT,
    option_bid          REAL,
    option_ask          REAL,
    option_last         REAL,
    option_mid          REAL,
    option_spread_pct   REAL,
    option_volume       REAL,
    option_open_interest REAL,
    quote_ts            TEXT,
    liquidity_score     REAL,
    liquidity_state     TEXT,
    rationale           TEXT,
    idea_state          TEXT    NOT NULL DEFAULT 'NEW',  -- NEW, STRONG, ACTIVE, WEAKENING, INVALID, EXPIRED
    confirm_count       INTEGER NOT NULL DEFAULT 1,
    invalid_reason      TEXT,
    is_taken            INTEGER NOT NULL DEFAULT 0,
    taken_at            TEXT,
    last_validated_ts   TEXT,
    status              TEXT    NOT NULL,   -- ACTIVE, EXPIRED, CANCELED
    created_ts          TEXT    NOT NULL,
    updated_ts          TEXT    NOT NULL,
    UNIQUE(symbol, option_type, expiry_date, strike)
);
"""

CREATE_OPTION_SETUP_OUTCOMES_TABLE = """
CREATE TABLE IF NOT EXISTS option_setup_outcomes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_id            INTEGER NOT NULL,
    symbol              TEXT    NOT NULL,
    option_type         TEXT    NOT NULL,
    horizon_minutes     INTEGER NOT NULL,
    entry_underlying    REAL    NOT NULL,
    future_underlying   REAL    NOT NULL,
    move_pct            REAL    NOT NULL,
    signed_move_pct     REAL    NOT NULL,
    outcome             TEXT    NOT NULL,   -- WIN, LOSS, FLAT
    hit                 INTEGER NOT NULL,   -- 1 win, 0 otherwise
    evaluated_at        TEXT    NOT NULL,
    source_signal_ts    TEXT,
    UNIQUE(setup_id, horizon_minutes)
);
"""

CREATE_EOD_ANALYSIS_TABLE = """
CREATE TABLE IF NOT EXISTS eod_analysis (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date          TEXT    NOT NULL UNIQUE,  -- YYYY-MM-DD
    total_trades        INTEGER NOT NULL,
    wins                INTEGER NOT NULL,
    losses              INTEGER NOT NULL,
    win_rate            REAL    NOT NULL,
    realized_pnl        REAL    NOT NULL,
    avg_loss            REAL    NOT NULL,
    max_loss            REAL    NOT NULL,
    top_loss_reason     TEXT,
    insights_json       TEXT,
    suggested_actions   TEXT,
    action_status       TEXT    NOT NULL DEFAULT 'PENDING',
    action_notes        TEXT,
    action_updated_ts   TEXT,
    created_ts          TEXT    NOT NULL
);
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp DESC);",
    "CREATE INDEX IF NOT EXISTS idx_signals_score ON signals(score DESC);",
    "CREATE INDEX IF NOT EXISTS idx_signals_signal ON signals(signal);",
    "CREATE INDEX IF NOT EXISTS idx_signals_composite ON signals(symbol, timeframe, timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_signals_regime ON signals(market_regime);",
    "CREATE INDEX IF NOT EXISTS idx_signals_sector ON signals(sector);",
    "CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status);",
    "CREATE INDEX IF NOT EXISTS idx_paper_symbol_status ON paper_trades(symbol, status);",
    "CREATE INDEX IF NOT EXISTS idx_paper_opened_at ON paper_trades(opened_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_paper_instrument ON paper_trades(instrument_type, status);",
    "CREATE INDEX IF NOT EXISTS idx_paper_strategy ON paper_trades(strategy_type, status);",
    "CREATE INDEX IF NOT EXISTS idx_paper_exec_mode ON paper_trades(execution_mode, status);",
    "CREATE INDEX IF NOT EXISTS idx_paper_ibkr_perm ON paper_trades(ibkr_perm_id);",
    "CREATE INDEX IF NOT EXISTS idx_option_setups_status ON option_setups(status, score DESC);",
    "CREATE INDEX IF NOT EXISTS idx_option_setups_symbol ON option_setups(symbol, created_ts DESC);",
    "CREATE INDEX IF NOT EXISTS idx_option_outcomes_setup ON option_setup_outcomes(setup_id, horizon_minutes);",
    "CREATE INDEX IF NOT EXISTS idx_option_outcomes_symbol ON option_setup_outcomes(symbol, evaluated_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_eod_trade_date ON eod_analysis(trade_date DESC);",
]
