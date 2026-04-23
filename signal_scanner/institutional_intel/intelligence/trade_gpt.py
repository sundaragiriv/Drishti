"""TradeGPT — Conversational AI for stock analysis with full data context.

Pre-analyzes institutional intelligence data into a structured briefing
before sending to the AI provider. Maintains conversation history for
follow-up questions.

Uses Claude API (claude-sonnet-4-6) by default, falls back to OpenAI/Gemini.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from loguru import logger

# Ensure .env is loaded for API keys
load_dotenv(Path(__file__).resolve().parents[3] / ".env")


TRADE_GPT_SYSTEM = """You are TradeGPT, a senior institutional trading intelligence analyst with 20+ years at top firms. You have access to a pre-analyzed intelligence briefing for each stock that includes:

- Institutional 13F flows (quarterly holder count changes, accumulation streaks)
- Form 4 insider activity (buy/sell clusters, officer-level buying, historical win rates)
- ML model predictions (percentile-ranked, AUC 0.56)
- Phase classification (EARLY_ACCUM → ACTIVE_ACCUM → LATE_ACCUM → EXPANSION → DISTRIBUTION)
- Squeeze detection (short interest, days to cover, dark pool activity)
- Technical alignment (trend score, 200-SMA positioning, momentum)
- Conviction scoring (0-100, multi-factor composite)
- Options-ready signals with entry zones, targets, and stops

