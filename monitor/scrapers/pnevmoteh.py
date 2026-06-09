"""Scraper for pnevmoteh.ru.

Engine: Drupal Commerce.
Source: sitemap.xml (index → ?page=N sub-sitemaps). Products are flat root
slugs (e.g. /gvozdi-cnw-3170-ri); category pages share the namespace and are
skipped when no price is found.

Markup:
- price:        [itemprop=price] / .ui-price-price  (RUB)
- availability: .in-stock / commerce-add-to-cart form
- specs:        repeated <dl class="clearfix"> with one dt/dd each
- brand:        H1 text after "//"
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
    extract_model_from_name,
)
from ..sitemap import collect_product_urls

logger = logging.getLogger(__name__)

BASE = "https://www.pnevmoteh.ru"
SITEMAP = "https://www.pnevmoteh.ru/sitemap.xml"
# Products and categories both live at the root; keep all, drop sitemap links.
EXCLUDE = ["sitemap", "/user", "/cart", "/search", "/blog", "/news", "/about",
           "/contact", "/dostavka", "/oplata", "?"]
MAX_URLS = int(os.getenv("PNEVMOTEH_MAX", "0")) or None


class PnevmotehScraper(BaseScraper):
    site = "pnevmoteh.ru"
    base_url = BASE
    # Robust nginx/openresty host — can take a faster cadence
    delay_min = 0.4
    delay_max = 0.9

    def discover(self) -> list[str]:
        return ["__sitemap__"]

    def fetch_listing(self, url: str) -> list[str]:
        urls = collect_product_urls(
            self.client, SITEMAP, include=[], exclude=EXCLUDE, max_urls=MAX_URLS
        )
        # Drop the homepage and any URL that is clearly not a product slug
        urls = [u for u in urls if u.rstrip("/") != BASE.rstrip("/")]
        logger.info("[pnevmoteh] %d candidate URLs from sitemap", len(urls))
        return urls

    def parse_product(self, url: str) -> Optional[Product]:
        resp = self.client.get(url)
        soup = BeautifulSoup(resp.content, "lxml")

        page_text = soup.get_text(" ", strip=True)
        h1 = soup.select_one("h1")
        h1_text = h1.get_text(" ", strip=True) if h1 else ""

        # Price. If none is found, the page may still be a real product sold
        # "по запросу / под заказ" (Drupal renders .card__notprice /
        # .ui-price-total.under-order-price-total). Keep those — only treat as a
        # category page when there's no product structure at all.
        price = self._price(soup)
        on_request = bool(soup.select_one(
            ".card__notprice, .under-order-price-total, .ui-price-total"))
        if price is None:
            low = page_text.lower()
            if not (on_request or "цена по запросу" in low or "под заказ" in low):
                return None  # genuine category / non-product page
            if not h1_text:
                return None  # no product title — not a product

        # Brand: text after the last "//" in the H1
        brand = ""
        name = h1_text
        if "//" in h1_text:
            head, _, tail = h1_text.rpartition("//")
            brand = tail.strip()
            name = head.strip()

        specs = self._extract_specs(soup)
        model = extract_model_from_name(name, brand)

        # Availability
        if price is None and on_request:
            availability = "Цена по запросу"
        elif soup.select_one(".in-stock, .commerce-add-to-cart, form[class*='add-to-cart']"):
            availability = "В наличии"
        else:
            av_el = soup.select_one("[class*='stock'], [class*='nalichie']")
            availability = av_el.get_text(" ", strip=True) if av_el else ""
        series_status = status_from_availability(availability) or "в наличии"
        if has_discontinued_signal(page_text):
            series_status = "снято"

        old_price = self._price(soup, old=True)
        discount_pct = None
        if price and old_price and old_price > price:
            discount_pct = round((old_price - price) / old_price * 100, 1)

        image_url = self._image(soup)
        cat_path = " > ".join(
            i.get_text(strip=True) for i in soup.select(".breadcrumb a, .breadcrumbs a, nav.breadcrumb li")
            if i.get_text(strip=True)
        )

        return Product(
            site=self.site, brand=brand, name=name, model=model,
            price=price, old_price=old_price, discount_pct=discount_pct,
            currency="RUB", availability=availability, series_status=series_status,
            specs=specs, category_path=cat_path, product_url=url, image_url=image_url,
        )

    # ------------------------------------------------------------------

    def _price(self, soup: BeautifulSoup, old: bool = False) -> float | None:
        if old:
            el = soup.select_one(".ui-price-old, .price-old, [class*='old-price'], .pr__card-price-old")
            return clean_price(el.get_text()) if el else None
        # itemprop first, then various class-based selectors used across page types
        for sel in ("[itemprop='price']", ".ui-price-price", ".pr__card-price"):
            el = soup.select_one(sel)
            if el:
                val = el.get("content") or el.get_text()
                p = clean_price(val)
                if p and p > 100:   # ignore stray tiny numbers
                    return p
        return None

    def _extract_specs(self, soup: BeautifulSoup) -> dict:
        specs: dict = {}
        # Repeated <dl class="clearfix"> with one dt + one dd each
        for dl in soup.select("dl.clearfix, dl"):
            for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
                k = dt.get_text(" ", strip=True).rstrip(":").strip()
                v = dd.get_text(" ", strip=True)
                if k and v:
                    specs[k] = v
        return specs

    def _image(self, soup: BeautifulSoup) -> str:
        img = soup.select_one("[itemprop='image'], .product-image img, .card__image img")
        if img:
            src = img.get("data-src") or img.get("src", "")
            if src:
                return src if src.startswith("http") else BASE + src
        return ""
