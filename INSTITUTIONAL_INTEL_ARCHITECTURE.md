# Institutional + Insider Intelligence Architecture (Local-First)

## 1) Objective
Build a dedicated institutional/insider intelligence layer that:
- Replicates Kubera-style reports report-by-report.
- Stays isolated from current scanner/recommendation logic until explicitly integrated.
- Runs fully local-first on your machine with strong reproducibility and auditability.

This layer will later power:
- Signature Reports
- Ask Brahma intelligence expansion
- Evidence-based EOD decision workflows

## 2) Non-Goals (for now)
- No changes to existing `signal_scanner` trading logic.
- No automatic strategy mutation from EOD outputs.
- No cloud dependency required to operate core pipelines.

## 3) High-Level Design

### 3.1 Subsystem Boundary
New subsystem lives under:
- `signal_scanner/institutional_intel/`

Proposed structure:
- `signal_scanner/institutional_intel/ingest/` (download/index ingestion)
- `signal_scanner/institutional_intel/parsers/` (13F/Form 4 parsers)
- `signal_scanner/institutional_intel/warehouse/` (DuckDB DDL/views)
- `signal_scanner/institutional_intel/jobs/` (backfill + incremental runners)
- `signal_scanner/institutional_intel/reports/` (report factor logic)
- `signal_scanner/institutional_intel/utils/` (mappers, validation, logging)

### 3.2 Data Flow
1. Ingest SEC filing metadata and raw docs.
2. Persist raw immutable payloads to local data lake.
3. Parse raw payloads into normalized tables.
4. Build factor views/materialized tables for reports.
5. Expose report outputs to dashboard (later integration phase).

## 4) Storage Strategy (Local Tool)

### 4.1 Layers
1. Raw (immutable):
- `data/raw/sec/...`
- Original SEC payloads (JSON/XML/TXT), partitioned by form/year/quarter/cik.

2. Processed (curated):
- `data/processed/...`
- Parsed Parquet facts/dimensions.

3. Serving warehouse:
- `data/warehouse/sec_intel.duckdb`
- Query-serving tables/views for reports and Ask Brahma.

4. Metadata/audit:
- `data/meta/...`
- Ingestion run logs, parser versions, row counts, errors, hashes.

### 4.2 Git Policy
Do **not** commit bulk data to repo history.

Add to `.gitignore`:
```gitignore
data/raw/
data/processed/
data/warehouse/
data/meta/
*.duckdb
*.parquet
```

Commit only:
- ETL code
- schema definitions
- tiny sample fixtures for tests

## 5) SEC Data Scope

### 5.1 Institutional (13F)
Forms:
- `13F-HR`
- `13F-HR/A`

Key extracted fields:
- Manager CIK/name
- Report period / filing date
- Holdings rows: issuer, CUSIP, class, shares, value, put/call, discretion

Derived intelligence:
- QoQ share/value change
- New positions / full exits
- Concentration shifts
- Sector allocation shifts

### 5.2 Insider
Forms:
- `4` (primary)
- `3`, `5` (supporting)

Key extracted fields:
- Insider identity + role
- Issuer CIK/ticker
- Transaction date/code
- Buy/sell direction
- Shares, price, ownership after transaction

Derived intelligence:
- Net insider buy/sell per symbol/day/week
- Executive-only signal quality
- Clustered insider activity alerts

## 6) Core Warehouse Model

### 6.1 Dimensions
- `dim_issuer` (issuer_cik, ticker, cusip, name, sector, industry, mapping_confidence)
- `dim_manager_13f` (manager_cik, manager_name)
- `dim_insider` (insider_id surrogate, insider_name, role metadata)
- `dim_calendar` (date keys for quarter/week aggregations)

### 6.2 Facts
- `fact_13f_positions`
  - manager_cik, issuer_cik/ticker/cusip, report_period, filed_at, shares, value_usd, put_call
- `fact_13f_manager_snapshot`
  - manager totals, concentration, active positions count