Rules:
- Be direct and specific. No hedging ("may", "could", "potentially").
- Always cite specific numbers from the briefing. Never invent figures.
- Give specific price levels, timeframes, and strategies.
- If data is missing, say so. Don't guess.
- Format with markdown: **bold** for key figures, bullet points for lists.
- Keep responses focused — under 250 words unless full analysis requested.
- When asked about entry/exit, give specific levels with risk/reward ratios.
- When asked about options, give strike selection logic, DTE, and strategy type.
- Cross-reference signals: e.g., insider buying + institutional accumulation + technical alignment = high conviction.
- Flag contradictions between signals (e.g., insiders buying but institutions exiting)."""

MAX_HISTORY = 20


def _synthesize_briefing(context: Dict[str, Any]) -> str:
    """Convert raw kubera_context into a structured intelligence briefing.

    This is the key differentiator — instead of dumping raw JSON, we
    pre-analyze the data into actionable intelligence that the AI can
    reason about effectively.
    """
    if not context:
        return "No data available for this ticker."

    ticker = context.get("ticker", "?")
    company = context.get("company", ticker)
    sector = context.get("sector", "Unknown")
    quarter = context.get("analysis_quarter", "?")

    # --- Phase & Conviction ---
    phase = context.get("current_phase", "UNKNOWN")
    conviction = context.get("conviction_score") or 0
    ml_score = context.get("ml_score") or 0
    accum_str = context.get("accumulation_strength") or 0

    # --- Institutional ---
    inst = context.get("institutional_summary", {})
    holders = inst.get("current_holders", 0)
    holder_chg = inst.get("holder_change", 0)
    holder_chg_pct = inst.get("holder_change_pct") or 0
    streak = inst.get("count_up_streak", 0)
    tier1 = inst.get("tier1_holders", 0)
    cascade = inst.get("cascade_stage", 0)
    initiations = inst.get("new_initiations_this_quarter", 0)
    top_mgrs = inst.get("top_5_managers", [])

    # --- Insider ---
    ins = context.get("insider_summary", {})
    cluster = ins.get("cluster_detected", False)
    net_buys = ins.get("net_buy_count_90d", 0)
    ceo_buying = ins.get("ceo_cfo_buying", False)
    insider_effect = ins.get("insider_effect_score") or 0
    insider_wr = ins.get("insider_hist_win_rate_90d") or 0
    insider_alpha = ins.get("insider_hist_alpha_90d") or 0
    recent_txns = ins.get("recent_transactions", [])
    hist_pattern = ins.get("historical_pattern") or {}

    # --- Price ---
    price = context.get("price_summary", {})
    current_px = price.get("current_price") or 0
    ret_30d = price.get("return_30d_pct") or 0
    high_52w = price.get("high_52w") or 0
    low_52w = price.get("low_52w") or 0
    pct_from_high = price.get("pct_from_52w_high") or 0
    pct_from_low = price.get("pct_from_52w_low") or 0

    # --- Trend & Pressure ---
    tp = context.get("trend_and_pressure", {})
    trend = tp.get("trend_score") or 0
    pressure = tp.get("institutional_pressure") or 0

    # --- Squeeze ---
    sq = context.get("short_squeeze_data", {})
    squeeze = sq.get("short_squeeze_score") or 0
    dtc = sq.get("days_to_cover") or 0
    si = sq.get("short_interest") or 0

    # --- Signals ---
    sig = context.get("trading_signals", {})
    swing = sig.get("swing_signal") or "N/A"
    swing_entry = sig.get("swing_entry_zone") or ""
    swing_target = sig.get("swing_target") or ""
    swing_stop = sig.get("swing_stop") or ""
    swing_opts = sig.get("swing_options") or ""
    lt_signal = sig.get("longterm_signal") or "N/A"
    lt_thesis = sig.get("longterm_thesis") or ""
    lt_opts = sig.get("longterm_options") or ""
    day_bias = sig.get("day_bias") or "NEUTRAL"

    # --- Distribution ---
    dist_warning = context.get("distribution_warning", False)
    dist_severity = context.get("distribution_severity")

    # --- Sector ---
    sec = context.get("sector_context", {})
    sec_trend = sec.get("sector_trend", "UNKNOWN")
    sec_flow = sec.get("sector_flow_pct_qoq") or 0

    # --- Lag ---
    lag_est = context.get("lag_estimate", "Unknown")
    lag_conf = context.get("lag_confidence", "LOW")

    # --- History ---
    hist_6q = context.get("accumulation_history_6q", [])

    # ===== BUILD BRIEFING =====

    # Signal alignment assessment
    bull_signals = []
    bear_signals = []
    if streak >= 2:
        bull_signals.append(f"{streak}Q accumulation streak")
    if cluster:
        bull_signals.append("insider cluster buy detected")
    if ceo_buying:
        bull_signals.append("CEO/CFO buying")
    if insider_wr > 55:
        bull_signals.append(f"insider hist win rate {insider_wr}%")
    if trend > 60:
        bull_signals.append(f"strong technical trend ({trend}/100)")
    if ml_score > 70:
        bull_signals.append(f"ML top percentile ({ml_score}/100)")
    if cascade >= 2:
        bull_signals.append(f"cascade stage {cascade} (multi-tier flow)")
    if squeeze > 60:
        bull_signals.append(f"squeeze setup score {squeeze}/100")

    if dist_warning:
        bear_signals.append(f"distribution warning (severity: {dist_severity})")
    if holder_chg < 0:
        bear_signals.append(f"institutions exiting ({holder_chg_pct:+.1f}%)")
    if trend < 30:
        bear_signals.append(f"weak technical trend ({trend}/100)")
    if pct_from_high and pct_from_high < -30:
        bear_signals.append(f"down {pct_from_high:.1f}% from 52w high")

    # Conviction interpretation
    if conviction >= 75:
        conv_label = "VERY HIGH — strong multi-factor alignment"
    elif conviction >= 55:
        conv_label = "MODERATE-HIGH — actionable with proper risk management"
    elif conviction >= 35:
        conv_label = "LOW-MODERATE — watch only, not enough confirmation"
    else:
        conv_label = "LOW — insufficient evidence for directional bias"

    # Top managers summary
    mgr_summary = ""
    if top_mgrs:
        mgr_lines = [f"  - {m.get('name', '?')} (Tier {m.get('tier', '?')}, ${m.get('value_usd_k', 0):,.0f}K)" for m in top_mgrs[:3]]
        mgr_summary = "\n".join(mgr_lines)

    # Recent insider transactions
    txn_summary = ""
    if recent_txns:
        txn_lines = []
        for t in recent_txns[:5]:
            val = (t.get("shares") or 0) * (t.get("price") or 0)
            txn_lines.append(f"  - {t.get('name', '?')} ({t.get('role', '?')}): {t.get('direction', '?')} {t.get('shares', 0):,.0f} shares @ ${t.get('price', 0):.2f} = ${val:,.0f} on {t.get('date', '?')}")
        txn_summary = "\n".join(txn_lines)

    # 6Q history summary
    hist_summary = ""
    if hist_6q:
        hist_lines = [f"  {h.get('quarter', '?')}: {h.get('inst_count', 0)} holders ({h.get('inst_change', 0):+d}), shares {h.get('shares_change_pct', 0):+.1f}%, price {h.get('price_change_pct', 0):+.1f}%" for h in hist_6q]
        hist_summary = "\n".join(hist_lines)

    briefing = f"""=== TRADEGPT INTELLIGENCE BRIEFING: {ticker} ({company}) ===
