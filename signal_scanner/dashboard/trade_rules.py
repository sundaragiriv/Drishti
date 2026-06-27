"""Per-page "Rules for Taking a Trade" tooltips.

The rules below mirror the system's ACTUAL entry gates. Keep in sync with:
  - Swing / Triple Lock .... signal_scanner/paper/idea_bridge.py
  - Intraday ML ............ signal_scanner/paper/{vwap_mr_live,fpb_live,orb_v2_live}.py
  - Options ................ signal_scanner/.../option_setup_engine.py
  - Regime gating .......... signal_scanner/institutional_intel/intelligence/regime_hmm.py
  - Global risk/position ... signal_scanner/paper/paper_trader.py

Usage in a layout header:
    from signal_scanner.dashboard.trade_rules import rules_tooltip
    html.H2(["Page Title", rules_tooltip("swing")])
"""

import dash_bootstrap_components as dbc
from dash import html

# page_key -> (heading, [checklist lines])
TRADE_RULES: dict[str, tuple[str, list[str]]] = {
    "swing": (
        "Swing (multi-day) — entry checklist",
        [
            "Regime allows LONG — never enter in CRASH (state 0)",
            "Conviction ≥ 65 (Triple Lock path needs ≥ 70)",
            "Phase is EARLY / ACTIVE / LATE ACCUM only",
            "LONG only if price > 200-day SMA (Triple Lock can override)",
            "Triple Lock = Conv ≥70 + ML v2 ≥70 + Form-4 insider buy + BUY",
            "Risk: stop = entry − 2× ATR; primary target 1R",
            "One entry per symbol per day; max 3 concurrent positions",
        ],
    ),
    "intraday": (
        "Intraday ML — entry checklist",
        [
            "Regime must allow trading — no entries in CRASH (state 0)",
            "All ML setups: ML prob ≥ 0.50 AND percentile ≥ 80 (top 20%)",
            "Phase EARLY / ACTIVE / LATE ACCUM",
            "VWAP_MR: dip >0.3% below VWAP, vol >1.2× avg, entry 10:00–11:30 ET (conv ≥65)",
            "FPB: pull back to opening-range high then close above it, entry 9:45–11:30 ET",
            "ORB_V2: close > OR-high + body >0.5 + wick <0.3 + vol >1.5× + price > VWAP, 9:50–10:30 ET",
            "Stops structure/ATR-based; trail after 1R. Caps: 15 open & 20 entries/day per strategy",
        ],
    ),
    "options": (
        "Options — entry checklist",
        [
            "Idea score ≥ ~72 and R:R ≥ 1.6",
            "Contract matches direction: CALL = bullish/LONG, PUT = bearish/SHORT",
            "GEX gate: CALL needs above-zero gamma; PUT needs below-zero gamma",
            "Regime gate: RISK-OFF blocks CALLs; RISK-ON blocks PUTs",
            "Prefer ACTIVE / STRONG ideas (validated repeatedly); skip INVALID",
            "Confirm the underlying's conviction + accumulation phase first",
        ],
    ),
    "intelligence": (
        "Intelligence — how to act",
        [
            "This page is conviction & evidence context, not a direct entry trigger",
            "Look for Conviction ≥ 65 + accumulation phase + insider/institutional confirmation",
            "Triple Lock (Conv ≥70 + ML v2 ≥70 + insider buy) = strongest setup",
            "Confirm the regime allows your direction before acting",
            "Take the actual entry on the Swing or Intraday page with a defined stop + target",
        ],
    ),
    "forecast": (
        "Forecast — how to act",
        [
            "Forecast is a probabilistic model outlook (ML v2 + HMM), not a standalone signal",
            "Use it as directional bias / context — not a reason to trade alone",
            "Require a concrete setup (Swing / Intraday / Options) + regime agreement to act",
            "No model retraining during the prove-it window — this is honest OOS evidence",
            "Size to risk; never trade on a prediction alone",
        ],
    ),
    "performance": (
        "Risk & position rules",
        [
            "Max 3 concurrent positions; one entry per symbol per day",
            "Skip new entries after 3+ losses in the last 5 trades (loss-cluster gate)",
            "No new entries after the late-entry cutoff (~3:30 PM ET)",
            "Honor the daily drawdown cap and the global open-risk cap",
            "Let winners run to target / trailing stop; cut at stop, never average down",
        ],
    ),
}