- `fact_form4_transactions`
  - issuer/ticker, insider, tx_date, tx_code, direction, shares, price, ownership_after
- `fact_insider_daily_agg`
  - symbol/day buy_shares/sell_shares/net_shares/net_value

### 6.3 Lineage / Operational
- `ingestion_runs`
  - source, started_at, finished_at, status, rows_ingested, rows_failed, parser_version
- `raw_file_manifest`
  - source_url, local_path, sha256, received_at, form_type, cik, accession_no

## 7) Critical Mapping Problem (CUSIP -> Ticker)
CUSIP-to-ticker mapping is a known failure point; use a dedicated mapper:
- Primary: SEC issuer metadata when available.
- Secondary: cached mapping table with confidence score.
- Keep unresolved records with `mapping_confidence = LOW`; do not drop.

All report logic must tolerate unresolved mapping rows.

## 8) Report Engine Design

### 8.1 Registry-Driven Reports
Implement `report_registry` with:
- report_id
- name
- factor definitions
- thresholds
- ranking formula
- freshness constraints

### 8.2 Initial report order
1. Institutional Smart Investor Moves
2. Institutional Exit Analysis
3. AI Alerts
4. Diamonds variants
5. Platinum / Ultimate Quarterly

Each report shipped with:
- SQL/view logic
- test fixture
- dashboard card + detail table

## 9) Integration Strategy
Current `signal_scanner` pipeline remains unchanged.

Integration path (later):
1. Add new dashboard sections for Signature Reports.
2. Feed Ask Brahma with institutional/insider factors.
3. Keep independent failure boundaries (if intel ingest fails, scanner still runs).

## 10) Refresh Cadence
- Historical backfill: last 5 years (initial run).
- Incremental metadata polling: daily.
- Parsing + warehouse refresh: daily (or on-demand button later).

## 11) Data Quality + Validation

Minimum checks:
- Row-level schema validation
- Null/invalid field checks per form
- Duplicate accession control
- Quarter-over-quarter continuity sanity checks
- Outlier checks on shares/value changes
- Parse error quarantine table

## 12) Security + Compliance (Operational)
- Respect SEC fair-access patterns (rate limiting + user-agent policy).
- Log every external request and local artifact hash.
- Keep local data encrypted at disk level if needed by environment policy.

## 13) Docker Decision
Docker is optional:
- For this local-first stage, native Python + DuckDB is simpler and faster to iterate.
- Add Docker later for portability/reproducible deployment once pipelines stabilize.

Recommendation now:
- Start without Docker.
- Add `docker-compose` only after Phase B when schemas and jobs are stable.

## 14) Phase Plan

### Phase A: Foundation (now)
1. Create module skeleton under `signal_scanner/institutional_intel/`
2. Create DuckDB schema + migration bootstrap
3. Implement SEC client + metadata ingestion
4. Implement raw file manifest tracking
5. Implement initial 13F and Form 4 parsers
6. Build first curated facts (`fact_13f_positions`, `fact_form4_transactions`)
7. Add backfill and incremental job runners

### Phase B: Intelligence
1. Build derived factor views (QoQ deltas, exits, insider net flow)
2. Add sector attribution + concentration analytics
3. Add report ranking logic and validation tests

### Phase C: UI + Ask Brahma Integration
1. Signature Reports dashboard section
2. Drill-down report pages/tables
3. Ask Brahma enrichment with institutional/insider strength block

## 15) Runbook (Target)
- `python -m signal_scanner.institutional_intel.jobs.backfill --from 2021-01-01`
- `python -m signal_scanner.institutional_intel.jobs.incremental`
- `python -m signal_scanner.institutional_intel.jobs.validate`

## 16) Decision Log
1. Keep subsystem isolated first; integrate later.
2. Use local data lake + DuckDB serving model.
3. Prioritize 13F + Form 4 first.
4. Registry-driven report engine to replicate and then extend Kubera reports.

## 17) Immediate Next Step
Begin Phase A implementation:
- create directories and bootstrap files
- define schema DDL
- build first ingest/parsing job with logging and manifest tracking
