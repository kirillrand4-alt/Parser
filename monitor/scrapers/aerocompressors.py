"""Scraper for aerocompressors.ru.

Behind ddos-guard (serves normally with a realistic UA + polite delays).
Source: sitemap.xml (flat, ~37k URLs) → products under /katalog_produkcii/.
Category pages share the namespace and are skipped when no price is found.

Markup:
- price:        [itemprop=price] / .price
- specs:        table.tech rows (label | value)
- brand:        /katalog_po_brendam/ link, then known-brand match on name
- availability: no explicit block; price present ⇒ "в наличии"
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from bs4 import BeautifulSoup

from ..base_scraper import BaseScraper
from ..models import (
    Product, clean_price, has_discontinued_signal, extract_brand_from_name,
    extract_model_from_name, parse_spec_table,
)
from ..sitemap import collect_product_urls

logger = logging.getLogger(__name__)

BASE = "https://aerocompressors.ru"
SITEMAP = "https://aerocompressors.ru/sitemap.xml"
INCLUDE = ["/katalog_produkcii/"]
EXCLUDE = ["/sitemap"]
MAX_URLS = int(os.getenv("AEROCOMPRESSORS_MAX", "0")) or None

# Only scrape compressor-related categories; skip generators, construction
# equipment, welding, sand-blasting, heat guns, and metalworking.
RELEVANT_CATEGORIES = {
    "kompressori",
    "podgotovka-szhatogo-vozduha",
    "pnevmoinstrument",
    "resivers-vozduhozaborniki",
}

_BRAND_KEYS = ("Бренд", "Производитель", "Марка", "Торговая марка")


def _top_category(url: str) -> str:
    """Return the top-level category slug from a /katalog_produkcii/<cat>/... URL."""
    import urllib.parse
    parts = urllib.parse.urlparse(url).path.strip("/").split("/")
    # parts[0] == "katalog_produkcii", parts[1] == top category
    return parts[1] if len(parts) > 1 else ""


class AerocompressorsScraper(BaseScraper):
    site = "aerocompressors.ru"
    base_url = BASE

    # ddos-guard: keep a gentle cadence
    delay_min = 0.8
    delay_max = 1.2

    def discover(self) -> list[str]:
        return ["__sitemap__"]

    def fetch_listing(self, url: str) -> list[str]:
        # Fetch the whole sitemap (single file), then keep only deep paths:
        # products live at depth >= 5; shallower paths are catalog categories.
        urls = collect_product_urls(
            self.client, SITEMAP, include=INCLUDE, exclude=EXCLUDE, max_urls=None
        )
        urls = [u for u in urls if u.rstrip("/").count("/") - 2 >= 5]
        # Keep only URLs whose second path segment (the top-level category) is
        # in RELEVANT_CATEGORIES.  URL shape:
        #   /katalog_produkcii/<category>/[sub/...]/<slug>
        urls = [
            u for u in urls
            if _top_category(u) in RELEVANT_CATEGORIES
        ]
        # Shuffle BEFORE the MAX cap so a capped test run samples the whole
        # catalog (the first sitemap URLs are category/landing pages).
        if os.getenv("SHUFFLE", "").strip() in ("1", "true", "yes"):
            import random
            random.shuffle(urls)
        if MAX_URLS:
            urls = urls[:MAX_URLS]
        logger.info("[aerocompressors] %d product URLs from sitemap", len(urls))
        return urls

    def parse_product(self, url: str) -> Optional[Product]:
        resp = self.client.get(url)
        soup = BeautifulSoup(resp.content, "lxml")

        specs, is_matrix = self._extract_specs(soup)
        if is_matrix:
            return None  # multi-variant series page — skip
        # Category pages carry an itemprop=price ("from" price) but no specs
        # table; only real product pages have table.tech — require it.
        if not specs:
            return None

        price = self._price(soup)
        h1 = soup.select_one("h1")
        name = h1.get_text(" ", strip=True) if h1 else ""

        # Brand from specs, else from the product name (the nav brand link is a
        # generic "Производители" label, so it is not used).
        brand = ""
        for k in _BRAND_KEYS:
            if specs.get(k):
                brand = specs[k]
                break
        if not brand:
            brand = extract_brand_from_name(name)

        old_price = self._old_price(soup)
        discount_pct = None
        if price and old_price and old_price > price:
            discount_pct = round((old_price - price) / old_price * 100, 1)

        # No explicit stock block: a numeric price implies in-stock; a product
        # shown "по запросу" (no number) is treated as orderable ("под заказ").
        if price is not None:
            availability, series_status = "в наличии", "в наличии"
        else:
            availability, series_status = "цена по запросу", "под заказ"
        # The phrase "снято с производства" appears in the site-wide catalog
        # menu, so only trust it when it is in the product's own H1/name.
        if has_discontinued_signal(name):
            series_status = "снято"
            availability = "снято с производства"

        category_path = self._category_from_url(url)
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

    def _price(self, soup: BeautifulSoup) -> float | None:
        el = soup.select_one("[itemprop='price']")
        if el:
            p = clean_price(el.get("content") or el.get_text())
            if p:
                return p
        el = soup.select_one(".price")
        if el:
            return clean_price(el.get_text())
        return None

    def _old_price(self, soup: BeautifulSoup) -> float | None:
        el = soup.select_one(".old-price, .price-old, [class*='old_price']")
        return clean_price(el.get_text()) if el else None

    def _extract_specs(self, soup: BeautifulSoup) -> tuple[dict, bool]:
        rows = soup.select("table.tech tr, table.characteristics tr, table.params tr")
        return parse_spec_table(rows)

    def _category_from_url(self, url: str) -> str:
        import urllib.parse
        parts = urllib.parse.urlparse(url).path.strip("/").split("/")
        # drop the leading "katalog_produkcii" and the final product slug
        crumbs = [p.replace("_", " ").replace("-", " ") for p in parts[1:-1]]
        return " > ".join(crumbs)

    def _image(self, soup: BeautifulSoup) -> str:
        img = soup.select_one("[itemprop='image'], .product-image img, .detail-picture img")
        if img:
            src = img.get("data-src") or img.get("content") or img.get("src", "")
            if src:
                return src if src.startswith("http") else BASE + src
        return ""
