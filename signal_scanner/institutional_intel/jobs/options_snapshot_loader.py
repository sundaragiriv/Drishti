"""Options Snapshot Loader — Polygon → DuckDB per-contract persistence.

Fetches option chain snapshots from Polygon Options Starter API
and persists at contract level for OI history, IV/skew analysis,
and options-expression scoring.

Usage:
    python -m signal_scanner.institutional_intel.jobs.options_snapshot_loader --tickers AAPL,MSFT
    python -m signal_scanner.institutional_intel.jobs.options_snapshot_loader --universe  # top Sniper tickers
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from loguru import logger

from signal_scanner.core.readiness import latest_complete_trading_day


# ---------------------------------------------------------------------------
# Polygon API
# ---------------------------------------------------------------------------

POLYGON_BASE = "https://api.polygon.io"
SNAPSHOT_ENDPOINT = "/v3/snapshot/options/{underlying}"


def _get_api_key() -> str:
    key = os.environ.get("MASSIVE_API_KEY", "")
    if not key:
        try:
            env_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")
            for line in open(env_path):
                if line.startswith("MASSIVE_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
        except Exception:
            pass
    return key


def fetch_options_snapshot(
    underlying: str,
    api_key: str,
    min_oi: int = 10,
    max_contracts: int = 250,
) -> List[Dict[str, Any]]:
    """Fetch all option contracts for an underlying from Polygon snapshot.

    Returns list of contract dicts ready for DB insertion.
    """
    contracts = []
    url = POLYGON_BASE + SNAPSHOT_ENDPOINT.format(underlying=underlying)
    params = {
        "apiKey": api_key,
        "limit": 250,
    }

    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            logger.debug("Polygon options snapshot {} error: {}", underlying, r.status_code)
            return []

        data = r.json()
        now = datetime.now(timezone.utc).isoformat()
        snapshot_date = latest_complete_trading_day().isoformat()

        for c in data.get("results", []):
            details = c.get("details", {})
            day = c.get("day", {})
            greeks = c.get("greeks", {})
            quote = c.get("last_quote", {})
            oi = c.get("open_interest", 0) or 0

            if oi < min_oi:
                continue

            contracts.append({
                "underlying": underlying,
                "contract_ticker": details.get("ticker", ""),
                "contract_type": details.get("contract_type", ""),
                "expiration_date": details.get("expiration_date", ""),
                "strike_price": details.get("strike_price"),
                "exercise_style": details.get("exercise_style", ""),
                "shares_per_contract": details.get("shares_per_contract", 100),
                # Quote
                "bid": quote.get("bid"),
                "ask": quote.get("ask"),
                "midpoint": quote.get("midpoint"),
                "last_price": day.get("close"),
                "volume": day.get("volume", 0) or 0,
                "vwap": day.get("vwap"),
                "open_interest": oi,
                # IV + Greeks
                "implied_volatility": c.get("implied_volatility"),
                "delta": greeks.get("delta"),
                "gamma": greeks.get("gamma"),
                "theta": greeks.get("theta"),
                "vega": greeks.get("vega"),
                # Day
                "day_open": day.get("open"),
                "day_high": day.get("high"),
                "day_low": day.get("low"),
                "day_close": day.get("close"),
                "day_change_pct": day.get("change_percent"),
                "prev_close": day.get("previous_close"),
                # Meta
                "snapshot_date": snapshot_date,
                "snapshot_ts": now,
            })

            if len(contracts) >= max_contracts:
                break

        # Handle pagination if needed
        next_url = data.get("next_url")
        while next_url and len(contracts) < max_contracts:
            time.sleep(0.25)  # rate limit
            r2 = requests.get(next_url + f"&apiKey={api_key}", timeout=15)
            if r2.status_code != 200:
                break
            data2 = r2.json()
            for c in data2.get("results", []):
                details = c.get("details", {})
                day = c.get("day", {})
                greeks = c.get("greeks", {})
                quote = c.get("last_quote", {})
                oi = c.get("open_interest", 0) or 0
                if oi < min_oi:
                    continue
                contracts.append({
                    "underlying": underlying,
                    "contract_ticker": details.get("ticker", ""),
                    "contract_type": details.get("contract_type", ""),
                    "expiration_date": details.get("expiration_date", ""),
                    "strike_price": details.get("strike_price"),
                    "exercise_style": details.get("exercise_style", ""),
                    "shares_per_contract": details.get("shares_per_contract", 100),
                    "bid": quote.get("bid"),
                    "ask": quote.get("ask"),
                    "midpoint": quote.get("midpoint"),
                    "last_price": day.get("close"),
                    "volume": day.get("volume", 0) or 0,
                    "vwap": day.get("vwap"),
                    "open_interest": oi,
                    "implied_volatility": c.get("implied_volatility"),
                    "delta": greeks.get("delta"),
                    "gamma": greeks.get("gamma"),
                    "theta": greeks.get("theta"),
                    "vega": greeks.get("vega"),
                    "day_open": day.get("open"),
                    "day_high": day.get("high"),
                    "day_low": day.get("low"),
                    "day_close": day.get("close"),
                    "day_change_pct": day.get("change_percent"),
                    "prev_close": day.get("previous_close"),
                    "snapshot_date": snapshot_date,
                    "snapshot_ts": now,
                })
                if len(contracts) >= max_contracts:
                    break
            next_url = data2.get("next_url")

    except Exception as e:
        logger.warning("Options snapshot fetch error for {}: {}", underlying, e)

    return contracts


# ---------------------------------------------------------------------------
# DuckDB persistence
# ---------------------------------------------------------------------------

CREATE_OPTIONS_CONTRACTS = """
CREATE TABLE IF NOT EXISTS fact_options_contracts (
    underlying          VARCHAR NOT NULL,
    contract_ticker     VARCHAR NOT NULL,
    contract_type       VARCHAR NOT NULL,       -- call / put
    expiration_date     DATE NOT NULL,
    strike_price        DOUBLE NOT NULL,
    exercise_style      VARCHAR,
    shares_per_contract INTEGER DEFAULT 100,
    -- Quote
    bid                 DOUBLE,
    ask                 DOUBLE,
    midpoint            DOUBLE,
    last_price          DOUBLE,
    volume              BIGINT DEFAULT 0,
    vwap                DOUBLE,
    open_interest       BIGINT DEFAULT 0,
    -- IV + Greeks
    implied_volatility  DOUBLE,
    delta               DOUBLE,
    gamma               DOUBLE,
    theta               DOUBLE,
    vega                DOUBLE,
    -- Day
    day_open            DOUBLE,
    day_high            DOUBLE,
    day_low             DOUBLE,
    day_close           DOUBLE,
    day_change_pct      DOUBLE,
    prev_close          DOUBLE,
    -- Meta
    snapshot_date       DATE NOT NULL,
    snapshot_ts         TIMESTAMP NOT NULL,
    PRIMARY KEY (contract_ticker, snapshot_date)
);
"""

CREATE_OI_HISTORY = """
CREATE TABLE IF NOT EXISTS fact_options_oi_history (
    contract_ticker     VARCHAR NOT NULL,
    underlying          VARCHAR NOT NULL,
    contract_type       VARCHAR NOT NULL,
    expiration_date     DATE NOT NULL,
    strike_price        DOUBLE NOT NULL,
    snapshot_date       DATE NOT NULL,
    open_interest       BIGINT NOT NULL,
    volume              BIGINT DEFAULT 0,
    implied_volatility  DOUBLE,
    oi_change           BIGINT,             -- vs prior day
    oi_change_pct       DOUBLE,
    PRIMARY KEY (contract_ticker, snapshot_date)
);
"""


def persist_contracts(conn, contracts: List[Dict]) -> int:
    """Write contract snapshots to DuckDB. Returns rows written."""
    conn.execute(CREATE_OPTIONS_CONTRACTS)
    conn.execute(CREATE_OI_HISTORY)

    written = 0
    for c in contracts:
        try:
            # Upsert contract snapshot
            conn.execute("""
                INSERT INTO fact_options_contracts VALUES (
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?
                )
                ON CONFLICT (contract_ticker, snapshot_date) DO UPDATE SET
                    bid = excluded.bid, ask = excluded.ask,
                    midpoint = excluded.midpoint, last_price = excluded.last_price,
                    volume = excluded.volume, open_interest = excluded.open_interest,
                    implied_volatility = excluded.implied_volatility,
                    delta = excluded.delta, gamma = excluded.gamma,
                    theta = excluded.theta, vega = excluded.vega,
                    snapshot_ts = excluded.snapshot_ts
            """, [
                c["underlying"], c["contract_ticker"], c["contract_type"],
                c["expiration_date"], c["strike_price"], c["exercise_style"],
                c["shares_per_contract"],
                c["bid"], c["ask"], c["midpoint"], c["last_price"],
                c["volume"], c["vwap"], c["open_interest"],
                c["implied_volatility"], c["delta"], c["gamma"],
                c["theta"], c["vega"],
                c["day_open"], c["day_high"], c["day_low"],
                c["day_close"], c["day_change_pct"], c["prev_close"],
                c["snapshot_date"], c["snapshot_ts"],
            ])

            # OI history — compute change from prior day
            prior = conn.execute("""
                SELECT open_interest FROM fact_options_oi_history
                WHERE contract_ticker = ? AND snapshot_date < ?
                ORDER BY snapshot_date DESC LIMIT 1
            """, [c["contract_ticker"], c["snapshot_date"]]).fetchone()

            oi_change = None
            oi_change_pct = None
            if prior and prior[0]:
                oi_change = c["open_interest"] - prior[0]
                oi_change_pct = round(oi_change / prior[0] * 100, 1) if prior[0] > 0 else None

            conn.execute("""
                INSERT INTO fact_options_oi_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (contract_ticker, snapshot_date) DO UPDATE SET
                    open_interest = excluded.open_interest,
                    volume = excluded.volume,
                    implied_volatility = excluded.implied_volatility,
                    oi_change = excluded.oi_change,
                    oi_change_pct = excluded.oi_change_pct
            """, [
                c["contract_ticker"], c["underlying"], c["contract_type"],
                c["expiration_date"], c["strike_price"],
                c["snapshot_date"], c["open_interest"], c["volume"],
                c["implied_volatility"], oi_change, oi_change_pct,
            ])

            written += 1
        except Exception as e:
            logger.debug("Options persist error {}: {}", c.get("contract_ticker"), e)

    return written


def load_options_for_tickers(
    tickers: List[str],
    rps: float = 3.0,
) -> Dict[str, int]:
    """Load options snapshots for a list of tickers.

    Args:
        tickers: list of underlying symbols
        rps: requests per second (Polygon rate limit)

    Returns: {ticker: contracts_written} dict.
    """
    from signal_scanner.institutional_intel.config import safe_duckdb_connect

    api_key = _get_api_key()
    if not api_key:
        logger.error("No MASSIVE_API_KEY found")
        return {}

    conn = safe_duckdb_connect(read_only=False)
    if not conn:
        logger.error("Cannot connect to DuckDB for options write")
        return {}

    results = {}
    try:
        for i, ticker in enumerate(tickers):
            contracts = fetch_options_snapshot(ticker, api_key)
            if contracts:
                n = persist_contracts(conn, contracts)
                results[ticker] = n
                logger.debug("Options {}: {} contracts persisted", ticker, n)
            else:
                results[ticker] = 0

            # Rate limit
            if i < len(tickers) - 1:
                time.sleep(1.0 / rps)

    finally:
        conn.close()

    total = sum(results.values())
    logger.info("Options snapshot: {} tickers, {} total contracts", len(results), total)
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load options snapshots from Polygon")
    parser.add_argument("--tickers", type=str, help="Comma-separated tickers")
    parser.add_argument("--universe", action="store_true",
                        help="Load top Sniper universe tickers")
    parser.add_argument("--top", type=int, default=30,
                        help="Number of top tickers from universe (default 30)")
    args = parser.parse_args()

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",")]
    elif args.universe:
        from signal_scanner.institutional_intel.config import safe_duckdb_connect, get_active_quarter
        conn = safe_duckdb_connect(read_only=True)
        quarter = get_active_quarter(conn)
        # Top LONG ideas (swing BUY, high conviction)
        long_rows = conn.execute("""
            SELECT ticker FROM intelligence_scores
            WHERE report_quarter = ? AND swing_signal = 'BUY'
            AND conviction_score >= 65 AND data_quality_score >= 50
            ORDER BY conviction_score DESC LIMIT ?
        """, [quarter, args.top]).fetchall()
        # Top SHORT ideas (distribution/decline)
        short_rows = conn.execute("""
            SELECT ticker FROM intelligence_scores
            WHERE report_quarter = ? AND short_swing_signal = 'SHORT'
            AND data_quality_score >= 50
            ORDER BY COALESCE(short_conviction_score, 0) DESC LIMIT ?
        """, [quarter, max(10, args.top // 3)]).fetchall()
        conn.close()
        seen = set()
        tickers = []
        for r in long_rows + short_rows:
            if r[0] not in seen:
                tickers.append(r[0])
                seen.add(r[0])
        # Always include SPY for context
        if "SPY" not in seen:
            tickers.insert(0, "SPY")
        logger.info("Universe: {} tickers ({} long + {} short + SPY) from {}",
                     len(tickers), len(long_rows), len(short_rows), quarter)
    else:
        tickers = ["SPY", "AAPL", "MSFT", "NVDA", "GOOGL"]

    results = load_options_for_tickers(tickers)
    for t, n in sorted(results.items(), key=lambda x: -x[1])[:10]:
        print(f"  {t}: {n} contracts")