Quarter: {quarter} | Sector: {sector} | Price: ${current_px:.2f}

--- VERDICT ---
Phase: {phase} ({context.get('phase_quarters', 0)}Q in phase)
Conviction: {conviction}/100 — {conv_label}
ML Score: {ml_score}/100 (percentile rank)
Day Bias: {day_bias} | Swing: {swing} | Long Term: {lt_signal}

--- SIGNAL ALIGNMENT ---
Bullish: {', '.join(bull_signals) if bull_signals else 'None detected'}
Bearish: {', '.join(bear_signals) if bear_signals else 'None detected'}
Net assessment: {len(bull_signals)} bullish vs {len(bear_signals)} bearish signals

--- INSTITUTIONAL FLOWS ---
Current holders: {holders} ({holder_chg:+d}, {holder_chg_pct:+.1f}% QoQ)
Accumulation streak: {streak} consecutive quarters
Tier-1 managers: {tier1} | Cascade stage: {cascade}/3
New initiations: {initiations} this quarter
Accum strength: {accum_str}/100 | Pressure: {pressure}/100
Top holders:
{mgr_summary or '  No data'}

--- INSIDER INTELLIGENCE ---
Net buys (90d): {net_buys} | Cluster: {'YES' if cluster else 'No'} | CEO/CFO buying: {'YES' if ceo_buying else 'No'}
Insider effect score: {insider_effect}/100
Historical win rate (90d): {insider_wr}% | Alpha: {insider_alpha:+.1f}%
Recent transactions:
{txn_summary or '  No recent transactions'}

--- PRICE & TECHNICALS ---
Current: ${current_px:.2f} | 30d return: {ret_30d:+.1f}%
52w range: ${low_52w:.2f} — ${high_52w:.2f} ({pct_from_high:+.1f}% from high, +{pct_from_low:.1f}% from low)
Trend score: {trend}/100 | Sector trend: {sec_trend} ({sec_flow:+.1f}% flow QoQ)

--- SHORT/SQUEEZE DATA ---
Short squeeze score: {squeeze}/100 | Days to cover: {dtc:.1f}
Short interest: {si:,} shares

--- TRADE SIGNALS ---
Swing: {swing} | Entry: {swing_entry} | Target: {swing_target} | Stop: {swing_stop}
Swing options: {swing_opts or 'N/A'}
Long term: {lt_signal} | Thesis: {lt_thesis}
LEAPS: {lt_opts or 'N/A'}
Lag estimate: {lag_est} (confidence: {lag_conf})

--- DISTRIBUTION WARNING ---
{'ACTIVE — ' + str(dist_severity) + ' severity' if dist_warning else 'None'}

