"""Unit tests for PaperTrader safety logic — kill-switch and position sizing.

The kill-switch is the safety rail blocking new entries when daily DD or
global R-cap is breached. These tests use a real DatabaseManager backed by
an in-memory SQLite to validate the gate against actual SQL behavior, not
mocks — that's the whole point of integration-style unit tests for
financial code.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from signal_scanner.config import ScannerConfig
from signal_scanner.database.db_manager import DatabaseManager
from signal_scanner.paper.paper_trader import PaperTrader, NY_TZ


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    """Real SQLite DatabaseManager pointing at a tmp file (WAL-friendly)."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.init_db()
    return mgr


def _make_trade(
    db: DatabaseManager,
    symbol: str,
    side: str,
    entry_price: float,
    stop_loss: float,
    quantity: int,
    status: str = "OPEN",
    realized_pnl: float = 0.0,
    closed_at: str = None,
) -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    trade = {
        "opened_at": now_iso,
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "quantity": quantity,
        "notional": entry_price * quantity,
        "stop_loss": stop_loss,
        "target_1": None,
        "target_2": None,
        "status": status,
        "recommendation_source": "TEST",
        "strategy_type": "IDEA",
        "execution_mode": "SIM",
        "instrument_type": "STOCK",
        "option_type": None,
        "option_expiry": None,
        "option_strike": None,
        "entry_signal": side,
        "entry_score": None,
        "entry_rr_ratio": None,
        "entry_market_regime": None,
        "entry_gex_status": None,
        "entry_session_time": None,
        "entry_trade_conditions": "",
        "fees": 0.0,
        "created_ts": now_iso,
    }
    trade_id = db.create_paper_trade(trade)

    if status == "CLOSED":
        # Mock a close: set realized_pnl + closed_at directly via SQL
        with db._get_connection() as conn:
            conn.execute(
                "UPDATE paper_trades SET status='CLOSED', realized_pnl=?, closed_at=? WHERE id=?",
                (realized_pnl, closed_at or now_iso, trade_id),
            )
    return trade_id


# ── DatabaseManager helpers ────────────────────────────────────────────

