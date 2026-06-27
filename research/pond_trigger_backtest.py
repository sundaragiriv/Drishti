"""Pond -> Trigger architecture: validation harness.

Gate 0 (this file, --census): point-in-time SAMPLE CENSUS.
  - Build the insider-cluster pond (>=2 distinct insiders, open-market BUYS,
    in a trailing 30d window), made "knowable" with a +2 trading-day SEC lag.
  - Build the daily trigger (20-day closing-high breakout + RVol>1.5) with a
    liquidity floor.
  - Count: trigger events INSIDE the pond (Strong-tier candidate trades) vs
    trigger events on the FULL universe (baseline) — per year.

Answers the only Gate 0 question: is the Strong tier tradeable (enough sample),
and does the pond concentrate trigger quality vs the whole market?

Point-in-time discipline:
  - Form-4 cluster known at transaction_date + 2 trading days (filing lag).
  - Trigger uses only PRIOR 20 bars (no look-ahead).
  - Universe is whatever traded on each date (survivorship-free, from prices).

Run:  .venv\\Scripts\\python -m research.pond_trigger_backtest --census
"""

from __future__ import annotations

import argparse

import duckdb

WAREHOUSE = r"e:\Quant-Bridge\data\warehouse\sec_intel.duckdb"

START = "2020-01-01"
END = "2025-12-31"
CLUSTER_WINDOW_DAYS = 30      # >=2 insiders within this trailing window
THESIS_HOLD_DAYS = 90         # pond membership lasts ~90 cal days (~60 trading)
RVOL_MIN = 1.5
MIN_PRICE = 5.0               # liquidity floor: price
MIN_ADV_DOLLARS = 1_000_000   # liquidity floor: 20d avg dollar volume


