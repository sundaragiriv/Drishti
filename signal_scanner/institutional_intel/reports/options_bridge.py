"""Options Bridge — Sync institutional contract ideas to option_setups table.

Runs at 7:00 AM and 12:00 PM on trading days (registered in main.py).
Reads the best clean quarter from DuckDB, generates contract ideas via
KuberaContractIdeas, then upserts into option_setups in signals.db so
the paper trader can monitor and execute them.

Usage (manual):
    python -m signal_scanner.institutional_intel.reports.options_bridge
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import List, Dict, Optional

from loguru import logger

from signal_scanner.institutional_intel.reports.contract_ideas import KuberaContractIdeas


def _expiry_guidance_to_date(guidance: str) -> str:
    """Convert '7-14 DTE (Weekly)' style guidance to a concrete YYYY-MM-DD expiry.

    Takes the midpoint of the DTE range, finds the nearest Friday on or after
    (today + midpoint_days), and returns that date.
    """
    # Extract first number from guidance (e.g. '7-14 DTE' → 7, '30-45 DTE' → 30)
    match = re.search(r"(\d+)", guidance)
    if match:
        low_dte = int(match.group(1))
    else:
        low_dte = 30

    # Try to get a second number for the range midpoint
    match2 = re.search(r"(\d+)-(\d+)", guidance)
    if match2:
        mid_dte = (int(match2.group(1)) + int(match2.group(2))) // 2
    else:
        mid_dte = low_dte + 7

    target = datetime.now(timezone.utc) + timedelta(days=mid_dte)
    # Roll forward to nearest Friday (weekday 4)
    days_to_friday = (4 - target.weekday()) % 7
    expiry = target + timedelta(days=days_to_friday)
    return expiry.strftime("%Y-%m-%d")


def sync_contract_ideas_to_option_setups(quarter: Optional[str] = None, max_ideas: int = 30) -> int:
    """Generate contract ideas from institutional intel and upsert to option_setups.

    Args:
        quarter: Target quarter (uses best clean quarter if None)
        max_ideas: Max ideas to sync

    Returns:
        Number of ideas upserted.
    """
    import sqlite3
    from signal_scanner.database.db_manager import DatabaseManager

    now_iso = datetime.now(timezone.utc).isoformat()

    # Generate ideas from institutional intelligence
    try:
        generator = KuberaContractIdeas()
        ideas = generator.generate_ideas(quarter=quarter, max_ideas=max_ideas)
    except Exception as e:
        logger.error("Options bridge: failed to generate contract ideas: {}", e)
        return 0

    if not ideas:
        logger.info("Options bridge: no contract ideas generated for quarter={}", quarter)
        return 0

    logger.info("Options bridge: {} ideas generated, syncing to option_setups...", len(ideas))

    # Load ML scores from DuckDB for ML overlay boost
    ml_scores: Dict[str, float] = {}
    flow_data: Dict[str, Dict] = {}
    try:
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        ddb = safe_duckdb_connect(read_only=True)
        if ddb:
            rows = ddb.execute(
                "SELECT ticker, ml_score_v2 FROM intelligence_scores WHERE ml_score_v2 IS NOT NULL"
            ).fetchall()
            ml_scores = {r[0]: float(r[1]) for r in rows}

            # Load options flow for rationale enrichment
            all_tickers = [idea.get("ticker", "") for idea in ideas if idea.get("ticker")]
            flow_data = KuberaContractIdeas.fetch_options_flow_batch(ddb, all_tickers)

            ddb.close()
            logger.info("Options bridge: loaded {} ML scores, {} flow records", len(ml_scores), len(flow_data))
    except Exception as e:
        logger.debug("Options bridge: ML/flow load skipped: {}", e)

    # Upsert into option_setups
    db = DatabaseManager()
    conn = sqlite3.connect(db._db_path)
    upserted = 0

    try:
        for idea in ideas:
            ticker = idea.get("ticker", "")
            if not ticker or len(ticker) > 5:
                continue

            option_type = idea.get("option_type", "CALL").upper()
            if option_type not in ("CALL", "PUT"):
                continue

            current_price = float(idea.get("current_price") or 0)
            if current_price <= 0:
                continue

            strike = float(idea.get("strike") or current_price)
            expiry_guidance = str(idea.get("expiry_guidance") or "30-45 DTE")
            expiry_date = _expiry_guidance_to_date(expiry_guidance)
            conviction = float(idea.get("conviction_score") or 50)
            rationale = str(idea.get("rationale") or "")
            source_tag = str(idea.get("source") or "INSTITUTIONAL_INTEL")

            # ML score overlay — boost conviction for tickers with high ML confidence
            ml_score = ml_scores.get(ticker, 0)
            ml_boost = 0
            if ml_score >= 80:
                ml_boost += 10  # High swing ML confidence
            elif ml_score >= 60:
                ml_boost += 5
            adjusted_score = min(100, conviction + ml_boost)

            recommendation = "BUY" if option_type == "CALL" else "SELL"
            signal = "LONG" if option_type == "CALL" else "SHORT"

            ml_tag = f" | ML={ml_score:.0f}(+{ml_boost})" if ml_boost else ""
            flow = flow_data.get(ticker, {})
            flow_tag = ""
            if flow.get("sentiment"):
                flow_tag = f" | Flow={flow['sentiment']}"
                if flow.get("put_call_ratio_vol"):
                    flow_tag += f" P/C={flow['put_call_ratio_vol']:.2f}"
            full_rationale = f"[{source_tag}] {rationale} | Conviction={conviction:.0f}/100{ml_tag}{flow_tag}"

            try:
                conn.execute("""
                    INSERT INTO option_setups
                        (symbol, option_type, expiry_date, strike, underlying_price,
                         recommendation, signal, score, rr_ratio, market_regime,
                         gex_status, rationale, status, created_ts, updated_ts,
                         idea_state, is_taken, confirm_count)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'ACTIVE',?,?,
                            'ACTIVE', 0, 1)
                    ON CONFLICT(symbol, option_type, expiry_date, strike) DO UPDATE SET
                        underlying_price = excluded.underlying_price,
                        score            = excluded.score,
                        rationale        = excluded.rationale,
                        updated_ts       = excluded.updated_ts,
                        idea_state       = CASE
                            WHEN option_setups.confirm_count + 1 >= 2 THEN 'STRONG'
                            ELSE 'ACTIVE'
                        END,
                        confirm_count    = option_setups.confirm_count + 1
                """, (
                    ticker, option_type, expiry_date, strike, current_price,
                    recommendation, signal, adjusted_score,
                    1.5,          # default R:R for institutional ideas
                    None,         # market_regime (not available from intel)
                    None,         # gex_status
                    full_rationale,
                    now_iso, now_iso,
                ))
                upserted += 1
            except Exception as e:
                logger.warning("Options bridge: upsert failed for {}: {}", ticker, e)

        conn.commit()
        logger.info("Options bridge: {} ideas upserted to option_setups", upserted)
    finally:
        conn.close()

    return upserted


def run_bridge_job() -> None:
    """Scheduled job entry point (called by APScheduler in main.py)."""
    logger.info("Options bridge job starting...")
    n = sync_contract_ideas_to_option_setups()
    logger.info("Options bridge job complete: {} setups synced", n)


if __name__ == "__main__":
    run_bridge_job()
