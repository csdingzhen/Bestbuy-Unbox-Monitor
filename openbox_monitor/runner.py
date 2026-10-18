"""The monitor loop.

Two stages per SKU, because requests are the scarce resource:

1. Sweep - one GET of the base product page yields the new price, every open-box
   tier and each tier's price. Cheap enough to run across thousands of SKUs.
2. Confirm - only for tiers whose price already looks alertable, one GET of that
   tier's own page settles whether it is actually purchasable and shippable
   (buttonState ADD_TO_CART) rather than pickup-only (CHECK_STORES).

In steady state almost nothing reaches stage 2, so the loop stays cheap while
remaining precise about shipping at the moment it matters.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import os
import time
from dataclasses import asdict
from typing import Iterator, Optional

from .client import BestBuyClient
from .detect import DealEvent, Thresholds, evaluate, worth_confirming
from .discovery import DEFAULT_SOURCES, discover
from .parser import apply_condition_page, parse
from .store import Store

log = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "database": "openbox.db",
    "discord_webhook": "",
    "discord_mention": "",
    "zip_code": "",
    "requests_per_second": 3.0,
    "workers": 6,
    "cycle_seconds": 60,
    "discovery_interval_seconds": 3600,
    "discovery_pages_per_source": 3,
    "discovery_sources": [],
    "max_skus_per_cycle": 400,
    "alert_cooldown_seconds": 21600,
    "intervals": {
        "hot": 120,
        "warm": 600,
        "cold": 1800,
        "hot_floor": 1500,
        "warm_floor": 600,
    },
    "thresholds": {
        "alert_pct": 35.0,
        "strong_pct": 45.0,
        "ladder_gap_pct": 8.0,
        "price_drop_pct": 10.0,
        "min_new_price": 400.0,
        "min_dollars_off": 150.0,
    },
}


def load_config(path: Optional[str]) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    env_hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if env_hook:
        cfg["discord_webhook"] = env_hook
    return cfg


class Monitor:
    """Scans Best Buy for shippable open-box deals.

    Integration points, in order of preference for an existing bot:

        mon = Monitor.from_config("config.json")
        for event in mon.scan_once():      # one pass, returns list[DealEvent]
            ...

        for event in mon.stream():         # blocking generator, paces itself
            ...
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = config or json.loads(json.dumps(DEFAULT_CONFIG))
        self.store = Store(self.config["database"])
        self.client = BestBuyClient(
            rate_per_sec=float(self.config["requests_per_second"]),
            zip_code=self.config.get("zip_code") or None,
        )
        self.thresholds = Thresholds(**self.config["thresholds"])
        self._last_discovery = 0.0

    @classmethod
    def from_config(cls, path: Optional[str] = None) -> "Monitor":
        return cls(load_config(path))

    # -- discovery ----------------------------------------------------------

    def refresh_watchlist(self, force: bool = False) -> int:
        """Harvest SKUs from listing pages. Returns how many were new."""
        interval = float(self.config["discovery_interval_seconds"])
        if not force and time.time() - self._last_discovery < interval:
            return 0
        self._last_discovery = time.time()
        sources = self.config.get("discovery_sources") or DEFAULT_SOURCES
        added = 0
        for url, skus in discover(
            self.client, sources, int(self.config["discovery_pages_per_source"])
        ).items():
            added += self.store.add_skus(skus, source=url)
        if added:
            log.info("discovery: %d new SKUs (watchlist now %d)", added, self.store.count())
        return added

    # -- scanning -----------------------------------------------------------

    def _scan_sku(self, sku: str, product_id: Optional[str]) -> list[DealEvent]:
        html = self.client.fetch_product(sku)
        if not html:
            self.store.touch_scan(sku)
            return []

        snap = parse(html, sku)
        self.store.update_product(
            sku, snap.product_id, snap.name, snap.image, snap.new_price
        )

        if not snap.offers:
            # No open-box tiers at all: forget any previous state so that a
            # later reappearance registers as a NEW_OFFER transition.
            for condition in ("Excellent", "Good", "Fair"):
                self.store.clear_offer(sku, condition)
            return []

        candidates = worth_confirming(snap, self.thresholds)
        pid = snap.product_id or product_id

        # Stage 2: confirm shipping/purchasability for promising tiers only.
        if candidates and pid:
            for offer in candidates:
                page = self.client.fetch_condition(sku, pid, offer.condition)
                if page:
                    apply_condition_page(snap, page, offer.condition)

        events: list[DealEvent] = []
        for offer in snap.offers:
            prev = self.store.previous_offer(sku, offer.condition)
            prev_price = prev["price"] if prev else None
            prev_buyable = bool(prev and prev["button_state"] == "ADD_TO_CART")

            event = evaluate(
                snap,
                offer,
                self.thresholds,
                previous_price=prev_price,
                previously_buyable=prev_buyable,
            )
            self.store.record_offer(
                sku, offer.condition, offer.price, offer.button_state, snap.new_price
            )
            if event is None:
                continue
            cooldown = float(self.config["alert_cooldown_seconds"])
            if self.store.already_alerted(sku, offer.condition, offer.price, cooldown):
                continue
            self.store.log_alert(sku, offer.condition, offer.price, event.reason)
            events.append(event)

        return events

    def scan_once(self) -> list[DealEvent]:
        """Run one pass over every SKU currently due. Returns alertable deals."""
        self.refresh_watchlist()
        due = self.store.due_skus(
            self.config["intervals"], self.thresholds.min_new_price
        )
        limit = int(self.config["max_skus_per_cycle"])
        due = due[:limit]
        if not due:
            return []

        log.info("scanning %d SKUs", len(due))
        events: list[DealEvent] = []
        workers = int(self.config["workers"])
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._scan_sku, row["sku"], row["product_id"]): row["sku"]
                for row in due
            }
            for future in cf.as_completed(futures):
                try:
                    events.extend(future.result())
                except Exception as exc:  # noqa: BLE001
                    log.exception("scan failed for %s: %s", futures[future], exc)

        events.sort(key=lambda e: (-e.severity, -e.pct_off))
        return events

    def stream(self) -> Iterator[DealEvent]:
        """Yield deals forever, sleeping between cycles."""
        cycle = float(self.config["cycle_seconds"])
        while True:
            started = time.time()
            try:
                for event in self.scan_once():
                    yield event
            except Exception as exc:  # noqa: BLE001
                log.exception("scan cycle failed: %s", exc)
            elapsed = time.time() - started
            if elapsed < cycle:
                time.sleep(cycle - elapsed)

    def close(self) -> None:
        self.store.close()
