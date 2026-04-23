"""ISR Recommendation Engine — computes verdict, reasons, risks, scorecard.

Takes intelligence data for a ticker and produces:
  - Verdict: Strong Buy / Buy / Watch / Neutral / Avoid / Strong Avoid
  - Confidence: High / Medium / Low
  - Horizon: Intraday / Swing 5D / Swing Thesis
  - Why Now: 3-5 strongest current reasons
  - What Weakens It: 2-4 current risks
  - Scorecard: component scores
  - Action Panel: entry/stop/targets/R:R
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def compute_recommendation(intel: Dict[str, Any], ticker: str) -> Dict[str, Any]:
    """Compute full recommendation from intelligence data.

    Args:
        intel: intelligence_scores row as dict
        ticker: stock symbol

    Returns: recommendation dict with verdict, reasons, risks, scorecard, action.
    """
    # Extract key metrics (with safe defaults)
    conv = _s(intel.get("conviction_score"), 0)
    phase = str(intel.get("accum_phase") or "DORMANT")
    ml = _s(intel.get("ml_score_v2"), 0)
    squeeze = _s(intel.get("squeeze_score"), 0)
    short_sq = _s(intel.get("short_squeeze_score"), 0)
    pressure = _s(intel.get("institutional_pressure"), 0)
    insider = _s(intel.get("insider_effect_score"), 0)
    insider_cluster = bool(intel.get("insider_cluster_detected"))
    trend = _s(intel.get("trend_score"), 0)
    momentum = _s(intel.get("price_momentum_90d"), 0)
    above_200 = bool(intel.get("price_above_200sma"))
    quality = _s(intel.get("data_quality_score"), 0)
    triple_lock = bool(intel.get("triple_lock"))
    swing_signal = str(intel.get("swing_signal") or "WATCH")
    short_signal = str(intel.get("short_swing_signal") or "")
    dist_warning = bool(intel.get("distribution_warning"))

    # --- Scorecard (0-100 per component) ---
    thesis_strength = min(100, conv * 1.2) if conv > 0 else 0
    setup_quality = _setup_score(phase, conv, above_200, momentum)
    predictive_edge = 0  # v1 model failed validation — no predictive score
    pressure_score = min(100, squeeze * 0.5 + pressure * 0.3 + (20 if insider_cluster else 0))
    sector_strength = min(100, trend * 1.5) if trend > 0 else 0
    risk_liquidity = min(100, quality)

    # Interconnected: from fact_interconnected_features if available
    interconnected = 0
    try:
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        _ic = safe_duckdb_connect(read_only=True)
        if _ic:
            try:
                row = _ic.execute("""
                    SELECT peer_count, peers_in_accum, sector_breadth_20d
                    FROM fact_interconnected_features
                    WHERE ticker = ?
                    ORDER BY trade_date DESC LIMIT 1
                """, [ticker]).fetchone()
                if row:
                    pc = row[0] or 0
                    pa = row[1] or 0
                    sb = (row[2] or 0) * 100
                    interconnected = min(100, pc * 2 + pa * 15 + sb * 0.3)
            finally:
                _ic.close()
    except Exception:
        pass

    # Options Quality: from fact_options_contracts if available
    options_quality = 0
    try:
        _oc = safe_duckdb_connect(read_only=True)
        if _oc:
            try:
                orow = _oc.execute("""
                    SELECT COUNT(*) as contracts,
                           SUM(open_interest) as total_oi,
                           AVG(CASE WHEN bid > 0 AND ask > 0 THEN (ask - bid) / ((ask + bid) / 2) * 100 END) as avg_spread
                    FROM fact_options_contracts
                    WHERE underlying = ?
                    AND snapshot_date = (SELECT MAX(snapshot_date) FROM fact_options_contracts WHERE underlying = ?)
                    AND open_interest >= 50
                """, [ticker, ticker]).fetchone()
                if orow and orow[0] and orow[0] > 0:
                    contracts = orow[0]
                    total_oi = orow[1] or 0
                    avg_spread = orow[2] or 50
                    options_quality = min(100,
                        min(30, contracts * 0.3) +
                        min(40, total_oi / 1000) +
                        max(0, 30 - avg_spread)
                    )
            finally:
                _oc.close()
    except Exception:
        pass

    scorecard = {
        "Thesis Strength": round(thesis_strength),
        "Setup Quality": round(setup_quality),
        "Predictive Edge": round(predictive_edge),
        "Pressure / Positioning": round(pressure_score),
        "Sector / Theme": round(sector_strength),
        "Interconnected": round(interconnected),
        "Options Quality": round(options_quality),
        "Risk / Liquidity": round(risk_liquidity),
    }

    # --- Composite score ---
    composite = (
        thesis_strength * 0.30
        + setup_quality * 0.25
        + pressure_score * 0.15
        + sector_strength * 0.10
        + risk_liquidity * 0.10
        + predictive_edge * 0.10
    )

    # --- Verdict ---
    verdict, confidence = _compute_verdict(
        composite, conv, phase, swing_signal, short_signal,
        triple_lock, dist_warning, above_200, ml,
    )

    # --- Horizon ---
    # Intraday: high squeeze + above 200SMA + bullish setup = short-term play
    if squeeze >= 70 and above_200 and conv >= 50:
        horizon = "Intraday"
    elif phase in ("ACTIVE_ACCUM", "LATE_ACCUM", "EARLY_ACCUM"):
        horizon = "Swing Thesis"
    elif swing_signal == "BUY" and conv >= 55:
        horizon = "Swing 5D"
    else:
        horizon = "Swing Thesis"

    # --- Why Now (3-5 strongest reasons) ---
    why_now = _build_why_now(intel)

    # --- What Weakens It (2-4 risks) ---
    weakens = _build_weakens(intel)

    return {
        "verdict": verdict,
        "confidence": confidence,
        "horizon": horizon,
        "composite_score": round(composite, 1),
        "scorecard": scorecard,
        "why_now": why_now,
        "weakens": weakens,
    }


def _compute_verdict(
    composite: float, conv: float, phase: str, swing: str, short: str,
    triple: bool, dist_warn: bool, above_200: bool, ml: float,
) -> Tuple[str, str]:
    """Map composite + signals to verdict + confidence."""
    if triple and conv >= 70 and phase in ("ACTIVE_ACCUM", "LATE_ACCUM"):
        return "Strong Buy", "High"

    if dist_warn and short == "SHORT":
        if conv <= 30:
            return "Strong Avoid", "High"
        return "Avoid", "Medium"

    if swing == "BUY":
        if composite >= 70 and conv >= 65:
            return "Buy", "High" if ml >= 60 else "Medium"
        if composite >= 50 and conv >= 55:
            return "Buy", "Medium" if ml >= 40 else "Low"
        return "Watch", "Low"

    if swing == "WATCH":
        if composite >= 60:
            return "Watch", "Medium"
        return "Neutral", "Low"

    if swing == "AVOID" or phase in ("DISTRIBUTION", "DECLINE"):
        return "Avoid", "Medium"

    return "Neutral", "Low"


def _setup_score(phase: str, conv: float, above_200: bool, momentum: float) -> float:
    """Score current setup quality."""
    score = 0
    if phase in ("ACTIVE_ACCUM", "LATE_ACCUM"):
        score += 40
    elif phase == "EARLY_ACCUM":
        score += 25
    elif phase == "EXPANSION":
        score += 15

    if conv >= 70:
        score += 25
    elif conv >= 55:
        score += 15

    if above_200:
        score += 15

    if momentum and momentum > 0:
        score += min(20, momentum * 2)

    return min(100, score)


def _build_why_now(intel: Dict) -> List[str]:
    """Build 3-5 strongest current reasons in plain language."""
    reasons = []
    conv = _s(intel.get("conviction_score"), 0)
    phase = str(intel.get("accum_phase") or "")
    ml = _s(intel.get("ml_score_v2"), 0)

    if phase in ("ACTIVE_ACCUM", "LATE_ACCUM"):
        reasons.append(f"Institutional Accumulation ({phase.replace('_', ' ').title()})")
    elif phase == "EARLY_ACCUM":
        reasons.append("Early Institutional Accumulation")

    if conv >= 70:
        reasons.append(f"High Conviction ({conv:.0f})")

    if bool(intel.get("triple_lock")):
        reasons.append("Triple Lock Convergence (Conv + ML + Insiders)")

    if bool(intel.get("insider_cluster_detected")):
        reasons.append("Insider Buying Cluster Detected")
    elif _s(intel.get("insider_effect_score"), 0) >= 30:
        reasons.append(f"Insider Activity (effect score {_s(intel.get('insider_effect_score'), 0):.0f})")

    if _s(intel.get("squeeze_score"), 0) >= 60:
        reasons.append(f"Short Squeeze Pressure ({_s(intel.get('squeeze_score'), 0):.0f})")

    if ml >= 70:
        reasons.append(f"ML Score Top Tier ({ml:.0f})")

    if bool(intel.get("price_above_200sma")):
        reasons.append("Above 200-day Moving Average")

    if _s(intel.get("institutional_pressure"), 0) >= 60:
        reasons.append(f"Strong Institutional Pressure ({_s(intel.get('institutional_pressure'), 0):.0f})")

    return reasons[:5]


def _build_weakens(intel: Dict) -> List[str]:
    """Build 2-4 current risks in plain language."""
    risks = []
    phase = str(intel.get("accum_phase") or "")
    conv = _s(intel.get("conviction_score"), 0)

    if phase in ("DISTRIBUTION", "DECLINE"):
        risks.append(f"Distribution Phase ({phase.replace('_', ' ').title()})")

    if bool(intel.get("distribution_warning")):
        risks.append("Distribution Warning Active")

    if not bool(intel.get("price_above_200sma")):
        risks.append("Below 200-day Moving Average")

    if _s(intel.get("price_momentum_90d"), 0) < -10:
        risks.append(f"Negative Momentum ({_s(intel.get('price_momentum_90d'), 0):.0f}% in 90d)")

    if conv < 50:
        risks.append(f"Low Conviction ({conv:.0f})")

    if phase == "DORMANT":
        risks.append("No Institutional Accumulation Signal")

    if _s(intel.get("data_quality_score"), 0) < 60:
        risks.append("Low Data Quality / Sparse Coverage")

    if _s(intel.get("squeeze_score"), 0) >= 70 and phase not in ("ACTIVE_ACCUM", "LATE_ACCUM"):
        risks.append("High Short Pressure Without Accumulation Support")

    return risks[:4]


def _s(val, default=0) -> float:
    """Safe float conversion."""
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default
