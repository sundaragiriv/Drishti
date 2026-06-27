"""Historical catalyst-check backtest — replay the gate against past entries.

Replays the Tier 1 catalyst rules (8-K material event, strong negative
news, news volume spike) in SQL against the universe of accumulation
candidates, then compares forward 10-day outcomes for FLAGGED vs CLEAN
entries at the live config (2*ATR stop, 1R = 2*ATR target).

Limitations: 8-K data starts 2026-01-08 and news_sentiment starts
2026-02-24, so the valid backtest window is roughly 2026-02-26 through
2026-04-10 (need 10 trading days forward for outcomes). Sample is
narrow but point-in-time-correct because we filter by
e.filed_date / n.published_at against the entry's trade_date.

Run:
    python -m research.catalyst_historical_backtest
"""
import time
import duckdb

t0 = time.time()
conn = duckdb.connect("data/warehouse/sec_intel.duckdb", read_only=True)

print("=" * 75)
print("CATALYST GATE — HISTORICAL BACKTEST")
print("=" * 75)
print("Window: 2026-02-26 (news data ramp) -> 2026-04-10 (10d fwd cushion)")
print("Frame:  2*ATR stops, 1R target, 10-day hold (live paper config)")
print()

# Build the candidate set: every accumulation stock-day where we have full
# catalyst-data backing (8-K + news), with forward prices computable.
conn.execute("""
    CREATE TEMP TABLE candidates AS
    WITH universe AS (
        SELECT ticker, report_quarter, conviction_score, accum_phase,
               COALESCE(triple_lock, FALSE) AS triple_lock,
               COALESCE(ml_score_v2, 0) AS ml_v2,
               CASE
                   WHEN report_quarter LIKE '%-Q1' THEN CAST(LEFT(report_quarter,4)||'-05-15' AS DATE)
                   WHEN report_quarter LIKE '%-Q2' THEN CAST(LEFT(report_quarter,4)||'-08-15' AS DATE)
                   WHEN report_quarter LIKE '%-Q3' THEN CAST(LEFT(report_quarter,4)||'-11-15' AS DATE)
                   WHEN report_quarter LIKE '%-Q4' THEN CAST(CAST(CAST(LEFT(report_quarter,4) AS INT)+1 AS VARCHAR)||'-02-15' AS DATE)
               END AS avail_date,
               CASE
                   WHEN report_quarter LIKE '%-Q1' THEN CAST(LEFT(report_quarter,4)||'-08-14' AS DATE)
                   WHEN report_quarter LIKE '%-Q2' THEN CAST(LEFT(report_quarter,4)||'-11-14' AS DATE)
                   WHEN report_quarter LIKE '%-Q3' THEN CAST(CAST(CAST(LEFT(report_quarter,4) AS INT)+1 AS VARCHAR)||'-02-14' AS DATE)
                   WHEN report_quarter LIKE '%-Q4' THEN CAST(CAST(CAST(LEFT(report_quarter,4) AS INT)+1 AS VARCHAR)||'-05-14' AS DATE)
               END AS expire_date
        FROM intelligence_scores
        WHERE accum_phase IN ('EARLY_ACCUM','ACTIVE_ACCUM','LATE_ACCUM')
    ),
    priced AS (
        SELECT p.ticker, p.trade_date, p.close,
               u.report_quarter, u.conviction_score, u.accum_phase,
               u.triple_lock, u.ml_v2,
               AVG(p.high-p.low) OVER (PARTITION BY p.ticker ORDER BY p.trade_date
                                       ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS atr20,
               LEAD(p.high,1)  OVER w AS h1,  LEAD(p.high,2)  OVER w AS h2,
               LEAD(p.high,3)  OVER w AS h3,  LEAD(p.high,4)  OVER w AS h4,
               LEAD(p.high,5)  OVER w AS h5,  LEAD(p.high,6)  OVER w AS h6,
               LEAD(p.high,7)  OVER w AS h7,  LEAD(p.high,8)  OVER w AS h8,
               LEAD(p.high,9)  OVER w AS h9,  LEAD(p.high,10) OVER w AS h10,
               LEAD(p.low,1)   OVER w AS l1,  LEAD(p.low,2)   OVER w AS l2,
               LEAD(p.low,3)   OVER w AS l3,  LEAD(p.low,4)   OVER w AS l4,
               LEAD(p.low,5)   OVER w AS l5,  LEAD(p.low,6)   OVER w AS l6,
               LEAD(p.low,7)   OVER w AS l7,  LEAD(p.low,8)   OVER w AS l8,
               LEAD(p.low,9)   OVER w AS l9,  LEAD(p.low,10)  OVER w AS l10
        FROM fact_daily_prices p
        INNER JOIN universe u ON p.ticker = u.ticker
            AND p.trade_date >= u.avail_date AND p.trade_date <= u.expire_date
        WHERE p.close > 5 AND p.volume > 0
          AND p.trade_date BETWEEN '2026-02-26' AND '2026-04-10'
        WINDOW w AS (PARTITION BY p.ticker ORDER BY p.trade_date)
    )
    SELECT *,
        -- 1R outcome (close + 2*atr20 reached before close - 2*atr20 stopped)
        CASE WHEN GREATEST(h1,h2,h3,h4,h5,h6,h7,h8,h9,h10) >= close + 2*atr20
              AND LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10) > close - 2*atr20
            THEN 1 ELSE 0 END AS hit_1R,
        CASE WHEN LEAST(l1,l2,l3,l4,l5,l6,l7,l8,l9,l10) <= close - 2*atr20
            THEN 1 ELSE 0 END AS stopped
    FROM priced
    WHERE h10 IS NOT NULL AND l10 IS NOT NULL AND atr20 > 0
""")

