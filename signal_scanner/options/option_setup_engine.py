"""Generate option setup ideas from stock recommendations."""

from datetime import datetime, timedelta, timezone
from math import ceil, floor
from typing import Dict, List, Optional

from loguru import logger

from signal_scanner.config import ScannerConfig
from signal_scanner.database.db_manager import DatabaseManager

try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except ImportError:
    NY_TZ = timezone.utc


class OptionSetupEngine:
    """Creates lightweight contract ideas (not live option execution)."""

    def __init__(self, db: DatabaseManager, config: Optional[ScannerConfig] = None) -> None:
        self._db = db
        self._cfg = config or ScannerConfig()

    def _past_late_entry_cutoff(self) -> bool:
        """Return True if current ET time is past the late-entry cutoff."""
        now_et = datetime.now(timezone.utc).astimezone(NY_TZ) if NY_TZ else datetime.now(timezone.utc)
        cutoff_hour = int(self._cfg.late_entry_cutoff_hour)
        cutoff_minute = int(self._cfg.late_entry_cutoff_minute)
        return (now_et.hour, now_et.minute) >= (cutoff_hour, cutoff_minute)

    def process_rows(self, mtf_rows: List[Dict]) -> None:
        """Persist option setups from high-conviction BUY/SELL recommendations."""
        now_iso = datetime.now(timezone.utc).isoformat()
        context_by_symbol = {str(r.get("symbol") or "").upper(): r for r in (mtf_rows or []) if r.get("symbol")}

        if not mtf_rows:
            self._validate_existing_setups(context_by_symbol, now_iso)
            self._auto_enter_option_paper_trades(now_iso)
            self._db.expire_old_option_setups(keep_days=3)
            self._db.evaluate_option_setup_outcomes(horizons_minutes=[30, 60, 1440])
            return

        # Late-entry cutoff — only validate existing ideas, skip new idea creation.
        past_cutoff = self._past_late_entry_cutoff()
        if past_cutoff:
            logger.info("OPTIONS: past late-entry cutoff — skipping new idea creation")

        # Config-driven thresholds (no magic numbers).
        min_score = float(self._cfg.paper_entry_min_score)
        min_rr = float(self._cfg.paper_entry_min_rr)
        min_mtf = float(self._cfg.paper_entry_min_mtf_score)
        allowed_sessions = {
            str(s).upper()
            for s in (self._cfg.paper_entry_allowed_sessions or [])
            if str(s).strip()
        }
        min_dist = float(self._cfg.paper_entry_min_distance_to_level_pct)

        candidates = 0
        filter_reasons: Dict[str, int] = {}

        for row in mtf_rows:
            if past_cutoff:
                break

            signal = row.get("signal")
            score = float(row.get("score") or 0.0)
            rr = float(row.get("rr_ratio") or 0.0)
            price = float(row.get("price") or 0.0)
            mtf_score = float(row.get("mtf_score") or 0.0)
            signal_age = int(row.get("signal_age") or 0)
            session_time = str(row.get("session_time") or "")
            regime = str(row.get("market_regime") or "")
            gex = str(row.get("gex_status") or "")
            atr = float(row.get("atr") or 0.0)
            dist_res = _as_float(row.get("distance_to_resistance_pct"))
            dist_sup = _as_float(row.get("distance_to_support_pct"))
            sym = str(row.get("symbol") or "")

            if signal not in ("LONG", "SHORT"):
                continue
            candidates += 1
            if score < min_score or rr < min_rr or price <= 0:
                filter_reasons["score/rr/price"] = filter_reasons.get("score/rr/price", 0) + 1
                continue
            # Allow age=1 when MTF score is very high (all timeframes agree)
            effective_min_age = 1 if mtf_score >= 0.90 else 2
            if mtf_score < min_mtf or signal_age < effective_min_age:
                filter_reasons[f"mtf({mtf_score:.2f}<{min_mtf})/age({signal_age}<{effective_min_age})"] = filter_reasons.get(f"mtf({mtf_score:.2f}<{min_mtf})/age({signal_age}<{effective_min_age})", 0) + 1
                continue
            if allowed_sessions and session_time not in allowed_sessions:
                filter_reasons[f"session({session_time})"] = filter_reasons.get(f"session({session_time})", 0) + 1
                continue
            # Only block when GEX actively contradicts signal direction.
            # UNKNOWN means no data — allow through (IBKR may not have Greeks outside market hours).
            if gex != "UNKNOWN":
                if signal == "LONG" and gex == "BELOW_ZERO_GAMMA":
                    filter_reasons[f"gex_contradicts(LONG+{gex})"] = filter_reasons.get(f"gex_contradicts(LONG+{gex})", 0) + 1
                    continue
                if signal == "SHORT" and gex == "ABOVE_ZERO_GAMMA":
                    filter_reasons[f"gex_contradicts(SHORT+{gex})"] = filter_reasons.get(f"gex_contradicts(SHORT+{gex})", 0) + 1
                    continue
            if regime == "RISK_OFF" and signal == "LONG":
                filter_reasons["regime_block"] = filter_reasons.get("regime_block", 0) + 1
                continue
            if regime == "RISK_ON" and signal == "SHORT":
                filter_reasons["regime_block"] = filter_reasons.get("regime_block", 0) + 1
                continue
            if min_dist > 0:
                if signal == "LONG" and dist_res is not None and dist_res < min_dist:
                    filter_reasons["dist_to_level"] = filter_reasons.get("dist_to_level", 0) + 1
                    continue
                if signal == "SHORT" and dist_sup is not None and dist_sup < min_dist:
                    filter_reasons["dist_to_level"] = filter_reasons.get("dist_to_level", 0) + 1
                    continue

            # Single-source verdict: options direction must follow stock signal direction.
            effective_rec = "BUY" if signal == "LONG" else "SELL"
            option_type = "CALL" if effective_rec == "BUY" else "PUT"
            strike = self._pick_strike(price, option_type, atr)
            expiry = self._pick_expiry(score=score, rr=rr, mtf_score=mtf_score)
            sticky = self._select_sticky_contract(
                symbol=str(row.get("symbol") or "").upper(),
                option_type=option_type,
                spot=price,
            )
            if sticky:
                strike = sticky["strike"]
                expiry = sticky["expiry_date"]

            rationale = (
                f"{signal} setup from stock engine | score {int(score)} | "
                f"RR {round(rr, 1)} | MTF {round(mtf_score, 2)} | Age {signal_age} | "
                f"GEX {gex or 'N/A'} | Regime {regime or 'N/A'} | Session {session_time or 'N/A'}"
            )
            payload = {
                "symbol": row.get("symbol"),
                "option_type": option_type,
                "expiry_date": expiry,
                "strike": strike,
                "underlying_price": round(price, 2),
                "recommendation": effective_rec,
                "signal": signal,
                "score": round(score, 1),
                "rr_ratio": round(rr, 2),
                "market_regime": row.get("market_regime"),
                "gex_status": row.get("gex_status"),
                "rationale": rationale,
                "idea_state": "NEW",
                "confirm_count": 1,
                "invalid_reason": "",
                "status": "ACTIVE",
                "created_ts": now_iso,
                "updated_ts": now_iso,
            }
            self._db.upsert_option_setup(payload)
            logger.info("OPTIONS: created idea for {} {} @ ${:.2f} (score={}, rr={})", sym, option_type, price, int(score), round(rr, 1))

        # Diagnostic summary
        if past_cutoff:
            logger.info("OPTIONS: {} LONG/SHORT signals skipped — past late-entry cutoff ({}:{}0 ET)", candidates, self._cfg.late_entry_cutoff_hour, self._cfg.late_entry_cutoff_minute)
        elif filter_reasons:
            logger.info("OPTIONS: {} candidates, filter breakdown: {}", candidates, dict(filter_reasons))
        elif candidates == 0:
            logger.info("OPTIONS: no LONG/SHORT signals in {} rows", len(mtf_rows))

        self._validate_existing_setups(context_by_symbol, now_iso)
        self._auto_enter_option_paper_trades(now_iso)
        # Keep only recent ideas so stale contracts don't linger in the UI.
        self._db.expire_old_option_setups(keep_days=3)
        # Persist horizon-based outcome snapshots from underlying path.
        self._db.evaluate_option_setup_outcomes(horizons_minutes=[30, 60, 1440])

    def _select_sticky_contract(self, symbol: str, option_type: str, spot: float) -> Dict | None:
        """Reuse the most recent valid contract key so confirms can accumulate."""
        rows = self._db.get_recent_option_setups_for_symbol(
            symbol=symbol,
            option_type=option_type,
            status="ACTIVE",
            limit=8,
        )
        if not rows:
            return None
        today = datetime.now(timezone.utc).date()
        max_delta = max(1.0, min(10.0, spot * 0.012))
        for r in rows:
            state = str(r.get("idea_state") or "").upper()
            if state in ("INVALID", "EXPIRED"):
                continue
            expiry = _parse_date(r.get("expiry_date"))
            if not expiry:
                continue
            # With short-dated expiry, only skip if already expired.
            if (expiry - today).days < 0:
                continue
            prev_strike = _as_float(r.get("strike"))
            if prev_strike is None or prev_strike <= 0:
                continue
            if abs(prev_strike - spot) > max_delta:
                continue
            return {"strike": round(prev_strike, 2), "expiry_date": expiry.isoformat()}
        return None

    def _validate_existing_setups(self, context_by_symbol: Dict[str, Dict], now_iso: str) -> None:
        """Update idea lifecycle state based on latest scanner context."""
        setups = self._db.get_option_setups_for_validation(status="ACTIVE")
        for s in setups:
            setup_id = int(s["id"])
            symbol = str(s.get("symbol") or "").upper()
            option_type = str(s.get("option_type") or "").upper()
            context = context_by_symbol.get(symbol)

            if not context:
                self._db.update_option_setup_state(
                    setup_id=setup_id,
                    idea_state="WEAKENING",
                    invalid_reason="No fresh symbol context in latest scan",
                    validated_ts=now_iso,
                )
                continue

            signal = str(context.get("signal") or "")
            score = float(context.get("score") or 0.0)
            rr = float(context.get("rr_ratio") or 0.0)
            mtf_score = float(context.get("mtf_score") or 0.0)
            signal_age = int(context.get("signal_age") or 0)
            confirm_count = int(s.get("confirm_count") or 1)
            session_time = str(context.get("session_time") or "")
            regime = str(context.get("market_regime") or "")
            gex = str(context.get("gex_status") or "")
            dist_res = _as_float(context.get("distance_to_resistance_pct"))
            dist_sup = _as_float(context.get("distance_to_support_pct"))
            created_dt = _parse_iso(s.get("created_ts"))
            age_minutes = (
                int((datetime.now(timezone.utc) - created_dt).total_seconds() // 60)
                if created_dt
                else 999999
            )
            prev_reason = str(s.get("invalid_reason") or "").strip()

            hard_issues: List[str] = []
            soft_issues: List[str] = []
            direction_conflict = False
            if option_type == "CALL" and signal != "LONG":
                hard_issues.append("Signal no longer LONG")
                direction_conflict = True
            if option_type == "PUT" and signal != "SHORT":
                hard_issues.append("Signal no longer SHORT")
                direction_conflict = True
            if score < 72:
                soft_issues.append("Score below 72")
            if rr < 1.6:
                soft_issues.append("R:R below 1.6")
            if gex == "UNKNOWN":
                soft_issues.append("GEX unavailable")
            if option_type == "CALL" and gex not in ("", "ABOVE_ZERO_GAMMA"):
                hard_issues.append("GEX no longer supports CALL")
            if option_type == "PUT" and gex not in ("", "BELOW_ZERO_GAMMA"):
                hard_issues.append("GEX no longer supports PUT")
            if regime == "RISK_OFF" and option_type == "CALL":
                hard_issues.append("RISK_OFF regime blocks CALL bias")
            if regime == "RISK_ON" and option_type == "PUT":
                hard_issues.append("RISK_ON regime blocks PUT bias")
            if option_type == "CALL" and dist_res is not None and dist_res < 1.0:
                soft_issues.append("Resistance too close")
            if option_type == "PUT" and dist_sup is not None and dist_sup < 1.0:
                soft_issues.append("Support too close")

            # Direction conflicts are fatal immediately; do not allow contradictory ideas.
            if direction_conflict:
                self._db.update_option_setup_state(
                    setup_id=setup_id,
                    idea_state="INVALID",
                    invalid_reason="; ".join(hard_issues[:3]),
                    validated_ts=now_iso,
                )
                continue

            # Grace period for non-direction hard issues to avoid one-cycle noise.
            if hard_issues and age_minutes >= 45:
                self._db.update_option_setup_state(
                    setup_id=setup_id,
                    idea_state="INVALID",
                    invalid_reason="; ".join(hard_issues[:3]),
                    validated_ts=now_iso,
                )
                continue

            # Soft thresholds derived from config (95% of entry threshold).
            soft_min_score = float(self._cfg.paper_entry_min_score) * 0.95
            soft_min_rr = float(self._cfg.paper_entry_min_rr) * 0.95
            soft_min_mtf = float(self._cfg.paper_entry_min_mtf_score)
            cfg_sessions = {
                str(s).upper()
                for s in (self._cfg.paper_entry_allowed_sessions or [])
                if str(s).strip()
            }

            base_soft_reasons: List[str] = sorted(set(soft_issues))
            if score < soft_min_score:
                base_soft_reasons.append(f"Score below {soft_min_score:.0f}")
            if rr < soft_min_rr:
                base_soft_reasons.append(f"R:R below {soft_min_rr:.2f}")
            if mtf_score < soft_min_mtf:
                base_soft_reasons.append(f"MTF below {soft_min_mtf:.2f}")
            if signal_age < 2:
                base_soft_reasons.append("Signal age below 2")
            if cfg_sessions and session_time not in cfg_sessions:
                base_soft_reasons.append(f"Session outside {'/'.join(sorted(cfg_sessions))}")

            # De-duplicate while preserving deterministic order.
            soft_unique = []
            seen = set()
            for reason in base_soft_reasons:
                key = str(reason).strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                soft_unique.append(key)

            if not soft_unique:
                self._db.update_option_setup_state(
                    setup_id=setup_id,
                    idea_state=("STRONG" if confirm_count >= 2 else "NEW"),
                    invalid_reason="",
                    validated_ts=now_iso,
                )
                continue

            soft_signature = "; ".join(soft_unique[:3])
            if prev_reason == soft_signature:
                # Second consecutive validation with the same soft issue set.
                self._db.update_option_setup_state(
                    setup_id=setup_id,
                    idea_state="WEAKENING",
                    invalid_reason=soft_signature,
                    validated_ts=now_iso,
                )
                continue

            # First soft hit: keep actionable state (NEW/STRONG), store reason for next-cycle confirmation.
            self._db.update_option_setup_state(
                setup_id=setup_id,
                idea_state=("STRONG" if confirm_count >= 2 else "NEW"),
                invalid_reason=soft_signature,
                validated_ts=now_iso,
            )

    def _auto_enter_option_paper_trades(self, now_iso: str) -> None:
        """Auto-create system paper trades for valid high-conviction option ideas."""
        setups = self._db.get_option_setups_for_validation(status="ACTIVE")
        for s in setups:
            state = str(s.get("idea_state") or "").upper()
            score = float(s.get("score") or 0.0)
            rr = float(s.get("rr_ratio") or 0.0)
            confirms = int(s.get("confirm_count") or 1)
            if state not in ("STRONG",):
                continue
            if score < 80 or rr < 1.9:
                continue
            if confirms < int(max(1, self._cfg.paper_entry_confirmations_required)):
                continue
            symbol = str(s.get("symbol") or "").upper()
            option_type = str(s.get("option_type") or "").upper()
            option_expiry = str(s.get("expiry_date") or "")
            option_strike = float(s.get("strike") or 0.0)
            recommendation = str(s.get("recommendation") or "").upper()
            side = "LONG" if recommendation == "BUY" else ("SHORT" if recommendation == "SELL" else "")
            if not symbol or option_type not in ("CALL", "PUT") or not option_expiry or option_strike <= 0 or not side:
                continue
            if self._db.has_open_option_trade(
                symbol=symbol,
                option_type=option_type,
                option_expiry=option_expiry,
                option_strike=option_strike,
            ):
                continue

            entry_underlying = float(s.get("underlying_price") or 0.0)
            if entry_underlying <= 0:
                continue
            min_notional = float(self._cfg.paper_min_notional_per_trade)
            qty = max(1, int(ceil(min_notional / entry_underlying)))
            notional = round(entry_underlying * qty, 2)
            self._db.create_paper_trade(
                {
                    "opened_at": now_iso,
                    "symbol": symbol,
                    "side": side,
                    "entry_price": round(entry_underlying, 4) if entry_underlying > 0 else 0.0,
                    "quantity": qty,
                    "notional": notional,
                    "stop_loss": None,
                    "target_1": None,
                    "target_2": None,
                    "status": "OPEN",
                    "recommendation_source": f"OPTION_IDEA_SYSTEM_{state}",
                    "instrument_type": "OPTION",
                    "option_type": option_type,
                    "option_expiry": option_expiry,
                    "option_strike": option_strike,
                    "entry_signal": s.get("signal"),
                    "entry_score": round(score, 2),
                    "entry_rr_ratio": round(rr, 2),
                    "entry_market_regime": s.get("market_regime"),
                    "entry_gex_status": s.get("gex_status"),
                    "entry_session_time": None,
                    "entry_trade_conditions": (
                        f"Auto paper entry from {state} option idea | "
                        f"Confirms={confirms} | MinNotional={min_notional:.0f}"
                    ),
                    "fees": 0.0,
                    "created_ts": now_iso,
                }
            )

    @staticmethod
    def _pick_strike(spot: float, option_type: str, atr: float = 0.0) -> float:
        """Choose strike from ATR-aware offset and rounded tradable increments."""
        increment = 1.0 if spot < 200 else (2.5 if spot < 500 else 5.0)
        atr_pct = (atr / spot) if atr and spot > 0 else 0.0
        offset_pct = max(0.005, min(0.02, atr_pct * 0.6))
        if option_type == "CALL":
            target = spot * (1.0 + offset_pct)
            return round(ceil(target / increment) * increment, 2)
        target = spot * (1.0 - offset_pct)
        return round(floor(target / increment) * increment, 2)

    @staticmethod
    def _pick_expiry(score: float, rr: float, mtf_score: float) -> str:
        """Choose short-dated expiry for day/swing trading focus.

        High conviction (score>=85, RR>=2.2): 0 DTE (same day, nearest Friday).
        Standard: 2-3 DTE for safety.
        """
        now = datetime.now(timezone.utc).date()
        if score >= 85 and rr >= 2.2 and mtf_score >= 0.8:
            # 0 DTE — find the nearest Friday (today if it's Friday).
            d = now
            while d.weekday() != 4:
                d += timedelta(days=1)
            return d.isoformat()
        # Standard: 2-3 DTE — next Friday that is at least 2 days out.
        d = now + timedelta(days=2)
        while d.weekday() != 4:
            d += timedelta(days=1)
        return d.isoformat()


def _parse_iso(value) -> datetime | None:
    try:
        if not value:
            return None
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _parse_date(value):
    try:
        if not value:
            return None
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None


def _as_float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
