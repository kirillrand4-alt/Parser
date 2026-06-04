"""Scraper for pnevmo-sklad.ru.

Engine: 1С-Битрикс.
Source: HTML listing (YML feed not publicly available; check /yml/ on first run).
Pagination: ?PAGEN_1=N  (standard Bitrix).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..base_scraper import BaseScraper
from ..models import Product, clean_price, detect_series_status

logger = logging.getLogger(__name__)

BASE = "https://pnevmo-sklad.ru"

CATEGORIES = [
    "/catalog/kompressory/vintovye-kompressory/",
    "/catalog/kompressory/porshnevye-kompressory/",
    "/catalog/kompressory/bezmaslyane-kompressory/",
    "/catalog/kompressory/spiralnye-kompressory/",
    "/catalog/kompressory/dizelnyye-kompressory/",
    "/catalog/pnevmooborudovanie/",
]

# YML feed paths to probe during recon
FEED_PATHS = ["/yml/", "/catalog.yml", "/export.yml", "/upload/iblock/export.yml"]


class PnevmoSkladScraper(BaseScraper):
    site = "pnevmo-sklad.ru"
    base_url = BASE

    def discover(self) -> list[str]:
        return [BASE + cat for cat in CATEGORIES]

    def fetch_listing(self, url: str) -> list[str]:
        product_urls: list[str] = []
        page = 1
        while True:
            paged_url = url if page == 1 else f"{url}?PAGEN_1={page}"
            try:
                resp = self.client.get(paged_url)
            except Exception as exc:
                logger.warning("[pnevmo-sklad] listing error %s: %s", paged_url, exc)
                break

            soup = BeautifulSoup(resp.content, "lxml")
            links = soup.select("div.catalog-section-item a.item-title, "
                                ".catalog-item a[href*='/catalog/'], "
                                "a.product-name[href*='/catalog/']")
            if not links:
                # Generic Bitrix selector
                links = soup.select(".bx_catalog_item a.bx_catalog_item_title")

            found = 0
            for a in links:
                href = a.get("href", "")
                if "/catalog/" in href and href.count("/") >= 4:
                    full = href if href.startswith("http") else BASE + href
                    product_urls.append(full)
                    found += 1

            if found == 0:
                break

            # Check for next Bitrix page link
            next_link = soup.select_one(f"a[href*='PAGEN_1={page + 1}']")
            if not next_link:
                break
            page += 1

        return list(dict.fromkeys(product_urls))

    def parse_product(self, url: str) -> Optional[Product]:
        resp = self.client.get(url)
        soup = BeautifulSoup(resp.content, "lxml")
        page_text = soup.get_text(" ", strip=True)

        name = ""
        h1 = soup.select_one("h1")
        if h1:
            name = h1.get_text(strip=True)

        # Brand from breadcrumb or itemprop
        brand = self._get_text(soup, "[itemprop='brand']") or self._breadcrumb_brand(soup)
        model = self._get_text(soup, "[itemprop='model'], .product-article span") or name

        price = self._get_price(soup, ".price, .cost, [itemprop='price']")
        old_price = self._get_price(soup, ".price-old, .old-price, .strike")
        discount_pct = self._calc_discount(price, old_price)

        availability_el = soup.select_one(".availability, .product-quantity, [itemprop='availability']")
        availability = availability_el.get_text(strip=True) if availability_el else ""
        series_status = detect_series_status(page_text)
        if series_status == "неизвестно" and availability:
            lower = availability.lower()
            if "в наличии" in lower or "есть" in lower:
                series_status = "в наличии"

        specs = self._extract_specs(soup)
        category_path = self._breadcrumb(soup)
        image_url = self._get_image(soup)
        sku = self._get_text(soup, ".article, [itemprop='sku'], .product-art span") or ""
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
        for row in soup.select(".props-table tr, table.chars tr, .properties tr, .product-props tr"):
            cells = row.select("td, th")
            if len(cells) >= 2:
                k = cells[0].get_text(strip=True)
                v = cells[-1].get_text(strip=True)
                if k and v:
                    specs[k] = v
        if not specs:
            for dt, dd in zip(
                soup.select(".props-list dt, dl dt"),
                soup.select(".props-list dd, dl dd"),
            ):
                k = dt.get_text(strip=True)
                v = dd.get_text(strip=True)
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