n_cand = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
print(f"Candidates: {n_cand:,} stock-days (accumulation universe)")
n_tl = conn.execute("SELECT COUNT(*) FROM candidates WHERE triple_lock=TRUE").fetchone()[0]
print(f"  of which Triple Lock: {n_tl:,}")
print()

# ----------------------------------------------------------------------
# Replay the catalyst gate in SQL — same 3 rules as catalyst_checker.py
# ----------------------------------------------------------------------
conn.execute("""
    CREATE TEMP TABLE flagged AS
    SELECT
        c.*,
        -- Rule 1: material 8-K filed in last 5 days
        (SELECT COUNT(*) FROM fact_form8k_events e
         WHERE e.ticker = c.ticker
           AND e.filed_date BETWEEN c.trade_date - INTERVAL '5' DAY AND c.trade_date
           AND (e.has_earnings OR e.has_acquisition
                OR e.has_officer_change OR e.has_cyber_incident)
        ) AS r1_8k_count,
        -- Rule 2: any negative news in last 48h.
        -- Polygon sentiment_score is only -1 / 0 / +1, so use <= -1.
        -- Tightened "strong negative" version requires 2+ in window (below).
        (SELECT COUNT(*) FROM fact_news_sentiment n
         WHERE n.ticker = c.ticker
           AND n.published_at >= CAST(c.trade_date AS TIMESTAMP) - INTERVAL '48' HOUR
           AND n.published_at <= CAST(c.trade_date AS TIMESTAMP)
           AND n.sentiment_score <= -1
        ) AS r2_negative_count,
        -- Rule 3: news volume spike in last 24h
        (SELECT COUNT(*) FROM fact_news_sentiment n
         WHERE n.ticker = c.ticker
           AND n.published_at >= CAST(c.trade_date AS TIMESTAMP) - INTERVAL '24' HOUR
           AND n.published_at <= CAST(c.trade_date AS TIMESTAMP)
        ) AS r3_news_count
    FROM candidates c
""")

# Compute multiple flag variants — sweep thresholds to see which (if any) helps
conn.execute("""
    CREATE TEMP TABLE labeled AS
    SELECT *,
        -- Variant A: any negative news (>=1) — broadest
        CASE WHEN r1_8k_count > 0 OR r2_negative_count >= 1 OR r3_news_count >= 5
             THEN 1 ELSE 0 END AS flag_any_neg,
        -- Variant B: cluster of negatives (>=2) — moderate
        CASE WHEN r1_8k_count > 0 OR r2_negative_count >= 2 OR r3_news_count >= 5
             THEN 1 ELSE 0 END AS flag_neg_cluster,
        -- Variant C: 8-K only — strictest (drop news rules)
        CASE WHEN r1_8k_count > 0
             THEN 1 ELSE 0 END AS flag_8k_only,
        -- Variant D: 8-K + only-strong-volume-spike (>=10 articles in 24h)
        CASE WHEN r1_8k_count > 0 OR r3_news_count >= 10
             THEN 1 ELSE 0 END AS flag_8k_or_high_vol
    FROM flagged
""")

# ----------------------------------------------------------------------
# Compare outcomes per population
# ----------------------------------------------------------------------
COST_R = 0.075  # at 2*ATR stop frame, see backtest_costs.py


