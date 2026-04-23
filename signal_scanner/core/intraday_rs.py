"""Intraday Relative Strength — stock vs sector ETF.

Computes real-time relative strength by comparing a stock's
first-30-minute return against its sector ETF's return.

Positive RS = stock outperforming sector (alpha move)
Negative RS = stock just riding sector beta

Used as:
  - Intraday ML / Snipers filter gate
  - Why-No-Trade diagnostic context
  - ISR intraday context

Usage:
    rs = compute_intraday_rs(bar_store, "AAPL", "XLK")
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
from loguru import logger


# Sector ETF mapping (GICS-based)
SECTOR_ETF_MAP = {
    # Technology
    "ELECTRONIC COMPUTERS": "XLK",
    "SEMICONDUCTORS": "XLK",
    "COMPUTER PROGRAMMING": "XLK",
    "COMPUTER PROCESSING": "XLK",
    "SERVICES-PREPACKAGED SOFTWARE": "XLK",
    "SERVICES-COMPUTER PROGRAMMING, DATA PROCESSING": "XLK",
    # Financials
    "NATIONAL COMMERCIAL BANKS": "XLF",
    "STATE CHARTERED BANKS": "XLF",
    "FIRE, MARINE & CASUALTY INSURANCE": "XLF",
    "INVESTMENT ADVICE": "XLF",
    "SECURITY BROKERS, DEALERS & FLOTATION COMPANIES": "XLF",
    # Energy
    "CRUDE PETROLEUM & NATURAL GAS": "XLE",
    "PETROLEUM REFINING": "XLE",
    "OIL AND GAS FIELD SERVICES": "XLE",
    # Healthcare
    "PHARMACEUTICAL PREPARATIONS": "XLV",
    "SURGICAL & MEDICAL INSTRUMENTS": "XLV",
    "BIOLOGICAL PRODUCTS": "XLV",
    "HOSPITAL & MEDICAL SERVICE PLANS": "XLV",
    # Industrials
    "GUIDED MISSILES & SPACE VEHICLES": "XLI",
    "GENERAL INDUSTRIAL MACHINERY": "XLI",
    "ELECTRONIC & OTHER ELECTRICAL EQUIPMENT": "XLI",
    # Consumer Discretionary
    "RETAIL-EATING PLACES": "XLY",
    "RETAIL-BUILDING MATERIALS": "XLY",
    "MOTOR VEHICLES & PASSENGER CAR BODIES": "XLY",
    # Consumer Staples
    "BEVERAGES": "XLP",
    "FOOD AND KINDRED PRODUCTS": "XLP",
    # Utilities
    "ELECTRIC SERVICES": "XLU",
    # Real Estate
    "REAL ESTATE INVESTMENT TRUSTS": "XLRE",
    # Communication
    "CABLE & OTHER PAY TELEVISION SERVICES": "XLC",
    "TELEPHONE COMMUNICATIONS": "XLC",
}

# Default fallback
DEFAULT_ETF = "SPY"


def get_sector_etf(sector: str) -> str:
    """Map a sector name to its SPDR sector ETF."""
    if not sector:
        return DEFAULT_ETF
    sector_upper = sector.upper()
    for key, etf in SECTOR_ETF_MAP.items():
        if key in sector_upper:
            return etf
    return DEFAULT_ETF


def compute_intraday_rs(
    bar_store,
    ticker: str,
    sector_etf: str = None,
    minutes: int = 30,
) -> Optional[float]:
    """Compute intraday relative strength: stock return - sector ETF return.

    Uses first N minutes of bars from the live bar store.

    Returns:
        float: RS value (positive = outperforming, negative = underperforming)
        None: if insufficient data
    """
    stock_bars = bar_store.get_bars(ticker)
    if stock_bars is None or len(stock_bars) < 5:
        return None

    etf = sector_etf or DEFAULT_ETF
    etf_bars = bar_store.get_bars(etf)
    if etf_bars is None or len(etf_bars) < 5:
        return None

    # Use first N bars (approximation of first N minutes)
    n_bars = min(minutes, len(stock_bars), len(etf_bars))

    stock_open = float(stock_bars.iloc[0]["Open"])
    stock_now = float(stock_bars.iloc[min(n_bars - 1, len(stock_bars) - 1)]["Close"])
    etf_open = float(etf_bars.iloc[0]["Open"])
    etf_now = float(etf_bars.iloc[min(n_bars - 1, len(etf_bars) - 1)]["Close"])

    if stock_open <= 0 or etf_open <= 0:
        return None

    stock_ret = (stock_now - stock_open) / stock_open
    etf_ret = (etf_now - etf_open) / etf_open

    return round(stock_ret - etf_ret, 6)
