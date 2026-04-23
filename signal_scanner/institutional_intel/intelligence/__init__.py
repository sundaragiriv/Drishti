"""Institutional Intelligence — rule-based phase classification and conviction scoring.

Modules:
    phase_classifier     — 7-phase accumulation cycle classifier
    lag_estimator        — filing-lag to price-impact estimator (inside phase_classifier)
    cascade_detector     — copy-cat institutional cascade detection
    divergence_scanner   — smart-money divergence (price down, inst up)
    manager_quality      — tier-1/2/3 manager scoring + concentration signals
    insider_intelligence — Form 4 cluster detection and CEO/CFO signals
    conviction_score     — 6-dimensional master conviction score (0-100)
    sector_rotation      — sector flow clock and cycle phase
    distribution_detector— early exit / distribution warning signals
    trading_signals      — Day / Swing / Long-Term signal generation
    backtest             — walk-forward backtest engine
    kubera_context       — per-ticker context dict builder for Ask Kubera
    kubera_engine        — Claude API report generator (Ask Kubera)
"""
