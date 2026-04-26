"""Catalyst-check A/B experiment — outcomes report.

Read paper_trades, group by cohort, report:
  - Sample sizes per cohort
  - Catalyst flag rates (so we can see how often the gate would fire)
  - Hit rate (target_1 reached) per cohort
  - Net P&L per cohort
  - For Cohort A only: hit-rate of "would-have-been-blocked" trades
    (the missed-edge / saved-loss decomposition)

Run after at least ~50 closed trades per cohort accumulate (target ~30 days).

Usage:
    python -m research.catalyst_ab_report
"""
import sqlite3
from pathlib import Path

DB = Path("signal_scanner/data/signals.db")


def _q(conn: sqlite3.Connection, sql: str, *args):
    return conn.execute(sql, args).fetchone()


def main() -> None:
    if not DB.exists():
        print(f"signals.db not found at {DB}")
        return
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print("=" * 70)
    print("CATALYST-CHECK A/B REPORT")
    print("=" * 70)

    # Sample sizes
    by_cohort = conn.execute("""
        SELECT cohort, status, COUNT(*) AS n
        FROM paper_trades
        WHERE cohort IN ('A','B') AND recommendation_source LIKE '%TRIPLE%'
                                  OR recommendation_source LIKE 'SWING_IDEA%'
                                  OR recommendation_source LIKE 'AI_TRIPLE_LOCK%'
        GROUP BY cohort, status
        ORDER BY cohort, status
    """).fetchall()
    if not by_cohort:
        print("\nNo cohort-tagged trades yet. Wait for IdeaBridge to enter trades.")
        return

    print(f"\n{'Cohort':<8} {'Status':<10} {'N':>6}")
    print("-" * 30)
    for r in by_cohort:
        print(f"{r['cohort']:<8} {r['status']:<10} {r['n']:>6}")

    # Catalyst flag rate per cohort (regardless of action)
    flag_rates = conn.execute("""
        SELECT cohort,
               COUNT(*) AS n,
               SUM(catalyst_flag) AS flagged
        FROM paper_trades
        WHERE cohort IN ('A','B')
        GROUP BY cohort
    """).fetchall()
    print("\n--- Catalyst flag observed rate (per cohort, all entered) ---")
    for r in flag_rates:
        n = r['n']
        flagged = r['flagged'] or 0
        pct = (flagged / n * 100) if n else 0
        print(f"  {r['cohort']}: {flagged}/{n} ({pct:.1f}%) flagged")
    print("  (Cohort B blocks flagged ones, so its 'flagged' should be 0.)")

    # Closed-trade hit rate + P&L per cohort
    pnl = conn.execute("""
        SELECT cohort,
               COUNT(*) AS n,
               AVG(CASE WHEN realized_pnl > 0 THEN 1.0 ELSE 0.0 END) AS hit_rate,
               SUM(realized_pnl) AS total_pnl,
               AVG(realized_pnl) AS avg_pnl,
               AVG(realized_pnl_pct) AS avg_pnl_pct
        FROM paper_trades
        WHERE cohort IN ('A','B') AND status = 'CLOSED'
        GROUP BY cohort
    """).fetchall()
    if pnl:
        print("\n--- CLOSED trade outcomes per cohort ---")
        print(f"{'Cohort':<8} {'N':>6} {'Hit%':>7} {'Total $':>10} {'Avg $':>9} {'Avg %':>8}")
        for r in pnl:
            n = r['n']
            print(f"{r['cohort']:<8} {n:>6} "
                  f"{(r['hit_rate'] or 0)*100:>6.1f}% "
                  f"{r['total_pnl'] or 0:>+9,.2f} "
                  f"{r['avg_pnl'] or 0:>+8,.2f} "
                  f"{r['avg_pnl_pct'] or 0:>+7.2f}%")

    # Cohort A counterfactual: what would B have blocked?
    print("\n--- Cohort A counterfactual: would-have-been-blocked trades ---")
    cf = conn.execute("""
        SELECT
            COUNT(*) AS n_blocked_in_a,
            AVG(CASE WHEN realized_pnl > 0 THEN 1.0 ELSE 0.0 END) AS hit_rate,
            SUM(realized_pnl) AS pnl_blocked
        FROM paper_trades
        WHERE cohort = 'A' AND catalyst_flag = 1 AND status = 'CLOSED'
    """).fetchone()
    if cf and cf['n_blocked_in_a']:
        n = cf['n_blocked_in_a']
        hit = (cf['hit_rate'] or 0) * 100
        pnl_b = cf['pnl_blocked'] or 0
        print(f"  N flagged-but-entered (A): {n}")
        print(f"  Their hit rate:            {hit:.1f}%")
        print(f"  Their cumulative $ P&L:    ${pnl_b:+,.2f}")
        if pnl_b > 0:
            print("  -> Cohort B SAVED nothing — it blocked profitable trades.")
        elif pnl_b < 0:
            print("  -> Cohort B SAVED money by blocking these. Filter has edge.")
        else:
            print("  -> Inconclusive: blocked trades net to ~0.")
    else:
        print("  No flagged-and-closed trades in Cohort A yet.")

    print()


if __name__ == "__main__":
    main()
