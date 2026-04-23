"""Watchlist loading and sector mapping."""

import re
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from signal_scanner.config import WATCHLIST_DIR

# Sector mapping for major index constituents
SECTOR_MAP: Dict[str, str] = {
    # Technology
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "AVGO": "Technology", "ORCL": "Technology", "CRM": "Technology",
    "CSCO": "Technology", "ADBE": "Technology", "ACN": "Technology",
    "IBM": "Technology", "TXN": "Technology", "QCOM": "Technology",
    "INTU": "Technology", "AMD": "Technology", "AMAT": "Technology",
    "ADI": "Technology", "LRCX": "Technology", "MU": "Technology",
    "KLAC": "Technology", "SNPS": "Technology", "CDNS": "Technology",
    "MCHP": "Technology", "FTNT": "Technology", "PANW": "Technology",
    "CRWD": "Technology", "NOW": "Technology", "PLTR": "Technology",
    # Communication Services
    "GOOGL": "Communication Services", "GOOG": "Communication Services",
    "META": "Communication Services", "NFLX": "Communication Services",
    "DIS": "Communication Services", "CMCSA": "Communication Services",
    "TMUS": "Communication Services", "VZ": "Communication Services",
    "T": "Communication Services",
    # Consumer Discretionary
    "AMZN": "Consumer Discretionary", "TSLA": "Consumer Discretionary",
    "HD": "Consumer Discretionary", "MCD": "Consumer Discretionary",
    "NKE": "Consumer Discretionary", "LOW": "Consumer Discretionary",
    "SBUX": "Consumer Discretionary", "TJX": "Consumer Discretionary",
    "BKNG": "Consumer Discretionary", "CMG": "Consumer Discretionary",
    # Consumer Staples
    "WMT": "Consumer Staples", "PG": "Consumer Staples",
    "COST": "Consumer Staples", "KO": "Consumer Staples",
    "PEP": "Consumer Staples", "PM": "Consumer Staples",
    "CL": "Consumer Staples", "MDLZ": "Consumer Staples",
    # Financials
    "JPM": "Financials", "V": "Financials", "MA": "Financials",
    "BAC": "Financials", "WFC": "Financials", "GS": "Financials",
    "MS": "Financials", "BLK": "Financials", "C": "Financials",
    "AXP": "Financials", "SCHW": "Financials", "CB": "Financials",
    "BRK.B": "Financials", "SPGI": "Financials", "MCO": "Financials",
    "CME": "Financials", "ICE": "Financials", "AON": "Financials",
    # Healthcare
    "UNH": "Healthcare", "JNJ": "Healthcare", "LLY": "Healthcare",
    "ABBV": "Healthcare", "MRK": "Healthcare", "PFE": "Healthcare",
    "TMO": "Healthcare", "ABT": "Healthcare", "DHR": "Healthcare",
    "AMGN": "Healthcare", "BMY": "Healthcare", "MDT": "Healthcare",
    "ISRG": "Healthcare", "GILD": "Healthcare", "VRTX": "Healthcare",
    "SYK": "Healthcare", "BSX": "Healthcare", "REGN": "Healthcare",
    # Industrials
    "GE": "Industrials", "CAT": "Industrials", "RTX": "Industrials",
    "HON": "Industrials", "UNP": "Industrials", "BA": "Industrials",
    "DE": "Industrials", "LMT": "Industrials", "UPS": "Industrials",
    "ADP": "Industrials", "MMM": "Industrials", "GD": "Industrials",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
    "SLB": "Energy", "EOG": "Energy", "MPC": "Energy",
    "PSX": "Energy", "VLO": "Energy", "OXY": "Energy",
    # Utilities
    "NEE": "Utilities", "SO": "Utilities", "DUK": "Utilities",
    "D": "Utilities", "AEP": "Utilities", "SRE": "Utilities",
    # Real Estate
    "PLD": "Real Estate", "AMT": "Real Estate", "CCI": "Real Estate",
    "EQIX": "Real Estate", "SPG": "Real Estate", "O": "Real Estate",
    # Materials
    "LIN": "Materials", "APD": "Materials", "SHW": "Materials",
    "FCX": "Materials", "NEM": "Materials", "ECL": "Materials",
    # ETFs
    "SPY": "ETF", "QQQ": "ETF", "IWM": "ETF", "DIA": "ETF",
}

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")

WATCHLIST_ALIASES: Dict[str, str] = {
    "russel2000": "russell2000",
    "russel": "russell2000",
    "russell": "russell2000",
    "rut": "russell2000",
    "rty": "russell2000",
    "nasdaq": "nasdaq100",
    "nasdaq-100": "nasdaq100",
    "ndx": "nasdaq100",
    "spx": "sp500",
    "s&p500": "sp500",
    "snp500": "sp500",
}


class WatchlistManager:
    """Loads and validates symbol watchlists from text files."""

    def __init__(self, watchlist_dir: Optional[Path] = None) -> None:
        self._dir = watchlist_dir or WATCHLIST_DIR

    def get_available_watchlists(self) -> List[str]:
        """Return names of all .txt watchlist files (without extension)."""
        if not self._dir.exists():
            logger.warning(f"Watchlist directory not found: {self._dir}")
            return []
        return sorted(p.stem for p in self._dir.glob("*.txt"))

    @staticmethod
    def _normalize_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())

    def resolve_watchlist_name(self, name: str) -> str:
        """Resolve user-entered watchlist name to a known watchlist file stem."""
        if not name:
            return ""

        available = self.get_available_watchlists()
        available_set = set(available)

        cleaned = (name or "").strip().lower()
        if cleaned in available_set:
            return cleaned

        normalized = self._normalize_key(cleaned)

        # Alias-based resolution (handles common names and typos, e.g. Russel2000).
        alias_target = WATCHLIST_ALIASES.get(normalized)
        if alias_target and alias_target in available_set:
            return alias_target

        # Fallback to normalized exact match against available names.
        normalized_map = {self._normalize_key(v): v for v in available}
        return normalized_map.get(normalized, cleaned)

    def load_watchlist(self, name: str) -> List[str]:
        """Load symbols from a watchlist file.

        Args:
            name: Watchlist name without .txt extension.

        Returns:
            List of validated uppercase ticker symbols.
        """
        resolved_name = self.resolve_watchlist_name(name)
        path = self._dir / f"{resolved_name}.txt"
        if not path.exists():
            logger.error(f"Watchlist file not found: {path}")
            return []

        symbols: List[str] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            symbol = line.upper()
            if _SYMBOL_RE.match(symbol):
                symbols.append(symbol)
            else:
                logger.warning(f"Skipping invalid symbol '{line}' in {resolved_name}.txt")

        logger.info(f"Loaded {len(symbols)} symbols from '{resolved_name}' watchlist")
        return symbols

    def get_all_symbols(self, max_symbols: int = 400) -> List[str]:
        """Return unique symbols across all watchlists (for quick symbol search UX)."""
        all_symbols = set()
        for wl in self.get_available_watchlists():
            for s in self.load_watchlist(wl):
                all_symbols.add(s)
                if len(all_symbols) >= max_symbols:
                    break
            if len(all_symbols) >= max_symbols:
                break
        return sorted(all_symbols)

    @staticmethod
    def get_sector(symbol: str) -> str:
        """Look up the sector for a symbol."""
        return SECTOR_MAP.get(symbol, "Unknown")

    @staticmethod
    def get_unique_sectors(symbols: List[str]) -> List[str]:
        """Return sorted unique sectors for a list of symbols."""
        sectors = {SECTOR_MAP.get(s, "Unknown") for s in symbols}
        return sorted(sectors)
