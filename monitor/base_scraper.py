"""Abstract base class for all site scrapers."""
from __future__ import annotations

import json
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

    def __init__(self) -> None:
        self.client = HttpClient(self.site)
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
        """Main entry point: discover → fetch listings → parse products."""
        seen_urls: set[str] = set(self._checkpoint.get("done_urls", []))

        category_urls = self.discover()
        logger.info("[%s] %d categories to crawl", self.site, len(category_urls))

        for cat_url in category_urls:
            try:
                product_urls = self.fetch_listing(cat_url)
            except Exception as exc:
                logger.warning("[%s] listing error %s: %s", self.site, cat_url, exc)
                continue

            for prod_url in product_urls:
                if prod_url in seen_urls:
                    continue
                try:
                    product = self.parse_product(prod_url)
                    if product:
                        seen_urls.add(prod_url)
                        self._save_checkpoint(seen_urls)
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
