"""Abstract base class for all site scrapers."""
from __future__ import annotations

import json
import os
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

from .http_client import HttpClient
from .models import Product

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Common interface every site scraper must implement."""

    site: str  # domain, e.g. "compressortyt.ru"
    base_url: str  # e.g. "https://compressortyt.ru"

    # Per-site polite delay between requests (seconds). Robust sites can set
    # these lower; sites behind anti-bot (ddos-guard) keep them higher.
    delay_min: float = 1.0
    delay_max: float = 2.5

    def __init__(self) -> None:
        # Global env override lets you tune requests/sec without code changes:
        #   DELAY_MIN / DELAY_MAX (seconds), or per-site
        #   DELAY_MIN__<SITE> / DELAY_MAX__<SITE> (dots/dashes → underscores).
        key = self.site.replace(".", "_").replace("-", "_").upper()
        dmin = os.getenv(f"DELAY_MIN__{key}") or os.getenv("DELAY_MIN")
        dmax = os.getenv(f"DELAY_MAX__{key}") or os.getenv("DELAY_MAX")
        delay_min = float(dmin) if dmin else self.delay_min
        delay_max = float(dmax) if dmax else self.delay_max
        self.client = HttpClient(self.site, delay_min=delay_min, delay_max=delay_max)
        self._checkpoint_path = Path(f"cache/{self.site}.checkpoint.json")
        self._checkpoint: dict = self._load_checkpoint()

    # ------------------------------------------------------------------
    # Interface
    # ------------------------------------------------------------------

    @abstractmethod
    def discover(self) -> list[str]:
        """Return list of category/listing URLs to crawl."""
        ...

    @abstractmethod
    def fetch_listing(self, url: str) -> list[str]:
        """Return product URLs from a listing/category page (handles pagination)."""
        ...

    @abstractmethod
    def parse_product(self, url: str) -> Product | None:
        """Fetch and parse a single product page. Return None on skip."""
        ...

    def scrape(self) -> Iterator[Product]:
        """Main entry point: discover → fetch listings → parse products.

        A fresh run re-scrapes every product (a price monitor wants current
        prices each time); the HTTP cache — not the checkpoint — protects the
        site from repeated load. The checkpoint only dedups within a single run.
        """
        seen_urls: set[str] = set()

        category_urls = self.discover()
        logger.info("[%s] %d categories to crawl", self.site, len(category_urls))

        for cat_url in category_urls:
            try:
                product_urls = self.fetch_listing(cat_url)
            except Exception as exc:
                logger.warning("[%s] listing error %s: %s", self.site, cat_url, exc)
                continue

            for prod_url in product_urls:
                if prod_url in seen_urls:  # de-dup within this run only
                    continue
                seen_urls.add(prod_url)
                try:
                    product = self.parse_product(prod_url)
                    if product:
                        yield product
                except Exception as exc:
                    logger.warning("[%s] product error %s: %s", self.site, prod_url, exc)

    # ------------------------------------------------------------------
    # Checkpoint helpers
    # ------------------------------------------------------------------

    def _load_checkpoint(self) -> dict:
        if self._checkpoint_path.exists():
            try:
                return json.loads(self._checkpoint_path.read_text())
            except Exception:
                pass
        return {}

    def _save_checkpoint(self, done_urls: set[str]) -> None:
        self._checkpoint["done_urls"] = list(done_urls)
        self._checkpoint_path.write_text(json.dumps(self._checkpoint))

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "BaseScraper":
        return self

    def __exit__(self, *_) -> None:
        self.close()
