"""HTTP client for bestbuy.com.

Plain requests/curl are blackholed by Akamai (the connection opens and then
hangs). curl_cffi's TLS impersonation gets a normal 200, so every request here
goes through it.

Best Buy throttles bursts by resetting HTTP/2 streams
(``curl: (92) ... INTERNAL_ERROR``) rather than returning 429, so that error is
treated as a rate-limit signal and backed off like one.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Optional

from curl_cffi import requests as cr

log = logging.getLogger(__name__)

#: Fingerprints accepted by bestbuy.com. Only curl_cffi's rolling "chrome"
#: profile gets through; pinned builds (chrome124, chrome131) are rejected with
#: an HTTP/2 stream reset, so do not "improve" this by rotating over them.
IMPERSONATIONS = ["chrome"]

RETRYABLE_STATUS = {403, 408, 425, 429, 500, 502, 503, 504}


class RateLimiter:
    """Simple thread-safe token bucket."""

    def __init__(self, rate_per_sec: float):
        self.interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if wait:
            time.sleep(wait)


class BestBuyClient:
    """Rate-limited, retrying fetcher with per-thread sessions."""

    BASE = "https://www.bestbuy.com"

    def __init__(
        self,
        rate_per_sec: float = 3.0,
        timeout: int = 45,
        max_retries: int = 4,
        zip_code: Optional[str] = None,
        jitter: float = 0.35,
    ):
        self.limiter = RateLimiter(rate_per_sec)
        self.timeout = timeout
        self.max_retries = max_retries
        self.zip_code = zip_code
        self.jitter = jitter
        self._local = threading.local()
        #: Set when we get throttled; all threads pause until it passes.
        self._cooldown_until = 0.0
        self._cooldown_lock = threading.Lock()

    # -- session management -------------------------------------------------

    def _session(self) -> cr.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = self._new_session()
            self._local.session = s
        return s

    def _new_session(self) -> cr.Session:
        s = cr.Session(impersonate=random.choice(IMPERSONATIONS))
        s.headers.update(
            {
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        if self.zip_code:
            # Best Buy reads the destination zip from this cookie; it changes
            # shipping eligibility, so pin it rather than inheriting their default.
            s.cookies.set("intl_splash", "false", domain=".bestbuy.com")
            s.cookies.set("locDestZip", self.zip_code, domain=".bestbuy.com")
        return s

    def reset_session(self) -> None:
        self._local.session = self._new_session()

    # -- cooldown -----------------------------------------------------------

    def _enter_cooldown(self, seconds: float) -> None:
        with self._cooldown_lock:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)

    def _await_cooldown(self) -> None:
        while True:
            with self._cooldown_lock:
                remaining = self._cooldown_until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 5.0))

    # -- fetching -----------------------------------------------------------

    def get(self, url: str) -> Optional[str]:
        """Fetch `url`, returning HTML or None if it could not be retrieved."""
        for attempt in range(self.max_retries):
            self._await_cooldown()
            self.limiter.acquire()
            if self.jitter:
                time.sleep(random.uniform(0, self.jitter))
            try:
                resp = self._session().get(url, timeout=self.timeout)
            except Exception as exc:  # noqa: BLE001 - curl_cffi raises many types
                # HTTP/2 stream resets are how Best Buy signals "slow down".
                backoff = self._backoff(attempt)
                log.debug("fetch error (%s), backing off %.1fs: %s", type(exc).__name__, backoff, url)
                self._enter_cooldown(backoff)
                self.reset_session()
                continue

            if resp.status_code == 200:
                return resp.text
            if resp.status_code in RETRYABLE_STATUS:
                backoff = self._backoff(attempt)
                log.debug("HTTP %s, backing off %.1fs: %s", resp.status_code, backoff, url)
                self._enter_cooldown(backoff)
                self.reset_session()
                continue
            log.debug("HTTP %s (not retrying): %s", resp.status_code, url)
            return None
        return None

    def _backoff(self, attempt: int) -> float:
        return min(60.0, (2.0 ** attempt) * 2.0) + random.uniform(0, 1.5)

    # -- Best Buy URLs ------------------------------------------------------

    def product_url(self, sku: str) -> str:
        """Base product page. Works from the SKU alone, no slug needed."""
        return "%s/site/-/%s.p?skuId=%s" % (self.BASE, sku, sku)

    def condition_url(self, sku: str, product_id: str, condition: str) -> str:
        """Open-box page for one condition.

        The slug segment is free-form, but the product id is required.
        """
        return "%s/product/a/%s/sku/%s/openbox?condition=%s" % (
            self.BASE,
            product_id,
            sku,
            condition.lower(),
        )

    def fetch_product(self, sku: str) -> Optional[str]:
        return self.get(self.product_url(sku))

    def fetch_condition(self, sku: str, product_id: str, condition: str) -> Optional[str]:
        return self.get(self.condition_url(sku, product_id, condition))
