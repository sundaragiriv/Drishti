"""Phase 2d: Insider Intelligence — Form 4 cluster detection.

Detects high-quality insider buying patterns:
    - Cluster buy: 3+ UNIQUE insiders buying the same stock within 30 days
    - CEO/CFO buying: highest-conviction insider signal
    - Net buy count: buys minus sells (signed)
    - Insider score: 0-100 composite

Cluster buy is the key signal. A single insider buying could be routine.
When 3+ different insiders buy the same stock in 30 days, it indicates
organizational conviction — they all believe the stock is undervalued.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb
from loguru import logger


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _safe_int(val, default: int = 0) -> int:
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


# Transaction types that count as insider BUY
BUY_TYPES = ("P", "A", "M")  # Purchase, Award, Option exercise
SELL_TYPES = ("S", "D")       # Sale, Disposition


def compute_insider_signals(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
    lookback_days: int = 90,
) -> list[dict]:
    """Compute insider signals for all tickers around the given quarter.

    For each ticker, computes:
        insider_cluster_detected — 3+ unique insiders buying within 30-day window
        insider_net_buy_count — buys - sells (count of transactions)
        ceo_cfo_buying — True if CEO or CFO is in the buyer set
        insider_score — 0-100 composite

    Args:
        conn: DuckDB connection
        quarter: Reference quarter (e.g. "2024-Q3")
        lookback_days: How many days before quarter end to look for insider activity
    """
    logger.info("Computing insider signals for quarter={}", quarter)

    # Derive a reference date range from the quarter
    year = int(quarter.split("-Q")[0])
    qnum = int(quarter.split("-Q")[1])
    import calendar
    quarter_end_month = {1: 3, 2: 6, 3: 9, 4: 12}[qnum]
    last_day = calendar.monthrange(year, quarter_end_month)[1]
    quarter_end = datetime(year, quarter_end_month, last_day)
    # Add 45 days for filing lag
    window_end = quarter_end + timedelta(days=45)
    window_start = window_end - timedelta(days=lookback_days)

    window_start_str = window_start.strftime("%Y-%m-%d")
    window_end_str = window_end.strftime("%Y-%m-%d")

    try:
        df = conn.execute("""
            SELECT
                ticker,
                transaction_date,
                transaction_code,
                insider_role,
                insider_name,
                shares
            FROM fact_form4_transactions
            WHERE transaction_date >= ?
              AND transaction_date <= ?
              AND transaction_code IS NOT NULL
            ORDER BY ticker, transaction_date
        """, [window_start_str, window_end_str]).fetchdf()
    except Exception as e:
        logger.warning("Insider query failed for {}: {}", quarter, e)
        return []

    if df.empty:
        logger.info("No insider transactions found for window {} to {}", window_start_str, window_end_str)
        return []

    # Cluster detection: 3+ unique insiders buying within any 30-day window
    results_by_ticker = {}

    for ticker, group in df.groupby("ticker"):
        buys = group[group["transaction_code"].isin(list(BUY_TYPES))]
        sells = group[group["transaction_code"].isin(list(SELL_TYPES))]

        buy_count = len(buys)
        sell_count = len(sells)
        net_buy = buy_count - sell_count

        # CEO/CFO detection
        ceo_cfo_buying = False
        for _, row in buys.iterrows():
            title = str(row.get("insider_role") or "").lower()
            if any(t in title for t in ("ceo", "chief executive", "cfo", "chief financial")):
                ceo_cfo_buying = True
                break

        # Cluster detection: find rolling 30-day windows with 3+ unique buyers
        cluster_detected = False
        if len(buys) >= 3:
            buy_dates = sorted(buys["transaction_date"].tolist())
            buy_names = buys["insider_name"].tolist()

            # Sliding window: for each buy, check if 3+ unique insiders bought in next 30 days
            from datetime import datetime as dt_cls
            for i, ref_date in enumerate(buy_dates):
                if isinstance(ref_date, str):
                    ref_dt = dt_cls.fromisoformat(ref_date)
                else:
                    try:
                        ref_dt = dt_cls.fromisoformat(str(ref_date)[:10])
                    except Exception:
                        continue

                window_buyers = set()
                for j, d in enumerate(buy_dates):
                    if isinstance(d, str):
                        d_dt = dt_cls.fromisoformat(d)
                    else:
                        try:
                            d_dt = dt_cls.fromisoformat(str(d)[:10])
                        except Exception:
                            continue
                    if 0 <= (d_dt - ref_dt).days <= 30:
                        try:
                            window_buyers.add(str(buy_names[j]))
                        except IndexError:
                            pass

                if len(window_buyers) >= 3:
                    cluster_detected = True
                    break

        # Insider score
        score = 0.0
        if cluster_detected:
            score += 60.0
        if ceo_cfo_buying:
            score += 25.0
        if net_buy >= 5:
            score += 15.0
        elif net_buy >= 3:
            score += 10.0
        elif net_buy >= 1:
            score += 5.0
        if sell_count > buy_count * 2:
            score = max(0.0, score - 30.0)

        results_by_ticker[str(ticker)] = {
            "ticker": str(ticker),
            "insider_cluster_detected": cluster_detected,
            "insider_net_buy_count": net_buy,
            "ceo_cfo_buying": ceo_cfo_buying,
            "insider_score": min(100.0, score),
        }

    results = list(results_by_ticker.values())
    logger.info("Insider signals computed: {} tickers for quarter={}", len(results), quarter)
    return results


def update_insider_in_intelligence(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
) -> int:
    """Write insider signals into intelligence_scores table."""
    results = compute_insider_signals(conn, quarter)
    if not results:
        return 0

    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    updated = 0

    for r in results:
        try:
            conn.execute("""
                UPDATE intelligence_scores
                SET insider_cluster_detected = ?,
                    insider_net_buy_count = ?,
                    ceo_cfo_buying = ?,
                    insider_score = ?,
                    computed_at = ?
                WHERE ticker = ? AND report_quarter = ?
            """, [
                r["insider_cluster_detected"],
                r["insider_net_buy_count"],
                r["ceo_cfo_buying"],
                r["insider_score"],
                now_iso,
                r["ticker"],
                quarter,
            ])
            updated += 1
        except Exception as e:
            logger.debug("Insider update failed for {}: {}", r["ticker"], e)

    logger.info("Insider signals updated: {}/{} for quarter={}", updated, len(results), quarter)
    return updated
