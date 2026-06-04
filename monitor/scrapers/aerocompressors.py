"""Scraper for aerocompressors.ru.

Specialized compressor retailer — expects rich technical specs.
Source strategy: probe YML/sitemap first, then HTML.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Optional

from bs4 import BeautifulSoup

from ..base_scraper import BaseScraper
from ..models import Product, clean_price, detect_series_status

logger = logging.getLogger(__name__)

BASE = "https://aerocompressors.ru"

YML_PATHS = ["/yml/", "/export.yml", "/yandex-market.xml", "/upload/yandex.xml"]

HTML_CATEGORIES = [
    "/catalog/kompressory/",
    "/catalog/kompressory/vintovye-kompressory/",
    "/catalog/kompressory/porshnevye-kompressory/",
    "/catalog/kompressory/bezmaslyane-kompressory/",
    "/catalog/pnevmooborudovanie/",
]


class AerocompressorsScraper(BaseScraper):
    site = "aerocompressors.ru"
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
                    logger.info("[aerocompressors] YML feed: %s", self._yml_url)
                    return ["__yml__"]
            except Exception:
                continue
        return [BASE + cat for cat in HTML_CATEGORIES]

    def scrape(self):  # type: ignore[override]
        urls = self.discover()
        if urls == ["__yml__"]:
            yield from self._parse_yml_feed()
        else:
            yield from super().scrape()

    def _parse_yml_feed(self):
        resp = self.client.get(self._yml_url)
        root = ET.fromstring(resp.content)
        shop = root.find("shop")
        if shop is None:
            return
        categories: dict[str, str] = {}
        for cat in shop.findall(".//category"):
            categories[cat.get("id", "")] = cat.text or ""
        for offer in shop.findall(".//offer"):
            try:
                yield self._offer_to_product(offer, categories)
            except Exception as exc:
                logger.debug("[aerocompressors] offer error: %s", exc)

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
        cat_path = categories.get(t("categoryId"), "")
        brand = t("vendor")
        model = t("model") or name
        image_url = t("picture")
        available = offer.get("available", "true") == "true"
        availability = "в наличии" if available else "нет в наличии"
        series_status = availability

        specs: dict = {}
        for param in offer.findall("param"):
            k = param.get("name", "")
            v = (param.text or "").strip()
            if k and v:
                specs[k] = v
        # Also pull description fields if labelled
        description = t("description")
        if description:
            specs["_description"] = description

        discount_pct = None
        if price and old_price and old_price > price:
            discount_pct = round((old_price - price) / old_price * 100, 1)

        return Product(
            site=self.site, brand=brand, name=name, model=model, sku=sku,
            price=price, old_price=old_price, discount_pct=discount_pct,
            currency=currency, availability=availability, series_status=series_status,
            specs=specs, category_path=cat_path, product_url=url, image_url=image_url,
        )

    def fetch_listing(self, url: str) -> list[str]:
        product_urls: list[str] = []
        page = 1
        while True:
            paged = url if page == 1 else f"{url}?PAGEN_1={page}"
            try:
                resp = self.client.get(paged)
            except Exception as exc:
                logger.warning("[aerocompressors] listing error %s: %s", paged, exc)
                break
            soup = BeautifulSoup(resp.content, "lxml")
            links = soup.select(
                ".catalog-item a, .product-item a[href*='/catalog/'], "
                "a.product-name[href], .catalog-item-title a"
            )
            found = 0
            for a in links:
                href = a.get("href", "")
                if href and "/catalog/" in href and href.count("/") >= 4:
                    product_urls.append(href if href.startswith("http") else BASE + href)
                    found += 1
            if found == 0:
                break
            if not soup.select_one(f"a[href*='PAGEN_1={page + 1}'], a[href*='page={page + 1}']"):
                break
            page += 1
        return list(dict.fromkeys(product_urls))

    def parse_product(self, url: str) -> Optional[Product]:
        resp = self.client.get(url)
        soup = BeautifulSoup(resp.content, "lxml")
        page_text = soup.get_text(" ", strip=True)

        name = self._text(soup, "h1") or ""
        brand = self._text(soup, "[itemprop='brand'], .vendor, .manufacturer")
        model = self._text(soup, "[itemprop='model'], .model-name") or name
        sku = self._text(soup, "[itemprop='sku'], .article, .product-article")

        price = self._price(soup, "[itemprop='price'], .price, .cost")
        old_price = self._price(soup, ".price-old, .old-price")
        discount_pct = None
        if price and old_price and old_price > price:
            discount_pct = round((old_price - price) / old_price * 100, 1)

        avail_el = soup.select_one("[itemprop='availability'], .availability, .in-stock")
        availability = avail_el.get_text(strip=True) if avail_el else ""
        series_status = detect_series_status(page_text)
        if series_status == "неизвестно" and "в наличии" in availability.lower():
            series_status = "в наличии"

        specs = self._extract_specs(soup)
        cat_path = " > ".join(
            i.get_text(strip=True) for i in soup.select(".breadcrumb li, .breadcrumbs span")
            if i.get_text(strip=True)
        )
        image_url = self._image(soup)
        replacement_model = self._get_replacement(soup)

        return Product(
            site=self.site, brand=brand, name=name, model=model, sku=sku,
            price=price, old_price=old_price, discount_pct=discount_pct,
            availability=availability, series_status=series_status,
            replacement_model=replacement_model, specs=specs,
            category_path=cat_path, product_url=url, image_url=image_url,
        )

    def _text(self, soup: BeautifulSoup, sel: str) -> str:
        el = soup.select_one(sel)
        return el.get_text(strip=True) if el else ""

    def _price(self, soup: BeautifulSoup, sel: str) -> float | None:
        el = soup.select_one(sel)
        if not el:
            return None
        return clean_price(el.get("content") or el.get_text())

    def _extract_specs(self, soup: BeautifulSoup) -> dict:
        specs: dict = {}
        for row in soup.select("table.chars tr, .product-specs tr, .characteristics tr"):
            cells = row.select("td, th")
            if len(cells) >= 2:
                k = cells[0].get_text(strip=True)
                v = cells[-1].get_text(strip=True)
                if k and v:
                    specs[k] = v
        if not specs:
            for dl in soup.select("dl.specs, dl.properties"):
                for dt, dd in zip(dl.select("dt"), dl.select("dd")):
                    specs[dt.get_text(strip=True)] = dd.get_text(strip=True)
        return specs

    def _image(self, soup: BeautifulSoup) -> str:
        img = soup.select_one("[itemprop='image'], .product-photo img, .detail-picture img")
        if img:
            src = img.get("data-src") or img.get("src", "")
            if src:
                return src if src.startswith("http") else BASE + src
        return ""

    def _get_replacement(self, soup: BeautifulSoup) -> str:
        for el in soup.select("p, .note, .warning"):
            text = el.get_text(strip=True)
            if any(k in text.lower() for k in ("аналог", "замена", "заменяет")):
                m = re.search(r"[A-ZА-Я][\w\-]{3,}", text)
                if m:
                    return m.group(0)
        return ""
