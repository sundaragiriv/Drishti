"""Post-backfill pipeline: backtest → ML train → ML score.

Run once after historical_intelligence_backfill completes to prove edge
and generate ml_scores for all clean quarters.

Usage:
    python -m signal_scanner.institutional_intel.jobs.run_post_backfill
    python -m signal_scanner.institutional_intel.jobs.run_post_backfill --skip-backtest
    python -m signal_scanner.institutional_intel.jobs.run_post_backfill --quarter 2025-Q2
"""

from __future__ import annotations

import argparse
import sys

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description="Post-backfill pipeline runner")
    parser.add_argument("--skip-backtest", action="store_true",
                        help="Skip backtest --run (use if already run)")
    parser.add_argument("--skip-ml-train", action="store_true",
                        help="Skip ML model training (use if model already exists)")
    parser.add_argument("--quarter", default=None,
                        help="Quarter to score with ML (default: latest clean quarter)")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Post-Backfill Pipeline Starting")
    logger.info("=" * 60)

    # Step 1: Backtest
    if not args.skip_backtest:
        logger.info("Step 1/4: Running walk-forward backtest...")
        from signal_scanner.institutional_intel.intelligence.backtest import (
            run_backtest, print_backtest_summary,
            backtest_high_conviction_filter, calibrate_weights_from_backtest,
        )
        conn = duckdb.connect(str(WAREHOUSE_PATH))
        try:
            n = run_backtest(conn)
            logger.info("Backtest complete: {:,} results", n)
            print_backtest_summary(conn)
            backtest_high_conviction_filter(conn)
            calibrate_weights_from_backtest(conn)
        finally:
            conn.close()
    else:
        logger.info("Step 1/4: Backtest skipped (--skip-backtest)")

    # Step 2: Train ML model
    if not args.skip_ml_train:
        logger.info("Step 2/4: Training XGBoost ML signal model...")
        from signal_scanner.institutional_intel.intelligence.ml_signal import train_model
        conn = duckdb.connect(str(WAREHOUSE_PATH))
        try:
            result = train_model(conn)
            logger.info(
                "ML training complete: train_auc={:.3f}  val_auc={}",
                result["train_auc"],
                f"{result['val_auc']:.3f}" if "val_auc" in result else "N/A",
            )
        except RuntimeError as e:
            logger.error("ML training failed: {}", e)
            sys.exit(1)
        finally:
            conn.close()
    else:
        logger.info("Step 2/4: ML training skipped (--skip-ml-train)")

    # Step 3: Validate ML model
    logger.info("Step 3/4: Running ML validation report...")
    from signal_scanner.institutional_intel.intelligence.ml_signal import validate_model
    conn = duckdb.connect(str(WAREHOUSE_PATH))
    try:
        validate_model(conn)
    except FileNotFoundError as e:
        logger.warning("Validation skipped: {}", e)
    finally:
        conn.close()

    # Step 4: Score latest clean quarter
    logger.info("Step 4/4: Scoring latest clean quarter with ML model...")
    from signal_scanner.institutional_intel.intelligence.ml_signal import score_quarter
    conn = duckdb.connect(str(WAREHOUSE_PATH))
    try:
        quarter = args.quarter
        if not quarter:
            row = conn.execute("""
                SELECT report_quarter FROM intelligence_scores
                WHERE COALESCE(data_quality_score, 100) >= 75
                GROUP BY report_quarter HAVING COUNT(*) >= 500
                ORDER BY report_quarter DESC LIMIT 1
            """).fetchone()
            quarter = row[0] if row else None

        if quarter:
            df = score_quarter(conn, quarter, write_to_db=True)
            logger.info("ML scores written for {} tickers in {}", len(df), quarter)
            logger.info("Top 10 by ml_score:")
            for _, row in df.head(10).iterrows():
                logger.info("  {:6s}  ml_score={:.1f}", str(row["ticker"]), row["ml_score"])
        else:
            logger.warning("No clean quarter found for ML scoring")
    except FileNotFoundError as e:
        logger.warning("ML scoring skipped: {}", e)
    finally:
        conn.close()

    logger.info("=" * 60)
    logger.info("Post-Backfill Pipeline Complete")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
