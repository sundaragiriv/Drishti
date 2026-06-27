# Drishti v2 — Overnight Redesign

**Shipped:** 2026-06-27 (overnight) · **Tagline:** Road to 10 Million

This redesign is honest. Nothing is delete-destroyed; every hidden surface is
one boolean flip from coming back. The visible product has been tightened around
*what we proved has edge* and styled to feel like a new app — not the old one
with a paint job.

---

## What changed (visible)

### 1. New visual system — `drishti_theme.css`
A refined visual layer loaded **on top of** the legacy `dark_theme.css`. Drops
in new tokens (`--dr-*`) and remaps legacy `--kb-*` vars so existing styles
inherit the new palette automatically. No legacy CSS deleted.

- **Palette:** warmer near-black (`#0a0a0e`), amber-400 gold (`#fbbf24`),
  emerald (`#10b981`) + rose (`#f43f5e`) for direction.
- **Typography:** Inter (body) + Space Grotesk (display / brand) +
  JetBrains Mono (numbers, tabular figures).
- **Navbar:** glass effect (backdrop-filter blur + saturation), active links
  in subtle gold tint.
- **Icons:** Phosphor (regular/fill/duotone) added. Legacy FontAwesome still
  works for unchanged code.
- **Motion:** `dr-fade-in` 420ms fade-up on load, card hover lifts.

### 2. Hero status row (replaces the small pill banner as the eye anchor)
Two cards above the legacy banner:

- **Regime hero card (left, dominant):** duotone icon, regime label, an
  *actionable guidance sentence* (e.g. "LONGs allowed with tight stops"), and
  LONG/SHORT allowed/blocked chips. Border-left color codes the state.
- **Road to 10M tracker (right):** current realized P&L, today's delta,
  %-of-$10M goal, plus a cumulative-PnL sparkline.

Source: `signal_scanner/dashboard/drishti_callbacks.py`.

### 3. Director-Cluster Watchlist — the validated edge, surfaced
A new section above the Sniper Board Top-10. Shows recent **Director-led
insider clusters** with their hold-window status.

- Definition: ≥2 distinct insiders bought (open-market, `transaction_code='P'`)
  in trailing 30 days, ≥1 of them a Director.
- Point-in-time correct: cluster "knowable" at `transaction_date + 2 SEC-lag days`.
- Each card shows: ticker, # Directors / total insiders, avg buy → current
  price, return since cluster, days remaining in 60-day hold window.
- Greys out once the 60-day window expires.

Why this matters: the `--pond-alone` backtest
(`research/pond_trigger_backtest.py`) measured this exact signal at
**+5.93% / 55.8% win at 60d** for Director-led clusters vs **+2.86% / 52.0%**
baseline. This is the validated swing edge — now it's a first-class UI surface.

Source: `signal_scanner/dashboard/director_clusters.py` (read-only).

### 4. Per-row "Why?" tags on the Sniper Board
New `Why` column showing each row's slow-edge source(s):

- `DIR` — ticker is currently inside an active Director-led cluster window
  (the validated edge — rendered in gold).
- `TRIPLE` — Triple Lock setup (lighter gold).
- `SQ` — squeeze candidate.
- `ACCUM` — institutional accumulation phase.
- `13F` — institutional thesis row (always present; it's the source pond).

A row that lights up `DIR · TRIPLE · ACCUM · 13F` is your highest-conviction
setup — all four slow edges aligned.

---

## What's hidden (from the Drishti v1 prune, all reversible)

Visible nav reduced from 6 → 3 (Swing · Intraday · P&L Ledger). Hidden via
`style={"display":"none"}` (one-line flip to bring back):

- Options nav tab
- Forecast nav tab (ML signal v2 AUC ≈ 0.56)
- Intelligence nav tab (8 sub-tabs of unvalidated Kubera scoring)
- Intraday → Confluence (Sniper) sub-tab (vestigial, never populated)
- Sniper Board AI Convergence +10% EV booster — guarded by
  `INJECT_CONVERGENCE_SIGNALS = False` in `sniper_callbacks.py`

Nothing deleted. Folder and package names (`signal_scanner`, repo path)
untouched.

---

## What's deferred

These would have been nice tonight but skipped to keep what shipped tested
and shippable:

- **GEX first-class panel.** We already have `gex_calculator.py`; the spec
  was right that GEX is the genuine differentiator. Next session: a panel
  with zero-gamma + walls visualization for SPY/QQQ + top sniper names.
- **"Why is this name here?" detail expansion** when you click a tag —
  showing the actual Director names + dates of the cluster.
- **Pre-market / post-market briefing card** generated each day:
  regime + top setups + what NOT to do.

---

## How to view tomorrow

```powershell
# Start TWS / IBKR Gateway as usual (paper port 7497)
cd e:\Quant-Bridge
.\quant-bridge.bat                  # full scanner + dashboard (live)
# or:
.venv\Scripts\python.exe run_dashboard.py    # dashboard only (no IBKR)
```

Open http://127.0.0.1:8050 — you should see:

1. **DRISHTI** in white with **Road to 10 Million** in gold underneath
2. A dominant **regime hero card** with current state + guidance
3. **Road to 10M** equity sparkline next to it
4. **Director-Cluster Watchlist** as the first content section on Swing
5. The Sniper Board with a new **Why** column highlighting `DIR` rows in gold

If the design looks off, hard-refresh the browser (Ctrl+Shift+R) — Dash caches
CSS aggressively.

---

## Honest caveats

- **Today's regime read may be CRASH** — that's what the HMM model fit on the
  available SPY data is saying. The regime card surfaces it honestly with
  "Sit out" guidance. Reality is the reality.
- **Director cluster cards show real recent clusters** — some have run hard
  already (EML +12.6%, PBLS +12.4%), some are flat or down. The card greys out
  once the 60-day window closes.
- **Equity tracker reads from `paper_trades` in `signals.db`** — if there are
  no closed trades yet, it shows `$0` and "no closed trades yet". Real signal
  starts once paper trades close.
- **`drishti_theme.css` is additive**, not a replacement. If anything looks
  wrong, it's safe to delete that one file to revert to legacy theme.

---

## Files changed

```
A  signal_scanner/dashboard/assets/drishti_theme.css     # new visual layer
A  signal_scanner/dashboard/director_clusters.py         # cluster watchlist data
A  signal_scanner/dashboard/drishti_callbacks.py         # hero + clusters callbacks
M  signal_scanner/dashboard/app.py                       # +Phosphor +Space Grotesk
M  signal_scanner/dashboard/layouts/main_view.py         # hero cards + brand
M  signal_scanner/dashboard/layouts/sniper_board_view.py # Why column + clusters section
M  signal_scanner/dashboard/sniper_callbacks.py          # why_tags builder
M  signal_scanner/main.py                                # register Drishti callbacks
M  run_dashboard.py                                      # register Drishti callbacks
```

All committed as `9b6af30` and pushed to `sundaragiriv/Drishti`.
