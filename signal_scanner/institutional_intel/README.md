# Institutional Intel (Phase A Scaffold)

## Purpose
Local-first SEC institutional + insider data layer, isolated from existing scanner logic.

## Shared Data Root (for multiple projects)
Set `SEC_INTEL_DATA_ROOT` to store/query the same data lake and DuckDB from different repos.

PowerShell example:
```powershell
$env:SEC_INTEL_DATA_ROOT="E:\\SharedData\\sec_intel"
```

## Jobs
Initialize storage + schema:
```bash
python -m signal_scanner.institutional_intel.jobs.bootstrap
```

Backfill scaffold:
```bash
python -m signal_scanner.institutional_intel.jobs.backfill --from-date 2021-01-01
```

If SEC blocks requests, set a specific contact identity:
```bash
$env:SEC_USER_AGENT="QuantBridge Research yourname@yourdomain.com"
python -m signal_scanner.institutional_intel.jobs.backfill --from-date 2021-01-01
```

Optimized insider run (universe-filtered + concurrent):
```bash
python -m signal_scanner.institutional_intel.jobs.backfill \
  --from-date 2024-01-01 \
  --forms "4,3,5" \
  --universe-file "signal_scanner/watchlists/universe_master.txt" \
  --workers 5 \
  --rps 8 \
  --progress-every 100
```

One-command fast staged pipeline (recommended):
```bash
python -m signal_scanner.institutional_intel.jobs.fast_pipeline \
  --from-date 2020-01-01 \
  --universe-file "signal_scanner/watchlists/universe_master.txt" \
  --workers 6 \
  --rps 8 \
  --progress-every 500
```

Include 13F metadata as phase 2:
```bash
python -m signal_scanner.institutional_intel.jobs.fast_pipeline \
  --from-date 2020-01-01 \
  --include-13f \
  --workers 6 \
  --rps 8
```

Metadata-first fast pass (no full filing body downloads):
```bash
python -m signal_scanner.institutional_intel.jobs.backfill \
  --from-date 2021-01-01 \
  --forms "13F-HR,13F-HR/A,4,3,5" \
  --metadata-only \
  --rps 8 \
  --workers 5
```

Note: `--metadata-only` now writes directly to DuckDB manifest (no per-filing JSON sidecar files),
which improves throughput and reduces disk overhead.

Incremental scaffold:
```bash
python -m signal_scanner.institutional_intel.jobs.incremental
```

13F manager inventory scaffold:
```bash
python -m signal_scanner.institutional_intel.jobs.manager_13f_scaffold
```

## Storage
- Raw SEC files: `data/raw/sec/`
- Curated files: `data/processed/`
- Metadata: `data/meta/`
- Warehouse: `data/warehouse/sec_intel.duckdb`

## Current Status
- Phase A skeleton complete.
- Next: implement SEC index pull, filing download, manifest write, parser load.
