"""Intelligence Layer Data Providers — one per report.

Each provider returns a dict ready for the layout builder.
All read from DuckDB warehouse (read-only).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger

from signal_scanner.institutional_intel.config import safe_duckdb_connect, get_active_quarter


def _conn():
    return safe_duckdb_connect(read_only=True)


def _freshness(conn, quarter: str) -> str:
    try:
        row = conn.execute(
            "SELECT MAX(computed_at) FROM intelligence_scores WHERE report_quarter = ?",
            [quarter],
        ).fetchone()
        return f"Last computed: {row[0]}" if row and row[0] else "Unknown"
    except Exception:
        return "Unknown"


# ---------------------------------------------------------------------------
# 1. Overview
# ---------------------------------------------------------------------------

def get_overview_data(quarter: str = None) -> dict:
    conn = _conn()
    if not conn:
        return {"error": "DB unavailable"}
    try:
        if not quarter:
            quarter = get_active_quarter(conn)
        freshness = _freshness(conn, quarter)

        # KPIs
        kpis = {}
        row = conn.execute("""
            SELECT
                COUNT(CASE WHEN accum_phase = 'ACTIVE_ACCUM' THEN 1 END),
                COUNT(CASE WHEN accum_phase = 'EARLY_ACCUM' THEN 1 END),
                COUNT(CASE WHEN conviction_score >= 70 THEN 1 END),
                COUNT(CASE WHEN insider_cluster_detected THEN 1 END),
                COUNT(CASE WHEN distribution_warning THEN 1 END),
                COUNT(CASE WHEN triple_lock THEN 1 END)
            FROM intelligence_scores
            WHERE report_quarter = ? AND data_quality_score >= 50
        """, [quarter]).fetchone()
        if row:
            kpis = {
                "active_accum": row[0], "early_accum": row[1],
                "high_conviction": row[2], "insider_clusters": row[3],
                "dist_warnings": row[4], "triple_lock": row[5],
            }

        # Top setups
        top_setups = [dict(zip(
            ["ticker", "conviction", "phase", "ml_v2", "insider", "pressure", "squeeze"],
            r
        )) for r in conn.execute("""
            SELECT ticker, ROUND(conviction_score, 1), accum_phase,
                   ROUND(COALESCE(ml_score_v2, 0), 1),
                   ROUND(COALESCE(insider_effect_score, 0), 1),
                   ROUND(COALESCE(institutional_pressure, 0), 1),
                   ROUND(COALESCE(squeeze_score, 0), 1)
            FROM intelligence_scores
            WHERE report_quarter = ? AND data_quality_score >= 50
              AND accum_phase IN ('ACTIVE_ACCUM', 'EARLY_ACCUM', 'LATE_ACCUM')
            ORDER BY conviction_score DESC LIMIT 25
        """, [quarter]).fetchall()]

        # Distribution warnings
        deteriorating = [dict(zip(
            ["ticker", "conviction", "phase", "warning"],
            r
        )) for r in conn.execute("""
            SELECT ticker, ROUND(conviction_score, 1), accum_phase,
                   CASE
                     WHEN distribution_warning THEN 'Distribution Warning'
                     WHEN accum_phase = 'DECLINE' THEN 'In Decline'
                     ELSE 'Weakening'
                   END
            FROM intelligence_scores
            WHERE report_quarter = ? AND data_quality_score >= 50
              AND (distribution_warning OR accum_phase IN ('DISTRIBUTION', 'DECLINE'))
            ORDER BY conviction_score DESC LIMIT 15
        """, [quarter]).fetchall()]

        return {
            "quarter": quarter, "freshness": freshness,
            "kpis": kpis, "top_setups": top_setups,
            "improving": [],  # QoQ change data needed for this
            "deteriorating": deteriorating,
        }
    except Exception as e:
        logger.warning("Overview data error: {}", e)
        return {"error": str(e)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. Institutional Report
# ---------------------------------------------------------------------------

def get_institutional_data(quarter: str = None) -> dict:
    conn = _conn()
    if not conn:
        return {"error": "DB unavailable"}
    try:
        if not quarter:
            quarter = get_active_quarter(conn)

        # Phase distribution
        phase_dist = {}
        for r in conn.execute("""
            SELECT accum_phase, COUNT(*) FROM intelligence_scores
            WHERE report_quarter = ? AND data_quality_score >= 50
            GROUP BY accum_phase ORDER BY COUNT(*) DESC
        """, [quarter]).fetchall():
            phase_dist[r[0] or "UNKNOWN"] = r[1]

        # Top quality
        top_quality = [dict(zip(
            ["ticker", "conviction", "phase", "tier1_mgrs", "manager_quality",
             "insider_score", "cascade"],
            r
        )) for r in conn.execute("""
            SELECT ticker, ROUND(conviction_score, 1), accum_phase,
                   COALESCE(tier1_manager_count, 0),
                   ROUND(COALESCE(manager_quality_score, 0), 1),
                   ROUND(COALESCE(insider_score, 0), 1),
                   COALESCE(cascade_stage, 0)
            FROM intelligence_scores
            WHERE report_quarter = ? AND data_quality_score >= 50
              AND conviction_score >= 55
            ORDER BY COALESCE(manager_quality_score, 0) DESC, conviction_score DESC
            LIMIT 30
        """, [quarter]).fetchall()]

        return {
            "freshness": _freshness(conn, quarter),
            "phase_distribution": phase_dist,
            "top_quality": top_quality,
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. Sector Rotation
# ---------------------------------------------------------------------------

def get_sector_rotation_data(quarter: str = None) -> dict:
    conn = _conn()
    if not conn:
        return {"error": "DB unavailable"}
    try:
        if not quarter:
            quarter = get_active_quarter(conn)

        sectors = [dict(zip(
            ["sector", "net_flow_pct", "inflow_streak", "tickers", "signal"],
            r
        )) for r in conn.execute("""
            SELECT sector, ROUND(net_flow_pct, 2), inflow_streak, ticker_count,
                   CASE WHEN net_flow_pct > 0 THEN 'INFLOW' ELSE 'OUTFLOW' END
            FROM agg_sector_rotation
            WHERE report_quarter = ?
            ORDER BY net_flow_pct DESC
        """, [quarter]).fetchall()]

        return {"freshness": _freshness(conn, quarter), "sectors": sectors}
    except Exception as e:
        return {"error": str(e), "sectors": []}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. Sector Strength
# ---------------------------------------------------------------------------

def get_sector_strength_data() -> dict:
    conn = _conn()
    if not conn:
        return {"error": "DB unavailable"}
    try:
        sectors = [dict(zip(
            ["sector", "breadth_pct", "avg_rsi", "above_200sma_pct", "avg_momentum", "tickers"],
            r
        )) for r in conn.execute("""
            SELECT iss.sector,
                   ROUND(AVG(CASE WHEN sf.close > sf.sma_20 THEN 1.0 ELSE 0.0 END) * 100, 1),
                   ROUND(AVG(sf.rsi_14), 1),
                   ROUND(AVG(CASE WHEN sf.price_vs_sma200_pct > 0 THEN 1.0 ELSE 0.0 END) * 100, 1),
                   ROUND(AVG(sf.roc_20), 2),
                   COUNT(*)
            FROM fact_swing_features sf
            JOIN dim_issuer iss ON sf.ticker = iss.ticker
            WHERE sf.trade_date = (SELECT MAX(trade_date) FROM fact_swing_features)
              AND iss.sector IS NOT NULL AND iss.sector != ''
            GROUP BY iss.sector
            HAVING COUNT(*) >= 5
            ORDER BY AVG(CASE WHEN sf.close > sf.sma_20 THEN 1.0 ELSE 0.0 END) DESC
        """).fetchall()]

        return {"freshness": "Latest swing features", "sectors": sectors}
    except Exception as e:
        return {"error": str(e), "sectors": []}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. Top Stocks by Sector
# ---------------------------------------------------------------------------

def get_top_by_sector_data(sector: str = None, quarter: str = None) -> dict:
    conn = _conn()
    if not conn:
        return {"error": "DB unavailable"}
    try:
        if not quarter:
            quarter = get_active_quarter(conn)

        # Available sectors
        avail = [r[0] for r in conn.execute("""
            SELECT DISTINCT iss.sector FROM intelligence_scores i
            JOIN dim_issuer iss ON i.ticker = iss.ticker
            WHERE i.report_quarter = ? AND iss.sector IS NOT NULL AND iss.sector != ''
              AND i.conviction_score >= 50
            ORDER BY iss.sector
        """, [quarter]).fetchall()]

        # Stocks for selected sector
        stocks = []
        if sector:
            stocks = [dict(zip(
                ["ticker", "conviction", "phase", "ml_v2", "pressure", "squeeze", "options"],
                r
            )) for r in conn.execute("""
                SELECT i.ticker, ROUND(i.conviction_score, 1), i.accum_phase,
                       ROUND(COALESCE(i.ml_score_v2, 0), 1),
                       ROUND(COALESCE(i.institutional_pressure, 0), 1),
                       ROUND(COALESCE(i.squeeze_score, 0), 1),
                       CASE WHEN EXISTS (
                           SELECT 1 FROM fact_options_contracts oc
                           WHERE oc.underlying = i.ticker AND oc.open_interest >= 100
                       ) THEN 'Yes' ELSE '' END
                FROM intelligence_scores i
                JOIN dim_issuer iss ON i.ticker = iss.ticker
                WHERE i.report_quarter = ? AND iss.sector = ?
                  AND i.data_quality_score >= 50 AND i.conviction_score >= 40
                ORDER BY i.conviction_score DESC LIMIT 30
            """, [quarter, sector]).fetchall()]

        return {
            "freshness": _freshness(conn, quarter),
            "available_sectors": avail,
            "stocks": stocks,
        }
    except Exception as e:
        return {"error": str(e), "available_sectors": [], "stocks": []}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. Theme Tracker
# ---------------------------------------------------------------------------

# Explicit theme membership rules
THEMES = {
    "AI / Machine Learning": ["NVDA", "MSFT", "GOOGL", "META", "AMD", "PLTR", "CRM", "SNOW", "AI", "PATH", "DDOG", "MDB", "SMCI"],
    "Semiconductors": ["NVDA", "AMD", "INTC", "AVGO", "QCOM", "TXN", "AMAT", "LRCX", "KLAC", "MRVL", "ON", "NXPI", "MU", "STX"],
    "Defense / Aerospace": ["LMT", "RTX", "NOC", "GD", "LHX", "BA", "HII", "KTOS", "LDOS"],
    "Cybersecurity": ["CRWD", "PANW", "ZS", "FTNT", "S", "NET", "QLYS", "TENB"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG", "PXD", "DVN", "OXY", "HAL", "BKR"],
    "Obesity / GLP-1": ["LLY", "NVO", "AMGN", "VKTX", "GPCR"],
    "Uranium / Nuclear": ["CCJ", "UEC", "DNN", "NXE", "LEU", "OKLO"],
    "Shipping": ["ZIM", "SBLK", "DAC", "GOGL", "EGLE", "GNK"],
    "Insurers": ["PGR", "ALL", "TRV", "CB", "AIG", "MET", "PRU", "AFL"],
    "Rate-Sensitive / REIT": ["O", "WELL", "AMT", "PLD", "SPG", "PSA", "EQR", "AVB", "NEE"],
}


def get_theme_data(quarter: str = None) -> dict:
    conn = _conn()
    if not conn:
        return {"error": "DB unavailable"}
    try:
        if not quarter:
            quarter = get_active_quarter(conn)

        themes = []
        for theme_name, members in THEMES.items():
            placeholders = ",".join(["?"] * len(members))
            rows = conn.execute(f"""
                SELECT ticker, conviction_score, accum_phase
                FROM intelligence_scores
                WHERE report_quarter = ? AND ticker IN ({placeholders})
                  AND data_quality_score >= 50
                ORDER BY conviction_score DESC
            """, [quarter] + members).fetchall()

            if not rows:
                continue

            in_accum = sum(1 for r in rows if r[2] in ("ACTIVE_ACCUM", "EARLY_ACCUM", "LATE_ACCUM"))
            leaders = ", ".join(r[0] for r in rows[:3])
            avg_conv = sum(r[1] or 0 for r in rows) / len(rows) if rows else 0

            strength = "Strong" if in_accum >= 3 else "Moderate" if in_accum >= 1 else "Weak"
            trend = "Accumulating" if in_accum > len(rows) * 0.4 else "Mixed" if in_accum > 0 else "No Signal"

            themes.append({
                "theme": theme_name,
                "strength": strength,
                "leaders": leaders,
                "breadth": f"{in_accum}/{len(rows)}",
                "trend": trend,
            })

        themes.sort(key=lambda x: {"Strong": 0, "Moderate": 1, "Weak": 2}.get(x["strength"], 3))

        return {"freshness": _freshness(conn, quarter), "themes": themes}
    except Exception as e:
        return {"error": str(e), "themes": []}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7. Market Drivers
# ---------------------------------------------------------------------------

def get_market_drivers_data(quarter: str = None) -> dict:
    conn = _conn()
    if not conn:
        return {"error": "DB unavailable"}
    try:
        if not quarter:
            quarter = get_active_quarter(conn)

        # Top pressure names
        pressure = [dict(zip(
            ["ticker", "squeeze", "short_squeeze", "dark_pool_pct", "svr_trend", "ctb"],
            r
        )) for r in conn.execute("""
            SELECT ticker, ROUND(COALESCE(squeeze_score, 0), 1),
                   ROUND(COALESCE(short_squeeze_score, 0), 1),
                   ROUND(COALESCE(dark_pool_pct_avg, 0), 1),
                   ROUND(COALESCE(short_volume_ratio_trend, 0), 2),
                   NULL
            FROM intelligence_scores
            WHERE report_quarter = ? AND data_quality_score >= 50
              AND COALESCE(squeeze_score, 0) >= 50
            ORDER BY squeeze_score DESC LIMIT 20
        """, [quarter]).fetchall()]

        # Recent catalysts (Form 4 + 8-K)
        catalysts = []
        try:
            f4 = conn.execute("""
                SELECT ticker, 'Insider Buy' as type, transaction_date, insider_name
                FROM fact_form4_transactions
                WHERE transaction_code = 'P'
                  AND transaction_date >= CURRENT_DATE - INTERVAL '7' DAY
                ORDER BY transaction_date DESC LIMIT 15
            """).fetchall()
            for r in f4:
                catalysts.append({"ticker": r[0], "catalyst_type": r[1], "date": str(r[2]), "detail": r[3]})
        except Exception:
            pass

        try:
            events = conn.execute("""
                SELECT ticker, form_type, filing_date, description
                FROM fact_form8k_events
                WHERE filing_date >= CURRENT_DATE - INTERVAL '7' DAY
                ORDER BY filing_date DESC LIMIT 15
            """).fetchall()
            for r in events:
                catalysts.append({"ticker": r[0], "catalyst_type": f"8-K ({r[1]})", "date": str(r[2]), "detail": str(r[3])[:80]})
        except Exception:
            pass

        return {
            "freshness": _freshness(conn, quarter),
            "pressure": pressure,
            "catalysts": catalysts[:20],
        }
    except Exception as e:
        return {"error": str(e), "pressure": [], "catalysts": []}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 8. Mean Reversion
# ---------------------------------------------------------------------------

def get_mean_reversion_data() -> dict:
    conn = _conn()
    if not conn:
        return {"error": "DB unavailable"}
    try:
        stocks = [dict(zip(
            ["ticker", "verdict", "price_vs_20sma", "price_vs_50sma", "price_vs_200sma", "rsi_14"],
            r
        )) for r in conn.execute("""
            SELECT sf.ticker,
                   CASE
                     WHEN sf.rsi_14 < 30 AND sf.price_vs_sma200_pct > 0 THEN 'Oversold in Uptrend'
                     WHEN sf.rsi_14 > 70 AND sf.price_vs_sma200_pct > 10 THEN 'Extended'
                     WHEN sf.rsi_14 > 70 AND sf.price_vs_sma200_pct < 0 THEN 'Overbought in Weak Trend'
                     WHEN sf.rsi_14 < 30 AND sf.price_vs_sma200_pct < -10 THEN 'Oversold in Downtrend'
                     ELSE 'Neutral'
                   END,
                   ROUND(COALESCE((sf.close - sf.sma_20) / NULLIF(sf.sma_20, 0) * 100, 0), 1),
                   ROUND(sf.price_vs_sma50_pct, 1),
                   ROUND(sf.price_vs_sma200_pct, 1),
                   ROUND(sf.rsi_14, 1)
            FROM fact_swing_features sf
            WHERE sf.trade_date = (SELECT MAX(trade_date) FROM fact_swing_features)
              AND (sf.rsi_14 < 30 OR sf.rsi_14 > 70)
            ORDER BY ABS(sf.rsi_14 - 50) DESC
            LIMIT 30
        """).fetchall()]

        # Market summary
        market = ""
        try:
            spy = conn.execute("""
                SELECT rsi_14, price_vs_sma200_pct, price_vs_sma50_pct
                FROM fact_swing_features
                WHERE ticker = 'SPY'
                ORDER BY trade_date DESC LIMIT 1
            """).fetchone()
            if spy:
                rsi = spy[0] or 50
                vs200 = spy[1] or 0
                if rsi < 30:
                    market = f"Market oversold (RSI {rsi:.0f}). {'+' if vs200 > 0 else ''}{vs200:.1f}% vs 200SMA."
                elif rsi > 70:
                    market = f"Market extended (RSI {rsi:.0f}). {'+' if vs200 > 0 else ''}{vs200:.1f}% vs 200SMA."
                else:
                    market = f"Market neutral (RSI {rsi:.0f}). {'+' if vs200 > 0 else ''}{vs200:.1f}% vs 200SMA."
        except Exception:
            pass

        return {"freshness": "Latest swing features", "stocks": stocks, "market_summary": market}
    except Exception as e:
        return {"error": str(e), "stocks": [], "market_summary": ""}
    finally:
        conn.close()
