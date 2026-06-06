"""Scraper for pnevmo-sklad.ru.

Engine: 1С-Битрикс.
Source: HTML listing (YML feed not publicly available; check /yml/ on first run).
Pagination: ?PAGEN_1=N  (standard Bitrix).

Proxy strategy (economical):
  - Requests go direct by default (fast, no proxy traffic cost).
  - On CONSECUTIVE_500_THRESHOLD consecutive HTTP 500s → switch to proxy.
  - After PROXY_RECOVER_AFTER successful requests via proxy → try direct again.
  - Proxy creds read from env PNEVMO_SKLAD_PROXY (socks5://user:pass@host:port).
  - IP refresh URL read from env PNEVMO_SKLAD_PROXY_REFRESH (GET request).
"""
from __future__ import annotations

import logging
import os
import re
import time
import urllib.parse
from typing import Optional

import requests
from bs4 import BeautifulSoup

from ..base_scraper import BaseScraper
from ..models import (
    Product, clean_price, detect_series_status,
    has_discontinued_signal, status_from_availability,
    extract_model_from_name, parse_spec_table,
)
from ..sitemap import collect_product_urls

logger = logging.getLogger(__name__)

# Switch to proxy after this many consecutive 500s
_CONSECUTIVE_500_THRESHOLD = int(os.getenv("PNEVMO_SKLAD_500_THRESHOLD", "3"))
# After this many successful proxy requests, try going direct again
_PROXY_RECOVER_AFTER = int(os.getenv("PNEVMO_SKLAD_PROXY_RECOVER", "50"))

BASE = "https://www.pnevmo-sklad.ru"
SITEMAP = "https://www.pnevmo-sklad.ru/sitemap.xml"

# Products live under /shop/<category>/.../<slug>. The flat sitemap lists 91k
# URLs, but ~66k are screw-compressor spare parts (zapchasti_*) and many are
# regional-subdomain duplicates (rostov./etc.). Keep only the compressor
# equipment subcategories under /shop/oborudovanie/, collapsing every regional
# host to www so duplicates merge. This matches the site's own "Компрессоры"
# count (~18.7k) instead of scraping spare parts and city mirrors.
INCLUDE = ["/shop/"]
EXCLUDE = ["/sitemap"]
MAX_URLS = int(os.getenv("PNEVMO_SKLAD_MAX", "0")) or None
CANON_HOST = "https://www.pnevmo-sklad.ru"

# Compressor-equipment subcategories under /shop/oborudovanie/ (air compressors,
# air preparation, receivers, tools). Spare parts and off-topic gear (light
# masts, nitrogen generators, sand-blasting) are excluded by omission.
COMPRESSOR_SUBCATS = {
    "vintovye_kompressory", "porshnevye_kompressory", "peredvizhnye_kompressory",
    "spiralnye_kompressory", "czentrobezhnyie_kompressoryi",
    "modulnyie_kompressornyie_stanczii", "osushiteli_vozduha",
    "resivery_dlya_kompressorov_vozduhosborniki", "magistralnye_filtry",
    "ciklonnye_separatory", "kondensatootvodchiki", "ochistka_kondensata",
    "konczevie_ohladiteli", "chillery", "pnevmoinstrument",
    "bu_oborudovanie", "snyatyie_s_proizvodstva",
}


