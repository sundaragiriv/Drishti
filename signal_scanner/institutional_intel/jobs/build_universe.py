"""Build local equity universe files for report filtering.

Creates:
- signal_scanner/watchlists/russell3000.txt   (master universe baseline)
- signal_scanner/watchlists/russell1000.txt
- signal_scanner/watchlists/russell2000_full.txt
- signal_scanner/watchlists/universe_master.txt
- signal_scanner/watchlists/universe_membership.csv
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Dict, List, Set

import requests
from loguru import logger


WATCHLIST_DIR = Path(__file__).resolve().parents[2] / "watchlists"

IWB_URL = (
    "https://www.ishares.com/us/products/239707/"
    "ishares-russell-1000-etf/1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"
)
IWM_URL = (
    "https://www.ishares.com/us/products/239710/"
    "ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"
)
IWV_URL = (
    "https://www.ishares.com/us/products/239714/"
    "ishares-russell-3000-etf/1467271812596.ajax?fileType=csv&fileName=IWV_holdings&dataType=fund"
)

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
IWB_IWM_TICKER_FIXUPS: Dict[str, str] = {
    "BRKB": "BRK.B",
    "BFB": "BF.B",
}


def _download_text(url: str) -> str:
    r = requests.get(url, timeout=45, headers={"User-Agent": "QuantBridge Universe Builder"})
    r.raise_for_status()
    return r.text


def _extract_tickers_from_holdings_csv(text: str) -> Set[str]:
    lines = text.splitlines()
    header_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("Ticker,Name,Sector,Asset Class,"):
            header_idx = i
            break
    if header_idx < 0:
        raise ValueError("Holdings CSV header not found")

    rows = list(csv.DictReader(lines[header_idx:]))
    out: Set[str] = set()
    for r in rows:
        asset_class = str(r.get("Asset Class") or "").strip().upper()
        t = str(r.get("Ticker") or "").strip().upper().replace("/", ".")
        if not t or t == "-" or asset_class != "EQUITY":
            continue
        t = IWB_IWM_TICKER_FIXUPS.get(t, t)
        if SYMBOL_RE.match(t):
            out.add(t)
    return out


def _read_watchlist(name: str) -> Set[str]:
    path = WATCHLIST_DIR / f"{name}.txt"
    if not path.exists():
        return set()
    out: Set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip().upper()
        if not s or s.startswith("#"):
            continue
        if SYMBOL_RE.match(s):
            out.add(s)
    return out


def _write_watchlist(path: Path, symbols: Set[str], header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = [f"# {header}", ""]
    lines.extend(sorted(symbols))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    logger.info("Building universe files in {}", WATCHLIST_DIR)
    WATCHLIST_DIR.mkdir(parents=True, exist_ok=True)

    # Download Russell universes from iShares holdings.
    r3000 = _extract_tickers_from_holdings_csv(_download_text(IWV_URL))
    r1000 = _extract_tickers_from_holdings_csv(_download_text(IWB_URL))
    r2000_full = _extract_tickers_from_holdings_csv(_download_text(IWM_URL))

    # Existing local categories
    sp500 = _read_watchlist("sp500")
    nasdaq100 = _read_watchlist("nasdaq100")
    custom = _read_watchlist("custom")

    # Write category watchlists.
    _write_watchlist(
        WATCHLIST_DIR / "russell3000.txt",
        r3000,
        "Russell 3000 (from iShares IWV holdings snapshot)",
    )
    _write_watchlist(
        WATCHLIST_DIR / "russell1000.txt",
        r1000,
        "Russell 1000 (from iShares IWB holdings snapshot)",
    )
    _write_watchlist(
        WATCHLIST_DIR / "russell2000_full.txt",
        r2000_full,
        "Russell 2000 full (from iShares IWM holdings snapshot)",
    )

    # Master universe (R3000 baseline + all explicit watchlist categories).
    master = set(r3000) | set(r1000) | set(r2000_full) | set(sp500) | set(nasdaq100) | set(custom)
    _write_watchlist(
        WATCHLIST_DIR / "universe_master.txt",
        master,
        "Quant-Bridge master universe (Russell 3000 + category watchlists union)",
    )

    # Membership table for category-aware filtering.
    membership_path = WATCHLIST_DIR / "universe_membership.csv"
    fields = [
        "symbol",
        "is_russell3000",
        "is_russell1000",
        "is_russell2000",
        "is_sp500",
        "is_nasdaq100",
        "is_custom",
    ]
    with membership_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for s in sorted(master):
            w.writerow(
                {
                    "symbol": s,
                    "is_russell3000": int(s in r3000),
                    "is_russell1000": int(s in r1000),
                    "is_russell2000": int(s in r2000_full),
                    "is_sp500": int(s in sp500),
                    "is_nasdaq100": int(s in nasdaq100),
                    "is_custom": int(s in custom),
                }
            )

    logger.info(
        "Universe built | r3000={} | r1000={} | r2000_full={} | sp500={} | nasdaq100={} | custom={} | master={}",
        len(r3000),
        len(r1000),
        len(r2000_full),
        len(sp500),
        len(nasdaq100),
        len(custom),
        len(master),
    )
    logger.info("Wrote {}", WATCHLIST_DIR / "russell3000.txt")
    logger.info("Wrote {}", WATCHLIST_DIR / "russell1000.txt")
    logger.info("Wrote {}", WATCHLIST_DIR / "russell2000_full.txt")
    logger.info("Wrote {}", WATCHLIST_DIR / "universe_master.txt")
    logger.info("Wrote {}", membership_path)


if __name__ == "__main__":
    main()