_BADGE_STYLE = {
    "display": "inline-flex",
    "alignItems": "center",
    "gap": "5px",
    "marginLeft": "12px",
    "padding": "3px 10px",
    "borderRadius": "12px",
    "fontSize": "0.68rem",
    "fontWeight": "600",
    "letterSpacing": "0.04em",
    "color": "#ffd43b",
    "backgroundColor": "rgba(255,212,59,0.08)",
    "border": "1px solid rgba(255,212,59,0.35)",
    "cursor": "help",
    "textTransform": "uppercase",
    "verticalAlign": "middle",
    "whiteSpace": "nowrap",
}

_TOOLTIP_STYLE = {
    "maxWidth": "470px",
    "textAlign": "left",
    "backgroundColor": "#0d1117",
    "border": "1px solid #30363d",
    "borderRadius": "8px",
    "padding": "10px 12px",
    "fontSize": "0.78rem",
    "lineHeight": "1.5",
    "opacity": "1",
}


# ---------------------------------------------------------------------------
# Rule-match predicate — decides whether an idea row satisfies the page's
# rules (mirrors the human-readable checklist above). Used to HIGHLIGHT
# rule-compliant ideas in the Sniper / Intraday tables. One source of truth.
# ---------------------------------------------------------------------------
RULE_MATCH_MARK = "✦"  # ✦ shown in the table + used in the highlight filter_query

_ACCUM_PHASES = ("EARLY_ACCUM", "ACTIVE_ACCUM", "LATE_ACCUM")


def _regime_allows(side: str, regime_state) -> bool:
    """HMM gating: 0=CRASH, 1=DISTRIBUTION, 2=ACCUM, 3=MEAN_REV, 4=BULL."""
    if regime_state is None:
        return True  # regime unknown — don't penalize
    if regime_state == 0:
        return False  # CRASH blocks everything
    if side == "LONG":
        return regime_state in (2, 3, 4)
    if side == "SHORT":
        return regime_state in (1, 3)
    return True


def _phase_is_accum(phase) -> bool:
    p = str(phase or "").upper().replace(" ", "_")
    return any(a in p for a in _ACCUM_PHASES)


def matches_rules(page_key: str, row: dict, regime_state=None) -> bool:
    """True when an idea row passes the page's trade rules (see TRADE_RULES).

    `row` is a dashboard idea dict. `regime_state` is the current HMM state
    (int 0-4) or None if unknown.
    """
    side = str(row.get("side", "LONG")).upper()
    if not _regime_allows(side, regime_state):
        return False

    if page_key == "swing":
        if str(row.get("source_badge", "")).upper() == "TRIPLE_LOCK":
            return True  # highest-conviction setup — always compliant
        rr = float(row.get("rr_ratio") or 0)
        if rr < 2.0:
            return False
        phase = row.get("_phase") or row.get("phase") or ""
        if side == "LONG":
            conv = float(row.get("_conviction") or row.get("conviction") or 0)
            return conv >= 65 and _phase_is_accum(phase)
        # SHORT
        sconv = float(row.get("_short_conv") or row.get("_conviction") or 0)
        p = str(phase).upper()
        return sconv >= 55 and ("DISTRIB" in p or "DECLIN" in p)

    if page_key == "intraday":
        conv = float(row.get("conviction") or 0)
        state = str(row.get("state", "")).upper()
        return state == "NEW" and conv >= 65

    return False


def rule_match_mark(page_key: str, row: dict, regime_state=None) -> str:
    """Return the highlight mark (✦) if the row matches, else empty string."""
    return RULE_MATCH_MARK if matches_rules(page_key, row, regime_state) else ""


def rules_tooltip(page_key: str):
    """Return an inline "Rules for taking a trade" badge + hover tooltip.

    Drop the result into a header (e.g. inside an html.H2 children list).
    """
    heading, rules = TRADE_RULES[page_key]
    tip_id = f"rules-tip-{page_key}"
    badge = html.Span(
        [html.I(className="fas fa-circle-info"), "Rules for taking a trade"],
        id=tip_id,
        style=_BADGE_STYLE,
    )
    tooltip = dbc.Tooltip(
        html.Div(
            [
                html.Div(
                    heading,
                    style={"fontWeight": "700", "marginBottom": "6px", "color": "#ffd43b"},
                ),
                html.Ul(
                    [html.Li(r, style={"marginBottom": "3px"}) for r in rules],
                    style={"margin": "0", "paddingLeft": "18px", "color": "#e6edf3"},
                ),
            ]
        ),
        target=tip_id,
        placement="bottom",
        autohide=False,
        style=_TOOLTIP_STYLE,
    )
    return html.Span([badge, tooltip], style={"display": "inline-flex", "alignItems": "center"})
