"""Predictive Model — 3d/5d forward return predictor.

D4: LightGBM quantile regression + direction classifier
D5: Platt calibration + quantile coverage validation
D6: Validation gate

Usage:
    python -m signal_scanner.institutional_intel.intelligence.predictive_model --train
    python -m signal_scanner.institutional_intel.intelligence.predictive_model --validate
"""

from __future__ import annotations

import argparse
import json
import pickle
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
from loguru import logger


MODEL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "warehouse" / "models"

# Validation thresholds (from PREDICTIVE_AI_SAFEGUARDS.md)
VALIDATION_THRESHOLDS = {
    "direction_accuracy": 0.55,      # > 55%
    "ece": 0.05,                     # < 0.05
    "ic": 0.05,                      # > 0.05
    "top_decile_sharpe": 1.5,        # > 1.5
    "regime_robustness_min": 3,      # profitable in 3/5 regimes (or quarters here)
}


def load_training_data(
    conn,
    train_end: str,
    val_end: str,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load train/val/test splits from predictive_training_data.

    Returns: (train_df, val_df, test_df)
    """
    from signal_scanner.institutional_intel.intelligence.predictive_features import (
        ALL_NUMERIC_FEATURES, FEATURES_CATEGORICAL,
    )

    feature_cols = ALL_NUMERIC_FEATURES + FEATURES_CATEGORICAL
    label_cols = ["fwd_return_3d", "fwd_return_5d", "fwd_direction", "fwd_magnitude", "fwd_alpha_5d"]
    select_cols = ["ticker", "trade_date"] + feature_cols + label_cols

    df = conn.execute(f"""
        SELECT {', '.join(select_cols)}
        FROM predictive_training_data
        WHERE fwd_return_5d IS NOT NULL
        ORDER BY trade_date, ticker
    """).fetchdf()

    logger.info("Loaded {} rows with {} features", len(df), len(feature_cols))

    # Split temporally
    train = df[df["trade_date"] <= train_end].copy()
    val = df[(df["trade_date"] > train_end) & (df["trade_date"] <= val_end)].copy()
    test = df[df["trade_date"] > val_end].copy()

    logger.info("Train: {} | Val: {} | Test: {}", len(train), len(val), len(test))
    return train, val, test


def train_model(
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
) -> Dict[str, Any]:
    """Train LightGBM models: direction classifier + quantile regression.

    Returns dict with models and training metadata.
    """
    import lightgbm as lgb
    from signal_scanner.institutional_intel.intelligence.predictive_features import (
        ALL_NUMERIC_FEATURES, FEATURES_CATEGORICAL,
    )

    feature_cols = ALL_NUMERIC_FEATURES
    cat_cols = FEATURES_CATEGORICAL

    # Encode categoricals
    for col in cat_cols:
        for df in [train_df, val_df]:
            df[col] = df[col].astype("category")

    all_features = feature_cols + cat_cols
    X_train = train_df[all_features]
    X_val = val_df[all_features]

    # --- Direction classifier (binary: fwd_return_5d > 0) ---
    y_dir_train = train_df["fwd_direction"].values
    y_dir_val = val_df["fwd_direction"].values

    logger.info("Training direction classifier...")
    dir_model = lgb.LGBMClassifier(
        n_estimators=500,
        max_depth=5,
        learning_rate=0.03,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_lambda=1.0,
        min_child_samples=50,
        random_state=42,
        verbose=-1,
    )
    dir_model.fit(
        X_train, y_dir_train,
        eval_set=[(X_val, y_dir_val)],
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )
    dir_probs_val = dir_model.predict_proba(X_val)[:, 1]
    dir_acc_val = np.mean((dir_probs_val >= 0.5) == y_dir_val)
    logger.info("Direction classifier: val accuracy = {:.3f}", dir_acc_val)

    # --- Quantile regression (predict median, 25th, 75th percentile of 5d return) ---
    y_ret_train = train_df["fwd_return_5d"].values
    y_ret_val = val_df["fwd_return_5d"].values

    quantile_models = {}
    for alpha, label in [(0.25, "q25"), (0.50, "q50"), (0.75, "q75")]:
        logger.info("Training quantile regression (alpha={})...", alpha)
        qm = lgb.LGBMRegressor(
            objective="quantile",
            alpha=alpha,
            n_estimators=400,
            max_depth=5,
            learning_rate=0.03,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_lambda=1.0,
            min_child_samples=50,
            random_state=42,
            verbose=-1,
        )
        qm.fit(
            X_train, y_ret_train,
            eval_set=[(X_val, y_ret_val)],
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        quantile_models[label] = qm

    # Predict on validation
    q50_val = quantile_models["q50"].predict(X_val)
    q25_val = quantile_models["q25"].predict(X_val)
    q75_val = quantile_models["q75"].predict(X_val)

    return {
        "direction_model": dir_model,
        "quantile_models": quantile_models,
        "feature_cols": all_features,
        "dir_acc_val": dir_acc_val,
        "val_predictions": {
            "dir_probs": dir_probs_val,
            "q25": q25_val,
            "q50": q50_val,
            "q75": q75_val,
            "y_dir": y_dir_val,
            "y_ret": y_ret_val,
        },
        "trained_at": datetime.utcnow().isoformat(),
    }


def calibrate_model(
    val_predictions: Dict,
) -> Dict[str, Any]:
    """D5: Platt scaling for direction probabilities + quantile coverage check.

    Returns calibration results.
    """
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.linear_model import LogisticRegression

    dir_probs = val_predictions["dir_probs"]
    y_dir = val_predictions["y_dir"]
    y_ret = val_predictions["y_ret"]
    q25 = val_predictions["q25"]
    q75 = val_predictions["q75"]

    # --- Platt scaling ---
    logger.info("Platt scaling (logistic recalibration)...")
    platt = LogisticRegression(C=1.0)
    platt.fit(dir_probs.reshape(-1, 1), y_dir)
    calibrated_probs = platt.predict_proba(dir_probs.reshape(-1, 1))[:, 1]

    # ECE (Expected Calibration Error)
    n_bins = 10
    bin_edges = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    for i in range(n_bins):
        mask = (calibrated_probs >= bin_edges[i]) & (calibrated_probs < bin_edges[i + 1])
        if mask.sum() == 0:
            continue
        bin_acc = y_dir[mask].mean()
        bin_conf = calibrated_probs[mask].mean()
        ece += mask.sum() / len(y_dir) * abs(bin_acc - bin_conf)

    logger.info("Platt ECE: {:.4f}", ece)

    # --- Quantile coverage ---
    q25_coverage = np.mean(y_ret < q25)  # should be ~0.25
    q75_coverage = np.mean(y_ret < q75)  # should be ~0.75
    band_width = np.median(q75 - q25)

    logger.info("Quantile coverage: q25={:.3f} (target 0.25) | q75={:.3f} (target 0.75) | band_width={:.4f}",
                q25_coverage, q75_coverage, band_width)

    q25_ok = 0.20 <= q25_coverage <= 0.30
    q75_ok = 0.70 <= q75_coverage <= 0.80

    return {
        "platt_model": platt,
        "ece": round(ece, 4),
        "calibrated_probs": calibrated_probs,
        "q25_coverage": round(q25_coverage, 3),
        "q75_coverage": round(q75_coverage, 3),
        "q25_ok": q25_ok,
        "q75_ok": q75_ok,
        "band_width_median": round(band_width, 4),
    }


def validate_model(
    val_df: pd.DataFrame,
    test_df: pd.DataFrame,
    model_result: Dict,
    calibration: Dict,
) -> Dict[str, Any]:
    """D6: Full validation gate.

    Returns validation report with pass/fail for each metric.
    """
    from scipy.stats import spearmanr
    from signal_scanner.institutional_intel.intelligence.predictive_features import (
        ALL_NUMERIC_FEATURES, FEATURES_CATEGORICAL,
    )

    feature_cols = ALL_NUMERIC_FEATURES + FEATURES_CATEGORICAL
    for col in FEATURES_CATEGORICAL:
        test_df[col] = test_df[col].astype("category")

    X_test = test_df[feature_cols]
    y_dir_test = test_df["fwd_direction"].values
    y_ret_test = test_df["fwd_return_5d"].values

    dir_model = model_result["direction_model"]
    q50_model = model_result["quantile_models"]["q50"]

    # 1. Direction accuracy (OOS)
    dir_probs_test = dir_model.predict_proba(X_test)[:, 1]
    dir_acc = np.mean((dir_probs_test >= 0.5) == y_dir_test)

    # 2. ECE on test (using Platt from val)
    platt = calibration["platt_model"]
    cal_probs_test = platt.predict_proba(dir_probs_test.reshape(-1, 1))[:, 1]
    n_bins = 10
    bin_edges = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    for i in range(n_bins):
        mask = (cal_probs_test >= bin_edges[i]) & (cal_probs_test < bin_edges[i + 1])
        if mask.sum() == 0:
            continue
        ece += mask.sum() / len(y_dir_test) * abs(y_dir_test[mask].mean() - cal_probs_test[mask].mean())

    # 3. IC (Spearman rank correlation: predicted vs actual 5d return)
    q50_test = q50_model.predict(X_test)
    ic, ic_pval = spearmanr(q50_test, y_ret_test)

    # 4. Top-decile Sharpe
    test_df_copy = test_df.copy()
    test_df_copy["pred_ret"] = q50_test
    top_decile = test_df_copy.nlargest(len(test_df_copy) // 10, "pred_ret")
    top_ret = top_decile["fwd_return_5d"]
    top_sharpe = (top_ret.mean() / top_ret.std()) * np.sqrt(252 / 5) if top_ret.std() > 0 else 0

    # 5. Discrimination check
    pct_positive_pred = np.mean(dir_probs_test >= 0.5)

    # 6. Quarterly robustness
    test_df_copy["quarter"] = pd.to_datetime(test_df_copy["trade_date"]).dt.to_period("Q").astype(str)
    quarter_results = {}
    for q, qdf in test_df_copy.groupby("quarter"):
        q_top = qdf.nlargest(max(1, len(qdf) // 10), "pred_ret")
        q_sharpe = (q_top["fwd_return_5d"].mean() / q_top["fwd_return_5d"].std()) * np.sqrt(252 / 5) if q_top["fwd_return_5d"].std() > 0 else 0
        quarter_results[q] = {"sharpe": round(q_sharpe, 2), "n": len(qdf)}

    profitable_quarters = sum(1 for v in quarter_results.values() if v["sharpe"] > 0)

    # Build report
    report = {
        "direction_accuracy": round(dir_acc, 4),
        "ece": round(ece, 4),
        "ic": round(ic, 4),
        "ic_pval": round(ic_pval, 6),
        "top_decile_sharpe": round(top_sharpe, 2),
        "pct_positive_predictions": round(pct_positive_pred, 3),
        "profitable_quarters": profitable_quarters,
        "total_quarters": len(quarter_results),
        "quarter_detail": quarter_results,
        "quantile_coverage": {
            "q25": calibration["q25_coverage"],
            "q75": calibration["q75_coverage"],
            "q25_ok": calibration["q25_ok"],
            "q75_ok": calibration["q75_ok"],
        },
        "test_rows": len(test_df),
        "test_tickers": test_df["ticker"].nunique(),
    }

    # Pass/fail gate
    gates = {
        "direction_accuracy": dir_acc >= VALIDATION_THRESHOLDS["direction_accuracy"],
        "ece": ece <= VALIDATION_THRESHOLDS["ece"],
        "ic": ic >= VALIDATION_THRESHOLDS["ic"],
        "top_decile_sharpe": top_sharpe >= VALIDATION_THRESHOLDS["top_decile_sharpe"],
        "discrimination": 0.20 <= pct_positive_pred <= 0.80,
    }
    report["gates"] = gates
    report["all_gates_pass"] = all(gates.values())

    return report


def save_models(model_result: Dict, calibration: Dict, report: Dict) -> str:
    """Save trained models to disk."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    path = MODEL_DIR / "predictive_fwd_v1.pkl"
    bundle = {
        "direction_model": model_result["direction_model"],
        "quantile_models": model_result["quantile_models"],
        "platt_model": calibration["platt_model"],
        "feature_cols": model_result["feature_cols"],
        "trained_at": model_result["trained_at"],
        "validation_report": report,
    }
    with open(path, "wb") as f:
        pickle.dump(bundle, f)
    logger.info("Model saved to {}", path)

    # Save validation report as JSON
    report_path = MODEL_DIR / "predictive_fwd_v1_validation.json"
    # Remove non-serializable items
    clean_report = {k: v for k, v in report.items()}
    with open(report_path, "w") as f:
        json.dump(clean_report, f, indent=2, default=str)
    logger.info("Validation report saved to {}", report_path)

    return str(path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Predictive forward return model")
    parser.add_argument("--train", action="store_true", help="Train + calibrate + validate")
    parser.add_argument("--validate", action="store_true", help="Show validation report only")
    args = parser.parse_args()

    from signal_scanner.institutional_intel.config import safe_duckdb_connect
    from signal_scanner.institutional_intel.intelligence.predictive_features import get_temporal_splits

    conn = safe_duckdb_connect(read_only=True)
    if not conn:
        logger.error("Cannot connect to DuckDB")
        exit(1)

    splits = get_temporal_splits(conn)
    train_end = splits["train"]["end"]
    val_end = splits["val"]["end"]
    logger.info("Splits: train end={}, val end={}", train_end, val_end)

    if args.train:
        train_df, val_df, test_df = load_training_data(conn, train_end, val_end)
        conn.close()

        # D4: Train
        model_result = train_model(train_df, val_df)

        # D5: Calibrate
        calibration = calibrate_model(model_result["val_predictions"])

        # D6: Validate on test set
        report = validate_model(val_df, test_df, model_result, calibration)

        # Print report
        print("\n" + "=" * 60)
        print("VALIDATION REPORT")
        print("=" * 60)
        for k, v in report.items():
            if k in ("quarter_detail", "quantile_coverage", "gates"):
                continue
            print(f"  {k}: {v}")
        print("\nGates:")
        for k, v in report["gates"].items():
            status = "PASS" if v else "FAIL"
            print(f"  [{status}] {k}: {report.get(k, '?')}")
        print(f"\nQuantile coverage:")
        for k, v in report["quantile_coverage"].items():
            print(f"  {k}: {v}")
        print(f"\nQuarter detail:")
        for q, v in report["quarter_detail"].items():
            print(f"  {q}: sharpe={v['sharpe']}, n={v['n']}")

        print(f"\n{'ALL GATES PASS' if report['all_gates_pass'] else 'VALIDATION FAILED — DO NOT SHIP'}")
        print("=" * 60)

        # Save
        save_models(model_result, calibration, report)

    elif args.validate:
        path = MODEL_DIR / "predictive_fwd_v1_validation.json"
        if path.exists():
            with open(path) as f:
                report = json.load(f)
            print(json.dumps(report, indent=2))
        else:
            print("No validation report found. Run --train first.")
        conn.close()
