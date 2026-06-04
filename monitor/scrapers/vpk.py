"""Scraper for v-p-k.ru (Вентиляция-Пневматика-Компрессоры).

Engine: likely 1С-Битрикс.
Source: HTML listing + JSON XHR probe (Bitrix sometimes exposes /bitrix/components/.../result.php).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..base_scraper import BaseScraper
from ..models import Product, clean_price, detect_series_status

logger = logging.getLogger(__name__)

BASE = "https://v-p-k.ru"

CATEGORIES = [
    "/catalog/kompressory/",
    "/catalog/kompressory/vintovye/",
    "/catalog/kompressory/porshnevye/",
    "/catalog/kompressory/bezmaslyane/",
    "/catalog/pnevmatika/",
]

# Bitrix AJAX endpoint pattern (varies by site setup)
AJAX_ENDPOINTS = [
    "/ajax/catalog/",
    "/bitrix/components/bitrix/catalog/",
]


class VpkScraper(BaseScraper):
    site = "v-p-k.ru"
    base_url = BASE

    def discover(self) -> list[str]:
        return [BASE + cat for cat in CATEGORIES]

    def fetch_listing(self, url: str) -> list[str]:
        """Try JSON XHR first, fall back to HTML."""
        product_urls: list[str] = []
        page = 1

        while True:
            paged = url if page == 1 else f"{url}?PAGEN_1={page}"
            try:
                resp = self.client.get(paged)
            except Exception as exc:
                logger.warning("[v-p-k] listing error %s: %s", paged, exc)
                break

            # Try to detect JSON response
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                data = resp.json()
                items = data.get("items") or data.get("products") or []
                for item in items:
                    u = item.get("url") or item.get("detail_page_url", "")
                    if u:
                        product_urls.append(u if u.startswith("http") else BASE + u)
                if not items:
                    break
                page += 1
                continue

            soup = BeautifulSoup(resp.content, "lxml")
            links = soup.select(
                ".catalog-item a.catalog-item-title, "
                ".bx-catalog a[href*='/catalog/'], "
                ".catalog-item a, "
                "a.product-title[href*='/catalog/']"
            )
            found = 0
            for a in links:
                href = a.get("href", "")
                if href and "/catalog/" in href and href.count("/") >= 4:
                    product_urls.append(href if href.startswith("http") else BASE + href)
                    found += 1

            if found == 0:
                break
            if not soup.select_one(f"a[href*='PAGEN_1={page + 1}']"):
                break
            page += 1

        return list(dict.fromkeys(product_urls))

    def parse_product(self, url: str) -> Optional[Product]:
        resp = self.client.get(url)
        soup = BeautifulSoup(resp.content, "lxml")
        page_text = soup.get_text(" ", strip=True)

        name = self._text(soup, "h1") or ""
        brand = (
            self._text(soup, "[itemprop='brand']")
            or self._text(soup, ".vendor-name, .manufacturer")
            or self._breadcrumb_part(soup, -2)
        )
        model = self._text(soup, "[itemprop='model'], .product-model") or name
        sku = self._text(soup, "[itemprop='sku'], .article")

        price = self._price(soup, "[itemprop='price'], .price, .cost")
        old_price = self._price(soup, ".price-old, .old-price")
        discount_pct = None
        if price and old_price and old_price > price:
            discount_pct = round((old_price - price) / old_price * 100, 1)

        avail_el = soup.select_one("[itemprop='availability'], .availability, .in-stock-status")
        availability = avail_el.get_text(strip=True) if avail_el else ""
        series_status = detect_series_status(page_text)
        if series_status == "неизвестно":
            low = availability.lower()
            if "в наличии" in low:
                series_status = "в наличии"
            elif "под заказ" in low:
                series_status = "под заказ"

        replacement_model = self._get_replacement(soup)
        specs = self._extract_specs(soup)
        cat_path = " > ".join(
            i.get_text(strip=True)
            for i in soup.select(".breadcrumb li, nav[aria-label] li")
            if i.get_text(strip=True)
        )
        image_url = self._image(soup)

        return Product(
            site=self.site, brand=brand, name=name, model=model, sku=sku,
            price=price, old_price=old_price, discount_pct=discount_pct,
            availability=availability, series_status=series_status,
            replacement_model=replacement_model, specs=specs,
            category_path=cat_path, product_url=url, image_url=image_url,
        )

    # ------------------------------------------------------------------

    def _text(self, soup: BeautifulSoup, sel: str) -> str:
        el = soup.select_one(sel)
        return el.get_text(strip=True) if el else ""

    def _price(self, soup: BeautifulSoup, sel: str) -> float | None:
        el = soup.select_one(sel)
        if not el:
            return None
        return clean_price(el.get("content") or el.get_text())

    def _breadcrumb_part(self, soup: BeautifulSoup, idx: int) -> str:
        items = [i.get_text(strip=True) for i in soup.select(".breadcrumb li") if i.get_text(strip=True)]
        try:
            return items[idx]
        except IndexError:
            return ""

    def _extract_specs(self, soup: BeautifulSoup) -> dict:
        specs: dict = {}
        for row in soup.select(".properties-table tr, .chars-table tr, table.product-props tr"):
            cells = row.select("td, th")
            if len(cells) >= 2:
                k = cells[0].get_text(strip=True)
                v = cells[-1].get_text(strip=True)
                if k and v:
                    specs[k] = v
        if not specs:
            for dl in soup.select("dl"):
                for dt, dd in zip(dl.select("dt"), dl.select("dd")):
                    specs[dt.get_text(strip=True)] = dd.get_text(strip=True)
        return specs

    def _image(self, soup: BeautifulSoup) -> str:
        img = soup.select_one("[itemprop='image'], .product-img img, .detail-picture img")
        if img:
            src = img.get("data-src") or img.get("src", "")
            if src:
                return src if src.startswith("http") else BASE + src
        return ""

    def _get_replacement(self, soup: BeautifulSoup) -> str:
        for el in soup.select("p, .note, div.replacement"):
            text = el.get_text(strip=True)
            if any(k in text.lower() for k in ("аналог", "замена", "заменяет", "заменён")):
                m = re.search(r"[A-ZА-Я][\w\-]{3,}", text)
                if m:
                    return m.group(0)
        return ""
