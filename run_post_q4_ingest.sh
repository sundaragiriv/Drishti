#!/usr/bin/env bash
# Post-Q4 2025 13F Ingest Pipeline
# Run this AFTER q4_13f_ingest pipeline completes (logs: logs/q4_13f_ingest.log)
#
# This script:
# 1. Creates new DB tables (options_flow, news_sentiment, related_stocks, 8-K events)
# 2. Runs aggregation stage (agg_quarterly_holdings)
# 3. Runs intelligence stage for 2025-Q4
# 4. Runs ML v2 scoring (exclusive DuckDB access required)
# 5. Derives dark pool from FINRA data
# 6. Runs initial news sentiment + options flow snapshot
# 7. Data quality cleanup (junk tickers + marks sparse/contaminated quarters)
# 8. Backfills 8-K events (last 30 days)
# 9. Prints health check

set -e
cd "$(dirname "$0")"

echo "=== POST-Q4 13F PIPELINE ==="
echo "Started: $(date)"

echo ""
echo "--- Step 1: Initialize new DB tables ---"
python -c "
from signal_scanner.institutional_intel.warehouse.db import init_warehouse
init_warehouse()
print('Tables initialized')
"

echo ""
echo "--- Step 2: Aggregation stage ---"
python -m signal_scanner.institutional_intel.jobs.run_pipeline \
  --stage aggregate \
  --max-runtime 60

echo ""
echo "--- Step 3: Intelligence stage (2025-Q4) ---"
python -m signal_scanner.institutional_intel.jobs.run_pipeline \
  --stage intelligence \
  --intelligence-quarter 2025-Q4 \
  --max-runtime 60

echo ""
echo "--- Step 4: ML v2 scoring (exclusive DuckDB access) ---"
echo "NOTE: Stop scanner/dashboard before this step if running"
python -m signal_scanner.institutional_intel.intelligence.ml_signal_v2 \
  --score --write

echo ""
echo "--- Step 5: Dark pool derivation from FINRA data (last 90 days) ---"
python -m signal_scanner.institutional_intel.jobs.short_data_loader \
  --mode dark-pool \
  --days-back 90

echo ""
echo "--- Step 6: Initial news sentiment load ---"
python -m signal_scanner.institutional_intel.jobs.news_sentiment_loader \
  --days-back 7 \
  --min-conviction 30

echo ""
echo "--- Step 7: Data quality cleanup (junk tickers + sparse/contaminated quarters) ---"
python -m signal_scanner.institutional_intel.jobs.data_cleanup

echo ""
echo "--- Step 8: 8-K material events backfill (last 30 days) ---"
python -m signal_scanner.institutional_intel.jobs.daily_8k_refresh \
  --days 30

echo ""
echo "--- Step 9: Health check ---"
python -m signal_scanner.daily_health_check

echo ""
echo "=== COMPLETE: $(date) ==="
