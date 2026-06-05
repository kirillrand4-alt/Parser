"""Scraper for v-p-k.ru (Вентиляция-Пневматика-Компрессоры).

Engine: 1С-Битрикс (Sotbit/Aspro template).
Source: sitemap-iblock-248.xml → products at /product/<slug>/ (categories live
under /catalog/ and are excluded).

Markup:
- specs:        .properties-group__item (name .properties-group__name,
                value .properties-group__value)
- price:        [itemprop=price] content (0 / "По запросу" → None)
- availability: .item-stock
- brand:        specs "Бренд"/"Производитель" → known-brand match on name
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from bs4 import BeautifulSoup

from ..base_scraper import BaseScraper
from ..models import (
    Product, clean_price, has_discontinued_signal, status_from_availability,
    extract_brand_from_name, extract_model_from_name,
)
from ..sitemap import collect_product_urls

logger = logging.getLogger(__name__)

BASE = "https://www.v-p-k.ru"
SITEMAP = "https://www.v-p-k.ru/sitemap-iblock-248.xml"
INCLUDE = ["/product/"]
EXCLUDE = ["/sitemap"]
MAX_URLS = int(os.getenv("VPK_MAX", "0")) or None

# v-p-k sells 39k items (generators, welding, construction etc.); keep only
# compressor-related slugs identified by URL keyword.
SLUG_KEYWORDS = (
    "kompressor", "vintov", "porshnev", "pnevmo",
    "resiver", "ressiver", "osushitel", "vozduh",
)

_BRAND_KEYS = ("Бренд", "Производитель", "Марка", "Торговая марка")


class VpkScraper(BaseScraper):
    site = "v-p-k.ru"
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
        logger.info("[v-p-k] %d product URLs from sitemap (after slug filter)", len(urls))
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
            brand = extract_brand_from_name(name)

        price = self._price(soup)
        old_price = self._old_price(soup)
        discount_pct = None
        if price and old_price and old_price > price:
            discount_pct = round((old_price - price) / old_price * 100, 1)

        av = soup.select_one(".item-stock, [class*='item-stock']")
        availability = av.get_text(" ", strip=True) if av else ""
        series_status = status_from_availability(availability)
        if series_status == "неизвестно":
            series_status = "под заказ" if "заказ" in availability.lower() else (
                "в наличии" if price else "неизвестно")
        if has_discontinued_signal(page_text):
            series_status = "снято"

        category_path = self._breadcrumb(soup)
        image_url = self._image(soup)

        return Product(
            site=self.site, brand=brand, name=name,
            model=extract_model_from_name(name, brand),
            price=price, old_price=old_price, discount_pct=discount_pct,
            availability=availability, series_status=series_status,
            specs=specs, category_path=category_path,
            product_url=url, image_url=image_url,
        )

    # ------------------------------------------------------------------

    def _extract_specs(self, soup: BeautifulSoup) -> dict:
        specs: dict = {}
        for item in soup.select(".properties-group__item"):
            name_el = item.select_one(".properties-group__name")
            val_el = item.select_one(".properties-group__value")
            if name_el and val_el:
                k = name_el.get_text(" ", strip=True).rstrip(":").strip()
                v = val_el.get_text(" ", strip=True)
                if k and v and k not in specs:
                    specs[k] = v
        return specs

    def _price(self, soup: BeautifulSoup) -> float | None:
        el = soup.select_one("[itemprop='price']")
        if el:
            p = clean_price(el.get("content") or el.get_text())
            if p:
                return p
        el = soup.select_one(".price.font-bold, .cost .price")
        if el:
            return clean_price(el.get_text())
        return None

    def _old_price(self, soup: BeautifulSoup) -> float | None:
        el = soup.select_one(".price-old, .old-price, [class*='old_price']")
        return clean_price(el.get_text()) if el else None

    def _breadcrumb(self, soup: BeautifulSoup) -> str:
        # Use schema.org breadcrumb only — .breadcrumbs a also matches the
        # entire left sidebar navigation on the Aspro/Bitrix template.
        items = soup.select("[itemprop='itemListElement'] [itemprop='name']")
        if items:
            return " > ".join(i.get_text(strip=True) for i in items if i.get_text(strip=True))
        nav = soup.select_one("nav.breadcrumb, nav[aria-label*='read']")
        if nav:
            return " > ".join(a.get_text(strip=True) for a in nav.select("a, span") if a.get_text(strip=True))
        return ""

    def _image(self, soup: BeautifulSoup) -> str:
        img = soup.select_one("[itemprop='image'], .product-detail-gallery img, .detail-picture img")
        if img:
            src = img.get("data-src") or img.get("content") or img.get("src", "")
            if src:
                return src if src.startswith("http") else BASE + src
        return ""
