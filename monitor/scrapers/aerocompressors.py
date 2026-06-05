"""Scraper for aerocompressors.ru.

Behind ddos-guard (serves normally with a realistic UA + polite delays).
Source: sitemap.xml (flat, ~37k URLs) → products under /katalog_produkcii/.
Category pages share the namespace and are skipped when no price is found.

Markup:
- price:        [itemprop=price] / .price
- specs:        table.tech rows (label | value)
- brand:        /katalog_po_brendam/ link, then known-brand match on name
- availability: no explicit block; price present ⇒ "в наличии"
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from bs4 import BeautifulSoup

from ..base_scraper import BaseScraper
from ..models import (
    Product, clean_price, has_discontinued_signal, extract_brand_from_name,
)
from ..sitemap import collect_product_urls

logger = logging.getLogger(__name__)

BASE = "https://aerocompressors.ru"
SITEMAP = "https://aerocompressors.ru/sitemap.xml"
INCLUDE = ["/katalog_produkcii/"]
EXCLUDE = ["/sitemap"]
MAX_URLS = int(os.getenv("AEROCOMPRESSORS_MAX", "0")) or None

_BRAND_KEYS = ("Бренд", "Производитель", "Марка", "Торговая марка")


class AerocompressorsScraper(BaseScraper):
    site = "aerocompressors.ru"
    base_url = BASE

    def __init__(self) -> None:
        super().__init__()
        # ddos-guard: be gentler than the default cadence
        self.client.delay_min = 1.5
        self.client.delay_max = 3.0

    def discover(self) -> list[str]:
        return ["__sitemap__"]

    def fetch_listing(self, url: str) -> list[str]:
        urls = collect_product_urls(
            self.client, SITEMAP, include=INCLUDE, exclude=EXCLUDE, max_urls=MAX_URLS
        )
        logger.info("[aerocompressors] %d candidate URLs from sitemap", len(urls))
        return urls

    def parse_product(self, url: str) -> Optional[Product]:
        resp = self.client.get(url)
        soup = BeautifulSoup(resp.content, "lxml")

        price = self._price(soup)
        specs = self._extract_specs(soup)

        # Distinguish a product (has a price block or a specs table) from a
        # category listing. A product with "цена по запросу" has no number but
        # still has the price element/specs — keep it as "под заказ".
        has_price_block = soup.select_one("[itemprop='price'], .price") is not None
        if price is None and not specs and not has_price_block:
            return None  # category page

        page_text = soup.get_text(" ", strip=True)
        h1 = soup.select_one("h1")
        name = h1.get_text(" ", strip=True) if h1 else ""

        brand = ""
        for k in _BRAND_KEYS:
            if specs.get(k):
                brand = specs[k]
                break
        if not brand:
            a = soup.select_one("a[href*='/katalog_po_brendam/']")
            if a and a.get_text(strip=True):
                brand = a.get_text(strip=True)
        if not brand:
            brand = extract_brand_from_name(name)

        old_price = self._old_price(soup)
        discount_pct = None
        if price and old_price and old_price > price:
            discount_pct = round((old_price - price) / old_price * 100, 1)

        # No explicit stock block: a numeric price implies in-stock; a product
        # shown "по запросу" (no number) is treated as orderable ("под заказ").
        if price is not None:
            availability, series_status = "в наличии", "в наличии"
        else:
            availability, series_status = "цена по запросу", "под заказ"
        if has_discontinued_signal(page_text):
            series_status = "снято"
            availability = "снято с производства"

        category_path = self._category_from_url(url)
        image_url = self._image(soup)

        return Product(
            site=self.site, brand=brand, name=name, model=name,
            price=price, old_price=old_price, discount_pct=discount_pct,
            availability=availability, series_status=series_status,
            specs=specs, category_path=category_path,
            product_url=url, image_url=image_url,
        )

    # ------------------------------------------------------------------

    def _price(self, soup: BeautifulSoup) -> float | None:
        el = soup.select_one("[itemprop='price']")
        if el:
            p = clean_price(el.get("content") or el.get_text())
            if p:
                return p
        el = soup.select_one(".price")
        if el:
            return clean_price(el.get_text())
        return None

    def _old_price(self, soup: BeautifulSoup) -> float | None:
        el = soup.select_one(".old-price, .price-old, [class*='old_price']")
        return clean_price(el.get_text()) if el else None

    def _extract_specs(self, soup: BeautifulSoup) -> dict:
        specs: dict = {}
        for row in soup.select("table.tech tr, table.characteristics tr, table.params tr"):
            cells = row.select("td, th")
            if len(cells) >= 2:
                k = cells[0].get_text(" ", strip=True).rstrip(":").strip()
                v = cells[-1].get_text(" ", strip=True)
                if k and v:
                    specs[k] = v
        return specs

    def _category_from_url(self, url: str) -> str:
        import urllib.parse
        parts = urllib.parse.urlparse(url).path.strip("/").split("/")
        # drop the leading "katalog_produkcii" and the final product slug
        crumbs = [p.replace("_", " ").replace("-", " ") for p in parts[1:-1]]
        return " > ".join(crumbs)

    def _image(self, soup: BeautifulSoup) -> str:
        img = soup.select_one("[itemprop='image'], .product-image img, .detail-picture img")
        if img:
            src = img.get("data-src") or img.get("content") or img.get("src", "")
            if src:
                return src if src.startswith("http") else BASE + src
        return ""
