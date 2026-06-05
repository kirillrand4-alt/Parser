"""Scraper for compressortyt.ru.

Source: HTML listing + product pages.
URL pattern: /stanciya/kompr/<category>/<brand>/<model>/
Prices contain U+200B (zero-width space) and NBSP — stripped by clean_price().
Images are lazy-loaded with placeholder ll.png — read data-src attribute.
"""
from __future__ import annotations

import logging
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Optional

from bs4 import BeautifulSoup

from ..base_scraper import BaseScraper
from ..models import (
    Product, clean_price, detect_series_status,
    extract_model_from_name, parse_spec_table,
)

logger = logging.getLogger(__name__)

BASE = "https://compressortyt.ru"
YML_URL = "https://compressortyt.ru/yml/"

# Enrich each product with full specs from its HTML page.
# Costs ~17k extra requests; off by default. Enable via COMPRESSORTYT_ENRICH=1.
ENRICH_SPECS = os.getenv("COMPRESSORTYT_ENRICH", "0") == "1"
# Cap offers parsed from the feed (0 = all). Useful for test runs.
MAX_OFFERS = int(os.getenv("COMPRESSORTYT_MAX", "0")) or None

# Leaf category slugs to crawl (avoids the huge root /stanciya/kompr/ page)
CATEGORIES = [
    "/stanciya/kompr/vintovye/",
    "/stanciya/kompr/porshnevye/",
    "/stanciya/kompr/spiralnye/",
    "/stanciya/kompr/bezmaslyany/",
    "/stanciya/kompr/bezmaloye/",          # low-oil
    "/stanciya/kompr/s-remennoy-peredachey/",
    "/stanciya/kompr/s-pryamym-privodom/",
    "/stanciya/kompr/dvukhstupenchatye/",
    "/stanciya/kompr/mobilnye/",
    "/stanciya/kompr/dizelnyy/",
]

_IMG_PLACEHOLDER = "ll.png"


