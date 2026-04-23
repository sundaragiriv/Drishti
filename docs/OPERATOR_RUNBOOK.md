# Quant-Bridge Operator Runbook

Instructions for running daily operations. Follow exactly in order.

## Environment
- Working directory: `e:\Quant-Bridge`
- Python: `python` (system Python 3.13)
- IBKR TWS: must be started manually by operator before market open
- Dashboard: http://127.0.0.1:8050 (starts with scanner)

---

## Morning Routine (Before 9:30 AM ET)

### Step 1: Ensure TWS is running
- Operator must start IBKR TWS/Gateway manually
- Accept any paper trading disclaimers
- Verify API enabled: File > Global Configuration > API > Settings > port 7497

### Step 2: Kill any stale processes
```bash
cd e:/Quant-Bridge
tasklist | grep python
# If any python processes exist, kill them:
tasklist | grep python | awk '{print $2}' | while read pid; do taskkill //F //PID $pid 2>/dev/null; done
# Clear stale session:
rm -f data/warehouse/session.json
```

### Step 3: Run premarket
```bash
cd e:/Quant-Bridge
python run_premarket.py
```
Expected output: `READINESS: READY` within 2-5 minutes.
If READINESS is BLOCKED, check the ITEMS NEEDING ATTENTION section.

### Step 4: Start the scanner + dashboard
```bash
cd e:/Quant-Bridge
python -m signal_scanner --watchlist universe_master --ibkr-port 7497 --port 8050
```
This runs in the foreground. Keep the terminal open all day.
Dashboard available at http://127.0.0.1:8050

### Step 5: Verify system is scanning
Wait 60 seconds, then check logs for:
- `PREFLIGHT PASSED: IBKR connected`
- `Universe built: 244 symbols`
- `BarPrinter cycle 1: X/244 tickers`
- `StrategyEngine cycle 1:`
- `Context Momentum scanner registered`

If BarPrinter shows 0 tickers or IBKR errors, check TWS connection.

---

## EOD Routine (After 4:00 PM ET)

### Step 1: Stop the scanner
In the scanner terminal, press Ctrl+C. Or:
```bash
tasklist | grep python | awk '{print $2}' | while read pid; do taskkill //F //PID $pid 2>/dev/null; done
rm -f e:/Quant-Bridge/data/warehouse/session.json
```

### Step 2: Run EOD pipeline
```bash
cd e:/Quant-Bridge
python -m signal_scanner.institutional_intel.jobs.run_eod_pipeline
```
Takes 60-90 minutes. Expected output: `EOD PIPELINE COMPLETE (all steps OK)`
The only acceptable failure is `options-flow` (known Polygon limitation).

### Step 3: Verify data freshness
```bash
cd e:/Quant-Bridge
python -c "
from signal_scanner.institutional_intel.config import safe_duckdb_connect
conn = safe_duckdb_connect(read_only=True)
for tbl, col in [('fact_daily_prices','trade_date'), ('fact_form4_transactions','transaction_date'), ('fact_short_volume','trade_date'), ('fact_dark_pool_daily','trade_date')]:
    r = conn.execute(f'SELECT MAX({col}) FROM {tbl}').fetchone()
    print(f'{tbl}: {r[0]}')
conn.close()
"
```
All dates should be today or yesterday (weekend gap is normal).

---

## Swing Features Backfill (One-Time, for Predictive AI v2)

This computes historical features from 2016-2023 for the predictive model.
Only needs to run once. Takes 2-3 hours.

### Prerequisites
- Scanner must NOT be running (DuckDB write lock)
- EOD pipeline must NOT be running

### Run the backfill
```bash
cd e:/Quant-Bridge
python -c "
from signal_scanner.institutional_intel.intelligence.swing_feature_engine import compute_swing_features
from signal_scanner.institutional_intel.config import safe_duckdb_connect

quarters = [
    '2016-Q1','2016-Q2','2016-Q3','2016-Q4',
    '2017-Q1','2017-Q2','2017-Q3','2017-Q4',
    '2018-Q1','2018-Q2','2018-Q3','2018-Q4',
    '2019-Q1','2019-Q2','2019-Q3','2019-Q4',
    '2020-Q1','2020-Q2','2020-Q3','2020-Q4',
    '2021-Q1','2021-Q2','2021-Q3','2021-Q4',
    '2022-Q1','2022-Q2','2022-Q3','2022-Q4',
    '2023-Q1','2023-Q2','2023-Q3',
]

for q in quarters:
    print(f'Computing {q}...')
    try:
        compute_swing_features(q)
        print(f'  {q} DONE')
    except Exception as e:
        print(f'  {q} ERROR: {e}')

print('BACKFILL COMPLETE')
"
```

