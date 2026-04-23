"""Earnings calendar — checks proximity to earnings dates for signal demotion.

Uses IBKR fundamental data to discover next earnings date per symbol.
Falls back gracefully if data is unavailable (never blocks scanning).
"""

import re
import time
from datetime import date, datetime, timedelta
from typing import Dict, Optional, Tuple

from loguru import logger


class EarningsCalendar:
    """In-memory earnings date cache with IBKR fundamental data refresh."""

    def __init__(self, buffer_days: int = 3, cache_ttl_hours: int = 12) -> None:
        self._buffer_days = buffer_days
        self._cache_ttl_s = cache_ttl_hours * 3600
        # {symbol: (earnings_date, fetched_timestamp)}
        self._cache: Dict[str, Tuple[Optional[date], float]] = {}

    def check_earnings_proximity(
        self,
        symbol: str,
        connector=None,
    ) -> Tuple[bool, Optional[int]]:
        """Check if a symbol is within buffer_days of an earnings date.

        Returns:
            (is_near_earnings, days_until_earnings)
            If data is unavailable, returns (False, None) — safe default.
        """
        earnings_date = self._get_cached_date(symbol)

        # Try IBKR refresh if stale/missing and connector available
        if earnings_date is None and connector is not None:
            earnings_date = self._fetch_from_ibkr(symbol, connector)

        if earnings_date is None:
            return False, None

        today = date.today()
        delta = (earnings_date - today).days

        # Check both pre and post earnings window
        if -1 <= delta <= self._buffer_days:
            return True, max(delta, 0)
        return False, delta if delta >= 0 else None

    def _get_cached_date(self, symbol: str) -> Optional[date]:
        """Return cached earnings date if still fresh."""
        entry = self._cache.get(symbol)
        if entry is None:
            return None
        earnings_date, fetched_at = entry
        if time.time() - fetched_at > self._cache_ttl_s:
            return None  # Stale
        return earnings_date

    def _fetch_from_ibkr(self, symbol: str, connector) -> Optional[date]:
        """Try to extract next earnings date from IBKR fundamental data."""
        try:
            ib = getattr(connector, "_ib", None)
            if ib is None or not connector.is_connected():
                return None

            from ib_insync import Stock

            contract = Stock(symbol, "SMART", "USD")
            ib.qualifyContracts(contract)

            xml_data = ib.reqFundamentalData(contract, "ReportSnapshot")
            if not xml_data:
                self._cache[symbol] = (None, time.time())
                return None

            earnings_date = self._parse_earnings_date(xml_data)
            self._cache[symbol] = (earnings_date, time.time())
            return earnings_date

        except Exception as e:
            logger.debug(f"Earnings date fetch failed for {symbol}: {e}")
            self._cache[symbol] = (None, time.time())
            return None

    @staticmethod
    def _parse_earnings_date(xml_data: str) -> Optional[date]:
        """Extract next earnings date from IBKR ReportSnapshot XML."""
        # IBKR returns earnings date in <FiscalPeriod> or <NextEarningsDate> tags
        patterns = [
            r"<NextEarningsDate[^>]*>(\d{4}-\d{2}-\d{2})</NextEarningsDate>",
            r"<nextEarningsDate>(\d{4}-\d{2}-\d{2})</nextEarningsDate>",
            r"earningsDate=\"(\d{4}-\d{2}-\d{2})\"",
        ]
        for pattern in patterns:
            match = re.search(pattern, xml_data, re.IGNORECASE)
            if match:
                try:
                    return datetime.strptime(match.group(1), "%Y-%m-%d").date()
                except ValueError:
                    continue
        return None

    def set_earnings_date(self, symbol: str, earnings_date: date) -> None:
        """Manually set an earnings date (useful for batch loading)."""
        self._cache[symbol] = (earnings_date, time.time())

    def bulk_load(self, dates: Dict[str, date]) -> None:
        """Bulk load earnings dates from external source."""
        now = time.time()
        for symbol, dt in dates.items():
            self._cache[symbol] = (dt, now)
        logger.info(f"Loaded {len(dates)} earnings dates into calendar")
