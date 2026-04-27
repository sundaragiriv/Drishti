"""Post-Earnings Announcement Drift (PEAD) — explicit strategy.

Academic foundation: Bernard & Thomas (1989), one of the most-replicated
anomalies in finance — stocks with positive earnings surprises continue
to drift up for 30-60 days, and vice versa. Estimated edge: ~+0.5R/trade
on the long side over 4-12 weeks.

Implementation note: we DO NOT have consensus EPS / actual EPS data, so
we use a price-based surprise proxy:
  - Identify tickers where an 8-K with has_earnings=True was filed in
    the last 5 trading days.
  - Compute the day-of-filing return (close vs prior close) as the
    "surprise magnitude."
  - LONG signal if surprise > +3% (gap-up = beat)
  - SHORT signal if surprise < -3% (gap-down = miss; only if regime allows)
  - Layer onto existing accumulation universe — only trade names already
    in EARLY/ACTIVE/LATE_ACCUM (preserves the institutional thesis).

Limits:
  - 8-K data only goes back to 2026-01-08 — backtest impossible until we
    bulk-backfill SEC EDGAR. Strategy ships forward-only and measured
    against paper trades.
  - Price-based proxy ≠ SUE. Likely captures ~70% of true PEAD signal.
  - Universe filter (accumulation phase) intentionally narrows the pool;
    expect 1-5 PEAD candidates per day at most.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional

import duckdb
from loguru import logger


@dataclass
class PEADCandidate:
    """A single PEAD-tradeable ticker."""
    ticker: str
    earnings_filed_date: date     # day the 8-K was filed
    surprise_pct: float           # day-of return (close vs prior close)
    direction: str                # "LONG" or "SHORT"
    days_since_earnings: int      # 0-5
    accum_phase: str              # EARLY/ACTIVE/LATE_ACCUM
    conviction_score: float
    close: float                  # most recent close (entry price proxy)
    atr20: float                  # 20-day ATR for stop sizing


class PEADStrategy:
    """Generate PEAD candidates from warehouse data.

    Stateless — query each cycle. Caller (IdeaBridge) is responsible
    for converting candidates to idea dicts and gating on regime,
    open positions, kill-switch, etc.
    """

    LOOKBACK_DAYS = 5                # how recent the earnings 8-K must be
    SURPRISE_LONG_THRESHOLD = 0.03   # +3% same-day return = positive surprise
    SURPRISE_SHORT_THRESHOLD = -0.03  # -3% = negative surprise
    MAX_CANDIDATES_PER_CYCLE = 5

    def __init__(self, conn: duckdb.DuckDBPyConnection,
                 active_quarter: Optional[str] = None) -> None:
        self._conn = conn
        self._active_quarter = active_quarter

    def get_candidates(self, as_of: Optional[date] = None) -> List[PEADCandidate]:
        """Return PEAD candidates ranked by surprise magnitude (descending)."""
        as_of = as_of or date.today()
        lookback = as_of - timedelta(days=self.LOOKBACK_DAYS)

        # If no active quarter passed, derive from the latest quality-75+ row.
        # Use the same accumulation gate as IdeaBridge (EARLY/ACTIVE/LATE_ACCUM).
        quarter_clause = ""
        if self._active_quarter:
            quarter_clause = f"AND s.report_quarter = '{self._active_quarter}'"
        else:
            quarter_clause = (
                "AND s.report_quarter = (SELECT MAX(report_quarter) "
                "FROM intelligence_scores WHERE data_quality_score >= 75)"
            )

        rows = self._conn.execute(f"""
            WITH recent_earnings AS (
                SELECT ticker, MAX(filed_date) AS filed_date
                FROM fact_form8k_events
                WHERE has_earnings = TRUE
                  AND filed_date BETWEEN ? AND ?
                GROUP BY ticker
            ),
            with_returns AS (
                SELECT
                    e.ticker, e.filed_date,
                    p.close,
                    LAG(p.close, 1) OVER (PARTITION BY p.ticker ORDER BY p.trade_date)
                        AS prev_close,
                    AVG(p.high - p.low) OVER (
                        PARTITION BY p.ticker ORDER BY p.trade_date
                        ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                    ) AS atr20,
                    p.trade_date
                FROM recent_earnings e
                JOIN fact_daily_prices p
                    ON p.ticker = e.ticker
                   AND p.trade_date BETWEEN e.filed_date - INTERVAL '1' DAY
                                        AND e.filed_date + INTERVAL '2' DAY
            ),
            surprise AS (
                -- Pick the row whose trade_date is the earnings filed_date
                -- (or the next trading day if filed after market close)
                SELECT ticker, filed_date,
                    FIRST(close ORDER BY trade_date) AS first_close,
                    FIRST(prev_close ORDER BY trade_date) AS first_prev_close,
                    FIRST(atr20 ORDER BY trade_date) AS atr20,
                    LAST(close ORDER BY trade_date) AS last_close
                FROM with_returns
                WHERE close IS NOT NULL
                  AND prev_close IS NOT NULL
                  AND atr20 > 0
                GROUP BY ticker, filed_date
            )
            SELECT
                sp.ticker,
                sp.filed_date,
                (sp.first_close - sp.first_prev_close) / sp.first_prev_close AS surprise_pct,
                sp.last_close,
                sp.atr20,
                s.accum_phase,
                COALESCE(s.conviction_score, 0) AS conviction_score
            FROM surprise sp
            INNER JOIN intelligence_scores s
                ON s.ticker = sp.ticker
            WHERE s.accum_phase IN ('EARLY_ACCUM', 'ACTIVE_ACCUM', 'LATE_ACCUM')
              {quarter_clause}
              AND ABS((sp.first_close - sp.first_prev_close) / sp.first_prev_close)
                  >= {self.SURPRISE_SHORT_THRESHOLD * -1}
            ORDER BY ABS((sp.first_close - sp.first_prev_close) / sp.first_prev_close) DESC
            LIMIT {self.MAX_CANDIDATES_PER_CYCLE}
        """, [lookback, as_of]).fetchall()

        candidates: List[PEADCandidate] = []
        for ticker, filed_date, surprise, close, atr20, phase, conv in rows:
            if close is None or close <= 0 or atr20 is None or atr20 <= 0:
                continue
            direction = (
                "LONG" if surprise >= self.SURPRISE_LONG_THRESHOLD
                else ("SHORT" if surprise <= self.SURPRISE_SHORT_THRESHOLD else None)
            )
            if direction is None:
                continue
            days_since = (as_of - filed_date).days
            if days_since < 0 or days_since > self.LOOKBACK_DAYS:
                continue
            candidates.append(PEADCandidate(
                ticker=ticker,
                earnings_filed_date=filed_date,
                surprise_pct=float(surprise),
                direction=direction,
                days_since_earnings=days_since,
                accum_phase=phase,
                conviction_score=float(conv),
                close=float(close),
                atr20=float(atr20),
            ))

        if candidates:
            logger.info(
                "PEAD: {} candidate(s) — {}",
                len(candidates),
                ", ".join(f"{c.ticker}({c.direction}, {c.surprise_pct*100:+.1f}%)"
                          for c in candidates),
            )
        return candidates

    @staticmethod
    def to_idea_dict(c: PEADCandidate, target_r: float, stretch_r: float) -> dict:
        """Convert a PEADCandidate to an IdeaBridge-compatible idea dict.

        Uses 2*ATR stops, target_r * (2*ATR) target — same frame as
        Triple Lock. target_r is the primary (1R default per af93337).
        """
        risk = 2.0 * c.atr20
        if c.direction == "LONG":
            stop = round(c.close - risk, 2)
            target_1 = round(c.close + risk * target_r, 2)
            target_2 = round(c.close + risk * stretch_r, 2)
        else:  # SHORT
            stop = round(c.close + risk, 2)
            target_1 = round(c.close - risk * target_r, 2)
            target_2 = round(c.close - risk * stretch_r, 2)
        return {
            "symbol": c.ticker,
            "side": c.direction,
            "entry_price": round(c.close, 2),
            "stop_loss": stop,
            "target_1": target_1,
            "target_2": target_2,
            "source": f"PEAD_DRIFT_{c.direction}",
            "conviction": c.conviction_score,
            "accum_phase": c.accum_phase,
            "score": c.conviction_score,
            "rr_ratio": target_r,
            "instrument_type": "STOCK",
            # PEAD-specific metadata for diagnostics
            "pead_surprise_pct": round(c.surprise_pct * 100, 2),
            "pead_days_since_earnings": c.days_since_earnings,
            "pead_filed_date": c.earnings_filed_date.isoformat(),
        }
