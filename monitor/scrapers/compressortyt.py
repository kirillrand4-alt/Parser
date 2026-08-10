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
    Product, cell_value, clean_price, detect_series_status,
    extract_model_from_name, harvest_specs, parse_spec_table,
)

logger = logging.getLogger(__name__)

BASE = "https://compressortyt.ru"
YML_URL = "https://compressortyt.ru/yml/"

# Enrich each product with full specs from its HTML page. The YML feed carries
# price/brand/name but NO specs, so without enrichment every compressortyt card
# has an empty `specs` — useless to the matcher (kW/bar/flow all missing). The
# product page DOES expose a full spec table (verified: 21 fields incl. power,
# pressure, flow, drive, VFD, dryer, cooling). So enrichment is ON by default;
# it costs ~16k extra page fetches, which is the same per-product cost the other
# five sites already pay (they have no feed). Opt out with COMPRESSORTYT_ENRICH=0.
ENRICH_SPECS = os.getenv("COMPRESSORTYT_ENRICH", "1") != "0"
# Parse HTML category pages instead of the YML feed. Slower (walks every
# category with pagination + fetches each product page) but reflects exactly
# what the site shows — use when the feed is stale/incomplete for some brand.
# Enable via COMPRESSORTYT_HTML=1.
USE_HTML = os.getenv("COMPRESSORTYT_HTML", "0") == "1"
# Cap offers parsed from the feed (0 = all). Useful for test runs.
MAX_OFFERS = int(os.getenv("COMPRESSORTYT_MAX", "0")) or None

