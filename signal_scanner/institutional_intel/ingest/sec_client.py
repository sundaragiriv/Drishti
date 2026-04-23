"""Simple SEC HTTP client with rate limiting."""

import threading
import time
from typing import Any, Dict, Optional

import requests
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from signal_scanner.institutional_intel.config import InstitutionalIntelConfig


class SecClient:
    """Minimal SEC client for metadata and filing payload download."""

    _global_lock = threading.Lock()
    _global_last_call = 0.0

    def __init__(self, config: Optional[InstitutionalIntelConfig] = None) -> None:
        self._cfg = config or InstitutionalIntelConfig()
        self._last_call = 0.0
        self._session = requests.Session()
        retries = Retry(
            total=4,
            backoff_factor=1.0,
            status_forcelist=[403, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _throttle(self) -> None:
        min_interval = 1.0 / max(self._cfg.requests_per_second, 0.1)
        # Pre-calculate sleep time inside lock, sleep OUTSIDE to reduce contention.
        sleep_time = 0.0
        with SecClient._global_lock:
            now = time.time()
            elapsed = now - SecClient._global_last_call
            if elapsed < min_interval:
                sleep_time = min_interval - elapsed
            # Reserve our slot immediately.
            SecClient._global_last_call = now + sleep_time
        if sleep_time > 0:
            time.sleep(sleep_time)
        self._last_call = time.time()

    def _headers(self) -> Dict[str, str]:
        return {
            "User-Agent": self._cfg.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Referer": "https://www.sec.gov/",
        }

    def _handle_rate_limit(self, response: requests.Response) -> None:
        """Handle 429 responses with Retry-After header parsing."""
        if response.status_code != 429:
            return
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                wait = float(retry_after)
            except (TypeError, ValueError):
                wait = 10.0
        else:
            wait = 10.0
        logger.warning(f"SEC 429 rate limited — waiting {wait:.1f}s")
        time.sleep(wait)

    def get_json(self, url: str) -> Dict[str, Any]:
        self._throttle()
        r = self._session.get(
            url,
            headers=self._headers(),
            timeout=self._cfg.request_timeout_seconds,
        )
        if r.status_code == 429:
            self._handle_rate_limit(r)
            self._throttle()
            r = self._session.get(
                url,
                headers=self._headers(),
                timeout=self._cfg.request_timeout_seconds,
            )
        r.raise_for_status()
        return r.json()

    def get_text(self, url: str) -> str:
        self._throttle()
        r = self._session.get(
            url,
            headers=self._headers(),
            timeout=self._cfg.request_timeout_seconds,
        )
        if r.status_code == 429:
            self._handle_rate_limit(r)
            self._throttle()
            r = self._session.get(
                url,
                headers=self._headers(),
                timeout=self._cfg.request_timeout_seconds,
            )
        r.raise_for_status()
        return r.text
