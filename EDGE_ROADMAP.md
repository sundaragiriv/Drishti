# Edge Roadmap (Tomorrow Checkpoint)

## Goal
Increase expectancy by reducing low-quality entries, then raise win rate from current baseline using controlled gates.

## Baseline We Will Track
- `Paper Win %` (closed paper trades)
- `Persisted Alert Win % (30m horizon, score>=60)` from `signals` table
- `Avg signed move %` for persisted BUY/SELL alerts
- `Stop-loss exit share %` of all losing trades

## Phase 1: Tighten Entry Quality (Immediate)
- Entry only when all are true:
  - `score >= 70`
  - `rr_ratio >= 1.8`
  - `signal_age >= 2`
  - `market_regime` aligned with direction (no LONG in `RISK_OFF`, no SHORT in `RISK_ON`)
  - `volume_ratio >= 1.2`
- Time filter:
  - Skip `MID_DAY` by default.
  - Allow `MID_DAY` only if `score >= 80` and `adx >= 25`.

## Phase 2: Stop-Loss Improvements
- Stop policy:
  - `max(ATR stop, structure stop)` where structure stop is recent swing level.
  - Reject trade if stop distance implies `rr_ratio < 1.8`.
- Exit policy:
  - Partial at `T1` (40-50% size), trail remainder.
  - Time stop: if no follow-through after `N` bars, reduce/exit.

## Phase 3: Anti-Whipsaw Controls
- Cooldown:
  - After stop-out on symbol, no re-entry for `2` scan cycles.
- Loss cluster rule:
  - If symbol has `>=3` losses in last `5` closed trades, skip until one strong winner appears.

## Tomorrow Acceptance Criteria
- `Persisted Alert Win % (30m)` improves by at least `+8 pts` vs today baseline.
- `Stop-loss exit share` decreases by at least `15%` relative.
- `Avg signed move %` on alerts remains positive.
- Paper realized P&L is non-negative for the day.

## Risk/Reality Constraints
- No guarantee of daily profit is possible.
- Objective is robust positive expectancy and lower drawdown, not overfit win rate inflation.