class CompressortytScraper(BaseScraper):
    site = "compressortyt.ru"
    base_url = BASE
    # Robust nginx host — faster cadence (matters in enrich mode, ~15k pages)
    delay_min = 0.4
    delay_max = 0.9

    def discover(self) -> list[str]:
        return [BASE + cat for cat in CATEGORIES]

    # ------------------------------------------------------------------
    # YML feed (primary source — full catalog with prices in one request)
    # ------------------------------------------------------------------

    def scrape(self, position: int = 0):  # type: ignore[override]
        """Parse the YML feed; optionally enrich each product with HTML specs."""
        try:
            from tqdm.auto import tqdm
        except Exception:
            def tqdm(iterable=None, **_):
                return iterable if iterable is not None else iter(())

        resp = self.client.get(YML_URL, timeout=60)
        root = ET.fromstring(resp.content)
        shop = root.find("shop")
        if shop is None:
            logger.warning("[compressortyt] no <shop> in feed, falling back to HTML")
            yield from super().scrape(position=position)
            return

        # Build category id → name and parent map for full path resolution
        cat_name: dict[str, str] = {}
        cat_parent: dict[str, str] = {}
        for c in shop.findall(".//category"):
            cid = c.get("id", "")
            cat_name[cid] = (c.text or "").strip()
            if c.get("parentId"):
                cat_parent[cid] = c.get("parentId")

        def cat_path(cid: str) -> str:
            parts, seen = [], set()
            while cid and cid in cat_name and cid not in seen:
                seen.add(cid)
                parts.append(cat_name[cid])
                cid = cat_parent.get(cid, "")
            return " > ".join(reversed(parts))

        offers = shop.findall(".//offer")
        if os.getenv("SHUFFLE", "").strip() in ("1", "true", "yes"):
            import random
            random.shuffle(offers)
        if MAX_OFFERS:
            offers = offers[:MAX_OFFERS]
        logger.info("[compressortyt] %d offers in feed", len(offers))

        # Resume support: like the base scraper, RESUME=1 skips offers already
        # parsed in a previous run (tracked by product URL in the checkpoint) so
        # this fast feed-based site doesn't re-do its whole catalog every run.
        resume = os.getenv("RESUME", "").strip().lower() in ("1", "true", "yes")
        done_urls: set[str] = set(self._checkpoint.get("done_urls", [])) if resume else set()
        failed_urls: set[str] = set(self._checkpoint.get("failed_urls", [])) if resume else set()

        def offer_url(o) -> str:
            el = o.find("url")
            return (el.text or "").strip() if el is not None else ""

        pending = [o for o in offers if offer_url(o) not in done_urls]
        if resume and len(pending) < len(offers):
            logger.info("[compressortyt] resume: skipping %d already-done offers",
                        len(offers) - len(pending))

        errors = 0
        i = 0
        bar = tqdm(total=len(pending), desc=f"{'compressortyt.ru':<20}", position=position,
                   unit="prod", leave=True, dynamic_ncols=True)
        try:
            for offer in pending:
                url = offer_url(offer)
                try:
                    product = self._offer_to_product(offer, cat_path)
                except Exception as exc:
                    logger.debug("[compressortyt] offer error: %s", exc)
                    errors += 1
                    if url:
                        failed_urls.add(url)
                    bar.update(1)
                    bar.set_postfix(err=errors, refresh=False)
                    continue
                if ENRICH_SPECS and product.product_url:
                    try:
                        self._enrich(product)
                    except Exception as exc:
                        logger.debug("[compressortyt] enrich error %s: %s", product.product_url, exc)
                if url:
                    done_urls.add(url)
                    failed_urls.discard(url)
                bar.update(1)
                bar.set_postfix(err=errors, refresh=False)
                i += 1
                if i % 100 == 0:
                    self._save_progress(done_urls, failed_urls)
                yield product
        finally:
            bar.close()
            self._save_progress(done_urls, failed_urls)

    def _offer_to_product(self, offer: ET.Element, cat_path) -> Product:
        def t(tag: str) -> str:
            el = offer.find(tag)
            return (el.text or "").strip() if el is not None else ""

        available = offer.get("available", "true") == "true"
        currency = t("currencyId").replace("RUR", "RUB") or "RUB"
        name = t("name")
        brand = t("vendor")
        return Product(
            site=self.site,
            brand=brand,
            name=name,
            model=extract_model_from_name(name, brand),
            sku=offer.get("id", ""),
            price=clean_price(t("price")),
            old_price=clean_price(t("oldprice")),
            currency=currency,
            availability="в наличии" if available else "нет в наличии",
            series_status="в наличии" if available else "нет в наличии",
            specs={},
            category_path=cat_path(t("categoryId")),
            product_url=t("url"),
            image_url=t("picture"),
        )

    def _enrich(self, product: Product) -> None:
        """Fetch product page and fill specs + refine status."""
        resp = self.client.get(product.product_url)
        soup = BeautifulSoup(resp.content, "lxml")
        product.specs = self._extract_specs(soup)
        page_text = soup.get_text(" ", strip=True)
        status = detect_series_status(page_text)
        if status != "неизвестно":
            product.series_status = status

    def fetch_listing(self, url: str) -> list[str]:
        """Collect all product URLs from a category, following pagination."""
        product_urls: list[str] = []
        page = 1
        while True:
            paged_url = url if page == 1 else f"{url}?page={page}"
            try:
                resp = self.client.get(paged_url)
            except Exception as exc:
                logger.warning("[compressortyt] listing page error %s: %s", paged_url, exc)
                break

            soup = BeautifulSoup(resp.content, "lxml")
            cards = soup.select("div.catalog-item, article.product-card, div.product-item")
            if not cards:
                # fallback: any link matching product URL pattern
                cards = soup.select("a[href*='/stanciya/kompr/']")

            found_on_page = 0
            for card in cards:
                link = card if card.name == "a" else card.select_one("a[href*='/stanciya/kompr/']")
                if not link:
                    continue
                href = link.get("href", "")
                # Only leaf product URLs (4+ path segments)
                parts = [p for p in href.split("/") if p]
                if len(parts) >= 4:
                    full = href if href.startswith("http") else BASE + href
                    product_urls.append(full)
                    found_on_page += 1

            logger.debug("[compressortyt] page %d of %s → %d products", page, url, found_on_page)

            # Check for next page
            next_link = soup.select_one("a.next, a[rel='next'], .pagination a:last-child")
            if not next_link or found_on_page == 0:
                break
            page += 1

        return list(dict.fromkeys(product_urls))  # deduplicate preserving order

    def parse_product(self, url: str) -> Optional[Product]:
        resp = self.client.get(url)
        soup = BeautifulSoup(resp.content, "lxml")
        page_text = soup.get_text(" ", strip=True)

        # --- Name / brand / model ---
        name = ""
        h1 = soup.select_one("h1")
        if h1:
            name = h1.get_text(strip=True)

        brand = self._extract_brand(soup, url)
        model = self._extract_model(soup, name, url)

        # --- Price ---
        price = self._extract_price(soup, "price", "current-price", "product__price")
        old_price = self._extract_price(soup, "old-price", "price-old", "crossed")
        discount_pct = self._calc_discount(price, old_price)

        # --- Availability ---
        availability, series_status = self._extract_availability(soup, page_text)

        # --- Replacement model ---
        replacement_model = self._extract_replacement(soup)

        # --- Specs ---
        specs = self._extract_specs(soup)

        # --- Category path ---
        breadcrumb = soup.select("nav.breadcrumb li, .breadcrumbs span, ol.breadcrumb li")
        category_path = " > ".join(
            b.get_text(strip=True) for b in breadcrumb if b.get_text(strip=True)
        )

        # --- Image ---
        image_url = self._extract_image(soup)

        # Series (2nd path segment from end of URL after brand)
        series = self._extract_series_from_url(url)

        return Product(
            site=self.site,
            brand=brand,
            series=series,
            name=name,
            model=model,
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
    # Helpers
    # ------------------------------------------------------------------

    def _extract_price(self, soup: BeautifulSoup, *css_hints: str) -> float | None:
        for hint in css_hints:
            el = soup.select_one(f"[class*='{hint}']")
            if el:
                return clean_price(el.get_text())
        # Generic fallback: itemprop="price"
        el = soup.select_one("[itemprop='price']")
        if el:
            val = el.get("content") or el.get_text()
            return clean_price(val)
        return None

    def _calc_discount(self, price: float | None, old_price: float | None) -> float | None:
        if price and old_price and old_price > price:
            return round((old_price - price) / old_price * 100, 1)
        return None

    def _extract_brand(self, soup: BeautifulSoup, url: str) -> str:
        # Try schema.org
        el = soup.select_one("[itemprop='brand'] [itemprop='name'], [itemprop='brand']")
        if el:
            return el.get("content") or el.get_text(strip=True)
        # Try URL: /stanciya/kompr/<cat>/<brand>/<model>/
        parts = urllib.parse.urlparse(url).path.strip("/").split("/")
        if len(parts) >= 4:
            return parts[-2].replace("-", " ").title()
        return ""

    def _extract_model(self, soup: BeautifulSoup, name: str, url: str) -> str:
        el = soup.select_one("[itemprop='model'], [class*='model'], [class*='article']")
        if el:
            return el.get_text(strip=True)
        # Last URL segment
        parts = urllib.parse.urlparse(url).path.strip("/").split("/")
        if parts:
            return parts[-1].replace("-", " ").upper()
        return name

    def _extract_availability(self, soup: BeautifulSoup, page_text: str) -> tuple[str, str]:
        availability_el = soup.select_one(
            "[itemprop='availability'], [class*='availability'], [class*='stock'], "
            "[class*='nalichie'], [class*='v-nalichii']"
        )
        availability_raw = availability_el.get_text(strip=True) if availability_el else ""
        series_status = detect_series_status(page_text)

        if series_status == "неизвестно":
            # Infer from availability text
            lower = availability_raw.lower()
            if any(k in lower for k in ("в наличии", "есть", "на складе")):
                series_status = "в наличии"
            elif availability_raw:
                series_status = detect_series_status(availability_raw)

        availability = availability_raw or series_status
        return availability, series_status

    def _extract_replacement(self, soup: BeautifulSoup) -> str:
        for el in soup.select("a, p, span"):
            text = el.get_text(strip=True)
            if any(k in text.lower() for k in ("замена", "аналог", "заменён", "заменяет", "replacement")):
                # Try to find a model code nearby
                match = re.search(r"[A-ZА-Я0-9][\w\-]{3,}", text)
                if match:
                    return match.group(0)
        return ""

    def _extract_specs(self, soup: BeautifulSoup) -> dict:
        # Method 1: specification table rows (skip multi-variant matrices)
        rows = soup.select(
            "table.specs tr, table.characteristics tr, .product-specs tr, .specification tr")
        specs, is_matrix = parse_spec_table(rows)
        if is_matrix:
            specs = {}

        # Method 2: dl/dt/dd pairs
        if not specs:
            for dl in soup.select("dl.specs, dl.characteristics, dl.properties"):
                dts = dl.select("dt")
                dds = dl.select("dd")
                for dt, dd in zip(dts, dds):
                    specs[dt.get_text(strip=True)] = dd.get_text(strip=True)

        # Method 3: itemprop attributes
        for el in soup.select("[itemprop]"):
            prop = el.get("itemprop", "")
            if prop and prop not in ("name", "description", "image", "url", "price", "priceCurrency", "availability", "brand"):
                val = el.get("content") or el.get_text(strip=True)
                if val:
                    specs[prop] = val

        return specs

    def _extract_image(self, soup: BeautifulSoup) -> str:
        # Lazy-loaded images use data-src; ignore placeholder ll.png
        for img in soup.select("[itemprop='image'], .product-gallery img, .product__image img, img.main-image"):
            src = img.get("data-src") or img.get("data-lazy") or img.get("src", "")
            if src and _IMG_PLACEHOLDER not in src:
                return src if src.startswith("http") else BASE + src
        return ""

    def _extract_series_from_url(self, url: str) -> str:
        parts = urllib.parse.urlparse(url).path.strip("/").split("/")
        # /stanciya/kompr/<category>/<brand>/<model>/ → category is series hint
        if len(parts) >= 3:
            return parts[2].replace("-", " ").title()
        return ""
