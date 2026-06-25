"""Scraper for air-energy.ru — ONLY Zega brand category.

Engine: 1С-Битрикс (Aspro / custom template).
Source: category page /catalog/...vintovye-kompressory-zega/ with ?PAGEN_1=N
        pagination; product pages at /catalog/.../product/<slug>/.

Markup (1C-Bitrix standard):
- listing cards: .catalog-item a.catalog-item-title, or a[href*="/catalog/"] inside .item
- price:         [itemprop="price"] content  OR  .catalog-item-price, .price
- availability:  .catalog-item-status, .status, [class*="availability"]
- specs:         table.props tr, .props-group li, .properties tr
- brand:         always "Зега" / "Zega" (category is brand-specific)
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from bs4 import BeautifulSoup

from ..base_scraper import BaseScraper
from ..models import (
    Product, clean_price, has_discontinued_signal, status_from_availability,
    extract_brand_from_name, extract_model_from_name, harvest_specs, parse_spec_table,
)

logger = logging.getLogger(__name__)

BASE = "https://www.air-energy.ru"
BRAND = "Зега"
MAX_URLS = int(os.getenv("AIR_ENERGY_MAX", "0")) or None
MAX_PAGES = 50

CATEGORIES = [
    "https://www.air-energy.ru/catalog/vozdushnye-kompressory/vintovye-kompressory/vintovye-kompressory-zega/",
]


class AirEnergyScraper(BaseScraper):
    site = "air-energy.ru"
    base_url = BASE
    delay_min = 0.8
    delay_max = 1.8

    def discover(self) -> list[str]:
        return CATEGORIES

    def fetch_listing(self, url: str) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []
        for page in range(1, MAX_PAGES + 1):
            page_url = url if page == 1 else f"{url}?PAGEN_1={page}"
            try:
                resp = self.client.get(page_url, timeout=15)
            except Exception as exc:
                logger.warning("[air-energy] page %d error: %s", page, exc)
                break
            soup = BeautifulSoup(resp.content, "lxml")
            new = 0
            for a in soup.select(
                "a.catalog-item-title, .catalog-item__title a, "
                ".catalog-item-name a, .item-title a, "
                "a[href*='/catalog/'][href*='/product']"
            ):
                href = (a.get("href") or "").split("?")[0].strip()
                if not href or href == url.rstrip("/"):
                    continue
                # skip category links — product URLs are deeper
                depth = len([p for p in href.split("/") if p])
                if depth < 3:
                    continue
                full = href if href.startswith("http") else BASE + href
                if full not in seen:
                    seen.add(full)
                    urls.append(full)
                    new += 1
            if new == 0:
                break
            logger.info("[air-energy] page %d: %d URLs so far", page, len(urls))
        if MAX_URLS:
            urls = urls[:MAX_URLS]
        logger.info("[air-energy] %d product URLs", len(urls))
        return urls

    def parse_product(self, url: str) -> Optional[Product]:
        resp = self.client.get(url)
        soup = BeautifulSoup(resp.content, "lxml")
        page_text = soup.get_text(" ", strip=True)

        h1 = soup.select_one("h1")
        name = h1.get_text(" ", strip=True) if h1 else ""

        specs = self._extract_specs(soup)

        price = self._price(soup)
        old_price = self._old_price(soup)
        discount_pct = None
        if price and old_price and old_price > price:
            discount_pct = round((old_price - price) / old_price * 100, 1)

        av_el = soup.select_one(
            ".catalog-item-status, .status-in-stock, .product-availability, "
            "[class*='availability'], [class*='in-stock'], [class*='nalichie']"
        )
        availability = av_el.get_text(" ", strip=True) if av_el else (
            "В наличии" if price else "по запросу"
        )
        series_status = status_from_availability(availability) or (
            "в наличии" if price else "под заказ"
        )
        if has_discontinued_signal(page_text):
            series_status = "снято"

        category_path = self._breadcrumb(soup)
        image_url = self._image(soup)

        return Product(
            site=self.site, brand=BRAND, name=name,
            model=extract_model_from_name(name, BRAND),
            price=price, old_price=old_price, discount_pct=discount_pct,
            availability=availability, series_status=series_status,
            specs=specs, category_path=category_path,
            product_url=url, image_url=image_url,
        )

    def _price(self, soup: BeautifulSoup) -> float | None:
        el = soup.select_one("[itemprop='price']")
        if el:
            p = clean_price(el.get("content") or el.get_text())
            if p:
                return p
        for sel in (".catalog-item-price .price, .price-value, "
                    ".product-price, .price.font-bold".split(",")):
            el = soup.select_one(sel.strip())
            if el:
                p = clean_price(el.get_text())
                if p:
                    return p
        return None

    def _old_price(self, soup: BeautifulSoup) -> float | None:
        el = soup.select_one(".old-price, .price-old, [class*='old_price']")
        return clean_price(el.get_text()) if el else None

    def _extract_specs(self, soup: BeautifulSoup) -> dict:
        specs: dict = {}
        # Bitrix properties table
        rows = soup.select(
            "table.props tr, .properties tr, table.characteristics tr, "
            ".props-group li, .product-properties tr"
        )
        if rows:
            extracted, is_matrix = parse_spec_table(rows)
            if not is_matrix:
                specs = extracted
        if len(specs) < 3:
            for k, v in harvest_specs(soup).items():
                specs.setdefault(k, v)
        return specs

    def _breadcrumb(self, soup: BeautifulSoup) -> str:
        items = soup.select("[itemprop='itemListElement'] [itemprop='name']")
        if items:
            return " > ".join(i.get_text(strip=True) for i in items if i.get_text(strip=True))
        nav = soup.select_one("nav.breadcrumb, .breadcrumbs, [class*='breadcrumb']")
        if nav:
            return " > ".join(a.get_text(strip=True) for a in nav.select("a, span")
                              if a.get_text(strip=True))
        return ""

    def _image(self, soup: BeautifulSoup) -> str:
        img = soup.select_one(
            "[itemprop='image'], .product-image img, .detail-picture img, "
            ".catalog-item-image img"
        )
        if img:
            src = img.get("data-src") or img.get("content") or img.get("src", "")
            if src:
                return src if src.startswith("http") else BASE + src
        return ""