class PnevmoSkladScraper(BaseScraper):
    site = "pnevmo-sklad.ru"
    base_url = BASE
    delay_min = 0.3
    delay_max = 0.5

    def __init__(self) -> None:
        super().__init__()
        self._proxy_url = os.getenv("PNEVMO_SKLAD_PROXY", "")
        self._proxy_refresh = os.getenv("PNEVMO_SKLAD_PROXY_REFRESH", "")
        self._using_proxy = False
        self._consecutive_500 = 0
        self._proxy_success = 0

    def _enable_proxy(self) -> None:
        if not self._proxy_url or self._using_proxy:
            return
        # Refresh IP before switching (avoid reusing a burned IP)
        if self._proxy_refresh:
            try:
                requests.get(self._proxy_refresh, timeout=10)
                time.sleep(3)  # give provider a moment to rotate
                logger.info("[pnevmo-sklad] proxy IP refreshed")
            except Exception as exc:
                logger.warning("[pnevmo-sklad] proxy IP refresh failed: %s", exc)
        proxies = {"http": self._proxy_url, "https": self._proxy_url}
        self.client._session.proxies.update(proxies)
        self._using_proxy = True
        self._proxy_success = 0
        logger.info("[pnevmo-sklad] switched to PROXY after %d consecutive 500s",
                    self._consecutive_500)

    def _disable_proxy(self) -> None:
        if not self._using_proxy:
            return
        self.client._session.proxies.clear()
        self._using_proxy = False
        self._consecutive_500 = 0
        logger.info("[pnevmo-sklad] back to DIRECT after %d proxy successes",
                    _PROXY_RECOVER_AFTER)

    def _get_with_proxy_fallback(self, url: str) -> requests.Response:
        """GET with automatic proxy switching on consecutive 500s."""
        try:
            resp = self.client.get(url)
            # Success — track proxy recovery
            self._consecutive_500 = 0
            if self._using_proxy:
                self._proxy_success += 1
                if self._proxy_success >= _PROXY_RECOVER_AFTER:
                    self._disable_proxy()
            return resp
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == 500:
                self._consecutive_500 += 1
                if (not self._using_proxy
                        and self._proxy_url
                        and self._consecutive_500 >= _CONSECUTIVE_500_THRESHOLD):
                    self._enable_proxy()
                    # Retry once via proxy
                    return self.client.get(url)
            raise

    def discover(self) -> list[str]:
        # Single sentinel; product URLs come from the sitemap in fetch_listing.
        return ["__sitemap__"]

    def fetch_listing(self, url: str) -> list[str]:
        raw = collect_product_urls(
            self.client, SITEMAP, include=INCLUDE, exclude=EXCLUDE, max_urls=None
        )
        seen: set[str] = set()
        urls: list[str] = []
        for u in raw:
            path = urllib.parse.urlparse(u).path
            parts = path.strip("/").split("/")
            # Expect /shop/oborudovanie/<subcat>/.../<slug> (>= 4 segments)
            if len(parts) < 4 or parts[0] != "shop" or parts[1] != "oborudovanie":
                continue
            if parts[2] not in COMPRESSOR_SUBCATS:
                continue
            canon = CANON_HOST + path  # collapse regional hosts → www
            if canon not in seen:
                seen.add(canon)
                urls.append(canon)
        if os.getenv("SHUFFLE", "").strip() in ("1", "true", "yes"):
            import random
            random.shuffle(urls)
        if MAX_URLS:
            urls = urls[:MAX_URLS]
        logger.info(
            "[pnevmo-sklad] %d compressor URLs (from %d sitemap entries)",
            len(urls), len(raw),
        )
        return urls

    def parse_product(self, url: str) -> Optional[Product]:
        resp = self._get_with_proxy_fallback(url)
        soup = BeautifulSoup(resp.content, "lxml")
        page_text = soup.get_text(" ", strip=True)

        name = ""
        h1 = soup.select_one("h1")
        if h1:
            name = h1.get_text(strip=True)

        # Characteristics table: <tr> with "Label:" | "Value"
        specs, is_matrix = self._extract_specs(soup)
        if is_matrix:
            return None  # multi-variant series page — skip

        # Brand and article come straight from the chars table
        brand = specs.get("Бренд", "") or specs.get("Производитель", "")
        sku = specs.get("Артикул", "")
        model = extract_model_from_name(name, brand)

        # Price: .pricebox__price holds the number, or "Цена по запросу" → None
        price = self._get_price(soup, ".pricebox__price, .prodsticky__price")
        old_price = self._get_price(
            soup, ".pricebox__oldprice, .hprod__oldprice, .price-old")
        # Guard: .hprod__oldprice can pick up an unrelated (lower) number; a real
        # strike-through old price is always higher than the current price.
        if old_price and price and old_price <= price:
            old_price = None
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

    def _extract_specs(self, soup: BeautifulSoup) -> tuple[dict, bool]:
        # Full characteristics table is .charstable (also .prodbig__chars-table teaser)
        rows = soup.select("table.charstable tr, .prodbig__chars-table tr")
        return parse_spec_table(rows)

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