### Verify backfill
```bash
cd e:/Quant-Bridge
python -c "
from signal_scanner.institutional_intel.config import safe_duckdb_connect
conn = safe_duckdb_connect(read_only=True)
rows = conn.execute('SELECT report_quarter, COUNT(*) as rows FROM fact_swing_features GROUP BY report_quarter ORDER BY report_quarter').fetchall()
for r in rows:
    print(f'{r[0]}: {r[1]:>8,} rows')
total = sum(r[1] for r in rows)
print(f'Total: {total:>8,} rows')
conn.close()
"
```
Expected: ~35 quarters, ~4.2M total rows.

---

## Predictive AI v2 Training (After Backfill)

Only run after swing features backfill is complete.

### Step 1: Rebuild feature dataset
```bash
cd e:/Quant-Bridge
python -m signal_scanner.institutional_intel.intelligence.predictive_features --build
```

### Step 2: Train model
```bash
cd e:/Quant-Bridge
python -m signal_scanner.institutional_intel.intelligence.predictive_model --train
```

### Step 3: Check validation report
```bash
cd e:/Quant-Bridge
cat data/warehouse/models/predictive_fwd_v2_validation.json
```
Key metrics to check:
- `direction_accuracy` > 0.55 (must pass)
- `top_decile_sharpe` > 1.0 (must pass)
- `ece` < 0.05 (must pass)
- `ic` > 0.03 (must pass)

If ALL pass: model is ready to wire into dashboard.
If ANY fail: model stays in research, do not wire.

---

## Options Snapshot Refresh (Daily, part of EOD)

Already included in EOD pipeline. To run standalone:
```bash
cd e:/Quant-Bridge
python -m signal_scanner.institutional_intel.jobs.options_snapshot_loader --universe
```

---

## Health Check (Anytime)

```bash
cd e:/Quant-Bridge
python -m signal_scanner.daily_health_check
```

---

## Validate Trading Paths (Pre-Market)

```bash
cd e:/Quant-Bridge
python -m signal_scanner.validate_trading_paths
```
All paths should show PASS except IBKR (needs TWS running).

---

## Session Monitor (During Market Hours)

```bash
cd e:/Quant-Bridge
python -m signal_scanner.session_monitor --heartbeat
```

---

## Troubleshooting

### IBKR won't connect
- Check TWS is running
- Accept paper trading disclaimer popup
- Verify API port 7497 in TWS settings
- Kill all python processes and restart

### DuckDB locked
- Only one write connection allowed
- Kill any running scanner/EOD/backfill process
- `rm -f data/warehouse/session.json`
- Retry

### Scanner shows 0 evaluations
- Check `intelligence snapshot loaded: X tickers` in logs
- If 0, DuckDB was locked at startup — restart scanner

### Context Momentum fires too many trades
- Max 8 entries/day, 5 open, one per ticker (guards built in)
- If duplicates appear, restart scanner

### Dashboard empty
- Check scanner terminal for errors
- Verify IBKR connected
- Refresh browser

---

## Full Night Pipeline (EOD + Backfill + Train)

Run this sequence for a complete overnight pipeline:

```bash
cd e:/Quant-Bridge

# 1. Kill everything
tasklist | grep python | awk '{print $2}' | while read pid; do taskkill //F //PID $pid 2>/dev/null; done
rm -f data/warehouse/session.json

# 2. EOD
python -m signal_scanner.institutional_intel.jobs.run_eod_pipeline

# 3. Backfill (only if not done before)
# Check first:
python -c "from signal_scanner.institutional_intel.config import safe_duckdb_connect; conn = safe_duckdb_connect(read_only=True); print(conn.execute('SELECT COUNT(DISTINCT report_quarter) FROM fact_swing_features').fetchone()[0], 'quarters'); conn.close()"
# If less than 35 quarters, run the backfill from the section above

# 4. Train v2 (only if backfill complete)
# python -m signal_scanner.institutional_intel.intelligence.predictive_features --build
# python -m signal_scanner.institutional_intel.intelligence.predictive_model --train

# 5. Premarket
python run_premarket.py
```

---

## Key Files

| File | Purpose |
|------|---------|
| `run_premarket.py` | Morning data refresh + readiness check |
| `signal_scanner/main.py` | Scanner + dashboard entry point |
| `run_dashboard.py` | Standalone dashboard (no IBKR needed) |
| `signal_scanner/institutional_intel/jobs/run_eod_pipeline.py` | Full EOD pipeline |
| `signal_scanner/daily_health_check.py` | Data health check |
| `signal_scanner/validate_trading_paths.py` | Trading path validation |
| `signal_scanner/session_monitor.py` | Live session monitoring |
| `data/warehouse/sec_intel.duckdb` | Main analytical warehouse |
| `signal_scanner/data/signals.db` | Scanner SQLite (trades, signals, ideas) |
| `signal_scanner/data/live_intraday.db` | Live bar store (SQLite WAL) |
