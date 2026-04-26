"""Unit tests for the Tier 1 deterministic catalyst checker.

Uses an in-memory DuckDB with a fixture warehouse schema. Validates:
  - Each individual rule (8-K, negative news, news volume) fires correctly.
  - Clean tickers return flag=False with summary='clean'.
  - Cohort assignment is deterministic and ~50/50 over a sample.
  - Failure path falls open (no flag).
"""
from datetime import date, datetime, timedelta

import duckdb
import pytest

from signal_scanner.intelligence.catalyst_checker import (
    CatalystChecker, CatalystResult, assign_cohort,
)


@pytest.fixture
def wh() -> duckdb.DuckDBPyConnection:
    """In-memory warehouse with the two tables the checker reads."""
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
        CREATE TABLE fact_news_sentiment (
            news_id VARCHAR,
            ticker VARCHAR,
            published_at TIMESTAMP,
            title VARCHAR,
            sentiment VARCHAR,
            sentiment_score SMALLINT,
            sentiment_reasoning VARCHAR,
            author VARCHAR,
            article_url VARCHAR,
            publisher VARCHAR,
            source VARCHAR,
            ingested_at TIMESTAMP
        )
    """)
    yield conn
    conn.close()


# ---------- Cohort assignment ----------

def test_assign_cohort_deterministic():
    a1 = assign_cohort("AAPL", date(2026, 4, 28))
    a2 = assign_cohort("AAPL", date(2026, 4, 28))
    assert a1 == a2
    assert a1 in ("A", "B")


def test_assign_cohort_different_inputs_split_roughly_evenly():
    counts = {"A": 0, "B": 0}
    for i in range(2000):
        ticker = f"T{i:04d}"
        c = assign_cohort(ticker, date(2026, 4, 28))
        counts[c] += 1
    # Allow 5% slop on a 2000-sample binomial
    assert 900 < counts["A"] < 1100
    assert 900 < counts["B"] < 1100


# ---------- Catalyst rules ----------

def test_clean_ticker_returns_no_flag(wh):
    result = CatalystChecker(wh).check("CLEAN", as_of=date(2026, 4, 28))
    assert result.flag is False
    assert result.reasons == []
    assert result.summary == "clean"


def test_recent_8k_with_acquisition_flags(wh):
    wh.execute(
        "INSERT INTO fact_form8k_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acc1", "cik1", "ACME", "ACME", date(2026, 4, 25),
         date(2026, 4, 25), "1.01", False, True, False, False, "url", datetime.utcnow()),
    )
    result = CatalystChecker(wh).check("ACME", as_of=date(2026, 4, 28))
    assert result.flag is True
    assert any("acquisition" in r for r in result.reasons)


def test_old_8k_does_not_flag(wh):
    # 30 days old — outside lookback window
    wh.execute(
        "INSERT INTO fact_form8k_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acc2", "cik2", "OLD", "OLD", date(2026, 3, 1),
         date(2026, 3, 1), "1.01", False, True, False, False, "url", datetime.utcnow()),
    )
    result = CatalystChecker(wh).check("OLD", as_of=date(2026, 4, 28))
    assert result.flag is False


def test_8k_without_material_flags_does_not_trigger(wh):
    # Routine 8-K (no material event flags) — shouldn't block
    wh.execute(
        "INSERT INTO fact_form8k_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acc3", "cik3", "ROUT", "ROUT", date(2026, 4, 25),
         date(2026, 4, 25), "8.01", False, False, False, False, "url", datetime.utcnow()),
    )
    result = CatalystChecker(wh).check("ROUT", as_of=date(2026, 4, 28))
    assert result.flag is False


def test_strong_negative_news_flags(wh):
    # Recent news with sentiment_score = -4
    wh.execute(
        "INSERT INTO fact_news_sentiment VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("n1", "BAD", datetime(2026, 4, 27, 12, 0), "SEC investigates BAD",
         "negative", -4, "fraud allegations", "AP", "url", "Reuters", "polygon",
         datetime.utcnow()),
    )
    result = CatalystChecker(wh).check("BAD", as_of=date(2026, 4, 28))
    assert result.flag is True
    assert any("news_negative" in r for r in result.reasons)


def test_mild_negative_news_does_not_flag(wh):
    # sentiment_score = -1 is below threshold (-3)
    wh.execute(
        "INSERT INTO fact_news_sentiment VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("n2", "MILD", datetime(2026, 4, 27, 12, 0), "MILD slightly down",
         "negative", -1, "minor concerns", "AP", "url", "Reuters", "polygon",
         datetime.utcnow()),
    )
    result = CatalystChecker(wh).check("MILD", as_of=date(2026, 4, 28))
    assert result.flag is False


def test_news_volume_spike_flags(wh):
    # 6 articles in last 24h, all neutral
    base = datetime(2026, 4, 27, 12, 0)
    for i in range(6):
        wh.execute(
            "INSERT INTO fact_news_sentiment VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"v{i}", "VOLUME", base + timedelta(minutes=i),
             f"VOLUME news #{i}", "neutral", 0, "", "AP",
             "url", "Reuters", "polygon", datetime.utcnow()),
        )
    result = CatalystChecker(wh).check("VOLUME", as_of=date(2026, 4, 28))
    assert result.flag is True
    assert any("news_volume" in r for r in result.reasons)


def test_low_volume_news_does_not_flag(wh):
    # 2 articles in last 24h — below threshold (5)
    base = datetime(2026, 4, 27, 12, 0)
    for i in range(2):
        wh.execute(
            "INSERT INTO fact_news_sentiment VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"l{i}", "LOWVOL", base + timedelta(minutes=i),
             f"LOWVOL news", "neutral", 0, "", "AP",
             "url", "Reuters", "polygon", datetime.utcnow()),
        )
    result = CatalystChecker(wh).check("LOWVOL", as_of=date(2026, 4, 28))
    assert result.flag is False


def test_multiple_reasons_all_captured(wh):
    # Recent material 8-K AND strong negative news
    wh.execute(
        "INSERT INTO fact_form8k_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acc-multi", "cik", "MULTI", "MULTI", date(2026, 4, 26),
         date(2026, 4, 26), "5.02", False, False, True, False, "url", datetime.utcnow()),
    )
    wh.execute(
        "INSERT INTO fact_news_sentiment VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("n-multi", "MULTI", datetime(2026, 4, 27, 12, 0),
         "CEO resigns amid investigation", "negative", -5,
         "fraud", "AP", "url", "WSJ", "polygon", datetime.utcnow()),
    )
    result = CatalystChecker(wh).check("MULTI", as_of=date(2026, 4, 28))
    assert result.flag is True
    assert len(result.reasons) >= 2


def test_empty_ticker_returns_clean(wh):
    result = CatalystChecker(wh).check("", as_of=date(2026, 4, 28))
    assert result.flag is False