def report(label, where_sql, flag_col="flag_any_neg"):
    sub = conn.execute(f"""
        SELECT
            COUNT(*) FILTER (WHERE {flag_col} = 1) AS n_flagged,
            COUNT(*) FILTER (WHERE {flag_col} = 0) AS n_clean,
            AVG(hit_1R) FILTER (WHERE {flag_col} = 1) AS hit_flag,
            AVG(hit_1R) FILTER (WHERE {flag_col} = 0) AS hit_clean,
            AVG(stopped) FILTER (WHERE {flag_col} = 1) AS stop_flag,
            AVG(stopped) FILTER (WHERE {flag_col} = 0) AS stop_clean
        FROM labeled
        WHERE {where_sql}
    """).fetchone()
    if not sub or (sub[0] or 0) + (sub[1] or 0) < 30:
        print(f"  {label:<30} n<30 — skip")
        return
    n_f, n_c, h_f, h_c, s_f, s_c = (x or 0 for x in sub)
    exp_f = (h_f or 0) * 1.0 - (s_f or 0) * 1.0 - COST_R
    exp_c = (h_c or 0) * 1.0 - (s_c or 0) * 1.0 - COST_R
    delta = exp_c - exp_f  # positive = clean better than flagged
    verdict = ("ADDS" if delta > 0.05 else ("NEUTRAL" if abs(delta) <= 0.05 else "HURTS"))
    print(f"  {label:<30} flag(n={n_f:>4}, net={exp_f:+.3f}R) | clean(n={n_c:>4}, net={exp_c:+.3f}R) | D={delta:+.3f}R [{verdict}]")


print("=" * 75)
print("OUTCOMES @ 1R/2*ATR/10d FRAME")
print("=" * 75)
print(f"(cost = {COST_R:.3f}R per trade — built into 'net' column)\n")

VARIANTS = [
    ("flag_any_neg",     "8-K + any neg news >=1 + vol spike >=5"),
    ("flag_neg_cluster", "8-K + neg cluster >=2     + vol spike >=5"),
    ("flag_8k_only",     "8-K material event ONLY"),
    ("flag_8k_or_high_vol", "8-K + vol spike >=10 (no news rule)"),
]

POPULATIONS = [
    ("ALL accumulation",      "1=1"),
    ("conviction_score>=65",  "conviction_score >= 65"),
    ("Triple Lock",           "triple_lock = TRUE"),
    ("ml_v2 >= 90",           "ml_v2 >= 90"),
]

for flag_col, desc in VARIANTS:
    print(f"\n--- VARIANT: {desc} ---")
    for label, where in POPULATIONS:
        report(label, where, flag_col=flag_col)

# ----------------------------------------------------------------------
# How often did the gate fire?
# ----------------------------------------------------------------------
print()
print("=" * 75)
print("GATE FIRE RATE")
print("=" * 75)
fire = conn.execute("""
    SELECT
        COUNT(*) AS n,
        AVG(flag_any_neg)*100 AS flag_any,
        AVG(flag_neg_cluster)*100 AS flag_clu,
        AVG(flag_8k_only)*100 AS flag_8k,
        AVG(flag_8k_or_high_vol)*100 AS flag_8k_vol,
        AVG(CASE WHEN r1_8k_count>0 THEN 1 ELSE 0 END)*100 AS pct_8k,
        AVG(CASE WHEN r2_negative_count>=1 THEN 1 ELSE 0 END)*100 AS pct_any_neg,
        AVG(CASE WHEN r2_negative_count>=2 THEN 1 ELSE 0 END)*100 AS pct_neg_cluster,
        AVG(CASE WHEN r3_news_count>=5 THEN 1 ELSE 0 END)*100 AS pct_vol5,
        AVG(CASE WHEN r3_news_count>=10 THEN 1 ELSE 0 END)*100 AS pct_vol10
    FROM labeled
""").fetchone()
n, f_any, f_clu, f_8k, f_8kvol, p_8k, p_any_neg, p_neg_clu, p_vol5, p_vol10 = fire
print(f"Universe size:                   {n:,} candidate stock-days\n")
print("Flag rates by variant:")
print(f"  any_neg (broadest):            {f_any:>5.1f}%  ({int(f_any*n/100):,} blocked)")
print(f"  neg_cluster (>=2 neg articles):{f_clu:>5.1f}%")
print(f"  8-K only:                      {f_8k:>5.1f}%")
print(f"  8-K + high vol (>=10):         {f_8kvol:>5.1f}%")
print()
print("Component rule fire rates (any one trigger):")
print(f"  8-K material event:            {p_8k:>5.1f}%")
print(f"  any negative news:             {p_any_neg:>5.1f}%")
print(f"  >=2 negative news:             {p_neg_clu:>5.1f}%")
print(f"  news vol >=5 in 24h:           {p_vol5:>5.1f}%")
print(f"  news vol >=10 in 24h:          {p_vol10:>5.1f}%")

print(f"\nDone in {time.time() - t0:.1f}s")
