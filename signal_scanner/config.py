"""Configuration for Signal Command Center.

All tunable parameters live here — no magic numbers in other modules.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

BASE_DIR = Path(__file__).resolve().parent
WATCHLIST_DIR = BASE_DIR / "watchlists"
DB_PATH = BASE_DIR / "data" / "signals.db"
LOG_DIR = BASE_DIR / "logs"


@dataclass
class IBKRConfig:
    """Interactive Brokers connection settings."""

    host: str = "127.0.0.1"
    port: int = 7497  # TWS paper=7497, live=7496; Gateway paper=4002, live=4001
    client_id: int = 20
    timeout: int = 30
    max_retries: int = 3
    retry_delay: int = 5


@dataclass
class ScannerConfig:
    """Scanner behavior settings."""

    scan_interval_seconds: int = 900
    timeframes: List[str] = field(default_factory=lambda: ["5m", "15m", "1h"])
    lookback_periods: Dict[str, str] = field(default_factory=lambda: {
        "5m": "5d",
        "15m": "10d",
        "1h": "30d",
    })

    # Indicator periods
    sma_period: int = 200
    sma_short_period: int = 50
    rsi_period: int = 14
    adx_period: int = 14
    atr_period: int = 14
    volume_avg_period: int = 20

    # Thresholds
    volume_threshold: float = 1.3
    adx_threshold: float = 25.0
    notification_score_threshold: int = 85

    # ATR-based stop/target multipliers (tighter for intraday focus)
    atr_stop_multiplier: float = 1.0   # Stop = price -/+ 1.0 * ATR
    atr_target1_multiplier: float = 1.5  # T1 = 1.5:1 risk
    atr_target2_multiplier: float = 2.0  # T2 = 2:1 risk

    # R:R gate — signals below this R:R are demoted to HOLD
    min_rr_ratio: float = 1.5

    # Momentum slope lookback (bars)
    momentum_slope_period: int = 3

    # Market regime
    regime_benchmark: str = "SPY"
    regime_vix_symbol: str = "^VIX"
    regime_vix_high: float = 25.0       # VIX above this = RISK_OFF
    regime_vix_low: float = 18.0        # VIX below this = RISK_ON

    # Relative strength lookback (bars)
    relative_strength_period: int = 20

    # Fair Value Gap detection
    fvg_lookback_bars: int = 30          # How many bars to scan for unfilled FVGs
    fvg_near_threshold_pct: float = 0.5  # Price within 0.5% of FVG edge = NEAR signal

    # Liquidity sweep/reclaim + VWAP mean-reversion feature controls
    liquidity_sweep_lookback: int = 20
    liquidity_reclaim_max_bars: int = 3
    liquidity_sweep_volume_spike_ratio: float = 1.5
    vwap_reversion_sd_threshold: float = 2.0
    rsi_divergence_lookback: int = 8

    # Session time boundaries (ET)
    session_early_end_minute: int = 30    # First 30 min = EARLY
    session_power_start_hour: int = 15    # 3 PM ET onward = POWER_HOUR

    # Time guards (ET)
    eod_evaluation_hour: int = 15       # 3:55 PM ET — smart EOD evaluation
    eod_evaluation_minute: int = 55
    late_entry_cutoff_hour: int = 15    # 3:30 PM ET — no new entries after this
    late_entry_cutoff_minute: int = 30
    signal_expiry_scans: int = 6        # Stale signal expiry (~90 min at 15-min scans)

    # Swing promotion — positions with strong momentum are kept overnight
    swing_promotion_enabled: bool = True
    swing_min_score: float = 70.0       # Minimum score to promote to SWING
    swing_min_rr: float = 1.5           # Minimum R:R to promote
    swing_require_trend_alignment: bool = True  # Must be in aligned trend
    swing_require_positive_pnl: bool = False    # If True, only promote if in profit
    swing_max_hold_days: int = 5        # Max days a swing can be held before forced close

    # Paper trading
    paper_trading_enabled: bool = True
    paper_starting_capital: float = 1000000.0  # $1M paper trading account
    paper_risk_per_trade_pct: float = 1.0
    paper_max_open_positions: int = 30  # Evaluation mode — maximize trade volume for analysis
    paper_fee_per_trade: float = 0.0
    paper_leverage_per_trade: float = 15000.0  # Max notional per trade
    paper_min_notional_per_trade: float = 10000.0  # Target ~$10K per trade
    paper_entry_confirmations_required: int = 1      # 1 confirmed scan — relaxed to avoid NEW-state deadlock
    paper_entry_min_score: float = 65.0              # Normalized scores post-GEX fix; 65 = strong technicals
    paper_entry_min_rr: float = 1.5                  # Lowered for evaluation mode
    paper_entry_min_mtf_score: float = 0.67          # 2/3 TF agreement sufficient for day trade entry
    paper_entry_require_trend_alignment: bool = True
    paper_entry_allowed_sessions: List[str] = field(default_factory=lambda: ["EARLY", "MID_DAY", "POWER_HOUR"])
    paper_entry_require_setup_trigger: bool = False  # Disabled — redundant with confluence (was True)
    paper_entry_require_gex_alignment: bool = False  # Disabled — already scored in confluence (was True)
    paper_entry_require_regime_gate: bool = True
    paper_entry_min_distance_to_level_pct: float = 0.5  # Relaxed from 1.0%
    paper_defensive_auto_from_eod: bool = False  # EOD is analysis only, not a trading gate
    paper_defensive_score_min: int = 70
    paper_defensive_rr_min: float = 1.5              # Aligned with base (was 1.8)
    paper_defensive_signal_age_min: int = 1
    paper_defensive_allowed_sessions: List[str] = field(default_factory=lambda: ["EARLY", "POWER_HOUR"])
    paper_stop_loss_rate_trigger_pct: float = 45.0
    paper_flip_rate_trigger_pct: float = 35.0
    paper_flip_confirm_cycles: int = 3               # More patience before flip exit (was 2)
    paper_require_flip_confirmation_always: bool = True
    # Daily risk kill-switch — block new entries once breached (trips reset at NY midnight)
    paper_daily_max_drawdown_pct: float = 2.0        # % of starting capital; 2% = $20K on $1M
    paper_global_r_cap_pct: float = 8.0              # Max concurrent $-at-risk across open positions
    # Idea-bridge target multiples (Triple Lock + Swing Ideas).
    # Was 2.5 — backtested at LIVE config (2*ATR stops) showed 2.5R target hit
    # only 9% of the time, killing net expectancy after costs (-0.131R/trade).
    # 1R target hits 38.6% on Triple Lock, net +0.024R/trade (POSITIVE).
    # See docs/live_config_expectancy_2026-04-25.md for full delta.
    paper_idea_target_r_multiple: float = 1.0        # primary take-profit (target_1)
    paper_idea_stretch_target_r_multiple: float = 1.5  # secondary stretch (target_2)
    options_liquidity_enabled: bool = True
    options_min_open_interest: int = 200
    options_min_volume: int = 25
    options_max_spread_pct: float = 0.06
    options_min_liquidity_score: float = 55.0
    options_max_quote_age_minutes: int = 20
    options_evaluation_min_days: int = 14


@dataclass
class GEXConfig:
    """Gamma Exposure calculation settings."""

    min_dte: int = 7
    max_dte: int = 45
    strike_range_pct: float = 0.15  # +/- 15% from spot
    risk_free_rate: float = 0.05
    prefer_ibkr_chain: bool = True  # IBKR is the only data source


@dataclass
class DashboardConfig:
    """Plotly Dash dashboard settings."""

    host: str = "127.0.0.1"
    port: int = 8050
    debug: bool = False
    refresh_interval_ms: int = 30_000

    # Kubera Dark + Turmeric Gold theme
    bg_color: str = "#0c0e14"
    card_color: str = "#14171e"
    card_color_elevated: str = "#1a1e28"
    text_color: str = "#f0f0f5"
    text_muted: str = "#8a92a6"
    border_color: str = "#2a2e3a"

    # Accent colors — Kubera Turmeric Gold
    accent_primary: str = "#D4A017"
    accent_primary_light: str = "#E8B930"
    accent_primary_dim: str = "rgba(212, 160, 23, 0.15)"

    # Signal colors (unchanged)
    accent_long: str = "#00d26a"
    accent_short: str = "#ff006e"
    accent_neutral: str = "#ffc107"

    # Legacy alias — remapped to gold so existing cfg.accent_cyan refs become gold
    accent_cyan: str = "#D4A017"


# Confluence scoring weights — must sum to 100
# Gradient scoring: each factor returns 0 to max_pts (not binary)
#
# 2026-04-24: gex_positioning DEMOTED to 0. The OI-only GEX formulation
# (gex_calculator.py) systematically mis-signs dealer positioning and was
# never validated against hit-rate in our data. GEX is still computed and
# shown on the dashboard for visualization; it just no longer steers
# scoring decisions. The 25 pts were redistributed to rsi_momentum (+10),
# trend_strength (+10), and vwap_position (+5) — all factors that have at
# least passed the smoke test of "moves with price action."
CONFLUENCE_WEIGHTS: Dict[str, int] = {
    "sma_position": 15,
    "gex_positioning": 0,        # was 25
    "rsi_momentum": 30,          # was 20 (+10)
    "volume_confirmation": 15,
    "trend_strength": 25,        # was 15 (+10)
    "vwap_position": 15,         # was 10 (+5)
}

# Regime-adaptive weight profiles — auto-selected based on MarketRegime
# Each profile must sum to 100.
REGIME_WEIGHTS: Dict[str, Dict[str, int]] = {
    "RISK_ON": {
        # Bullish regime: trend & momentum matter most
        "sma_position": 20,
        "gex_positioning": 0,    # was 15
        "rsi_momentum": 30,      # was 25 (+5)
        "volume_confirmation": 15,
        "trend_strength": 25,    # was 15 (+10)
        "vwap_position": 10,
    },
    "RISK_OFF": {
        # Bearish/volatile regime: trust volume confirmation, broaden trend
        "sma_position": 15,      # was 10 (+5)
        "gex_positioning": 0,    # was 30
        "rsi_momentum": 20,      # was 15 (+5)
        "volume_confirmation": 25,  # was 20 (+5)
        "trend_strength": 20,    # was 10 (+10)
        "vwap_position": 20,     # was 15 (+5)
    },
    "NEUTRAL": CONFLUENCE_WEIGHTS,  # Default balanced weights
}

# IBKR bar size and duration mappings
IBKR_BAR_SIZES: Dict[str, str] = {
    "1m": "1 min",
    "5m": "5 mins",
    "15m": "15 mins",
    "1h": "1 hour",
}
IBKR_DURATIONS: Dict[str, str] = {
    # Ensure enough bars for SMA-200 under RTH data.
    "1m": "1 D",    # Today's 1-min bars (for VWAP_MR live scanner)
    "5m": "5 D",    # ~390 RTH bars
    "15m": "10 D",  # ~260 RTH bars
    "1h": "60 D",   # ~390 RTH bars
}


# Column tooltip descriptions for dashboard
COLUMN_TOOLTIPS: Dict[str, str] = {
    "symbol": "Ticker symbol",
    "signal": "LONG/SHORT/NEUTRAL — direction from highest-conviction timeframe",
    "recommendation": "BUY (LONG score>=60 & R:R>=1.5) / SELL (SHORT score>=60 & R:R>=1.5) / HOLD",
    "score": "0-100 gradient confluence score: SMA(15) + GEX(25) + RSI(20) + Volume(15) + Trend(15) + VWAP(10)",
    "mtf_agreement": "Multi-timeframe agreement: 3/3 = all TFs align, 2/3 = majority, 1/3 = single TF only",
    "price": "Current price at scan time",
    "trend_direction": "UPTREND (SMA50>SMA200 + price>SMA50), DOWNTREND (opposite), SIDEWAYS (ADX<20 or tangled)",
    "rr_ratio": "Risk:Reward ratio — reward (distance to target) / risk (distance to stop). Min 1.5 for BUY/SELL",
    "stop_loss": "ATR-based stop: price -/+ 1.5×ATR. Adjusted to nearest GEX support/resistance if tighter",
    "target_1": "Target 1: 1R distance (same as stop distance). Take partial profit here",
    "target_2": "Target 2: 2R distance or gamma wall. Trail stop after T1 hit",
    "rsi": "RSI-14 value. Arrow shows 3-bar slope: rising/falling/flat",
    "adx": "ADX-14 trend strength. 25-40=strong trend, >50=extreme (exhaustion risk)",
    "vwap_status": "Price vs VWAP (Volume-Weighted Avg Price). Institutional benchmark for intraday",
    "gex_status": "Above/Below Zero Gamma + distance%. Above ZG = dealers suppress vol, Below ZG = vol amplified",
    "volume_ratio": "Current volume / 20-period avg. >1.3x = institutional activity",
    "relative_strength": "Performance vs SPY over 20 bars. Positive = outperforming market",
    "market_regime": "RISK_ON (SPY uptrend + low VIX) / RISK_OFF (SPY downtrend or high VIX) / NEUTRAL",
    "sector": "Stock sector classification",
    "signal_age": "How many consecutive scans this signal has persisted. Higher = more confirmed",
    "score_delta": "Score change since last scan. Positive = strengthening, negative = weakening",
    "signal_momentum": "STRENGTHENING (delta>+5) / WEAKENING (delta<-5) / STABLE / NEW",
    "near_earnings": "True if earnings report is within 3 days — signals demoted to HOLD",
    "session_time": "EARLY (first 30min) / MID_DAY / POWER_HOUR (last hour) / PRE_MARKET",
    "last_updated": "Local time of last scan update",
}
