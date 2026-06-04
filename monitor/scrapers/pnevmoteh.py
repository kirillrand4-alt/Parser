"""Scraper for pnevmoteh.ru.

Source strategy: try YML feed at /yml.php first (common OpenCart path).
If feed is absent (HTTP != 200 or non-XML), fall back to HTML listing.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Optional

import requests
from bs4 import BeautifulSoup

from ..base_scraper import BaseScraper
from ..models import Product, clean_price, detect_series_status

logger = logging.getLogger(__name__)

BASE = "https://pnevmoteh.ru"
YML_PATHS = ["/yml.php", "/feed/yml", "/export/yandex.xml", "/catalog.yml"]

HTML_CATEGORIES = [
    "/catalog/kompressory/",
    "/catalog/pnevmaticheskoe-oborudovanie/",
]


class PnevmotehScraper(BaseScraper):
    site = "pnevmoteh.ru"
    base_url = BASE
    _yml_url: str | None = None

    def discover(self) -> list[str]:
        # Probe for YML feed
        for path in YML_PATHS:
            try:
                resp = self.client.get(BASE + path, timeout=15)
                ct = resp.headers.get("Content-Type", "")
                if "xml" in ct or resp.text.lstrip().startswith("<?xml"):
                    self._yml_url = BASE + path
                    logger.info("[pnevmoteh] YML feed found: %s", self._yml_url)
                    return ["__yml__"]  # sentinel
            except Exception:
                continue
        logger.info("[pnevmoteh] No YML feed, using HTML listing")
        return [BASE + cat for cat in HTML_CATEGORIES]

    def fetch_listing(self, url: str) -> list[str]:
        if url == "__yml__":
            return ["__yml__"]  # all products parsed directly from feed
        # HTML pagination — OpenCart style: ?page=N
        product_urls: list[str] = []
        page = 1
        while True:
            paged = url if page == 1 else f"{url}?page={page}"
            try:
                resp = self.client.get(paged)
            except Exception as exc:
                logger.warning("[pnevmoteh] listing error %s: %s", paged, exc)
                break
            soup = BeautifulSoup(resp.content, "lxml")
            links = soup.select(".product-thumb a, .product-layout a.product-name, "
                                "h4.product-title a, a[href*='/product/']")
            found = 0
            for a in links:
                href = a.get("href", "")
                if href and ("/product/" in href or "/catalog/" in href):
                    product_urls.append(href if href.startswith("http") else BASE + href)
                    found += 1
            if found == 0:
                break
            next_btn = soup.select_one(f"a[href*='page={page + 1}']")
            if not next_btn:
                break
            page += 1
        return list(dict.fromkeys(product_urls))

    def scrape(self):  # type: ignore[override]
        """Override to handle YML feed specially."""
        urls = self.discover()
        if urls == ["__yml__"]:
            yield from self._parse_yml_feed()
        else:
            yield from super().scrape()

    def _parse_yml_feed(self):
        """Parse YML (Yandex Market Language) XML feed — fastest path."""
        resp = self.client.get(self._yml_url)
        root = ET.fromstring(resp.content)
        ns = {"yml": ""}

        shop = root.find("shop")
        if shop is None:
            return

        # Build category map
        categories: dict[str, str] = {}
        for cat in shop.findall(".//category"):
            categories[cat.get("id", "")] = cat.text or ""

        currencies: dict[str, str] = {}
        for cur in shop.findall(".//currency"):
            currencies[cur.get("id", "")] = cur.get("rate", "1")

        for offer in shop.findall(".//offer"):
            try:
                yield self._offer_to_product(offer, categories)
            except Exception as exc:
                logger.debug("[pnevmoteh] offer parse error: %s", exc)

    def _offer_to_product(self, offer: ET.Element, categories: dict[str, str]) -> Product:
        def t(tag: str) -> str:
            el = offer.find(tag)
            return (el.text or "").strip() if el is not None else ""

        sku = offer.get("id", "")
        name = t("name")
        url = t("url")
        price = clean_price(t("price"))
        old_price = clean_price(t("oldprice"))
        currency = t("currencyId") or "RUB"
        cat_id = t("categoryId")
        cat_path = categories.get(cat_id, "")
        brand = t("vendor") or t("brand")
        model = t("model") or name
        image_url = t("picture")
        available = offer.get("available", "true")
        availability = "в наличии" if available == "true" else "нет в наличии"
        series_status = "в наличии" if available == "true" else "нет в наличии"

        specs: dict = {}
        for param in offer.findall("param"):
            key = param.get("name", "")
            val = (param.text or "").strip()
            if key and val:
                specs[key] = val

        discount_pct = None
        if price and old_price and old_price > price:
            discount_pct = round((old_price - price) / old_price * 100, 1)

        return Product(
            site=self.site,
            brand=brand,
            name=name,
            model=model,
            sku=sku,
            price=price,
            old_price=old_price,
            discount_pct=discount_pct,
            currency=currency,
            availability=availability,
            series_status=series_status,
            specs=specs,
            category_path=cat_path,
            product_url=url,
            image_url=image_url,
        )

    def parse_product(self, url: str) -> Optional[Product]:
        resp = self.client.get(url)
        soup = BeautifulSoup(resp.content, "lxml")
        page_text = soup.get_text(" ", strip=True)

        name = (soup.select_one("h1") or soup.select_one(".product-title") or BeautifulSoup("<span/>", "lxml").span)
        name = name.get_text(strip=True) if name else ""

        price = self._get_price(soup, ".price, [itemprop='price'], .special-price")
        old_price = self._get_price(soup, ".price-old, .old-price")
        discount_pct = None
        if price and old_price and old_price > price:
            discount_pct = round((old_price - price) / old_price * 100, 1)

        brand = self._get_text(soup, "[itemprop='brand'], .manufacturer")
        model = self._get_text(soup, "[itemprop='model'], .model")
        sku = self._get_text(soup, "[itemprop='sku'], .sku")
        availability_el = soup.select_one(".availability, [itemprop='availability']")
        availability = availability_el.get_text(strip=True) if availability_el else ""
        series_status = detect_series_status(page_text)

        specs: dict = {}
        for row in soup.select(".product-specs tr, table.attribute tr"):
            cells = row.select("td")
            if len(cells) >= 2:
                specs[cells[0].get_text(strip=True)] = cells[1].get_text(strip=True)

        breadcrumb = soup.select(".breadcrumb li")
        cat_path = " > ".join(b.get_text(strip=True) for b in breadcrumb)
        img = soup.select_one("[itemprop='image'], .product-image img")
        image_url = ""
        if img:
            image_url = img.get("data-src") or img.get("src", "")
            if image_url and not image_url.startswith("http"):
                image_url = BASE + image_url

        return Product(
            site=self.site, brand=brand, name=name, model=model or name, sku=sku,
            price=price, old_price=old_price, discount_pct=discount_pct,
            availability=availability, series_status=series_status,
            specs=specs, category_path=cat_path, product_url=url, image_url=image_url,
        )

    def _get_text(self, soup: BeautifulSoup, selector: str) -> str:
        el = soup.select_one(selector)
        return el.get_text(strip=True) if el else ""

    def _get_price(self, soup: BeautifulSoup, selector: str) -> float | None:
        el = soup.select_one(selector)
        if not el:
            return None
        return clean_price(el.get("content") or el.get_text())