--- 6-QUARTER HISTORY ---
{hist_summary or 'No history available'}"""

    return briefing


class TradeGPT:
    """Conversational AI for stock analysis with full data context."""

    def __init__(self) -> None:
        self.conversations: Dict[str, List[Dict[str, str]]] = {}
        self._active_tickers: Dict[str, str] = {}

    def chat(
        self,
        session_id: str,
        user_message: str,
        ticker: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Send a message, get a response, maintain conversation history.

        Args:
            session_id: Unique session identifier.
            user_message: The user's message.
            ticker: Optional ticker to set/change context for.
            context: Stock context dict (from kubera_context.build_stock_context).

        Returns:
            AI response string.
        """
        if not user_message or not user_message.strip():
            return "Please enter a question."

        if session_id not in self.conversations:
            self.conversations[session_id] = []

        history = self.conversations[session_id]

        # If ticker changed or first time, inject synthesized briefing
        if ticker and ticker != self._active_tickers.get(session_id):
            self._active_tickers[session_id] = ticker
            if context and len(context) > 1:
                # Full briefing available
                briefing = _synthesize_briefing(context)
                # Clear any prior conversation for this session (new ticker)
                history.clear()
                history.append({"role": "user", "content": f"[INTELLIGENCE BRIEFING FOR {ticker}]\n\n{briefing}\n\nAbove is the complete pre-analyzed intelligence briefing for {ticker}. Use ONLY this data to answer questions. Do NOT ask the user to paste data — you already have everything."})
                history.append({"role": "assistant", "content": f"**{ticker}** intelligence briefing loaded. I have all institutional flows, insider activity, ML predictions, phase analysis, squeeze data, and trade signals. What would you like to know?"})
            else:
                # No data in warehouse — tell the AI explicitly
                history.clear()
                history.append({"role": "user", "content": f"[CONTEXT] The user is asking about {ticker}. This ticker has limited or no data in our institutional intelligence warehouse. Answer based on general market knowledge, but clearly state that our proprietary data (13F flows, Form 4, ML scores, phase classification) is not available for this ticker."})
                history.append({"role": "assistant", "content": f"**{ticker}** — I don't have institutional intelligence data for this ticker in our warehouse. I can discuss it based on general market knowledge, but won't have our proprietary 13F flows, insider activity, ML scores, or phase classification. What would you like to know?"})

        # Add user message
        history.append({"role": "user", "content": user_message})

        # Trim history — keep briefing (first 2) + recent messages
        if len(history) > MAX_HISTORY:
            if len(history) > MAX_HISTORY + 2:
                history[:] = history[:2] + history[-(MAX_HISTORY - 2):]

        # Call AI
        try:
            response = self._call_ai(history)
        except Exception as e:
            logger.error("TradeGPT API call failed: {}", e)
            response = f"**Error**: {e}"

        history.append({"role": "assistant", "content": response})
        return response

    def clear_session(self, session_id: str) -> None:
        """Clear conversation history for a session."""
        self.conversations.pop(session_id, None)
        self._active_tickers.pop(session_id, None)

    def get_active_ticker(self, session_id: str) -> Optional[str]:
        """Return the active ticker for a session."""
        return self._active_tickers.get(session_id)

    def _call_ai(self, messages: List[Dict[str, str]]) -> str:
        """Call the AI provider with conversation history."""
        provider, api_key = _detect_provider()
        if not provider:
            return (
                "**No AI provider configured.** Add one of these to your `.env` file:\n\n"
                "- `ANTHROPIC_API_KEY=sk-ant-...` (Claude)\n"
                "- `OPENAI_API_KEY=sk-...` (GPT-4o)\n"
                "- `GEMINI_API_KEY=...` (Gemini 1.5 Pro)"
            )

        if provider == "anthropic":
            return _call_anthropic(api_key, messages)
        elif provider == "openai":
            return _call_openai(api_key, messages)
        else:
            return _call_gemini(api_key, messages)


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _detect_provider() -> tuple[str, str]:
    """Return (provider, api_key) for whichever key is configured."""
    for provider, env_var in [
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("openai",    "OPENAI_API_KEY"),
        ("gemini",    "GEMINI_API_KEY"),
    ]:
        key = os.getenv(env_var, "").strip()
        if key:
            return provider, key
    return "", ""


def _call_anthropic(api_key: str, messages: List[Dict[str, str]]) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=TRADE_GPT_SYSTEM,
        messages=messages,
    )
    return message.content[0].text


def _call_openai(api_key: str, messages: List[Dict[str, str]]) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    api_messages = [{"role": "system", "content": TRADE_GPT_SYSTEM}] + messages
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=2000,
        messages=api_messages,
    )
    return response.choices[0].message.content


def _call_gemini(api_key: str, messages: List[Dict[str, str]]) -> str:
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name="gemini-1.5-pro",
        system_instruction=TRADE_GPT_SYSTEM,
    )
    contents = []
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [msg["content"]]})
    response = model.generate_content(
        contents,
        generation_config={"max_output_tokens": 2000},
    )
    return response.text


# Singleton
_trade_gpt: Optional[TradeGPT] = None


def get_trade_gpt() -> TradeGPT:
    """Get or create the singleton TradeGPT instance."""
    global _trade_gpt
    if _trade_gpt is None:
        _trade_gpt = TradeGPT()
    return _trade_gpt
