"""Forecast tab callbacks — reads existing intelligence_scores + model artifacts.

NO new training, NO new features. Surfaces what we already compute:
  - HMM regime state + transition probabilities
  - Top 10 by ml_signal_v2 (28-feat conviction scorer)
  - Triple Lock candidates (the 59.8% WR filter on n=132)
  - Model health metadata (training date, AUC)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from dash import Input, Output, html

from signal_scanner.institutional_intel.config import (
    WAREHOUSE_PATH,
    get_active_quarter,
    safe_duckdb_connect,
)

logger = logging.getLogger(__name__)

# Map HMM state index → (label, color, what's allowed)
_HMM_STATES = {
    0: ("CRASH",        "#ff4488", "All entries blocked"),
    1: ("DISTRIBUTING", "#ff8c00", "SHORT only"),
    2: ("ACCUMULATING", "#ffd43b", "LONG with tight stops"),
    3: ("MEAN-REV",     "#4da3ff", "LONG allowed"),
    4: ("TRENDING",     "#00ff88", "LONG primary"),
}


def _model_artifact_mtime(path: Path) -> str:
    """Human-readable age of a model file, or 'missing'."""
    if not path.exists():
        return "missing"
    try:
        age_days = (datetime.now().timestamp() - path.stat().st_mtime) / 86400.0
        if age_days < 1:
            return f"{age_days * 24:.0f}h ago"
        if age_days < 60:
            return f"{age_days:.0f}d ago"
        return f"{age_days / 30:.0f}mo ago"
    except Exception:
        return "?"


def _read_v3_structural_metrics() -> dict:
    """Load v3_structural metrics from its sidecar JSON."""
    metrics_path = WAREHOUSE_PATH.parents[1] / "models" / "flow_predictor_v3_structural_metrics.json"
    if not metrics_path.exists():
        return {}
    try:
        return json.loads(metrics_path.read_text())
    except Exception:
        return {}


def register_forecast_callbacks(app) -> None:
    """Wire up the Forecast tab."""

    # ────────────────────────────────────────────────────────────────
    # 1. HMM regime card body
    # ────────────────────────────────────────────────────────────────
    @app.callback(
        Output("forecast-regime-body", "children"),
        Input("forecast-refresh-interval", "n_intervals"),
        prevent_initial_call=False,
    )
    def update_regime_card(_n):
        try:
            from signal_scanner.institutional_intel.intelligence.regime_hmm import (
                DailyRegimeHMM,
            )
            hmm = DailyRegimeHMM()
            hmm.load()
            if hmm._model is None:
                return html.Span("HMM model not available.",
                                 style={"color": "var(--kb-text-muted)"})
            state, probs, name = hmm.current_regime()
            label, color, allowed = _HMM_STATES.get(
                state, ("UNKNOWN", "#888", "—"))
            # Transition probabilities for the current state
            transmat_row = hmm._model.transmat_[state] if hasattr(hmm._model, "transmat_") else []
            top_transitions = []
            try:
                idx_sorted = sorted(range(len(transmat_row)),
                                    key=lambda i: transmat_row[i],
                                    reverse=True)[:3]
                for i in idx_sorted:
                    tl, tc, _ = _HMM_STATES.get(i, (f"S{i}", "#888", ""))
                    top_transitions.append((tl, float(transmat_row[i]), tc))
            except Exception:
                pass

            children = [
                html.Div(
                    style={"display": "flex", "alignItems": "baseline",
                           "gap": "12px", "marginBottom": "10px"},
                    children=[
                        html.Span(f"State {state} ·",
                                  style={"color": "var(--kb-text-muted)"}),
                        html.Span(label,
                                  style={"color": color,
                                         "fontWeight": "800",
                                         "fontSize": "1.4rem",
                                         "letterSpacing": "0.04em"}),
                        html.Span(f"  → {allowed}",
                                  style={"color": "var(--kb-text-muted)",
                                         "marginLeft": "auto"}),
                    ],
                ),
            ]
            if top_transitions:
                children.append(
                    html.Div(
                        style={"fontSize": "0.78rem",
                               "color": "var(--kb-text-muted)"},
                        children=[
                            html.Span("Next-day transitions: ",
                                      style={"letterSpacing": "0.04em"}),
                            *[html.Span(
                                f"{tl} {p:.0%}" + ("  " if i < len(top_transitions) - 1 else ""),
                                style={"color": tc,
                                       "fontWeight": "600",
                                       "marginRight": "12px"},
                              ) for i, (tl, p, tc) in enumerate(top_transitions)],
                        ],
                    )
                )
            return children
        except Exception as e:
            logger.warning("Forecast regime callback error: %s", e)
            return html.Span(f"Regime fetch error: {e}",
                             style={"color": "var(--kb-short)"})

    # ────────────────────────────────────────────────────────────────
    # 2. Top 10 by ml_signal_v2  +  Triple Lock candidates
    #    Both pull from intelligence_scores so they share a query.
    # ────────────────────────────────────────────────────────────────
    @app.callback(
        Output("forecast-ml-table", "data"),
        Output("forecast-ml-summary", "children"),
        Output("forecast-triple-table", "data"),
        Output("forecast-triple-summary", "children"),
        Input("forecast-refresh-interval", "n_intervals"),
        prevent_initial_call=False,
    )
    def update_forecast_tables(_n):
        conn = safe_duckdb_connect(read_only=True)
        if conn is None:
            return [], "DuckDB locked", [], "DuckDB locked"
        try:
            quarter = get_active_quarter(conn) or "2025-Q4"
            # Pull a generous set of columns we surface or need for filtering.
            # Schema is documented in CLAUDE.md.
            rows = conn.execute(
                """
                SELECT ticker,
                       conviction_score,
                       ml_score_v2,
                       triple_lock,
                       accum_phase,
                       inst_f4_distinct_60d,
                       price_momentum_90d,
                       price_above_200sma,
                       squeeze_score,
                       short_conviction_score
                FROM intelligence_scores
                WHERE report_quarter = ?
                  AND ticker IS NOT NULL
                  AND conviction_score IS NOT NULL
                ORDER BY ml_score_v2 DESC NULLS LAST
                """,
                [quarter],
            ).fetchall()
        except Exception as e:
            logger.warning("Forecast tables query error: %s", e)
            return [], "Query error", [], "Query error"
        finally:
            conn.close()

        if not rows:
            return [], "No intelligence rows for active quarter", [], "No data"

        def _side_from_row(r) -> str:
            conv = float(r[1] or 0.0)
            short_conv = float(r[9] or 0.0)
            phase = str(r[4] or "").upper()
            if "DISTRIB" in phase or "DECLIN" in phase or short_conv > conv:
                return "SHORT"
            return "LONG"

        def _triple(r) -> str:
            try:
                conv = float(r[1] or 0.0)
                ml = float(r[2] or 0.0)
                f4 = int(r[5] or 0)
                phase = str(r[4] or "").upper()
                tl_flag = bool(r[3])
                if tl_flag:
                    return "YES"
                if conv >= 70 and ml >= 70 and f4 >= 1 and "ACCUM" in phase:
                    return "YES"
                return "no"
            except Exception:
                return "no"

        # Top 10 by ml_score_v2 (already sorted by query)
        top_ml = []
        for i, r in enumerate(rows[:10], start=1):
            top_ml.append({
                "rank": i,
                "symbol": r[0],
                "ml_score_v2": round(float(r[2] or 0.0), 1),
                "conviction_score": round(float(r[1] or 0.0), 1),
                "accum_phase": (r[4] or "—").replace("_", " "),
                "side": _side_from_row(r),
                "triple_lock": _triple(r),
                "inst_f4_distinct_60d": int(r[5] or 0),
                "price_momentum_90d": round(float(r[6] or 0.0), 1),
            })

        # Triple-Lock candidates — pull rows that pass the filter
        triple_rows = [r for r in rows if _triple(r) == "YES"]
        triple_data = []
        for r in triple_rows[:20]:
            triple_data.append({
                "symbol": r[0],
                "conviction_score": round(float(r[1] or 0.0), 1),
                "ml_score_v2": round(float(r[2] or 0.0), 1),
                "inst_f4_distinct_60d": int(r[5] or 0),
                "accum_phase": (r[4] or "—").replace("_", " "),
                "squeeze_score": round(float(r[8] or 0.0), 1),
            })

        ml_summary = f"{len(rows)} scored · top 10 shown · quarter {quarter}"
        triple_summary = f"{len(triple_rows)} names pass · 59.8% historical WR (n=132)"
        return top_ml, ml_summary, triple_data, triple_summary

    # ────────────────────────────────────────────────────────────────
    # 3. Model health footer
    # ────────────────────────────────────────────────────────────────
    @app.callback(
        Output("forecast-model-health", "children"),
        Input("forecast-refresh-interval", "n_intervals"),
        prevent_initial_call=False,
    )
    def update_model_health(_n):
        from signal_scanner.dashboard.layouts.forecast_view import _model_health_row

        models_dir = WAREHOUSE_PATH.parents[1] / "models"
        intraday_dir = WAREHOUSE_PATH.parent / "models"

        ml_v2 = models_dir / "ml_signal_v2.pkl"
        v3_struct = models_dir / "flow_predictor_v3_structural.pkl"
        hmm = intraday_dir / "regime_hmm_daily.pkl"

        v3_metrics = _read_v3_structural_metrics()
        v3_auc = v3_metrics.get("purged_cv_mean_auc")
        v3_auc_txt = f"{v3_auc:.3f}" if isinstance(v3_auc, (int, float)) else "—"

        return [
            _model_health_row(
                "ml_signal_v2",
                f"trained {_model_artifact_mtime(ml_v2)} · val AUC 0.560 · 28 features · cross-sectional ranker",
            ),
            _model_health_row(
                "v3_structural",
                f"trained {_model_artifact_mtime(v3_struct)} · purged CV AUC {v3_auc_txt} · YZ + Hurst features",
            ),
            _model_health_row(
                "HMM regime (5-state)",
                f"refit {_model_artifact_mtime(hmm)} · walk-forward Sharpe 3.47 · daily refit via EOD",
            ),
        ]
