"""Write products to CSV (utf-8-sig for Excel) and SQLite."""
from __future__ import annotations

import csv
import json
import logging
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import Product

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    site            TEXT NOT NULL,
    brand           TEXT,
    series          TEXT,
    name            TEXT,
    model           TEXT,
    sku             TEXT,
    price           REAL,
    old_price       REAL,
    discount_pct    REAL,
    currency        TEXT DEFAULT 'RUB',
    availability    TEXT,
    series_status   TEXT,
    replacement_model TEXT,
    specs           TEXT,
    category_path   TEXT,
    product_url     TEXT,
    image_url       TEXT,
    normalized_key  TEXT,
    scraped_at      TEXT,
    UNIQUE(site, product_url, run_id)
);
CREATE INDEX IF NOT EXISTS idx_products_run ON products(run_id);
CREATE INDEX IF NOT EXISTS idx_products_site ON products(site);
CREATE INDEX IF NOT EXISTS idx_products_key ON products(normalized_key);
"""


class Storage:
    def __init__(self, db_path: Path, csv_path: Path, run_id: str) -> None:
        self.db_path = db_path
        self.csv_path = csv_path
        self.run_id = run_id
        db_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(CREATE_TABLE_SQL)
        self._conn.commit()

        self._csv_file = open(csv_path, "w", newline="", encoding="utf-8-sig")
        self._writer = csv.DictWriter(self._csv_file, fieldnames=Product.csv_headers())
        self._writer.writeheader()

        self._counts: dict[str, int] = {}

    def write(self, product: Product) -> None:
        row = product.to_dict()
        # CSV
        self._writer.writerow(row)

        # SQLite
        cols = Product.csv_headers()
        placeholders = ", ".join("?" for _ in cols)
        col_names = ", ".join(cols)
        vals = [row[c] for c in cols]
        try:
            self._conn.execute(
                f"INSERT OR IGNORE INTO products (run_id, {col_names}) "
                f"VALUES (?, {placeholders})",
                [self.run_id] + vals,
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("SQLite write error: %s", exc)

        site = product.site
        self._counts[site] = self._counts.get(site, 0) + 1

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def close(self) -> None:
        self._conn.close()
        self._csv_file.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *_) -> None:
        self.close()


def diff_runs(db_path: Path, run_a: str, run_b: str) -> dict:
    """Compare two run_ids. Return price changes and new discontinued items."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    def fetch(run_id: str) -> dict[str, sqlite3.Row]:
        rows = conn.execute(
            "SELECT * FROM products WHERE run_id = ?", [run_id]
        ).fetchall()
        return {f"{r['site']}||{r['product_url']}": r for r in rows}

    a_data = fetch(run_a)
    b_data = fetch(run_b)
    conn.close()

    price_changes = []
    new_discontinued = []
    new_products = []

    all_keys = set(a_data) | set(b_data)
    for key in all_keys:
        a = a_data.get(key)
        b = b_data.get(key)

        if a is None and b is not None:
            new_products.append(dict(b))
            continue

        if a is None or b is None:
            continue

        # Price change
        pa = a["price"]
        pb = b["price"]
        if pa is not None and pb is not None and pa != pb:
            price_changes.append({
                "site": b["site"],
                "name": b["name"],
                "url": b["product_url"],
                "price_before": pa,
                "price_after": pb,
                "change_pct": round((pb - pa) / pa * 100, 1) if pa else None,
            })

        # Newly discontinued
        sa = a["series_status"]
        sb = b["series_status"]
        if sa != "снято" and sb == "снято":
            new_discontinued.append({
                "site": b["site"],
                "name": b["name"],
                "url": b["product_url"],
                "was": sa,
                "replacement": b["replacement_model"],
            })

    return {
        "run_a": run_a,
        "run_b": run_b,
        "price_changes": sorted(price_changes, key=lambda x: abs(x["change_pct"] or 0), reverse=True),
        "new_discontinued": new_discontinued,
        "new_products": new_products,
    }
