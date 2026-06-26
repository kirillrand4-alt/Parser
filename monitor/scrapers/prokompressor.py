"""Scraper for prokompressor.ru — our own site, prices via YML feed.

Source: Bitrix YML feed  https://prokompressor.ru/bitrix/catalog_export/prokompressor.ru.xml
No HTML requests needed — the feed contains name, brand, price, availability,
category path, product URL, and image.

Specs are NOT in Bitrix YML (no <param> tags in most Bitrix exports), so an
optional HTML enrich pass (PROKOMPRESSOR_ENRICH=1) fetches each product page.
By default enrich is off — we just need current prices.
"""
from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from typing import Optional

from bs4 import BeautifulSoup

from ..base_scraper import BaseScraper
from ..models import (
    Product, clean_price, extract_model_from_name,
    detect_series_status, harvest_specs, parse_spec_table, cell_value,
)

logger = logging.getLogger(__name__)

BASE = "https://prokompressor.ru"
YML_URL = os.getenv(
    "PROKOMPRESSOR_YML",
    "https://prokompressor.ru/bitrix/catalog_export/prokompressor.ru.xml",
)
ENRICH_SPECS = os.getenv("PROKOMPRESSOR_ENRICH", "0") == "1"
MAX_OFFERS = int(os.getenv("PROKOMPRESSOR_MAX", "0")) or None


class ProkompressorScraper(BaseScraper):
    site = "prokompressor.ru"
    base_url = BASE
    delay_min = 0.3
    delay_max = 0.7  # own site — no need to be polite

    # ------------------------------------------------------------------
    # Discovery stubs (unused — scrape() overrides the whole flow)
    # ------------------------------------------------------------------
    def discover(self) -> list[str]:
        return [YML_URL]

    def fetch_listing(self, url: str) -> list[str]:
        return []

    def parse_product(self, url: str) -> Optional[Product]:
        return None  # not used; _offer_to_product handles parsing

    # ------------------------------------------------------------------
    # Main entry: parse YML feed directly
    # ------------------------------------------------------------------
    def scrape(self, position: int = 0):  # type: ignore[override]
        try:
            from tqdm.auto import tqdm
        except Exception:
            def tqdm(iterable=None, **_):
                return iterable if iterable is not None else iter(())

        logger.info("[prokompressor] fetching YML feed: %s", YML_URL)
        resp = self.client.get(YML_URL, timeout=90)
        root = ET.fromstring(resp.content)
        shop = root.find("shop")
        if shop is None:
            logger.error("[prokompressor] no <shop> element in feed")
            return

        # Category id → name and parent map for full path
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
        logger.info("[prokompressor] %d offers in feed", len(offers))

        resume = os.getenv("RESUME", "").strip().lower() in ("1", "true", "yes")
        done_urls: set[str] = set(self._checkpoint.get("done_urls", [])) if resume else set()
        failed_urls: set[str] = set()

        def offer_url(o: ET.Element) -> str:
            el = o.find("url")
            return (el.text or "").strip() if el is not None else ""

        pending = [o for o in offers if offer_url(o) not in done_urls]
        if resume and len(pending) < len(offers):
            logger.info("[prokompressor] resume: skipping %d done", len(offers) - len(pending))

        errors = 0
        i = 0
        bar = tqdm(total=len(pending), desc=f"{'prokompressor.ru':<20}",
                   position=position, unit="prod", leave=True, dynamic_ncols=True)
        try:
            for offer in pending:
                url = offer_url(offer)
                try:
                    product = self._offer_to_product(offer, cat_path)
                except Exception as exc:
                    logger.debug("[prokompressor] offer parse error: %s", exc)
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
                        logger.debug("[prokompressor] enrich error %s: %s",
                                     product.product_url, exc)
                if url:
                    done_urls.add(url)
                bar.update(1)
                bar.set_postfix(err=errors, refresh=False)
                i += 1
                if i % 200 == 0:
                    self._save_progress(done_urls, failed_urls)
                yield product
        finally:
            bar.close()
            self._save_progress(done_urls, failed_urls)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _offer_to_product(self, offer: ET.Element, cat_path) -> Product:
        def t(tag: str) -> str:
            el = offer.find(tag)
            return (el.text or "").strip() if el is not None else ""

        available = offer.get("available", "true") == "true"
        name = t("name")
        brand = t("vendor")

        # Bitrix YML may carry <param> spec tags
        specs: dict = {}
        for param in offer.findall("param"):
            k = (param.get("name") or "").strip()
            v = (param.text or "").strip()
            unit = param.get("unit", "")
            if k and v:
                specs[k] = f"{v} {unit}".strip() if unit else v

        price_raw = t("price")
        old_price_raw = t("oldprice") or t("old_price")
        price = clean_price(price_raw)
        old_price = clean_price(old_price_raw)
        if old_price and price and old_price <= price:
            old_price = None
        discount_pct = None
        if price and old_price and old_price > price:
            discount_pct = round((old_price - price) / old_price * 100, 1)

        currency = t("currencyId").replace("RUR", "RUB") or "RUB"
        availability = "в наличии" if available else "нет в наличии"
        series_status = "в наличии" if available else "нет в наличии"

        return Product(
            site=self.site,
            brand=brand,
            name=name,
            model=extract_model_from_name(name, brand),
            sku=offer.get("id", ""),
            price=price,
            old_price=old_price,
            discount_pct=discount_pct,
            currency=currency,
            availability=availability,
            series_status=series_status,
            specs=specs,
            category_path=cat_path(t("categoryId")),
            product_url=t("url"),
            image_url=t("picture"),
        )

    def _enrich(self, product: Product) -> None:
        """Fetch the product HTML page and fill specs (optional, PROKOMPRESSOR_ENRICH=1)."""
        resp = self.client.get(product.product_url)
        soup = BeautifulSoup(resp.content, "lxml")

        specs: dict = dict(product.specs)  # keep YML params

        # Bitrix property table
        rows = soup.select("table.props tr, .properties tr, .product-properties tr")
        if rows:
            table_specs, is_matrix = parse_spec_table(rows)
            if not is_matrix:
                for k, v in table_specs.items():
                    specs.setdefault(k, v)

        # .options-table (Aspro / custom themes)
        for item in soup.select(".options-table__item"):
            cap = item.select_one(".options-table__item-caption")
            val = item.select_one(".options-table__item-value")
            if cap and val:
                k = cap.get_text(" ", strip=True).rstrip(":").strip()
                v = cell_value(val)
                if k and v:
                    specs.setdefault(k, v)

        if len(specs) < 3:
            for k, v in harvest_specs(soup).items():
                specs.setdefault(k, v)

        product.specs = specs

        page_text = soup.get_text(" ", strip=True)
        status = detect_series_status(page_text)
        if status != "неизвестно":
            product.series_status = status