def run_census() -> None:
    con = duckdb.connect(WAREHOUSE, read_only=True)

    # ---- Insider-cluster pond (PIT: known at last buy + 2 trading days) ----
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE clusters AS
        WITH buys AS (
            SELECT ticker, insider_name, transaction_date
            FROM fact_form4_transactions
            WHERE transaction_code = 'P' AND upper(direction) = 'BUY'
              AND ticker IS NOT NULL AND ticker <> ''
              AND transaction_date BETWEEN DATE '2019-06-01' AND DATE '{END}'
        ),
        clustered AS (
            SELECT b.ticker, b.transaction_date AS d,
                   COUNT(DISTINCT b2.insider_name) AS n_insiders
            FROM buys b
            JOIN buys b2
              ON b2.ticker = b.ticker
             AND b2.transaction_date BETWEEN b.transaction_date - INTERVAL '{CLUSTER_WINDOW_DAYS}' DAY
                                         AND b.transaction_date
            GROUP BY b.ticker, b.transaction_date
        )
        SELECT ticker,
               (d + INTERVAL '2' DAY)::DATE AS known_date,   -- +2d SEC filing lag
               (d + INTERVAL '{THESIS_HOLD_DAYS + 2}' DAY)::DATE AS expiry_date
        FROM clustered
        WHERE n_insiders >= 2
    """)
    n_clusters = con.execute("SELECT count(*) FROM clusters").fetchone()[0]
    n_cluster_tickers = con.execute("SELECT count(DISTINCT ticker) FROM clusters").fetchone()[0]

    # ---- Daily trigger: 20d closing-high breakout + RVol, liquidity floor ----
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE triggers AS
        WITH px AS (
            SELECT ticker, trade_date, close, volume,
                   MAX(close)  OVER w AS hi20,
                   AVG(volume) OVER w AS vol20
            FROM fact_daily_prices
            WHERE trade_date BETWEEN DATE '{START}' AND DATE '{END}'
              AND close IS NOT NULL AND volume IS NOT NULL
            WINDOW w AS (PARTITION BY ticker ORDER BY trade_date
                         ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING)
        )
        SELECT ticker, trade_date, close
        FROM px
        WHERE hi20 IS NOT NULL AND vol20 > 0
          AND close > hi20                       -- 20-day breakout
          AND volume > {RVOL_MIN} * vol20        -- relative volume
          AND close >= {MIN_PRICE}               -- liquidity: price floor
          AND (vol20 * close) >= {MIN_ADV_DOLLARS}  -- liquidity: ADV $ floor
    """)
    n_trig_universe = con.execute("SELECT count(*) FROM triggers").fetchone()[0]

    # ---- Pond triggers: trigger fired while ticker was in an active cluster ----
    con.execute("""
        CREATE OR REPLACE TEMP TABLE pond_triggers AS
        SELECT DISTINCT t.ticker, t.trade_date, t.close
        FROM triggers t
        JOIN clusters c
          ON c.ticker = t.ticker
         AND t.trade_date BETWEEN c.known_date AND c.expiry_date
    """)
    n_trig_pond = con.execute("SELECT count(*) FROM pond_triggers").fetchone()[0]
    n_pond_trig_tickers = con.execute("SELECT count(DISTINCT ticker) FROM pond_triggers").fetchone()[0]

    # ---- Base rates for a first independence hint ----
    universe_days = con.execute(f"""
        SELECT count(*) FROM fact_daily_prices
        WHERE trade_date BETWEEN DATE '{START}' AND DATE '{END}'
          AND close >= {MIN_PRICE}
    """).fetchone()[0]
    pond_name_days = con.execute("""
        SELECT count(*) FROM fact_daily_prices p
        WHERE EXISTS (SELECT 1 FROM clusters c
                      WHERE c.ticker = p.ticker
                        AND p.trade_date BETWEEN c.known_date AND c.expiry_date)
    """).fetchone()[0]

    by_year = con.execute("""
        SELECT EXTRACT(year FROM trade_date) AS yr, count(*) FROM pond_triggers GROUP BY yr ORDER BY yr
    """).fetchall()

    base_rate = (n_trig_universe / universe_days * 100) if universe_days else 0
    pond_rate = (n_trig_pond / pond_name_days * 100) if pond_name_days else 0

    print("=" * 64)
    print("GATE 0 — SAMPLE CENSUS (insider-cluster pond x daily trigger)")
    print(f"window {START}..{END} | hold {THESIS_HOLD_DAYS}d | RVol>{RVOL_MIN} "
          f"| price>=${MIN_PRICE:.0f} | ADV>=${MIN_ADV_DOLLARS:,}")
    print("=" * 64)
    print(f"Insider clusters (>=2 insiders/30d) : {n_clusters:,}  across {n_cluster_tickers:,} tickers")
    print(f"Trigger events — FULL universe      : {n_trig_universe:,}")
    print(f"Trigger events — INSIDE pond (Strong): {n_trig_pond:,}  across {n_pond_trig_tickers:,} tickers")
    print(f"  Strong-tier trades / year         : {n_trig_pond / 6:,.0f}")
    print("-" * 64)
    print("First independence hint (trigger fire-rate per name-day):")
    print(f"  full universe : {base_rate:.3f}%   ({n_trig_universe:,} / {universe_days:,})")
    print(f"  inside pond   : {pond_rate:.3f}%   ({n_trig_pond:,} / {pond_name_days:,})")
    if base_rate > 0:
        print(f"  pond lift     : {pond_rate / base_rate:.2f}x  (>1 = pond concentrates triggers)")
    print("-" * 64)
    print("Strong-tier trades by year:")
    for yr, n in by_year:
        print(f"  {int(yr)}: {n:,}")
    print("=" * 64)
    con.close()