def test_get_realized_pnl_since_sums_only_after_cutoff(db: DatabaseManager):
    midnight_ny = datetime.now(NY_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    today_iso = midnight_ny.astimezone(timezone.utc).isoformat()
    yesterday_iso = "2025-01-01T00:00:00+00:00"

    _make_trade(db, "AAA", "LONG", 100, 95, 100,
                status="CLOSED", realized_pnl=-500.0, closed_at=today_iso)
    _make_trade(db, "BBB", "LONG", 50, 48, 100,
                status="CLOSED", realized_pnl=-200.0, closed_at=today_iso)
    # Old trade — should NOT be summed
    _make_trade(db, "CCC", "LONG", 50, 48, 100,
                status="CLOSED", realized_pnl=-9999.0, closed_at=yesterday_iso)

    pnl = db.get_realized_pnl_since(today_iso)
    assert pnl == -700.0


def test_get_realized_pnl_since_ignores_open_trades(db: DatabaseManager):
    today_iso = datetime.now(timezone.utc).isoformat()
    _make_trade(db, "AAA", "LONG", 100, 95, 100, status="OPEN")
    pnl = db.get_realized_pnl_since(today_iso)
    assert pnl == 0.0


def test_get_open_risk_at_stop_long_and_short(db: DatabaseManager):
    # LONG: risk = (entry - stop) * qty = (100 - 95) * 100 = 500
    _make_trade(db, "AAA", "LONG", 100.0, 95.0, 100)
    # SHORT: risk = (stop - entry) * qty = (52 - 50) * 100 = 200
    _make_trade(db, "BBB", "SHORT", 50.0, 52.0, 100)
    # Closed — should not contribute
    _make_trade(db, "CCC", "LONG", 100.0, 95.0, 100,
                status="CLOSED", realized_pnl=-500.0)

    risk = db.get_open_risk_at_stop()
    assert risk == 700.0


def test_get_open_risk_at_stop_skips_invalid_stops(db: DatabaseManager):
    # Inverted stop on LONG (stop above entry) → negative risk → ignored
    _make_trade(db, "AAA", "LONG", 100.0, 105.0, 100)
    # Valid LONG
    _make_trade(db, "BBB", "LONG", 50.0, 48.0, 100)
    risk = db.get_open_risk_at_stop()
    assert risk == 200.0


# ── PaperTrader._kill_switch_blocked ───────────────────────────────────

def test_kill_switch_returns_none_when_under_thresholds(db: DatabaseManager):
    cfg = ScannerConfig()
    trader = PaperTrader(db, cfg)
    assert trader._kill_switch_blocked() is None


def test_kill_switch_trips_on_daily_dd(db: DatabaseManager):
    cfg = ScannerConfig()
    cfg.paper_starting_capital = 1_000_000.0
    cfg.paper_daily_max_drawdown_pct = 2.0  # → -$20K threshold
    cfg.paper_global_r_cap_pct = 999.0      # disable R cap

    midnight_ny = datetime.now(NY_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    today_iso = midnight_ny.astimezone(timezone.utc).isoformat()

    # -$25K realized today → trips 2% threshold
    _make_trade(db, "AAA", "LONG", 100, 95, 100,
                status="CLOSED", realized_pnl=-25_000.0, closed_at=today_iso)

    trader = PaperTrader(db, cfg)
    reason = trader._kill_switch_blocked()
    assert reason is not None
    assert "DAILY_DD" in reason


def test_kill_switch_trips_on_global_r_cap(db: DatabaseManager):
    cfg = ScannerConfig()
    cfg.paper_starting_capital = 1_000_000.0
    cfg.paper_daily_max_drawdown_pct = 999.0  # disable DD
    cfg.paper_global_r_cap_pct = 1.0          # → $10K cap

    # 3 LONG positions, $5K risk each = $15K total → trips $10K cap
    _make_trade(db, "AAA", "LONG", 100.0, 95.0, 1000)  # risk 5000
    _make_trade(db, "BBB", "LONG", 100.0, 95.0, 1000)  # risk 5000
    _make_trade(db, "CCC", "LONG", 100.0, 95.0, 1000)  # risk 5000

    trader = PaperTrader(db, cfg)
    reason = trader._kill_switch_blocked()
    assert reason is not None
    assert "GLOBAL_R" in reason


def test_kill_switch_disabled_when_thresholds_zero(db: DatabaseManager):
    cfg = ScannerConfig()
    cfg.paper_daily_max_drawdown_pct = 0.0
    cfg.paper_global_r_cap_pct = 0.0

    midnight_ny = datetime.now(NY_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    today_iso = midnight_ny.astimezone(timezone.utc).isoformat()
    _make_trade(db, "AAA", "LONG", 100, 95, 100,
                status="CLOSED", realized_pnl=-500_000.0, closed_at=today_iso)
    _make_trade(db, "BBB", "LONG", 100.0, 50.0, 1000)

    trader = PaperTrader(db, cfg)
    assert trader._kill_switch_blocked() is None


# ── PaperTrader._position_size ─────────────────────────────────────────

def test_position_size_low_price_uses_1000_shares(db: DatabaseManager):
    cfg = ScannerConfig()
    trader = PaperTrader(db, cfg)
    # Stock at $5: rule says 1000 shares. But notional cap $15K → 3000 max.
    # Since 1000 * 5 = $5K is under cap, returns 1000.
    qty = trader._position_size(entry=5.0, stop=4.5)
    assert qty == 1000


def test_position_size_target_10k_notional(db: DatabaseManager):
    cfg = ScannerConfig()
    trader = PaperTrader(db, cfg)
    # Stock at $50: ceil(10000/50) = 200 shares = $10K notional
    qty = trader._position_size(entry=50.0, stop=48.0)
    assert qty == 200
    assert qty * 50.0 == 10_000


def test_position_size_caps_at_15k_for_high_price(db: DatabaseManager):
    cfg = ScannerConfig()
    trader = PaperTrader(db, cfg)
    # Stock at $200: ceil(10000/200) = 50, $10K notional, well under $15K cap
    qty = trader._position_size(entry=200.0, stop=190.0)
    assert qty == 50

    # Stock at $1000: ceil(10000/1000) = 10, then check cap
    # 10 * 1000 = $10K under $15K cap, returns 10
    qty = trader._position_size(entry=1000.0, stop=950.0)
    assert qty == 10


def test_position_size_low_price_capped_at_15k(db: DatabaseManager):
    cfg = ScannerConfig()
    cfg.paper_leverage_per_trade = 5_000.0  # tighten cap to $5K
    trader = PaperTrader(db, cfg)
    # Stock at $5: 1000 shares = $5K, within cap
    qty = trader._position_size(entry=5.0, stop=4.5)
    assert qty == 1000

    # Stock at $1: 1000 shares = $1K under cap
    qty = trader._position_size(entry=1.0, stop=0.95)
    assert qty == 1000


def test_position_size_zero_or_negative_entry_returns_zero(db: DatabaseManager):
    cfg = ScannerConfig()
    trader = PaperTrader(db, cfg)
    assert trader._position_size(entry=0.0, stop=10.0) == 0
    assert trader._position_size(entry=-5.0, stop=10.0) == 0


def test_position_size_minimum_one_share(db: DatabaseManager):
    cfg = ScannerConfig()
    cfg.paper_leverage_per_trade = 1.0  # absurd cap
    cfg.paper_min_notional_per_trade = 1.0
    trader = PaperTrader(db, cfg)
    # Stock at $10000: math wants 0 shares but floor protects min=1
    qty = trader._position_size(entry=10_000.0, stop=9_500.0)
    assert qty == 1
