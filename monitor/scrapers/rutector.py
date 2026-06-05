"""Scraper for rutector.ru.

Engine: 1С-Битрикс.
Source: sitemap-iblock-4.xml → products at /products/<slug> (~23k items).

Markup:
- specs:        table.zebra rows (label | value)
- price:        [itemprop=price] content (0 / "по запросу" → None)
- availability: .not-available_* present → price-on-request ("под заказ")
- brand:        specs "Бренд"/"Производитель", else breadcrumb
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..base_scraper import BaseScraper
from ..models import (
    Product, clean_price, has_discontinued_signal, status_from_availability,
    extract_brand_from_name,
)
from ..sitemap import collect_product_urls

logger = logging.getLogger(__name__)

BASE = "https://rutector.ru"
SITEMAP = "https://rutector.ru/sitemap-iblock-4.xml"
INCLUDE = ["/products/"]
EXCLUDE = ["/sitemap"]
MAX_URLS = int(os.getenv("RUTECTOR_MAX", "0")) or None

# Filter by URL slug — rutector sells everything (23k items); keep only
# compressor-related slugs. Generators, welding, water-lowering etc. are excluded.
SLUG_KEYWORDS = (
    "kompressor", "vintov", "porshnev", "pnevmo",
    "resiver", "ressiver", "osushitel", "vozduh",
)

_BRAND_KEYS = ("Бренд", "Производитель", "Марка", "Торговая марка")


class RutectorScraper(BaseScraper):
    site = "rutector.ru"
    base_url = BASE
    # Robust nginx/openresty host — can take a faster cadence
    delay_min = 0.4
    delay_max = 0.9

    def discover(self) -> list[str]:
        return ["__sitemap__"]

    def fetch_listing(self, url: str) -> list[str]:
        urls = collect_product_urls(
            self.client, SITEMAP, include=INCLUDE, exclude=EXCLUDE, max_urls=None
        )
        urls = [u for u in urls if any(k in u.lower() for k in SLUG_KEYWORDS)]
        if MAX_URLS:
            urls = urls[:MAX_URLS]
        logger.info("[rutector] %d product URLs from sitemap (after slug filter)", len(urls))
        return urls

    def parse_product(self, url: str) -> Optional[Product]:
        resp = self.client.get(url)
        soup = BeautifulSoup(resp.content, "lxml")
        page_text = soup.get_text(" ", strip=True)

        h1 = soup.select_one("h1")
        name = h1.get_text(" ", strip=True) if h1 else ""

        specs = self._extract_specs(soup)

        brand = ""
        for k in _BRAND_KEYS:
            if specs.get(k):
                brand = specs[k]
                break
        if not brand:
            # Product pages link to their brand page /brands/<slug>
            a = soup.select_one("a[href*='/brands/']")
            if a and a.get_text(strip=True):
                brand = a.get_text(strip=True)
        if not brand:
            brand = extract_brand_from_name(name)

        sku = specs.get("Артикул", "") or specs.get("Код товара", "")
        model = sku or name

        price = self._price(soup)
        old_price = self._old_price(soup)
        discount_pct = None
        if price and old_price and old_price > price:
            discount_pct = round((old_price - price) / old_price * 100, 1)

        # Availability: "not-available" block means price-on-request / orderable
        if soup.select_one(".not-available_main, .not-available_block"):
            availability = "по запросу"
            series_status = "под заказ"
        else:
            av = soup.select_one("[class*='in-stock'], [class*='available'], [class*='nalichie']")
            availability = av.get_text(" ", strip=True) if av else ("в наличии" if price else "")
            series_status = status_from_availability(availability) or ("в наличии" if price else "неизвестно")
        if has_discontinued_signal(page_text):
            series_status = "снято"

        replacement_model = self._replacement(soup)
        category_path = self._breadcrumb(soup)
        image_url = self._image(soup)

        return Product(
            site=self.site, brand=brand, name=name, model=model, sku=sku,
            price=price, old_price=old_price, discount_pct=discount_pct,
            availability=availability, series_status=series_status,
            replacement_model=replacement_model, specs=specs,
            category_path=category_path, product_url=url, image_url=image_url,
        )

    # ------------------------------------------------------------------

    def _price(self, soup: BeautifulSoup) -> float | None:
        el = soup.select_one("[itemprop='price']")
        if el:
            p = clean_price(el.get("content") or el.get_text())
            if p:  # 0 / empty → None (по запросу)
                return p
        el = soup.select_one(".price-current, .monoblock-threaded_price .price")
        return clean_price(el.get_text()) if el else None

    def _old_price(self, soup: BeautifulSoup) -> float | None:
        el = soup.select_one(".old-price, .price-old, [class*='old_price'], [class*='old-price']")
        return clean_price(el.get_text()) if el else None

    def _extract_specs(self, soup: BeautifulSoup) -> dict:
        specs: dict = {}
        for row in soup.select("table.zebra tr, table.props tr, .characteristics tr"):
            cells = row.select("td, th")
            if len(cells) >= 2:
                k = cells[0].get_text(" ", strip=True).rstrip(":").strip()
                v = cells[-1].get_text(" ", strip=True)
                if k and v:
                    specs[k] = v
        return specs

    def _breadcrumb(self, soup: BeautifulSoup) -> str:
        items = soup.select(".breadcrumb a, .breadcrumbs a, [itemprop='itemListElement'] [itemprop='name']")
        return " > ".join(i.get_text(strip=True) for i in items if i.get_text(strip=True))

    def _breadcrumb_brand(self, soup: BeautifulSoup) -> str:
        items = [i.get_text(strip=True) for i in soup.select(".breadcrumb a, .breadcrumbs a") if i.get_text(strip=True)]
        return items[-1] if items else ""

    def _image(self, soup: BeautifulSoup) -> str:
        img = soup.select_one("[itemprop='image'], .product-image img, .detail-picture img")
        if img:
            src = img.get("data-src") or img.get("content") or img.get("src", "")
            if src:
                return src if src.startswith("http") else BASE + src
        return ""

    def _replacement(self, soup: BeautifulSoup) -> str:
        for el in soup.select("p, .note, .replacement"):
            text = el.get_text(strip=True)
            if any(k in text.lower() for k in ("аналог", "замена", "заменяет", "заменён")):
                m = re.search(r"[A-ZА-Я][\w\-]{3,}", text)
                if m:
                    return m.group(0)
        return ""
