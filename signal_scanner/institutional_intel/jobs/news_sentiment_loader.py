"""News & sentiment data loader using Polygon v2/reference/news.

Fetches recent news articles with per-ticker sentiment insights and stores
in fact_news_sentiment. Polygon's `insights` field provides AI-generated
per-ticker sentiment with reasoning.

Usage:
    python -m signal_scanner.institutional_intel.jobs.news_sentiment_loader
    python -m signal_scanner.institutional_intel.jobs.news_sentiment_loader --tickers AAPL,TSLA --days-back 7
    python -m signal_scanner.institutional_intel.jobs.news_sentiment_loader --min-conviction 50
"""
from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests
from loguru import logger

from signal_scanner.institutional_intel.config import (
    MASSIVE_API_KEY,
    MASSIVE_BASE_URL,
    safe_duckdb_connect,
)


# Sentiment to numeric score
_SENTIMENT_SCORE = {"positive": 1, "negative": -1, "neutral": 0}


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def _fetch_news(ticker: str, published_gte: str, limit: int = 50) -> List[Dict]:
    """Fetch news articles for a ticker from Polygon v2/reference/news."""
    url = f"{MASSIVE_BASE_URL}/v2/reference/news"
    params = {
        "apiKey": MASSIVE_API_KEY,
        "ticker": ticker,
        "published_utc.gte": published_gte,
        "order": "desc",
        "limit": str(min(limit, 1000)),
    }
    articles: List[Dict] = []
    pages = 0
    while url and pages < 5:  # max 5 pages per ticker
        try:
            resp = requests.get(url, params=params if pages == 0 else None, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            articles.extend(data.get("results", []))
            pages += 1
            next_url = data.get("next_url")
            url = (next_url + f"&apiKey={MASSIVE_API_KEY}") if next_url else None
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status == 429:
                logger.warning("Rate limited on news, sleeping 60s")
                time.sleep(60)
                continue
            logger.debug("News {} HTTP {}: {}", ticker, status, exc)
            break
        except Exception as exc:
            logger.debug("News {} error: {}", ticker, exc)
            break
    return articles


def _parse_article_rows(article: Dict, target_ticker: str) -> List[tuple]:
    """Extract per-ticker sentiment rows from an article's insights field."""
    news_id = article.get("id", "")
    title = article.get("title", "")
    published_utc = article.get("published_utc", "")
    author = article.get("author", "")
    article_url = article.get("article_url", "")
    publisher = (article.get("publisher") or {}).get("name", "")
    now_iso = datetime.now(timezone.utc).isoformat()

    rows = []
    insights = article.get("insights") or []
    if not insights:
        # No insights — create a row with neutral sentiment for the target ticker
        rows.append((
            news_id, target_ticker, published_utc, title,
            "neutral", 0, None, author, article_url, publisher,
            "polygon", now_iso,
        ))
        return rows

    for insight in insights:
        ticker = insight.get("ticker", "").upper()
        if not ticker:
            continue
        sentiment = (insight.get("sentiment") or "neutral").lower()
        score = _SENTIMENT_SCORE.get(sentiment, 0)
        reasoning = insight.get("sentiment_reasoning", "")
        rows.append((
            news_id, ticker, published_utc, title,
            sentiment, score, reasoning, author, article_url, publisher,
            "polygon", now_iso,
        ))
    return rows


# ---------------------------------------------------------------------------
# Main loader
# ---------------------------------------------------------------------------

def load_news_sentiment(
    tickers: List[str],
    days_back: int = 3,
    rps: float = 4.0,
) -> Dict:
    """Fetch news with sentiment for given tickers and store in fact_news_sentiment."""
    cutoff = (date.today() - timedelta(days=days_back)).isoformat() + "T00:00:00Z"
    logger.info("[NEWS] {} tickers | from {}", len(tickers), cutoff)

    all_rows: List[tuple] = []
    seen_ids: set = set()
    errors = 0
    delay = 1.0 / max(rps, 1.0)

    for i, ticker in enumerate(tickers):
        try:
            articles = _fetch_news(ticker, cutoff)
            for art in articles:
                rows = _parse_article_rows(art, ticker)
                for row in rows:
                    key = (row[0], row[1])  # (news_id, ticker)
                    if key not in seen_ids:
                        seen_ids.add(key)
                        all_rows.append(row)
        except Exception as exc:
            errors += 1
            logger.debug("News error {}: {}", ticker, exc)

        if (i + 1) % 50 == 0:
            logger.info("  [{}/{}] fetched, {} rows so far", i + 1, len(tickers), len(all_rows))

        time.sleep(delay)

    if not all_rows:
        logger.warning("[NEWS] No articles found for {} tickers", len(tickers))
        return {"total_rows": 0, "errors": errors}

    # Upsert: delete existing rows for same (news_id, ticker), then insert
    conn = safe_duckdb_connect(read_only=False)
    if conn is None:
        logger.error("[NEWS] Cannot connect to warehouse")
        return {"total_rows": 0, "errors": errors}
    try:
        conn.execute("CREATE TEMP TABLE _news_load AS SELECT * FROM fact_news_sentiment LIMIT 0")
        conn.executemany("INSERT INTO _news_load VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", all_rows)
        conn.execute("""
            DELETE FROM fact_news_sentiment
            WHERE (news_id, ticker) IN (SELECT news_id, ticker FROM _news_load)
        """)
        conn.execute("""
            INSERT INTO fact_news_sentiment
            SELECT news_id, ticker, published_at::TIMESTAMP, title,
                   sentiment, sentiment_score, sentiment_reasoning,
                   author, article_url, publisher, source, ingested_at::TIMESTAMP
            FROM _news_load
        """)
    finally:
        conn.close()

    logger.info("[NEWS] Saved {} rows | {} errors", len(all_rows), errors)
    return {"total_rows": len(all_rows), "errors": errors}


def get_news_tickers(min_conviction: float = 30) -> List[str]:
    """Get tickers to fetch news for (top conviction, max 500)."""
    conn = safe_duckdb_connect(read_only=True)
    if conn is None:
        return []
    try:
        rows = conn.execute("""
            SELECT ticker FROM intelligence_scores
            WHERE conviction_score >= ?
              AND ticker NOT IN ('N/A','NONE','NULL','')
              AND LENGTH(ticker) <= 5
            ORDER BY conviction_score DESC
            LIMIT 500
        """, [min_conviction]).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="News sentiment loader")
    p.add_argument("--tickers", default="", help="Comma-separated tickers")
    p.add_argument("--min-conviction", type=float, default=30)
    p.add_argument("--days-back", type=int, default=3)
    p.add_argument("--rps", type=float, default=4.0)
    args = p.parse_args()

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = get_news_tickers(min_conviction=args.min_conviction)

    if not tickers:
        logger.warning("No tickers to load")
        return

    logger.info("Loading news sentiment for {} tickers", len(tickers))
    result = load_news_sentiment(tickers, days_back=args.days_back, rps=args.rps)
    logger.info("Complete: {}", result)


if __name__ == "__main__":
    main()
