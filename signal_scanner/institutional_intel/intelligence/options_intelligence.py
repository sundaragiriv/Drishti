"""Options Intelligence — IV/skew analysis + options expression engine.

Reads from fact_options_contracts to compute:
  - ATM IV per underlying
  - Call-put skew
  - Near/far term structure
  - OI concentration (walls)
  - Best option expressions for stock ideas

Usage:
    engine = OptionsIntelligence(conn)
    summary = engine.get_underlying_summary("AAPL")
    expressions = engine.recommend_expressions("AAPL", "LONG", target_delta=0.40)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger


class OptionsIntelligence:
    """Options analysis engine reading from fact_options_contracts."""

    def __init__(self, conn):
        self._conn = conn

    def get_underlying_summary(self, underlying: str) -> Dict[str, Any]:
        """Get IV/skew/OI summary for an underlying.

        Returns dict with ATM IV, skew, term structure, OI walls.
        """
        try:
            rows = self._conn.execute("""
                SELECT contract_type, expiration_date, strike_price,
                       open_interest, volume, implied_volatility,
                       delta, bid, ask, last_price, snapshot_date
                FROM fact_options_contracts
                WHERE underlying = ?
                  AND snapshot_date = (SELECT MAX(snapshot_date)
                                       FROM fact_options_contracts WHERE underlying = ?)
                ORDER BY open_interest DESC
            """, [underlying, underlying]).fetchall()
        except Exception as e:
            logger.debug("Options summary query error: {}", e)
            return {}

        if not rows:
            return {"underlying": underlying, "has_data": False}

        calls = [r for r in rows if r[0] == "call"]
        puts = [r for r in rows if r[0] == "put"]

        # ATM IV — contracts with delta closest to 0.50 / -0.50
        atm_call_iv = self._find_atm_iv(calls, target_delta=0.50)
        atm_put_iv = self._find_atm_iv(puts, target_delta=-0.50)
        atm_iv = atm_call_iv if atm_call_iv else atm_put_iv

        # Call-put skew
        skew = None
        if atm_call_iv and atm_put_iv:
            skew = round(atm_put_iv - atm_call_iv, 4)

        # Near-term vs far-term IV spread
        near_iv, far_iv = self._term_structure_ivs(rows)
        term_spread = round(near_iv - far_iv, 4) if near_iv and far_iv else None

        # Total OI
        total_call_oi = sum(r[3] for r in calls)
        total_put_oi = sum(r[3] for r in puts)
        put_call_ratio = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else None

        # OI walls — strikes with highest concentration
        call_wall = max(calls, key=lambda r: r[3]) if calls else None
        put_wall = max(puts, key=lambda r: r[3]) if puts else None

        # Total volume
        total_volume = sum(r[4] for r in rows)

        return {
            "underlying": underlying,
            "has_data": True,
            "snapshot_date": str(rows[0][10]) if rows else None,
            "total_contracts": len(rows),
            "calls": len(calls),
            "puts": len(puts),
            # IV
            "atm_iv": round(atm_iv, 4) if atm_iv else None,
            "atm_call_iv": round(atm_call_iv, 4) if atm_call_iv else None,
            "atm_put_iv": round(atm_put_iv, 4) if atm_put_iv else None,
            "call_put_skew": skew,
            "term_spread": term_spread,
            "near_term_iv": round(near_iv, 4) if near_iv else None,
            "far_term_iv": round(far_iv, 4) if far_iv else None,
            # OI
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "put_call_ratio": put_call_ratio,
            "total_volume": total_volume,
            # Walls
            "call_wall_strike": call_wall[2] if call_wall else None,
            "call_wall_oi": call_wall[3] if call_wall else None,
            "put_wall_strike": put_wall[2] if put_wall else None,
            "put_wall_oi": put_wall[3] if put_wall else None,
        }

    def recommend_expressions(
        self,
        underlying: str,
        direction: str = "LONG",
        target_delta: float = 0.40,
        min_oi: int = 100,
        max_spread_pct: float = 15.0,
        max_results: int = 5,
    ) -> List[Dict[str, Any]]:
        """Recommend best option expressions for a stock idea.

        Scores contracts by: liquidity, delta fit, IV sanity, spread quality.

        Args:
            underlying: stock ticker
            direction: LONG (buy calls) or SHORT (buy puts)
            target_delta: ideal delta magnitude (0.30-0.50 typical)
            min_oi: minimum open interest
            max_spread_pct: max bid-ask spread as % of midpoint
            max_results: number of recommendations

        Returns: list of scored contract recommendations.
        """
        contract_type = "call" if direction == "LONG" else "put"

        try:
            rows = self._conn.execute("""
                SELECT contract_ticker, contract_type, expiration_date,
                       strike_price, bid, ask, midpoint, last_price,
                       volume, open_interest, implied_volatility,
                       delta, gamma, theta, vega, snapshot_date
                FROM fact_options_contracts
                WHERE underlying = ?
                  AND contract_type = ?
                  AND open_interest >= ?
                  AND snapshot_date = (SELECT MAX(snapshot_date)
                                       FROM fact_options_contracts WHERE underlying = ?)
                ORDER BY open_interest DESC
            """, [underlying, contract_type, min_oi, underlying]).fetchall()
        except Exception:
            return []

        if not rows:
            return []

        candidates = []
        for r in rows:
            (ticker, ctype, expiry, strike, bid, ask, mid, last,
             vol, oi, iv, delta_val, gamma, theta, vega, snap) = r

            # Spread quality
            bid = bid or 0
            ask = ask or 0
            mid = mid or ((bid + ask) / 2 if bid and ask else last or 0)
            spread_pct = ((ask - bid) / mid * 100) if mid > 0 else 999

            if spread_pct > max_spread_pct:
                continue

            # Delta fit (how close to target)
            abs_delta = abs(delta_val) if delta_val else 0
            delta_fit = 1.0 - min(1.0, abs(abs_delta - target_delta) / 0.3)

            # Liquidity score (OI + volume)
            liquidity = min(1.0, (oi / 5000) * 0.7 + (vol / 1000) * 0.3)

            # Spread quality score
            spread_score = max(0, 1.0 - spread_pct / max_spread_pct)

            # Days to expiry
            try:
                dte = (datetime.strptime(str(expiry), "%Y-%m-%d") - datetime.now()).days
            except Exception:
                dte = 30

            # Expiry fit (prefer 14-45 DTE for swing)
            expiry_fit = 1.0
            if dte < 7:
                expiry_fit = 0.3  # too short
            elif dte < 14:
                expiry_fit = 0.7
            elif dte > 90:
                expiry_fit = 0.5  # too far

            # Composite score
            score = round(
                delta_fit * 30 + liquidity * 30 + spread_score * 20 + expiry_fit * 20,
                1,
            )

            candidates.append({
                "contract_ticker": ticker,
                "contract_type": ctype,
                "expiry": str(expiry),
                "strike": strike,
                "dte": dte,
                "bid": round(bid, 2),
                "ask": round(ask, 2),
                "mid": round(mid, 2),
                "volume": vol,
                "open_interest": oi,
                "iv": round(iv, 3) if iv else None,
                "delta": round(delta_val, 3) if delta_val else None,
                "theta": round(theta, 3) if theta else None,
                "spread_pct": round(spread_pct, 1),
                "score": score,
                "liquidity": round(liquidity, 2),
                "snapshot_date": str(snap),
            })

        # Sort by score descending
        candidates.sort(key=lambda x: -x["score"])
        return candidates[:max_results]

    def get_oi_changes(self, underlying: str, days: int = 5) -> List[Dict]:
        """Get recent OI changes for an underlying's contracts."""
        try:
            rows = self._conn.execute("""
                SELECT contract_ticker, contract_type, strike_price,
                       expiration_date, snapshot_date, open_interest,
                       oi_change, oi_change_pct
                FROM fact_options_oi_history
                WHERE underlying = ?
                  AND oi_change IS NOT NULL
                  AND ABS(oi_change) > 0
                ORDER BY ABS(oi_change) DESC
                LIMIT 20
            """, [underlying]).fetchall()
            return [dict(zip(
                ["contract", "type", "strike", "expiry", "date", "oi", "change", "change_pct"],
                r
            )) for r in rows]
        except Exception:
            return []

    # --- Internal helpers ---

    def _find_atm_iv(self, contracts: list, target_delta: float) -> Optional[float]:
        """Find IV of contract closest to target delta."""
        best = None
        best_dist = 999
        for r in contracts:
            delta = r[6]  # delta column
            iv = r[5]     # iv column
            if delta is None or iv is None:
                continue
            dist = abs(abs(delta) - abs(target_delta))
            if dist < best_dist:
                best_dist = dist
                best = iv
        return best

    def _term_structure_ivs(self, rows: list):
        """Get near-term and far-term average IVs."""
        now = datetime.now()
        near_ivs = []
        far_ivs = []
        for r in rows:
            expiry = r[1]  # expiration_date
            iv = r[5]      # implied_volatility
            if iv is None:
                continue
            try:
                exp_date = datetime.strptime(str(expiry), "%Y-%m-%d")
                dte = (exp_date - now).days
                if 7 <= dte <= 30:
                    near_ivs.append(iv)
                elif 60 <= dte <= 120:
                    far_ivs.append(iv)
            except Exception:
                continue

        near = sum(near_ivs) / len(near_ivs) if near_ivs else None
        far = sum(far_ivs) / len(far_ivs) if far_ivs else None
        return near, far
