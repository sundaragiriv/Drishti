"""Ask Kubera — Multi-Provider AI Intelligence Engine.

Takes structured stock context from kubera_context.py and generates a
comprehensive, data-backed investment analysis with specific entry/exit
recommendations across all three trading horizons.

Supports (auto-detected by which key is set in .env):
    ANTHROPIC_API_KEY  → Claude Sonnet (default if multiple keys present)
    OPENAI_API_KEY     → GPT-4o
    GEMINI_API_KEY     → Gemini 1.5 Pro
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from loguru import logger

KUBERA_SYSTEM_PROMPT = """You are Kubera, a senior financial analyst with 20+ years of institutional experience at BlackRock and Goldman Sachs. You specialize in reading 13F institutional flows, Form 4 insider activity, and translating quantitative data into actionable trading intelligence.

Your analysis is grounded ONLY in the data provided. You:
- Cite specific numbers from the data (never invent figures)
- Give specific price levels, timeframes, and options strategies
- Are direct and confident — no hedging language like "may" or "could"
- Structure analysis across three horizons: Day Trading, Swing Trading (2-8 weeks), Long Term (1-4 quarters)
- Include options strategies for each horizon where applicable
- Identify the single most important data point that drives your conclusion
- Flag risks that would invalidate the thesis

If data is missing or insufficient for a signal, say so explicitly rather than guessing."""

KUBERA_USER_TEMPLATE = """Analyze this stock and produce the full Kubera Intelligence Report.

STOCK DATA:
{context_json}

Structure your response in exactly this format:

## EXECUTIVE SUMMARY
[2-3 sentences. The headline thesis. Start with the ticker and verdict.]

## INSTITUTIONAL INTELLIGENCE
[What the 13F data tells us. Reference specific numbers: holder count, change, streak, tier-1 presence, cascade stage, concentration.]

## INSIDER INTELLIGENCE
[What insider behavior confirms or contradicts. Specific names/roles if available. Net buy/sell count. Cluster detection.]

## PHASE ANALYSIS
[Current phase name and what it means. Expected timeline. Lag estimate with confidence. What needs to happen for phase to advance.]

## DAY TRADING
- **Bias**: [LONG_ONLY / SHORT_ONLY / NEUTRAL]
- **Key Support**: [specific level if price data available, else "Monitor intraday structure"]
- **Key Resistance**: [specific level]
- **Options (0DTE/Weekly)**: [specific strategy or "N/A — insufficient price data"]

## SWING TRADING (2-8 weeks)
- **Signal**: [BUY / WATCH / AVOID / SHORT]
- **Entry Zone**: [specific description]
- **Target**: [% or price]
- **Stop Loss**: [% or price]
- **Options (30-45 DTE)**: [specific strategy with strike guidance]

## LONG TERM (1-4 quarters)
- **Signal**: [BUY / ACCUMULATE / HOLD / REDUCE / EXIT]
- **Thesis**: [2-3 sentences grounded in accumulation data]
- **Target Quarter**: [e.g., Q3 2026]
- **LEAPS Strategy**: [specific strategy or "N/A"]

## RISK FACTORS
[2-3 specific risks that would invalidate this thesis. Grounded in the data.]

