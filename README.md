# Signal Command Center

Multi-symbol quantitative signal scanner that monitors S&P 500, Nasdaq 100, and custom watchlists in real-time, calculating institutional positioning (GEX), technical confluence, and ranking trade opportunities by strength score.

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

**Note:** `pandas-ta` may require `numpy<2.0` on some systems. If you see numpy errors:
```bash
pip install "numpy<2.0"
```

### 2. Configure IBKR (Optional)

If using Interactive Brokers for real-time data:

1. Open TWS or IB Gateway
2. Enable API connections: **File > Global Configuration > API > Settings**
   - Check "Enable ActiveX and Socket Clients"
   - Set Socket Port: `7497` (paper) or `7496` (live)
3. Edit `signal_scanner/config.py` to match your port/client ID

If IBKR is not available, the scanner automatically falls back to **yfinance** (free, no setup required).

### 3. Watchlists

Pre-bundled watchlists in `signal_scanner/watchlists/`:
- `sp500.txt` — Top 100 S&P 500 by market cap (default)
- `russell2000.txt` — Small-cap / Russell-focused starter basket
- `nasdaq100.txt` — Nasdaq 100 constituents
- `custom.txt` — User-editable (add your own symbols)
- `test.txt` — 5 symbols for development (SPY, AAPL, MSFT, NVDA, TSLA)

Format: one symbol per line, `#` for comments.

## Usage

### Full Scanner + Dashboard

```bash
python -m signal_scanner.main
```

Opens dashboard at [http://localhost:8050](http://localhost:8050).
Default watchlist is `sp500`.

### Common Options

```bash
# Use a specific watchlist
python -m signal_scanner.main --watchlist sp500

# Skip IBKR, use yfinance only
python -m signal_scanner.main --no-ibkr

# Single scan and exit (testing)
python -m signal_scanner.main --scan-once --no-ibkr

# Scanner only, no web dashboard
python -m signal_scanner.main --no-dashboard

# Custom dashboard port
python -m signal_scanner.main --port 9050
```

### Quick Test

```bash
python -m signal_scanner.main --watchlist test --no-ibkr --scan-once
```

This runs a single scan of 5 symbols via yfinance and prints results.

## Configuration

All settings are in `signal_scanner/config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `IBKR_PORT` | 7497 | TWS paper trading port |
| `scan_interval_seconds` | 120 | Seconds between scans |
| `timeframes` | 5m, 15m, 1h | Timeframes to analyze |
| `sma_period` | 200 | SMA lookback period |
| `rsi_period` | 14 | RSI calculation period |
| `adx_period` | 14 | ADX calculation period |
| `volume_threshold` | 1.3 | Volume spike multiplier |
| `adx_threshold` | 25.0 | ADX trend threshold |
| `notification_score_threshold` | 85 | Min score for desktop alert |
| `DASHBOARD_PORT` | 8050 | Web dashboard port |

## Signal Interpretation

### Confluence Score (0-100)

Each factor contributes points:

| Factor | Criteria | Points |
|--------|----------|--------|
| SMA Position | Price above/below 200 SMA | 20 |
| GEX Positioning | Price above/below Zero Gamma | 25 |
| RSI Momentum | RSI above/below 50 | 20 |
| Volume Confirmation | Volume > 1.3x 20-period avg | 15 |
| Trend Strength | ADX > 25 | 20 |

**Score ranges:**
- **80-100:** High conviction — strong multi-factor alignment
- **60-79:** Medium conviction — most factors aligned
- **40-59:** Low conviction — mixed signals
- **<40:** No signal (shown as NEUTRAL)

### GEX (Gamma Exposure)

- **Above Zero Gamma:** Dealers hedge by selling rallies (price tends to mean-revert up = bullish zone)
- **Below Zero Gamma:** Dealers hedge by buying dips (price tends to accelerate down = bearish zone)
- **Gamma Walls:** Strikes with highest GEX concentration act as support/resistance

## Architecture

```
signal_scanner/
├── config.py               # All tunable parameters
├── main.py                 # Entry point
├── core/
│   ├── ibkr_connector.py   # IBKR + yfinance data
│   ├── gex_calculator.py   # GEX math
│   ├── technical_analyzer.py  # SMA, RSI, ADX, Volume
│   ├── confluence_engine.py   # 5-factor scoring
│   └── watchlist_manager.py   # Symbol lists
├── scanner/
│   ├── multi_symbol_scanner.py  # Main scan loop
│   └── signal_ranker.py        # Rank & filter
├── database/
│   ├── models.py           # SQLite schema
│   └── db_manager.py       # CRUD operations
├── dashboard/
│   ├── app.py              # Plotly Dash init
│   ├── layouts/
│   │   ├── main_view.py    # Signal table
│   │   └── detail_view.py  # Symbol drill-down
│   └── callbacks.py        # Dashboard interactivity
└── utils/
    ├── logger.py           # loguru setup
    └── notifications.py    # Desktop alerts
```

## Troubleshooting

### IBKR Connection Issues
- Ensure TWS/Gateway is running and API is enabled
- Check port matches config (paper=7497, live=7496)
- Only one client ID per connection — change `client_id` if another app uses ID 10
- Firewall may block localhost connections

### yfinance Rate Limiting
- Large watchlists (500+ symbols) will be slow with yfinance
- Scanner uses a rate limiter (10 req/sec) to avoid bans
- Use `--watchlist test` for development

### No Options Data
- Some symbols (especially small-caps) have no options chains
- Scanner logs warnings and continues with `GEX: UNKNOWN`
- These symbols score 0 on the GEX factor (25 pts lost)

### Dashboard Not Loading
- Check port 8050 is free: `netstat -an | findstr 8050`
- Use `--port 9050` to try a different port
- Check firewall settings for localhost

## Data Storage

Signals are stored in `signal_scanner/data/signals.db` (SQLite). The database auto-creates on first run. Old signals are kept for 7 days by default.
