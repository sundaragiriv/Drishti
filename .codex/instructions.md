# Quant-Bridge Code Review Instructions

You are reviewing code changes for Quant-Bridge, an institutional intelligence + ML-powered stock scanner with paper trading.

## System Context
- **Stack**: Python 3.13, DuckDB warehouse, Dash dashboard, IBKR connector
- **DB**: `data/warehouse/sec_intel.duckdb` (DuckDB — only ONE write connection at a time on Windows)
- **Signals DB**: `signal_scanner/data/signals.db` (SQLite with WAL mode)
- **Key constraint**: DuckDB Windows file locking — all read paths must use `read_only=True`

## Review Checklist

### Correctness
- [ ] SQL column names match actual schema (common bug source)
- [ ] DuckDB connections use `read_only=True` unless explicitly writing
- [ ] `fact_daily_prices` uses `trade_date` (NOT `date`), `close`/`high`/`low` (NOT `close_price` etc.)
- [ ] `get_active_quarter(conn)` always receives a connection argument
- [ ] DuckDB INTERVAL syntax uses f-strings, not parameterized placeholders
- [ ] Paper trades table uses `opened_at`/`closed_at` (NOT `entry_time`/`exit_time`)

### Safety
- [ ] No SQL injection vulnerabilities (parameterize user inputs)
- [ ] No write operations in read-only contexts
- [ ] Error handling for DuckDB lock conflicts (return graceful degradation, not crash)
- [ ] No hardcoded API keys or secrets

### Architecture
- [ ] Changes align with existing patterns (safe_duckdb_connect, _sqlite_ro, etc.)
- [ ] No unnecessary dependencies added
- [ ] Functions are testable and have clear inputs/outputs
- [ ] MCP tools return JSON-serializable results

### Trading Logic (if applicable)
- [ ] Regime gates respected (State 0 blocks ALL, State 1 blocks LONG)
- [ ] Position limits enforced (max 3 open)
- [ ] R:R minimum enforced (2.0 for scanner, 2.5 for IdeaBridge)
- [ ] Phase gates applied correctly (EARLY/ACTIVE/LATE_ACCUM only)

## Output Format
Provide a structured review:
1. **VALIDATED** or **CHANGES REQUESTED**
2. **Summary**: 2-3 sentences on what the changes do
3. **Issues**: List any problems found (Critical / Warning / Suggestion)
4. **Risk Assessment**: Low / Medium / High — what could break?