## VERDICT
**[BUY / ACCUMULATE / WATCH / AVOID / SHORT]** — Conviction: [X/100]
[One sentence: the single most important reason for this verdict.]"""


def _detect_provider() -> tuple[str, str]:
    """Return (provider, api_key) for whichever key is configured.

    Priority: ANTHROPIC → OPENAI → GEMINI
    """
    for provider, env_var in [
        ("openai",    "OPENAI_API_KEY"),
        ("gemini",    "GEMINI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
    ]:
        key = os.getenv(env_var, "").strip()
        if key:
            return provider, key
    return "", ""


def _call_anthropic(api_key: str, context_json: str, max_tokens: int) -> str:
    try:
        import anthropic
    except ImportError:
        return "**Error**: `anthropic` package not installed. Run: `pip install anthropic`"
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=KUBERA_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": KUBERA_USER_TEMPLATE.format(context_json=context_json)}],
    )
    return message.content[0].text


def _call_openai(api_key: str, context_json: str, max_tokens: int) -> str:
    try:
        from openai import OpenAI
    except ImportError:
        return "**Error**: `openai` package not installed. Run: `pip install openai`"
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": KUBERA_SYSTEM_PROMPT},
            {"role": "user", "content": KUBERA_USER_TEMPLATE.format(context_json=context_json)},
        ],
    )
    return response.choices[0].message.content


def _call_gemini(api_key: str, context_json: str, max_tokens: int) -> str:
    try:
        import google.generativeai as genai
    except ImportError:
        return "**Error**: `google-generativeai` package not installed. Run: `pip install google-generativeai`"
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name="gemini-1.5-pro",
        system_instruction=KUBERA_SYSTEM_PROMPT,
    )
    response = model.generate_content(
        KUBERA_USER_TEMPLATE.format(context_json=context_json),
        generation_config={"max_output_tokens": max_tokens},
    )
    return response.text


def generate_kubera_report(
    context: Dict[str, Any],
    max_tokens: int = 2000,
) -> str:
    """Generate a Kubera Intelligence Report using whichever AI provider is configured.

    Checks environment for ANTHROPIC_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY
    (in that priority order). Add the key for your provider to your .env file.

    Args:
        context: Stock context dict from kubera_context.build_stock_context()
        max_tokens: Max response tokens

    Returns:
        Formatted markdown report string.
        Returns error message string if API call fails.
    """
    if not context:
        return "**Error**: No stock data found for this ticker. Ensure the pipeline has run."

    provider, api_key = _detect_provider()
    if not provider:
        return (
            "**No AI provider configured.** Add one of these to your `.env` file:\n\n"
            "- `ANTHROPIC_API_KEY=sk-ant-...` (Claude)\n"
            "- `OPENAI_API_KEY=sk-...` (GPT-4o)\n"
            "- `GEMINI_API_KEY=...` (Gemini 1.5 Pro)"
        )

    ticker = context.get("ticker", "UNKNOWN")
    logger.info("Generating Kubera report for {} via {}...", ticker, provider)

    context_json = json.dumps(context, indent=2, default=str)

    try:
        if provider == "anthropic":
            report = _call_anthropic(api_key, context_json, max_tokens)
        elif provider == "openai":
            report = _call_openai(api_key, context_json, max_tokens)
        else:
            report = _call_gemini(api_key, context_json, max_tokens)

        logger.info("Kubera report generated for {} via {}: {} chars", ticker, provider, len(report))
        return report

    except Exception as e:
        logger.error("Kubera API call failed for {} via {}: {}", ticker, provider, e)
        return f"**Error generating report**: {e}"


def generate_quick_summary(context: Dict[str, Any]) -> str:
    """Generate a quick 3-line summary without API call (rule-based fallback)."""
    if not context:
        return "No data available."

    phase = context.get("current_phase", "UNKNOWN")
    conviction = context.get("conviction_score") or 0
    ticker = context.get("ticker", "?")
    company = context.get("company", ticker)
    streak = context.get("institutional_summary", {}).get("count_up_streak", 0)
    swing = context.get("trading_signals", {}).get("swing_signal", "N/A")
    lt = context.get("trading_signals", {}).get("longterm_signal", "N/A")
    lag = context.get("lag_estimate", "Unknown")

    phase_labels = {
        "ACTIVE_ACCUM": "Active Accumulation — PRIMARY BUY ZONE",
        "EARLY_ACCUM": "Early Accumulation — Building Position",
        "LATE_ACCUM": "Late Accumulation — Approaching Impact",
        "EXPANSION": "Expansion — Retail Driving Now",
        "DISTRIBUTION": "Distribution — Smart Money Exiting",
        "DECLINE": "Decline — Institutional Exit Complete",
        "DORMANT": "Dormant — No Significant Activity",
    }

    phase_label = phase_labels.get(phase, phase)
    ins = context.get("insider_summary", {})
    insider_note = ""
    if ins.get("cluster_detected"):
        insider_note = " | Insider Cluster Buy Detected ✓"
    if ins.get("ceo_cfo_buying"):
        insider_note += " | CEO/CFO Buying ✓"

    return (
        f"**{ticker}** ({company}) | Phase: **{phase_label}**{insider_note}\n"
        f"Conviction: **{conviction}/100** | {streak}Q Accumulation Streak | "
        f"Price Impact Est: **{lag}**\n"
        f"Swing: **{swing}** | Long Term: **{lt}**"
    )
