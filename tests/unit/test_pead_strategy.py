"""Unit tests for the PEAD (Post-Earnings Announcement Drift) strategy."""

from datetime import date, datetime, timedelta

import duckdb
import pytest

from signal_scanner.intelligence.pead_strategy import PEADCandidate, PEADStrategy


@pytest.fixture
def wh() -> duckdb.DuckDBPyConnection:
    """In-memory warehouse with the tables PEADStrategy reads."""
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE fact_form8k_events (
            filing_accession_no VARCHAR,
            filer_cik VARCHAR,
            company_name VARCHAR,
            ticker VARCHAR,
            filed_date DATE,
            report_date DATE,
            event_items VARCHAR,
            has_earnings BOOLEAN,
            has_acquisition BOOLEAN,
            has_officer_change BOOLEAN,
            has_cyber_incident BOOLEAN,
            source_url VARCHAR,
            ingested_at TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE fact_daily_prices (
            ticker VARCHAR,
            trade_date DATE,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume BIGINT
        )
    """)
    conn.execute("""
        CREATE TABLE intelligence_scores (
            ticker VARCHAR,
            report_quarter VARCHAR,
            accum_phase VARCHAR,
            conviction_score DOUBLE,
            data_quality_score DOUBLE
        )
    """)
    yield conn
    conn.close()


def _seed_prices(conn, ticker: str, dates_and_closes: list, atr_pct: float = 0.02):
    """Helper: seed fact_daily_prices with deterministic OHLC."""
    for d, close in dates_and_closes:
        # Manufacture high/low to give a consistent ATR
        atr = close * atr_pct
        high = close + atr / 2
        low = close - atr / 2
        conn.execute(
            "INSERT INTO fact_daily_prices VALUES (?,?,?,?,?,?,?)",
            (ticker, d, close, high, low, close, 1_000_000),
        )


def _seed_intel(conn, ticker: str, phase: str = "ACTIVE_ACCUM",
                quarter: str = "2025-Q4", conviction: float = 70.0):
    conn.execute(
        "INSERT INTO intelligence_scores VALUES (?,?,?,?,?)",
        (ticker, quarter, phase, conviction, 80.0),
    )


def _seed_earnings(conn, ticker: str, filed_date: date):
    conn.execute(
        "INSERT INTO fact_form8k_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"acc-{ticker}-{filed_date}", "cik", ticker, ticker,
         filed_date, filed_date, "2.02",
         True, False, False, False, "url", datetime.utcnow()),
    )


# ---------- Candidate generation ----------

def test_no_earnings_no_candidates(wh):
    _seed_intel(wh, "AAPL")
    _seed_prices(wh, "AAPL", [(date(2026, 4, 25), 200.0), (date(2026, 4, 26), 200.0)])
    strat = PEADStrategy(wh, active_quarter="2025-Q4")
    assert strat.get_candidates(as_of=date(2026, 4, 26)) == []


def test_positive_surprise_long_candidate(wh):
    # Earnings filed Apr 25; on Apr 25 stock jumped +5% from Apr 24
    _seed_intel(wh, "BEAT")
    _seed_prices(wh, "BEAT", [
        (date(2026, 4, 24), 100.0),
        (date(2026, 4, 25), 105.0),  # +5% gap
        (date(2026, 4, 26), 106.0),
    ])
    # Need 19 prior days for ATR window — seed flat history
    for offset in range(20, 1, -1):
        d = date(2026, 4, 24) - timedelta(days=offset)
        _seed_prices(wh, "BEAT", [(d, 100.0)])
    _seed_earnings(wh, "BEAT", date(2026, 4, 25))
    strat = PEADStrategy(wh, active_quarter="2025-Q4")
    cands = strat.get_candidates(as_of=date(2026, 4, 26))
    assert len(cands) == 1
    c = cands[0]
    assert c.ticker == "BEAT"
    assert c.direction == "LONG"
    assert c.surprise_pct >= 0.03


def test_negative_surprise_short_candidate(wh):
    _seed_intel(wh, "MISS")
    _seed_prices(wh, "MISS", [
        (date(2026, 4, 24), 100.0),
        (date(2026, 4, 25), 94.0),  # -6% gap
    ])
    for offset in range(20, 1, -1):
        d = date(2026, 4, 24) - timedelta(days=offset)
        _seed_prices(wh, "MISS", [(d, 100.0)])
    _seed_earnings(wh, "MISS", date(2026, 4, 25))
    strat = PEADStrategy(wh, active_quarter="2025-Q4")
    cands = strat.get_candidates(as_of=date(2026, 4, 26))
    assert len(cands) == 1
    assert cands[0].direction == "SHORT"


def test_small_surprise_filtered_out(wh):
    # +1% is below the +3% LONG threshold
    _seed_intel(wh, "MEH")
    _seed_prices(wh, "MEH", [
        (date(2026, 4, 24), 100.0),
        (date(2026, 4, 25), 101.0),
    ])
    for offset in range(20, 1, -1):
        d = date(2026, 4, 24) - timedelta(days=offset)
        _seed_prices(wh, "MEH", [(d, 100.0)])
    _seed_earnings(wh, "MEH", date(2026, 4, 25))
    strat = PEADStrategy(wh, active_quarter="2025-Q4")
    assert strat.get_candidates(as_of=date(2026, 4, 26)) == []


def test_earnings_outside_lookback_filtered_out(wh):
    # Earnings filed 10 days ago (lookback = 5 days)
    _seed_intel(wh, "OLD")
    _seed_prices(wh, "OLD", [
        (date(2026, 4, 15), 100.0),
        (date(2026, 4, 16), 105.0),
    ])
    for offset in range(20, 1, -1):
        d = date(2026, 4, 15) - timedelta(days=offset)
        _seed_prices(wh, "OLD", [(d, 100.0)])
    _seed_earnings(wh, "OLD", date(2026, 4, 16))
    strat = PEADStrategy(wh, active_quarter="2025-Q4")
    assert strat.get_candidates(as_of=date(2026, 4, 26)) == []


def test_non_accumulation_phase_excluded(wh):
    _seed_intel(wh, "DIST", phase="DISTRIBUTION")
    _seed_prices(wh, "DIST", [
        (date(2026, 4, 24), 100.0),
        (date(2026, 4, 25), 110.0),
    ])
    for offset in range(20, 1, -1):
        d = date(2026, 4, 24) - timedelta(days=offset)
        _seed_prices(wh, "DIST", [(d, 100.0)])
    _seed_earnings(wh, "DIST", date(2026, 4, 25))
    strat = PEADStrategy(wh, active_quarter="2025-Q4")
    assert strat.get_candidates(as_of=date(2026, 4, 26)) == []


# ---------- to_idea_dict ----------

def test_to_idea_dict_long_at_1R():
    c = PEADCandidate(
        ticker="LONG",
        earnings_filed_date=date(2026, 4, 25),
        surprise_pct=0.05,
        direction="LONG",
        days_since_earnings=1,
        accum_phase="ACTIVE_ACCUM",
        conviction_score=72.0,
        close=100.0,
        atr20=2.0,
    )
    idea = PEADStrategy.to_idea_dict(c, target_r=1.0, stretch_r=1.5)
    # 1R = 2*ATR = 4
    assert idea["entry_price"] == 100.0
    assert idea["stop_loss"] == 96.0       # 100 - 4
    assert idea["target_1"] == 104.0       # 100 + 4 * 1.0
    assert idea["target_2"] == 106.0       # 100 + 4 * 1.5
    assert idea["source"] == "PEAD_DRIFT_LONG"
    assert idea["pead_surprise_pct"] == 5.0
    assert idea["pead_filed_date"] == "2026-04-25"


def test_to_idea_dict_short_inverts_levels():
    c = PEADCandidate(
        ticker="SHORT",
        earnings_filed_date=date(2026, 4, 25),
        surprise_pct=-0.06,
        direction="SHORT",
        days_since_earnings=0,
        accum_phase="LATE_ACCUM",
        conviction_score=70.0,
        close=100.0,
        atr20=2.0,
    )
    idea = PEADStrategy.to_idea_dict(c, target_r=1.0, stretch_r=1.5)
    assert idea["stop_loss"] == 104.0     # above for SHORT
    assert idea["target_1"] == 96.0       # below for SHORT
    assert idea["source"] == "PEAD_DRIFT_SHORT"
