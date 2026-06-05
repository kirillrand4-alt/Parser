"""Scraper for pnevmo-sklad.ru.

Engine: 1С-Битрикс.
Source: HTML listing (YML feed not publicly available; check /yml/ on first run).
Pagination: ?PAGEN_1=N  (standard Bitrix).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..base_scraper import BaseScraper
from ..models import (
    Product, clean_price, detect_series_status,
    has_discontinued_signal, status_from_availability,
)
from ..sitemap import collect_product_urls

logger = logging.getLogger(__name__)

BASE = "https://www.pnevmo-sklad.ru"
SITEMAP = "https://www.pnevmo-sklad.ru/sitemap.xml"

# Products live under /shop/<category>/.../<slug>. Keep compressor-relevant
# categories; skip spare-part feeds to stay on topic. Tune via env.
INCLUDE = ["/shop/"]
EXCLUDE = ["ulyanovsk.", "/sitemap"]
MAX_URLS = int(os.getenv("PNEVMO_SKLAD_MAX", "0")) or None


class PnevmoSkladScraper(BaseScraper):
    site = "pnevmo-sklad.ru"
    base_url = BASE

    def discover(self) -> list[str]:
        # Single sentinel; product URLs come from the sitemap in fetch_listing.
        return ["__sitemap__"]

    def fetch_listing(self, url: str) -> list[str]:
        urls = collect_product_urls(
            self.client, SITEMAP, include=INCLUDE, exclude=EXCLUDE, max_urls=MAX_URLS
        )
        logger.info("[pnevmo-sklad] %d product URLs from sitemap", len(urls))
        return urls

    def parse_product(self, url: str) -> Optional[Product]:
        resp = self.client.get(url)
        soup = BeautifulSoup(resp.content, "lxml")
        page_text = soup.get_text(" ", strip=True)

        name = ""
        h1 = soup.select_one("h1")
        if h1:
            name = h1.get_text(strip=True)

        # Characteristics table: <tr> with "Label:" | "Value"
        specs = self._extract_specs(soup)

        # Brand and article come straight from the chars table
        brand = specs.get("Бренд", "") or specs.get("Производитель", "")
        sku = specs.get("Артикул", "")
        model = sku or name

        # Price: .pricebox__price holds the number, or "Цена по запросу" → None
        price = self._get_price(soup, ".pricebox__price, .prodsticky__price")
        old_price = self._get_price(soup, ".pricebox__oldprice, .price-old")
        discount_pct = self._calc_discount(price, old_price)

        availability_el = soup.select_one(".pricebox__instock, .ltprod__instock, .prodbig__instock")
        availability = availability_el.get_text(" ", strip=True) if availability_el else ""
        # Trust the product's own availability block first; only let strong
        # "снято/архив" signals from the page override it.
        series_status = status_from_availability(availability)
        if has_discontinued_signal(page_text):
            series_status = "снято"

        category_path = self._breadcrumb(soup)
        image_url = self._get_image(soup)
        replacement_model = self._get_replacement(soup)

        return Product(
            site=self.site,
            brand=brand,
            name=name,
            model=model,
            sku=sku,
            price=price,
            old_price=old_price,
            discount_pct=discount_pct,
            availability=availability,
            series_status=series_status,
            replacement_model=replacement_model,
            specs=specs,
            category_path=category_path,
            product_url=url,
            image_url=image_url,
        )

    # ------------------------------------------------------------------

    def _get_text(self, soup: BeautifulSoup, selector: str) -> str:
        el = soup.select_one(selector)
        return el.get_text(strip=True) if el else ""

    def _get_price(self, soup: BeautifulSoup, selector: str) -> float | None:
        el = soup.select_one(selector)
        if not el:
            return None
        val = el.get("content") or el.get_text()
        return clean_price(val)

    def _calc_discount(self, price: float | None, old: float | None) -> float | None:
        if price and old and old > price:
            return round((old - price) / old * 100, 1)
        return None

    def _breadcrumb(self, soup: BeautifulSoup) -> str:
        items = soup.select(".breadcrumb li, nav[aria-label='breadcrumb'] li, .breadcrumbs span")
        return " > ".join(i.get_text(strip=True) for i in items if i.get_text(strip=True))

    def _breadcrumb_brand(self, soup: BeautifulSoup) -> str:
        items = soup.select(".breadcrumb li, nav[aria-label='breadcrumb'] li")
        # Brand is usually the second-to-last breadcrumb
        texts = [i.get_text(strip=True) for i in items if i.get_text(strip=True)]
        if len(texts) >= 2:
            return texts[-2]
        return ""

    def _extract_specs(self, soup: BeautifulSoup) -> dict:
        specs: dict = {}
        # Full characteristics table is .charstable (also .prodbig__chars-table teaser)
        for row in soup.select("table.charstable tr, .prodbig__chars-table tr"):
            cells = row.select("td, th")
            if len(cells) >= 2:
                k = cells[0].get_text(" ", strip=True).rstrip(":").strip()
                v = cells[-1].get_text(" ", strip=True)
                if k and v:
                    specs[k] = v
        return specs

    def _get_image(self, soup: BeautifulSoup) -> str:
        for sel in ("[itemprop='image']", ".product-image img", ".detail-picture img"):
            img = soup.select_one(sel)
            if img:
                src = img.get("data-src") or img.get("src", "")
                if src and not src.endswith(".svg"):
                    return src if src.startswith("http") else BASE + src
        return ""

    def _get_replacement(self, soup: BeautifulSoup) -> str:
        for el in soup.select("p, span, div"):
            text = el.get_text(strip=True)
            if any(k in text.lower() for k in ("аналог", "замена", "заменяет")):
                m = re.search(r"[A-ZА-Я][\w\-]{3,}", text)
                if m:
                    return m.group(0)
        return ""