# HTML-mode entry point. Earlier this was a list of leaf slugs, but 6 of the 10
# 404'd (bezmaslyany, bezmaloye, s-remennoy-peredachey, dvukhstupenchatye,
# mobilnye, dizelnyy) and the pagination used `?page=N` which the site answers
# with 404 — HTML mode collected 631 URLs instead of ~15.9k. The root compressor
# category paginates over EVERY compressor (site counter: "Найдено 15908"), so
# walking it with the real `/page-N/` scheme is both complete and self-maintaining.
CATEGORIES = ["/stanciya/kompr/"]

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
        """Parse the YML feed; optionally enrich each product with HTML specs.

        With COMPRESSORTYT_HTML=1 the feed is skipped entirely and products are
        collected by walking the HTML category pages (discover/fetch_listing/
        parse_product in the base class).
        """
        if USE_HTML:
            logger.info("[compressortyt] COMPRESSORTYT_HTML=1 — parsing category "
                        "pages instead of the YML feed")
            yield from super().scrape(position=position)
            return

        try:
            from tqdm.auto import tqdm
        except Exception:
            from ..base_scraper import tqdm  # no-op bar stub (same contract)

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

        seed_done = self._load_seed_done() if resume else set()
        if seed_done:
            logger.info("[compressortyt] seeded %d done URLs from prior CSV output",
                        len(seed_done))

        pending = [o for o in offers
                   if offer_url(o) not in done_urls and offer_url(o) not in seed_done]
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

        # The feed is NOT the whole catalog: measured 2026-08-10, the compressor
        # section lists 17 678 products while the feed carries only 11 255 of
        # them — 6 510 missing, overwhelmingly screw compressors (Kaeser DSD/DSG,
        # Kraftmann TAURUS/VEGA, ЗИФ СВЭ, Atmos ST). They are missing because the
        # feed mostly carries priced offers, and those cards are "по запросу" —
        # exactly the ones GAP analysis needs. So after the feed, walk the
        # catalog and pick up whatever it did not contain (opt out with
        # COMPRESSORTYT_UNION=0).
        if os.getenv("COMPRESSORTYT_UNION", "1") == "0":
            return
        feed_urls = {offer_url(o) for o in offers}
        # Обход каталога стоит дорого: ~600 страниц, и в боевом прогоне это
        # ЧАСЫ, а не минуты — замер 10.08 на v-p-k дал 26 с/страница (крупные
        # страницы + прокси + шесть сайтов параллельно делят канал). Платить
        # это каждый прогон нельзя, поэтому список URL кешируется в чекпоинте
        # ровно так же, как base_scraper кеширует product_urls: при RESUME=1
        # берём готовый, при «с нуля» — обходим заново.
        listed: list[str] = self._checkpoint.get("catalog_urls", []) if resume else []
        if listed:
            logger.info("[compressortyt] каталог: %d URL из чекпоинта "
                        "(обход пропущен)", len(listed))
        else:
            try:
                listed = []
                for cat in CATEGORIES:
                    listed += self.fetch_listing(BASE + cat)
            except Exception as exc:
                logger.warning("[compressortyt] catalog walk failed: %s", exc)
                return
            self._checkpoint["catalog_urls"] = listed
            self._save_progress(done_urls, failed_urls)
            logger.info("[compressortyt] каталог обойдён: %d URL, сохранено в "
                        "чекпоинт", len(listed))
        extra = [u for u in dict.fromkeys(listed)
                 if u not in feed_urls and u not in done_urls
                 and u not in seed_done]
        if not extra:
            return
        logger.info("[compressortyt] catalog walk: %d products the feed lacks",
                    len(extra))
        bar2 = tqdm(total=len(extra), desc=f"{'compressortyt(+cat)':<20}",
                    position=position, unit="prod", leave=True, dynamic_ncols=True)
        try:
            for n, url in enumerate(extra, 1):
                try:
                    product = self.parse_product(url)
                except Exception as exc:
                    logger.debug("[compressortyt] extra parse error %s: %s", url, exc)
                    failed_urls.add(url)
                    bar2.update(1)
                    continue
                done_urls.add(url)
                failed_urls.discard(url)
                bar2.update(1)
                if n % 100 == 0:
                    self._save_progress(done_urls, failed_urls)
                if product is not None:   # None = comparison-matrix page
                    yield product
        finally:
            bar2.close()
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
        """Collect all product URLs from a category, following pagination.

        Two site facts learned the hard way:
          * pagination is `/page-N/` appended to the category path, NOT `?page=N`
            (the query form 404s);
          * a product tile is `a.product__title-link` / `a.js-product-link` whose
            href has 5 path segments (`/stanciya/kompr/<type>/<brand>/<model>/`);
            brand and category tiles share the `/stanciya/kompr/` prefix but have
            only 3-4 segments, so a plain prefix match pulls in non-products.
        The listing tile also carries data-product-price/name/brand, but we only
        need the URL here — the feed/enrich path fills the rest.
        """
        from concurrent.futures import ThreadPoolExecutor
        from urllib.parse import urlsplit

        base = url.rstrip("/")
        path = urlsplit(base).path.rstrip("/")

        def products_on(html: bytes) -> list[str]:
            soup = BeautifulSoup(html, "lxml")
            links = soup.select("a.product__title-link, a.js-product-link")
            if not links:  # template drift — fall back to any kompr link, filtered below
                links = soup.select("a[href*='/stanciya/kompr/']")
            out = []
            for link in links:
                href = (link.get("href") or "").split("?")[0]
                if "/stanciya/kompr/" not in href:
                    continue
                # 5 segments = real product; fewer = brand/category/filter page
                parts = [p for p in href.split("://")[-1].split("/")[1:] if p]
                if len(parts) >= 5:
                    out.append(href if href.startswith("http") else BASE + href)
            return out

        # Страница 1 нужна в любом случае — и ради товаров, и ради числа страниц.
        try:
            first = self.client.get(f"{base}/").content
        except Exception as exc:
            logger.warning("[compressortyt] listing page error %s/: %s", base, exc)
            return []
        product_urls: list[str] = products_on(first)

        # Сайт печатает ссылку на ПОСЛЕДНЮЮ страницу прямо на первой
        # (/stanciya/kompr/page-594/), поэтому перебирать вслепую до 404 не надо:
        # берём число и качаем страницы пачками параллельно. Паттерн привязан к
        # пути категории — иначе в него попадает пагинация отзывов
        # (/o-kompanii/reviews/page-27/) и число страниц выходит чужое.
        pages = [int(n) for n in re.findall(
            re.escape(path) + r"/page-(\d+)/", first.decode("utf-8", "replace"))]
        last = max(pages) if pages else 0

        if last > 1:
            # 594 страницы по одной — это часы (замер: 26 с/страница через прокси
            # при шести сайтах параллельно). Пул запросов идёт через РАЗНЫЕ прокси
            # из пула, поэтому нагрузка на сайт размазана по адресам.
            workers = int(os.getenv("COMPRESSORTYT_LISTING_WORKERS", "16"))
            logger.info("[compressortyt] каталог %s: %d страниц, качаю по %d",
                        path, last, workers)
            done = 0

            def grab(p: int) -> tuple[int, list[str]]:
                try:
                    return p, products_on(self.client.get(f"{base}/page-{p}/").content)
                except Exception as exc:
                    logger.warning("[compressortyt] страница %d: %s", p, exc)
                    return p, []

            with ThreadPoolExecutor(max_workers=workers) as pool:
                # pool.map отдаёт результаты в порядке аргументов, то есть список
                # URL не «плавает» между прогонами. Оборачивать в sorted() нельзя:
                # он дожидается ВСЕХ страниц, и отметки прогресса не печатаются
                # до самого конца обхода — ровно то, от чего мы уходим.
                for p, found in pool.map(grab, range(2, last + 1)):
                    product_urls += found
                    done += 1
                    if done % 50 == 0:
                        logger.info("[compressortyt] обход: %d/%d страниц, "
                                    "%d товаров", done, last - 1, len(product_urls))
        else:
            # Разметка пагинации поменялась — идём по одной, пока не кончится.
            logger.info("[compressortyt] число страниц не найдено — обход по одной")
            page, empty_streak = 2, 0
            while page <= 800:
                try:
                    found = products_on(self.client.get(f"{base}/page-{page}/").content)
                except Exception as exc:
                    logger.warning("[compressortyt] страница %d: %s", page, exc)
                    break
                product_urls += found
                empty_streak = empty_streak + 1 if not found else 0
                if empty_streak >= 2:  # одну странную страницу терпим, на второй встаём
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
        # The strike-through old price lives in .product-card__price_sale; no
        # itemprop fallback here, or the old price would echo the current one.
        old_price = self._extract_price(
            soup, "price_sale", "old-price", "price-old", "crossed", itemprop=False)
        if old_price and price and old_price <= price:
            old_price = None
        discount_pct = self._calc_discount(price, old_price)

        # --- Availability ---
        availability, series_status = self._extract_availability(soup, page_text)
        # No explicit stock block on most pages: a numeric price implies the
        # item is sellable; no price and no block means price-on-request.
        if not availability:
            availability = "в наличии" if price is not None else "по запросу"
            if series_status == "неизвестно":
                series_status = "в наличии" if price is not None else "под заказ"

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

    def _extract_price(self, soup: BeautifulSoup, *css_hints: str,
                       itemprop: bool = True) -> float | None:
        # itemprop content carries the exact machine-readable number — prefer
        # it over class-based text that may mix current and old prices.
        if itemprop:
            el = soup.select_one("[itemprop='price']")
            if el is not None:
                p = clean_price(el.get("content") or el.get_text(" "))
                if p:
                    return p
        for hint in css_hints:
            el = soup.select_one(f"[class*='{hint}']")
            if el:
                p = clean_price(el.get_text(" "))
                if p:
                    return p
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

        # Don't expose the internal "неизвестно" sentinel as availability text;
        # the caller fills an empty value based on whether a price was found.
        availability = availability_raw or (
            series_status if series_status != "неизвестно" else "")
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
        # Method 0: native div-based spec rows. The product page renders specs
        # as .options-table__item (caption/value divs) inside
        # #product-specifications, plus a quick-facts block in the header.
        # Scoped to the product's own containers so analog products'
        # .product__option-* rows are not picked up.
        specs: dict = {}
        for item in soup.select(
                ".productCardSpecifications .options-table__item, "
                "#product-specifications .options-table__item, "
                ".product-card__main-options-item"):
            cap = item.select_one(
                ".options-table__item-caption, .product-card__main-options-name")
            val = item.select_one(
                ".options-table__item-value, .product-card__main-options-value")
            if not cap or not val:
                continue
            for hint in cap.select(".hint"):  # "?" tooltip icon bleeds into text
                hint.decompose()
            k = cap.get_text(" ", strip=True).strip().rstrip("?").rstrip(":").strip()
            v = cell_value(val)
            if k and v and k not in specs:
                specs[k] = v
        if len(specs) >= 3:
            return specs

        # Method 1: specification table rows (skip multi-variant matrices)
        rows = soup.select(
            "table.specs tr, table.characteristics tr, .product-specs tr, .specification tr")
        table_specs, is_matrix = parse_spec_table(rows)
        if not is_matrix:
            for k, v in table_specs.items():
                specs.setdefault(k, v)

        # Method 2: dl/dt/dd pairs
        if not specs:
            for dl in soup.select("dl.specs, dl.characteristics, dl.properties"):
                dts = dl.select("dt")
                dds = dl.select("dd")
                for dt, dd in zip(dts, dds):
                    v = cell_value(dd)
                    if v:
                        specs[dt.get_text(strip=True)] = v

        # Method 3: universal harvester when site selectors found little
        if len(specs) < 3:
            for k, v in harvest_specs(soup).items():
                specs.setdefault(k, v)

        # Method 4: itemprop attributes — only real product properties. Pages
        # carry Organization/Breadcrumb microdata too (telephone, logo,
        # itemListElement, ...) which must never land in specs.
        itemprop_skip = (
            "name", "description", "image", "url", "price", "pricecurrency",
            "availability", "brand", "telephone", "email", "logo", "address",
            "streetaddress", "postalcode", "addresslocality", "addressregion",
            "addresscountry", "itemlistelement", "item", "position",
            "contactpoint", "openinghours", "offers", "aggregaterating",
            "ratingvalue", "reviewcount", "review", "sku",
        )
        for el in soup.select("[itemprop]"):
            prop = el.get("itemprop", "")
            if prop and prop.lower() not in itemprop_skip:
                val = el.get("content") or el.get_text(strip=True)
                if val and len(val) <= 300:
                    specs.setdefault(prop, val)

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
