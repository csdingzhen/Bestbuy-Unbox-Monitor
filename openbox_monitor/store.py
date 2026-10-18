"""SQLite persistence.

Holds the SKU watchlist, the last seen state of every open-box offer, and an
alert log used for de-duplication. Offer state is what turns a scan into an
event: an alert fires on the *transition* into a deep discount, not on every
cycle where the price happens to be low.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku         TEXT PRIMARY KEY,
    product_id  TEXT,
    name        TEXT,
    image       TEXT,
    new_price   REAL,
    first_seen  REAL,
    last_seen   REAL,
    last_scan   REAL DEFAULT 0,
    source      TEXT
);

CREATE TABLE IF NOT EXISTS offers (
    sku          TEXT NOT NULL,
    condition    TEXT NOT NULL,
    price        REAL,
    button_state TEXT,
    new_price    REAL,
    seen_at      REAL,
    PRIMARY KEY (sku, condition)
);

CREATE TABLE IF NOT EXISTS alerts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sku       TEXT,
    condition TEXT,
    price     REAL,
    reason    TEXT,
    sent_at   REAL
);

CREATE INDEX IF NOT EXISTS idx_alerts_dedupe ON alerts (sku, condition, price);
CREATE INDEX IF NOT EXISTS idx_products_scan ON products (last_scan);
"""


class Store:
    def __init__(self, path: str = "openbox.db"):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- watchlist ----------------------------------------------------------

    def add_skus(self, skus: Iterable[str], source: str = "") -> int:
        """Insert unseen SKUs. Returns how many were new."""
        now = time.time()
        added = 0
        with self._lock:
            for sku in skus:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO products (sku, first_seen, last_seen, source)"
                    " VALUES (?,?,?,?)",
                    (sku, now, now, source),
                )
                added += cur.rowcount
            self._conn.commit()
        return added

    def update_product(
        self,
        sku: str,
        product_id: Optional[str],
        name: str,
        image: Optional[str],
        new_price: Optional[float],
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE products SET product_id=COALESCE(?,product_id),"
                " name=COALESCE(NULLIF(?,''),name), image=COALESCE(?,image),"
                " new_price=COALESCE(?,new_price), last_seen=?, last_scan=?"
                " WHERE sku=?",
                (product_id, name, image, new_price, time.time(), time.time(), sku),
            )
            self._conn.commit()

    def touch_scan(self, sku: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE products SET last_scan=? WHERE sku=?", (time.time(), sku)
            )
            self._conn.commit()

    def due_skus(self, intervals: dict[str, float], min_new_price: float) -> list[sqlite3.Row]:
        """SKUs whose polling interval has elapsed, most valuable first.

        `intervals` maps tier name -> seconds, with tiers assigned by new price:
        hot (>= hot_floor), warm (>= warm_floor), cold (everything else).
        Unpriced SKUs are treated as hot so they get an initial scan.
        """
        now = time.time()
        hot_floor = intervals.get("hot_floor", 1500.0)
        warm_floor = intervals.get("warm_floor", 600.0)
        rows = []
        with self._lock:
            for row in self._conn.execute("SELECT * FROM products"):
                price = row["new_price"]
                if price is None:
                    every = intervals.get("hot", 120.0)
                elif price < min_new_price:
                    continue
                elif price >= hot_floor:
                    every = intervals.get("hot", 120.0)
                elif price >= warm_floor:
                    every = intervals.get("warm", 600.0)
                else:
                    every = intervals.get("cold", 1800.0)
                if now - (row["last_scan"] or 0) >= every:
                    rows.append(row)
        rows.sort(key=lambda r: -(r["new_price"] or 1e9))
        return rows

    def all_products(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute("SELECT * FROM products"))

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]

    # -- offer state --------------------------------------------------------

    def previous_offer(self, sku: str, condition: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM offers WHERE sku=? AND condition=?", (sku, condition)
            ).fetchone()

    def record_offer(
        self,
        sku: str,
        condition: str,
        price: Optional[float],
        button_state: Optional[str],
        new_price: Optional[float],
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO offers (sku, condition, price, button_state, new_price, seen_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(sku, condition) DO UPDATE SET"
                " price=excluded.price, button_state=excluded.button_state,"
                " new_price=excluded.new_price, seen_at=excluded.seen_at",
                (sku, condition, price, button_state, new_price, time.time()),
            )
            self._conn.commit()

    def clear_offer(self, sku: str, condition: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM offers WHERE sku=? AND condition=?", (sku, condition)
            )
            self._conn.commit()

    # -- alert de-duplication ----------------------------------------------

    def already_alerted(self, sku: str, condition: str, price: float, within: float) -> bool:
        cutoff = time.time() - within
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM alerts WHERE sku=? AND condition=? AND ABS(price-?)<0.01"
                " AND sent_at>? LIMIT 1",
                (sku, condition, price, cutoff),
            ).fetchone()
        return row is not None

    def log_alert(self, sku: str, condition: str, price: float, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO alerts (sku, condition, price, reason, sent_at) VALUES (?,?,?,?,?)",
                (sku, condition, price, reason, time.time()),
            )
            self._conn.commit()
