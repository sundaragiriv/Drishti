"""IBKR-only data connector.

Manages the IBKR connection lifecycle including auto-reconnect.
No fallback data source — IBKR must be connected for the scanner to operate.
"""

import time
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger
from scipy.stats import norm

from signal_scanner.config import (
    GEXConfig,
    IBKRConfig,
    IBKR_BAR_SIZES,
    IBKR_DURATIONS,
)


class RateLimiter:
    """Simple token-bucket rate limiter to respect IBKR API limits."""

    def __init__(self, max_calls_per_second: int = 45) -> None:
        self._max = max_calls_per_second
        self._calls: List[float] = []

    def wait(self) -> None:
        """Block until a request slot is available."""
        now = time.time()
        self._calls = [t for t in self._calls if now - t < 1.0]
        if len(self._calls) >= self._max:
            sleep_time = 1.0 - (now - self._calls[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
        self._calls.append(time.time())


class DataConnector:
    """IBKR-only data connector.

    Usage:
        connector = DataConnector()
        connector.connect_ibkr()  # required — no fallback
        df = connector.get_price_data("AAPL", "5m")
        calls, puts, price = connector.get_option_chain("AAPL")
    """

    def __init__(self, ibkr_config: Optional[IBKRConfig] = None) -> None:
        self._ibkr_config = ibkr_config or IBKRConfig()
        self._requested_port: int = int(self._ibkr_config.port)
        self._requested_client_id: int = int(self._ibkr_config.client_id)
        self._ib = None  # ib_insync.IB instance (lazy import)
        self._connected: bool = False
        self._rate_limiter = RateLimiter(max_calls_per_second=45)
        self._last_ibkr_error: str = ""
        self._connected_port: Optional[int] = None
        self._connected_client_id: Optional[int] = None
        self._ibkr_attempted_ports: List[int] = []
        self._ibkr_attempted_client_ids: List[int] = []
        self._next_ibkr_retry_at: float = 0.0
        # 30s cooldown matches the scheduler heartbeat — retry every tick.
        # The previous 300s cooldown made cold-starts (user brings TWS up
        # AFTER the scanner) feel broken because each retry sat idle for 5 min.
        self._ibkr_retry_cooldown_s: int = 30
        self._liquid_hours_cache: Dict = {}  # keyed by YYYYMMDD date string
        self._contract_cache: Dict[str, Any] = {}  # symbol → qualified contract
        # Session-conflict (Error 162 "different IP") tolerance — once we cross
        # threshold we flip `_connected=False` so the heartbeat reconnects with
        # a fresh clientId. Without this the system spams 162s indefinitely.
        self._session_conflict_count: int = 0
        self._session_conflict_threshold: int = 10
        self._client_id_blacklist: set = set()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect_ibkr(self) -> bool:
        """Attempt to connect to IBKR TWS/Gateway with retry logic."""
        now = time.time()
        if now < self._next_ibkr_retry_at:
            remaining = int(self._next_ibkr_retry_at - now)
            logger.info(f"Skipping IBKR reconnect attempt for {remaining}s (backoff active)")
            return False
        self._ensure_thread_event_loop()
        try:
            from ib_insync import IB
        except ImportError:
            logger.error(
                "ib_insync not installed — cannot connect to IBKR. "
                "Install with: pip install ib_insync"
            )
            return False

        candidate_ports = [self._ibkr_config.port, 7497, 7496, 4002, 4001]
        seen = set()
        ports = [p for p in candidate_ports if not (p in seen or seen.add(p))]
        # Try requested clientId first, then a short fallback range for collisions.
        # Skip clientIds we've burned via Error 162 in this process lifetime.
        base = self._ibkr_config.client_id
        candidate_client_ids = [base + i for i in range(0, 10)
                                if (base + i) not in self._client_id_blacklist]
        if not candidate_client_ids:
            # If everything is blacklisted, reset and start fresh — better to
            # retry burned ids than fail outright.
            logger.warning("All clientIds blacklisted — resetting and retrying")
            self._client_id_blacklist.clear()
            candidate_client_ids = [base + i for i in range(0, 10)]
        self._ibkr_attempted_ports = ports[:]
        self._ibkr_attempted_client_ids = candidate_client_ids[:]
        self._last_ibkr_error = ""
        self._connected_client_id = None

        for port in ports:
            for client_id in candidate_client_ids:
                try:
                    if self._ib:
                        try:
                            self._ib.disconnect()
                        except Exception:
                            pass
                    self._ib = IB()
                    self._ib.connect(
                        self._ibkr_config.host,
                        port,
                        clientId=client_id,
                        timeout=self._ibkr_config.timeout,
                    )
                    self._ib.disconnectedEvent += self._on_disconnect
                    self._ib.errorEvent += self._on_error
                    self._connected = True
                    self._connected_port = port
                    self._connected_client_id = client_id
                    self._next_ibkr_retry_at = 0.0
                    self._session_conflict_count = 0
                    logger.info(f"Connected to IBKR on port {port} (clientId={client_id})")
                    return True
                except Exception as e:
                    self._last_ibkr_error = f"port {port}, clientId {client_id}: {repr(e)}"
                    logger.warning(
                        f"IBKR connection failed on port {port} "
                        f"(clientId={client_id}): {repr(e)}"
                    )
                    msg = str(e).lower()
                    # If port is not open, don't waste time trying more client IDs on same port.
                    if (
                        "connectionrefusederror" in repr(e).lower()
                        or "refused" in msg
                        or "winerror 1225" in msg
                    ):
                        break
                    time.sleep(0.2)

        logger.error(
            "All IBKR connection attempts failed. "
            "Verify TWS/Gateway is running, API is enabled, and socket port is one of "
            "7497/7496/4002/4001. Scanner cannot operate without IBKR."
        )
        self._connected = False
        self._next_ibkr_retry_at = time.time() + float(self._ibkr_retry_cooldown_s)
        return False

    def _on_disconnect(self) -> None:
        """Handle unexpected IBKR disconnection."""
        logger.warning("IBKR disconnected — scanner paused until reconnection")
        self._connected = False
        self._last_ibkr_error = "Disconnected event from IBKR session"
        # Contracts and liquid hours may not survive the reconnect cleanly —
        # drop caches so we re-qualify against the new session.
        self._contract_cache.clear()
        self._liquid_hours_cache.clear()
        self._session_conflict_count = 0

    def _on_error(self, reqId, errorCode, errorString, contract=None) -> None:
        """Handle IBKR error events.

        Some error codes indicate the session is broken even though
        ``disconnectedEvent`` never fires.  When that happens we have to flip
        ``_connected = False`` ourselves so the heartbeat reconnects.

        Codes:
          - 1100/1300/504/10182: connection lost
          - 1101: restored, market data subscriptions lost (re-qualify)
          - 1102: restored, data maintained (informational)
          - 162: "different IP" / session-conflict.  A few are tolerable but
                 sustained 162s mean the session is unusable — flip and reconnect
                 on a fresh clientId after we cross threshold.
        """
        try:
            code = int(errorCode)
        except Exception:
            return

        if code in (1100, 1300, 504, 10182):
            logger.warning(
                f"IBKR connection error {code}: {errorString} — flagging disconnected"
            )
            self._connected = False
            self._last_ibkr_error = f"errorCode {code}: {errorString}"
            self._contract_cache.clear()
            self._liquid_hours_cache.clear()
        elif code == 1101:
            logger.warning(
                f"IBKR connectivity restored, data subs lost (1101): {errorString}"
            )
            self._contract_cache.clear()
        elif code == 1102:
            logger.info(f"IBKR connectivity restored, data maintained (1102): {errorString}")
        elif code == 162:
            self._session_conflict_count += 1
            if self._session_conflict_count >= self._session_conflict_threshold:
                burned = self._connected_client_id
                if burned is not None:
                    self._client_id_blacklist.add(burned)
                logger.error(
                    f"IBKR Error 162 threshold ({self._session_conflict_threshold}) "
                    f"crossed — session unusable on clientId={burned}. "
                    f"Forcing reconnect with a different clientId. msg='{errorString}'"
                )
                self._connected = False
                self._last_ibkr_error = (
                    f"errorCode 162 (session conflict, clientId={burned}): {errorString}"
                )
                self._contract_cache.clear()
                self._liquid_hours_cache.clear()
                self._session_conflict_count = 0
                # Also try to disconnect the broken IB instance so the next
                # connect_ibkr() starts cleanly.
                if self._ib is not None:
                    try:
                        self._ib.disconnect()
                    except Exception:
                        pass

    def disconnect(self) -> None:
        """Gracefully disconnect from IBKR."""
        if self._ib and self._connected:
            self._ib.disconnect()
            self._connected = False
            self._connected_port = None
            self._connected_client_id = None
            logger.info("Disconnected from IBKR")

    def is_connected(self) -> bool:
        """Return True if IBKR is actively connected."""
        return self._connected

    def get_data_source(self) -> str:
        """Return the current data source name."""
        return "IBKR" if self._connected else "NOT_CONNECTED"

    # ------------------------------------------------------------------
    # Price data
    # ------------------------------------------------------------------

    def get_price_data(self, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        """Fetch OHLCV data for a symbol and timeframe.

        Returns DataFrame with columns: Open, High, Low, Close, Volume.
        Returns None if IBKR is not connected or data fetch fails.
        """
        if not self._connected:
            logger.debug(f"IBKR not connected — cannot fetch price data for {symbol}")
            return None

        self._rate_limiter.wait()
        try:
            return self._get_price_ibkr(symbol, timeframe)
        except Exception as e:
            logger.warning(f"IBKR price fetch failed for {symbol}: {e}")
            return None

    def _get_price_ibkr(self, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        """Fetch price data from IBKR."""
        from ib_insync import Stock, util
        self._ensure_thread_event_loop()
        contract = self._qualify_stock_contract(symbol)
        if contract is None:
            return None

        bars = self._ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=IBKR_DURATIONS[timeframe],
            barSizeSetting=IBKR_BAR_SIZES[timeframe],
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
            timeout=10,  # 10s max per request — prevents infinite hang
        )

        if not bars:
            return None

        df = util.df(bars)
        df = df.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume", "date": "Date",
        })
        df.set_index("Date", inplace=True)
        return df[["Open", "High", "Low", "Close", "Volume"]]

    def _qualify_stock_contract(self, symbol: str):
        """Qualify an IBKR stock contract with symbol fallbacks (e.g., BRK.B).

        Caches successful qualifications to avoid repeated API calls (~50-200ms each).
        """
        # Check cache first
        if symbol in self._contract_cache:
            return self._contract_cache[symbol]

        from ib_insync import Stock

        candidates = [symbol]
        if "." in symbol:
            candidates.extend([symbol.replace(".", " "), symbol.replace(".", "-")])

        tried = []
        for sym in candidates:
            try:
                contract = Stock(sym, "SMART", "USD")
                qualified = self._ib.qualifyContracts(contract)
                if qualified:
                    self._contract_cache[symbol] = qualified[0]
                    return qualified[0]
            except Exception:
                tried.append(sym)
                continue
            tried.append(sym)

        logger.debug(f"IBKR could not qualify stock contract for {symbol} (tried={tried})")
        self._contract_cache[symbol] = None  # cache failures too
        return None

    # ------------------------------------------------------------------
    # Options chain
    # ------------------------------------------------------------------

    def get_option_chain(
        self,
        symbol: str,
        gex_config: Optional[GEXConfig] = None,
    ) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[float]]:
        """Fetch option chain data for a symbol from IBKR.

        Returns:
            (calls_df, puts_df, underlying_price) with columns including
            'strike', 'openInterest', 'gamma'. Returns (None, None, None) on failure.
        """
        if not self._connected:
            logger.debug(f"IBKR not connected — cannot fetch option chain for {symbol}")
            return None, None, None

        cfg = gex_config or GEXConfig()
        self._rate_limiter.wait()

        try:
            return self._get_chain_ibkr(symbol, cfg)
        except Exception as e:
            logger.warning(f"IBKR option chain failed for {symbol}: {e}")
            return None, None, None

    def _get_chain_ibkr(
        self, symbol: str, cfg: GEXConfig
    ) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[float]]:
        """Fetch option chain from IBKR with real Greeks."""
        from ib_insync import Stock, Option
        self._ensure_thread_event_loop()

        contract = self._qualify_stock_contract(symbol)
        if contract is None:
            return None, None, None

        # Get current price
        [ticker] = self._ib.reqTickers(contract)
        spot = ticker.marketPrice()
        if np.isnan(spot):
            spot = ticker.close
        if np.isnan(spot):
            logger.warning(f"Cannot get price for {symbol} from IBKR")
            return None, None, None

        # Get option chain parameters
        chains = self._ib.reqSecDefOptParams(symbol, "", "STK", contract.conId)
        if not chains:
            return None, None, None

        chain = next((c for c in chains if c.exchange == "SMART"), chains[0])

        # Filter expirations to target DTE range
        today = datetime.now().date()
        valid_expirations = []
        for exp in sorted(chain.expirations):
            exp_date = datetime.strptime(exp, "%Y%m%d").date()
            dte = (exp_date - today).days
            if cfg.min_dte <= dte <= cfg.max_dte:
                valid_expirations.append(exp)

        if not valid_expirations:
            logger.warning(f"No valid expirations for {symbol} in {cfg.min_dte}-{cfg.max_dte} DTE")
            return None, None, None

        # Filter strikes to range around spot.
        min_strike = spot * (1 - cfg.strike_range_pct)
        max_strike = spot * (1 + cfg.strike_range_pct)
        base_strikes = [s for s in chain.strikes if min_strike <= s <= max_strike]
        if not base_strikes:
            return None, None, None
        base_strikes = sorted(base_strikes, key=lambda s: abs(s - spot))

        qualified_contracts = []
        selected_exp = None
        candidate_exps = valid_expirations[:5] if len(valid_expirations) > 5 else valid_expirations
        for exp in candidate_exps:
            strikes = base_strikes[:12]
            trial_contracts = []
            for strike in strikes:
                trial_contracts.append(
                    Option(
                        symbol,
                        exp,
                        strike,
                        "C",
                        "SMART",
                        multiplier=getattr(chain, "multiplier", "100"),
                        tradingClass=getattr(chain, "tradingClass", ""),
                    )
                )
                trial_contracts.append(
                    Option(
                        symbol,
                        exp,
                        strike,
                        "P",
                        "SMART",
                        multiplier=getattr(chain, "multiplier", "100"),
                        tradingClass=getattr(chain, "tradingClass", ""),
                    )
                )

            qualified = []
            for contract in trial_contracts:
                try:
                    q = self._ib.qualifyContracts(contract)
                    if q:
                        qualified.append(q[0])
                except Exception:
                    continue

            if len(qualified) >= 6:
                selected_exp = exp
                qualified_contracts = qualified
                break

        if len(qualified_contracts) < 6:
            logger.warning(
                f"IBKR contract qualification sparse for {symbol}; "
                "insufficient option data for GEX calculation"
            )
            return None, None, None
        if selected_exp:
            logger.debug(f"IBKR option chain selected expiry {selected_exp} for {symbol}")

        # Request market data
        tickers = self._ib.reqTickers(*qualified_contracts)

        calls_data = []
        puts_data = []
        for t in tickers:
            if t.contract.right == "C":
                target = calls_data
            else:
                target = puts_data

            gamma = 0.0
            oi = 0
            if t.modelGreeks:
                gamma = t.modelGreeks.gamma or 0.0
            if hasattr(t, "callOpenInterest"):
                oi = t.callOpenInterest if t.contract.right == "C" else t.putOpenInterest
            elif hasattr(t, "openInterest"):
                oi = t.openInterest or 0

            target.append({
                "strike": t.contract.strike,
                "openInterest": oi,
                "gamma": gamma,
                "impliedVolatility": t.modelGreeks.impliedVol if t.modelGreeks else 0.0,
                "bid": float(t.bid or 0.0),
                "ask": float(t.ask or 0.0),
                "lastPrice": float(t.last or 0.0),
                "volume": float(t.volume or 0.0),
                "quote_ts": datetime.now().isoformat(),
                "expiry_date": datetime.strptime(str(t.contract.lastTradeDateOrContractMonth), "%Y%m%d").date().isoformat()
                if str(t.contract.lastTradeDateOrContractMonth).isdigit() and len(str(t.contract.lastTradeDateOrContractMonth)) == 8
                else None,
            })

        calls_df = pd.DataFrame(calls_data) if calls_data else None
        puts_df = pd.DataFrame(puts_data) if puts_data else None

        return calls_df, puts_df, spot

    def get_ibkr_diagnostics(self) -> Dict[str, object]:
        """Return IBKR connectivity diagnostics for UI/debugging."""
        return {
            "connected": self.is_connected(),
            "host": self._ibkr_config.host,
            "port": self._requested_port,
            "connected_port": self._connected_port,
            "connected_client_id": self._connected_client_id,
            "client_id": self._requested_client_id,
            "attempted_ports": list(self._ibkr_attempted_ports),
            "attempted_client_ids": list(self._ibkr_attempted_client_ids),
            "last_error": self._last_ibkr_error,
        }

    # ------------------------------------------------------------------
    # Market hours — IBKR-authoritative, cached per calendar day
    # ------------------------------------------------------------------

    def get_market_status(self) -> Dict:
        """Return current market session using IBKR liquid hours.

        Queries IBKR reqContractDetails(SPY) once per calendar day and caches
        the open/close times.  Each call recomputes the current session live
        from the cached times so the session label always reflects the actual
        clock position within the trading day.

        Returns dict with keys:
            is_trading_day (bool), is_open (bool),
            session (str: CLOSED|PRE_MARKET|EARLY|MID_DAY|POWER_HOUR|AFTER_HOURS),
            open_time (str, "HH:MM"), close_time (str, "HH:MM")
        """
        now_et = self._et_now()
        today_str = now_et.strftime("%Y%m%d")

        if self._liquid_hours_cache.get("date") != today_str:
            self._refresh_liquid_hours(today_str)

        cached = self._liquid_hours_cache
        if not cached.get("is_trading_day"):
            return {
                "is_trading_day": False,
                "is_open": False,
                "session": "CLOSED",
                "open_time": None,
                "close_time": None,
            }

        open_hm = cached["open_hm"]    # e.g. 930
        close_hm = cached["close_hm"]  # e.g. 1600
        hour_min = now_et.hour * 100 + now_et.minute

        is_open = open_hm <= hour_min < close_hm
        session = self._classify_session(hour_min, open_hm, close_hm)
        return {
            "is_trading_day": True,
            "is_open": is_open,
            "session": session,
            "open_time": f"{open_hm // 100:02d}:{open_hm % 100:02d}",
            "close_time": f"{close_hm // 100:02d}:{close_hm % 100:02d}",
        }

    def _refresh_liquid_hours(self, today_str: str) -> None:
        """Fetch today's liquid trading hours from IBKR and cache them."""
        if self._connected:
            try:
                from ib_insync import Stock
                self._ensure_thread_event_loop()
                contract = Stock("SPY", "SMART", "USD")
                details = self._ib.reqContractDetails(contract)
                if details:
                    parsed = self._parse_liquid_hours(details[0].liquidHours, today_str)
                    self._liquid_hours_cache = {"date": today_str, **parsed}
                    logger.debug(
                        f"Market hours from IBKR: trading_day={parsed['is_trading_day']} "
                        f"open={parsed.get('open_hm')} close={parsed.get('close_hm')}"
                    )
                    return
            except Exception as e:
                logger.debug(f"IBKR liquid hours fetch failed — using clock fallback: {e}")

        # Fallback: weekday = trading day, standard 09:30-16:00 ET
        is_weekday = datetime.strptime(today_str, "%Y%m%d").weekday() < 5
        self._liquid_hours_cache = {
            "date": today_str,
            "is_trading_day": is_weekday,
            "open_hm": 930,
            "close_hm": 1600,
        }

    @staticmethod
    def _parse_liquid_hours(liquid_hours: str, today_str: str) -> Dict:
        """Parse IBKR liquidHours string for today's session.

        Format: "20250219:0930-20250219:1600;20250220:CLOSED;..."
        Returns dict with is_trading_day, open_hm (int), close_hm (int).
        """
        for segment in liquid_hours.split(";"):
            segment = segment.strip()
            if not segment.startswith(today_str):
                continue
            rest = segment[len(today_str) + 1:]  # everything after "YYYYMMDD:"
            if rest.upper() == "CLOSED":
                return {"is_trading_day": False, "open_hm": 0, "close_hm": 0}
            parts = segment.split("-")
            if len(parts) == 2:
                try:
                    open_hm = int(parts[0].split(":")[-1])   # 930
                    close_hm = int(parts[1].split(":")[-1])  # 1600
                    return {"is_trading_day": True, "open_hm": open_hm, "close_hm": close_hm}
                except (ValueError, IndexError):
                    pass
        # today_str not present in liquidHours — treat as non-trading day
        return {"is_trading_day": False, "open_hm": 0, "close_hm": 0}

    @staticmethod
    def _classify_session(hour_min: int, open_hm: int, close_hm: int) -> str:
        """Map current ET hour_min (e.g. 1030) to session label."""
        if hour_min < open_hm:
            return "PRE_MARKET"
        if hour_min >= close_hm:
            return "AFTER_HOURS"
        # EARLY: first 30 minutes of regular session
        early_cutoff = open_hm + 30
        # POWER_HOUR: last 60 minutes of regular session
        power_start = close_hm - 100
        if hour_min < early_cutoff:
            return "EARLY"
        if hour_min >= power_start:
            return "POWER_HOUR"
        return "MID_DAY"

    def _et_now(self) -> datetime:
        """Return current time in US/Eastern timezone."""
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("America/New_York"))
        except ImportError:
            try:
                import pytz
                return datetime.now(pytz.timezone("US/Eastern"))
            except ImportError:
                return datetime.now()

    @staticmethod
    def _ensure_thread_event_loop() -> None:
        """Ensure current thread has an asyncio event loop for ib_insync calls."""
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
