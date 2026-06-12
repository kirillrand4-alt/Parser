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

import json
import logging
import os
from typing import Optional

from bs4 import BeautifulSoup

from ..base_scraper import BaseScraper
from ..models import (
    Product, clean_price, has_discontinued_signal, status_from_availability,
    cell_value, extract_brand_from_name, extract_model_from_name, harvest_specs,
)

logger = logging.getLogger(__name__)

BASE = "https://www.v-p-k.ru"
MAX_URLS = int(os.getenv("VPK_MAX", "0")) or None

# v-p-k product slugs are model names ("/product/easy-air/"), so a sitemap slug
# filter cannot find compressors. Instead we walk the catalog category, which
# lists exactly the compressors the site counts (~10.5k over ~331 pages).
CATEGORY = "https://www.v-p-k.ru/catalog/kompressory/"
MAX_PAGES = 400  # safety cap; real catalog is ~331 pages

_BRAND_KEYS = ("Бренд", "Производитель", "Марка", "Торговая марка")


class VpkScraper(BaseScraper):
    site = "v-p-k.ru"
    base_url = BASE
    # Robust nginx/openresty host — can take a faster cadence
    delay_min = 0.4
    delay_max = 0.9

    def discover(self) -> list[str]:
        return ["__category__"]

    def fetch_listing(self, url: str) -> list[str]:
        # The catalog walk is ~331 single-threaded pages (5-7 min). If a session
        # dies mid-walk it used to restart from page 1 every time. We persist the
        # accumulated URLs and last completed page into the checkpoint every few
        # pages, and resume from there. Partial state lives under dedicated keys
        # (vpk_partial_*) so the base scraper's product_urls cache (written only
        # on a *complete* walk) is never fed a half-built list.
        resume = os.getenv("RESUME", "").strip().lower() in ("1", "true", "yes")
        urls: list[str] = []
        start_page = 1
        if resume:
            saved = self._checkpoint.get("vpk_partial_urls") or []
            last = int(self._checkpoint.get("vpk_partial_last_page") or 0)
            if saved and last:
                urls = list(saved)
                start_page = last + 1
                logger.info("[v-p-k] resume pagination: %d URLs, continuing from page %d",
                            len(urls), start_page)
        seen: set[str] = set(urls)

        # Per-page timeout: a slow/hung catalog page shouldn't eat 40s × retries.
        # PAGEN timeouts on a single page are not fatal — log and move on rather
        # than aborting the whole walk (a transient hiccup used to kill it).
        page_timeout = int(os.getenv("VPK_PAGE_TIMEOUT", "15"))
        persist_every = int(os.getenv("VPK_PERSIST_EVERY", "10"))
        consecutive_fail = 0
        for page in range(start_page, MAX_PAGES + 1):
            page_url = CATEGORY if page == 1 else f"{CATEGORY}?PAGEN_1={page}"
            try:
                resp = self.client.get(page_url, timeout=page_timeout)
            except Exception as exc:
                consecutive_fail += 1
                logger.warning("[v-p-k] page %d fetch error (%d in a row): %s",
                               page, consecutive_fail, exc)
                # Give up only after several pages fail back-to-back; a lone slow
                # page shouldn't truncate the catalog. Save progress first.
                self._persist_pages(page - 1, urls)
                if consecutive_fail >= 3:
                    logger.error("[v-p-k] %d pages failed in a row — stopping walk "
                                 "at page %d (resume with RESUME=1)", consecutive_fail, page)
                    break
                continue
            consecutive_fail = 0
            soup = BeautifulSoup(resp.content, "lxml")
            new = 0
            for a in soup.select("a[href*='/product/']"):
                href = (a.get("href") or "").split("?")[0]
                if not href:
                    continue
                full = href if href.startswith("http") else BASE + href
                if full not in seen:
                    seen.add(full)
                    urls.append(full)
                    new += 1
            # Bitrix serves the last page's content for out-of-range pages, so a
            # page that adds no new product link means we've reached the end.
            if new == 0:
                break
            if page % persist_every == 0:
                logger.info("[v-p-k] catalog page %d, %d URLs so far", page, len(urls))
                self._persist_pages(page, urls)
        # Walk finished — drop the partial-progress keys so a later run doesn't
        # try to resume a completed pagination.
        self._checkpoint.pop("vpk_partial_urls", None)
        self._checkpoint.pop("vpk_partial_last_page", None)
        if os.getenv("SHUFFLE", "").strip() in ("1", "true", "yes"):
            import random
            random.shuffle(urls)
        if MAX_URLS:
            urls = urls[:MAX_URLS]
        logger.info("[v-p-k] %d product URLs from catalog pagination", len(urls))
        return urls

    def _persist_pages(self, page: int, urls: list[str]) -> None:
        """Atomically save mid-walk pagination progress to the checkpoint."""
        self._checkpoint["vpk_partial_urls"] = urls
        self._checkpoint["vpk_partial_last_page"] = page
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._checkpoint_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._checkpoint, ensure_ascii=False))
        tmp.replace(self._checkpoint_path)

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
        availability = av.get_text(" ", strip=True) if av else (
            "В наличии" if price else "цена по запросу")
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
                v = cell_value(val_el)
                if not k or not v:
                    continue
                # "Давление" can appear twice: once as a numeric value ("10 бар")
                # and once as a category label ("Среднего давления"). Split them
                # into separate keys so the number is never overwritten by text.
                if k == "Давление":
                    if any(ch.isdigit() for ch in v):
                        k = "Давление, бар"
                    else:
                        k = "Тип давления"
                if k not in specs:
                    specs[k] = v
        # If the main field carried only the category text, the number usually
        # lives in a sibling field — promote it so pressure is never lost.
        if "Давление, бар" not in specs:
            for src in ("Давление от-до", "Класс давления"):
                v = specs.get(src, "")
                if any(ch.isdigit() for ch in v):
                    specs["Давление, бар"] = v
                    break
        if len(specs) < 3:
            for k, v in harvest_specs(soup).items():
                specs.setdefault(k, v)
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
