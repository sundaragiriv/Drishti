"""Deterministic catalyst check for Triple Lock / Swing Idea entries.

Tier 1 of the catalyst-check experiment (2026-04-25). Pure DuckDB
queries against existing warehouse data — no LLM, no API, $0 cost.

A catalyst is a near-term event that may produce a binary price move
unrelated to the institutional-accumulation thesis. We don't want to
enter a 1R-target swing the day before earnings or after a CEO
resignation. The check answers a single question:

  "Is there a known disruptive event in the recent past or near
   future for this ticker?"

For Tier 1 we use three signals already in the warehouse:

  1. RECENT 8-K filing (last 5 calendar days) with material flags —
     earnings, acquisition, officer change, cyber incident.
  2. STRONG NEGATIVE NEWS in last 48 hours (sentiment_score <= -3).
  3. NEWS VOLUME SPIKE (>= 5 articles in last 24 hours) — even if
     sentiment is mixed, attention surges often precede binary moves.

What Tier 1 does NOT cover:
  - Forward-looking earnings calendar (we have post-earnings 8-K but
    not "earnings scheduled in 5 days"). Polygon Stocks Starter
    includes /vX/reference/tickers/{ticker} → next_earnings_date.
    Wire that in Tier 2 if Tier 1 doesn't show edge.
  - FDA event calendar, analyst day, conference dates. Tier 2/3.
  - Sector-wide catalysts (Fed meetings, jobs reports). Out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import List, Optional

import duckdb
from loguru import logger


@dataclass
class CatalystResult:
    """Result of a single ticker catalyst check."""
    ticker: str
    as_of: date
    flag: bool                           # True = block this entry
    reasons: List[str] = field(default_factory=list)
    summary: str = ""

    def __str__(self) -> str:
        return f"CatalystResult({self.ticker} flag={self.flag} reasons={self.reasons})"


class CatalystChecker:
    """Pure-SQL catalyst detection from warehouse tables.

    Stateless — query each call. Negligible cost (each check is
    a couple of indexed reads against `fact_form8k_events` and
    `fact_news_sentiment`).
    """

    # Tunables — kept conservative for first run; can tighten later
    EIGHTK_LOOKBACK_DAYS = 5
    NEWS_NEGATIVE_LOOKBACK_HOURS = 48
    NEWS_NEGATIVE_THRESHOLD = -3        # sentiment_score range -5..+5
    NEWS_VOLUME_LOOKBACK_HOURS = 24
    NEWS_VOLUME_THRESHOLD = 5

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn

    def check(self, ticker: str, as_of: Optional[date] = None) -> CatalystResult:
        """Return a CatalystResult. flag=True means a catalyst was detected
        and the caller should consider blocking the entry."""
        ticker = (ticker or "").upper().strip()
        as_of = as_of or datetime.utcnow().date()
        result = CatalystResult(ticker=ticker, as_of=as_of, flag=False)

        if not ticker:
            return result

        try:
            self._check_recent_8k(ticker, as_of, result)
            self._check_negative_news(ticker, as_of, result)
            self._check_news_volume_spike(ticker, as_of, result)
        except Exception as e:
            # Fail-open: if catalyst check breaks, do NOT block the trade.
            # Better to take a noisy trade than to silently block the system.
            logger.warning("CatalystChecker error on {}: {}", ticker, e)
            result.summary = f"check_error:{type(e).__name__}"
            result.flag = False
            return result

        result.flag = bool(result.reasons)
        result.summary = "; ".join(result.reasons) if result.reasons else "clean"
        return result

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_recent_8k(self, ticker: str, as_of: date, result: CatalystResult) -> None:
        """Material 8-K filed in last N days."""
        lookback = as_of - timedelta(days=self.EIGHTK_LOOKBACK_DAYS)
        rows = self._conn.execute(
            """
            SELECT filed_date, event_items, has_earnings, has_acquisition,
                   has_officer_change, has_cyber_incident
            FROM fact_form8k_events
            WHERE ticker = ?
              AND filed_date >= ?
              AND filed_date <= ?
            ORDER BY filed_date DESC
            LIMIT 5
            """,
            [ticker, lookback, as_of],
        ).fetchall()

        for filed_date, items, earnings, acq, officer, cyber in rows:
            tags = []
            if earnings:
                tags.append("earnings")
            if acq:
                tags.append("acquisition")
            if officer:
                tags.append("officer_change")
            if cyber:
                tags.append("cyber_incident")
            if tags:
                result.reasons.append(
                    f"8k_{filed_date.isoformat()}_{'-'.join(tags)}"
                )

    def _check_negative_news(self, ticker: str, as_of: date, result: CatalystResult) -> None:
        """Strong negative news sentiment in lookback window."""
        cutoff = datetime.combine(as_of, datetime.min.time()) - timedelta(
            hours=self.NEWS_NEGATIVE_LOOKBACK_HOURS
        )
        row = self._conn.execute(
            """
            SELECT COUNT(*), MIN(sentiment_score), MIN(title)
            FROM fact_news_sentiment
            WHERE ticker = ?
              AND published_at >= ?
              AND sentiment_score <= ?
            """,
            [ticker, cutoff, self.NEWS_NEGATIVE_THRESHOLD],
        ).fetchone()

        n, min_score, title = row[0] or 0, row[1], row[2]
        if n > 0:
            short_title = (title or "")[:60].replace(";", ",")
            result.reasons.append(
                f"news_negative_n{n}_min{min_score}:{short_title}"
            )

    def _check_news_volume_spike(self, ticker: str, as_of: date, result: CatalystResult) -> None:
        """Surge in news article count regardless of sentiment."""
        cutoff = datetime.combine(as_of, datetime.min.time()) - timedelta(
            hours=self.NEWS_VOLUME_LOOKBACK_HOURS
        )
        row = self._conn.execute(
            """
            SELECT COUNT(*)
            FROM fact_news_sentiment
            WHERE ticker = ?
              AND published_at >= ?
            """,
            [ticker, cutoff],
        ).fetchone()
        n = row[0] or 0
        if n >= self.NEWS_VOLUME_THRESHOLD:
            result.reasons.append(f"news_volume_n{n}_24h")


# ----------------------------------------------------------------------
# Cohort assignment — deterministic A/B
# ----------------------------------------------------------------------

import hashlib


def assign_cohort(ticker: str, as_of: date, salt: str = "catalyst-v1") -> str:
    """Deterministic A/B cohort. Same (ticker, date) always lands in
    the same cohort across reruns. Splits ~50/50.

    Cohort A = control (no catalyst check).
    Cohort B = treatment (block on catalyst flag).
    """
    key = f"{salt}|{ticker.upper()}|{as_of.isoformat()}".encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()
    # First hex char % 2 → 0 = A, 1 = B (sha256 uniform → 50/50 split)
    return "A" if int(digest[0], 16) % 2 == 0 else "B"