def run_gate1() -> None:
    """Gate 1 — independence by OUTCOME: do pond-triggers beat universe-triggers?

    Fixed-horizon forward returns (close[t] -> close[t+N]), no stops, so it's a
    clean apples-to-apples comparison of signal QUALITY, not a strategy P&L.
    Entry = trigger-day close (EOD signal). Survivorship in the forward window
    is symmetric across both groups, so the comparison stays fair.
    """
    con = duckdb.connect(WAREHOUSE, read_only=True)

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE clusters AS
        WITH buys AS (
            SELECT ticker, insider_name, transaction_date
            FROM fact_form4_transactions
            WHERE transaction_code = 'P' AND upper(direction) = 'BUY'
              AND ticker IS NOT NULL AND ticker <> ''
              AND transaction_date BETWEEN DATE '2019-06-01' AND DATE '{END}'
        ),
        clustered AS (
            SELECT b.ticker, b.transaction_date AS d,
                   COUNT(DISTINCT b2.insider_name) AS n_insiders
            FROM buys b JOIN buys b2
              ON b2.ticker = b.ticker
             AND b2.transaction_date BETWEEN b.transaction_date - INTERVAL '{CLUSTER_WINDOW_DAYS}' DAY
                                         AND b.transaction_date
            GROUP BY b.ticker, b.transaction_date
        )
        SELECT ticker, (d + INTERVAL '2' DAY)::DATE AS known_date,
               (d + INTERVAL '{THESIS_HOLD_DAYS + 2}' DAY)::DATE AS expiry_date
        FROM clustered WHERE n_insiders >= 2
    """)

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE trig AS
        WITH px AS (
            SELECT ticker, trade_date, close, volume,
                   MAX(close)  OVER w AS hi20,
                   AVG(volume) OVER w AS vol20,
                   LEAD(close, 5)  OVER o AS c5,
                   LEAD(close, 10) OVER o AS c10,
                   LEAD(close, 20) OVER o AS c20
            FROM fact_daily_prices
            WHERE trade_date BETWEEN DATE '{START}' AND DATE '{END}'
              AND close IS NOT NULL AND volume IS NOT NULL
            WINDOW w AS (PARTITION BY ticker ORDER BY trade_date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING),
                   o AS (PARTITION BY ticker ORDER BY trade_date)
        )
        SELECT ticker, trade_date, close,
               (c5/close - 1)*100  AS r5,
               (c10/close - 1)*100 AS r10,
               (c20/close - 1)*100 AS r20
        FROM px
        WHERE hi20 IS NOT NULL AND vol20 > 0
          AND close > hi20 AND volume > {RVOL_MIN} * vol20
          AND close >= {MIN_PRICE} AND (vol20 * close) >= {MIN_ADV_DOLLARS}
    """)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE trig_pond AS
        SELECT DISTINCT t.* FROM trig t
        JOIN clusters c ON c.ticker = t.ticker
         AND t.trade_date BETWEEN c.known_date AND c.expiry_date
    """)

    def stats(tbl, col):
        return con.execute(f"""
            SELECT count({col}), avg({col}), median({col}),
                   100.0*sum(CASE WHEN {col} > 0 THEN 1 ELSE 0 END)/count({col}),
                   stddev_samp({col})
            FROM {tbl} WHERE {col} IS NOT NULL
        """).fetchone()

    print("=" * 70)
    print("GATE 1 — INDEPENDENCE BY OUTCOME (forward return: universe vs pond)")
    print("=" * 70)
    for horizon, col in (("5-day", "r5"), ("10-day", "r10"), ("20-day", "r20")):
        u = stats("trig", col)
        p = stats("trig_pond", col)
        print(f"\n[{horizon} forward return]")
        print(f"  {'group':18s} {'n':>7s} {'mean%':>8s} {'med%':>7s} {'win%':>7s} {'sharpe*':>8s}")
        for name, s in (("universe trigger", u), ("POND trigger", p)):
            n, mean, med, win, sd = s
            sharpe = (mean / sd) if sd else 0
            print(f"  {name:18s} {n:>7,} {mean:>8.2f} {med:>7.2f} {win:>7.1f} {sharpe:>8.3f}")
    # Unconditional baseline (just being long a random liquid name)
    base = con.execute(f"""
        WITH px AS (
            SELECT close, LEAD(close,10) OVER (PARTITION BY ticker ORDER BY trade_date) AS c10,
                   AVG(volume) OVER (PARTITION BY ticker ORDER BY trade_date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS vol20
            FROM fact_daily_prices WHERE trade_date BETWEEN DATE '{START}' AND DATE '{END}'
              AND close >= {MIN_PRICE}
        )
        SELECT avg((c10/close-1)*100), 100.0*sum(CASE WHEN c10>close THEN 1 ELSE 0 END)/count(*)
        FROM px WHERE c10 IS NOT NULL AND vol20*close >= {MIN_ADV_DOLLARS}
    """).fetchone()
    print(f"\n  baseline (any liquid name, 10d): mean {base[0]:.2f}%  win {base[1]:.1f}%")
    print("  * sharpe = mean/stddev per-trade (not annualized) — relative quality proxy")
    print("=" * 70)
    con.close()


def run_pond_alone() -> None:
    """Does the insider-cluster signal have edge ON ITS OWN (no trigger)?

    Entry = first close on/after cluster-known date (+2d filing lag already in).
    Forward 20/40/60 trading-day returns vs unconditional baseline. Stratified
    by cluster strength (>=3 insiders) and by Director involvement.
    """
    con = duckdb.connect(WAREHOUSE, read_only=True)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE clusters AS
        WITH buys AS (
            SELECT ticker, insider_name, insider_role, transaction_date
            FROM fact_form4_transactions
            WHERE transaction_code = 'P' AND upper(direction) = 'BUY'
              AND ticker IS NOT NULL AND ticker <> ''
              AND transaction_date BETWEEN DATE '2019-06-01' AND DATE '{END}'
        ),
        clustered AS (
            SELECT b.ticker, b.transaction_date AS d,
                   COUNT(DISTINCT b2.insider_name) AS n_insiders,
                   MAX(CASE WHEN b2.insider_role ILIKE '%director%' THEN 1 ELSE 0 END) AS is_director
            FROM buys b JOIN buys b2
              ON b2.ticker = b.ticker
             AND b2.transaction_date BETWEEN b.transaction_date - INTERVAL '{CLUSTER_WINDOW_DAYS}' DAY
                                         AND b.transaction_date
            GROUP BY b.ticker, b.transaction_date
        )
        SELECT ticker, (d + INTERVAL '2' DAY)::DATE AS known_date, n_insiders, is_director
        FROM clustered WHERE n_insiders >= 2
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE entries AS
        WITH px AS (
            SELECT ticker, trade_date, close,
                   LEAD(close,20) OVER o AS c20, LEAD(close,40) OVER o AS c40, LEAD(close,60) OVER o AS c60
            FROM fact_daily_prices
            WHERE trade_date BETWEEN DATE '2019-12-01' AND DATE '{END}' AND close IS NOT NULL
            WINDOW o AS (PARTITION BY ticker ORDER BY trade_date)
        ),
        ent AS (
            SELECT c.ticker, c.n_insiders, c.is_director, p.close,
                   (p.c20/p.close-1)*100 AS r20, (p.c40/p.close-1)*100 AS r40, (p.c60/p.close-1)*100 AS r60,
                   ROW_NUMBER() OVER (PARTITION BY c.ticker, c.known_date ORDER BY p.trade_date) AS k
            FROM clusters c JOIN px p
              ON p.ticker = c.ticker AND p.trade_date >= c.known_date
             AND p.trade_date <= c.known_date + INTERVAL '7' DAY
            WHERE p.close >= {MIN_PRICE}
        )
        SELECT * FROM ent WHERE k = 1
    """)

    def stats(where, col):
        return con.execute(f"""
            SELECT count({col}), avg({col}), median({col}),
                   100.0*sum(CASE WHEN {col}>0 THEN 1 ELSE 0 END)/count({col})
            FROM entries WHERE {col} IS NOT NULL AND {where}
        """).fetchone()

    print("=" * 70)
    print("POND-ALONE — insider clusters, no trigger (forward return vs baseline)")
    print("=" * 70)
    groups = [("all clusters (>=2)", "1=1"),
              (">=3 insiders", "n_insiders >= 3"),
              ("Director involved", "is_director = 1")]
    for horizon, col in (("20-day", "r20"), ("40-day", "r40"), ("60-day", "r60")):
        print(f"\n[{horizon} forward return]")
        print(f"  {'group':22s} {'n':>7s} {'mean%':>8s} {'med%':>7s} {'win%':>7s}")
        for name, where in groups:
            n, mean, med, win = stats(where, col)
            print(f"  {name:22s} {n:>7,} {mean:>8.2f} {med:>7.2f} {win:>7.1f}")
        b = con.execute(f"""
            WITH px AS (SELECT close, LEAD(close,{20 if col=='r20' else 40 if col=='r40' else 60}) OVER (PARTITION BY ticker ORDER BY trade_date) AS cf
                        FROM fact_daily_prices WHERE trade_date BETWEEN DATE '{START}' AND DATE '{END}' AND close >= {MIN_PRICE})
            SELECT avg((cf/close-1)*100), 100.0*sum(CASE WHEN cf>close THEN 1 ELSE 0 END)/count(*) FROM px WHERE cf IS NOT NULL
        """).fetchone()
        print(f"  {'baseline (any name)':22s} {'':>7s} {b[0]:>8.2f} {'':>7s} {b[1]:>7.1f}")
    print("=" * 70)
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--census", action="store_true", help="Gate 0 sample census")
    ap.add_argument("--gate1", action="store_true", help="Gate 1 independence by outcome")
    ap.add_argument("--pond-alone", action="store_true", help="Insider-cluster edge without trigger")
    args = ap.parse_args()
    if args.census:
        run_census()
    if args.gate1:
        run_gate1()
    if args.pond_alone:
        run_pond_alone()
    if not (args.census or args.gate1 or args.pond_alone):
        print("pass --census and/or --gate1 and/or --pond-alone")
