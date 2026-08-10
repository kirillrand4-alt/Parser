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
import re
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
CATEGORIES = [
    "https://www.v-p-k.ru/catalog/kompressory/",
    "https://www.v-p-k.ru/catalog/podgotovka-vozdukha/",
]
MAX_PAGES = 400  # safety cap per category

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
        """Собрать товарные URL из категорий каталога.

        Битрикс печатает номер последней страницы прямо на первой
        (`?PAGEN_1=338`), поэтому число страниц известно заранее и они качаются
        пачками параллельно, а не по одной.

        Так уходят сразу две беды прежнего обхода «по одной до пустой страницы»:
          * он был медленным — страницы тут по 2.5 МБ, замер на боевом прогоне
            дал 26 с/страница, то есть часы на категорию;
          * он ОБРЫВАЛСЯ на первой же странице при RESUME=1. Обход грузил ранее
            сохранённые URL в `seen`, но шёл опять с первой страницы — все её
            товары уже были знакомы, срабатывало «нет новых», и категория
            закрывалась. Сохранённый номер страницы при этом не использовался.
            На боевом прогоне это давало 5 202 товара вместо ~15 600.
        """
        from concurrent.futures import ThreadPoolExecutor

        resume = os.getenv("RESUME", "").strip().lower() in ("1", "true", "yes")
        page_timeout = int(os.getenv("VPK_PAGE_TIMEOUT", "30"))
        # Страницы тяжёлые (2.5 МБ), поэтому потоков меньше, чем у compressortyt.
        workers = int(os.getenv("VPK_LISTING_WORKERS", "8"))

        cached = self._checkpoint.get("vpk_catalog_urls") if resume else None
        if cached:
            logger.info("[v-p-k] каталог: %d URL из чекпоинта (обход пропущен)",
                        len(cached))
            urls = list(cached)
        else:
            urls = []
            seen: set[str] = set()

            def products_on(html: bytes) -> list[str]:
                soup = BeautifulSoup(html, "lxml")
                out = []
                for a in soup.select("a[href*='/product/']"):
                    href = (a.get("href") or "").split("?")[0]
                    if href:
                        out.append(href if href.startswith("http") else BASE + href)
                return out

            def add(found: list[str]) -> None:
                for full in found:
                    if full not in seen:
                        seen.add(full)
                        urls.append(full)

            for category in CATEGORIES:
                try:
                    first = self.client.get(category, timeout=page_timeout).content
                except Exception as exc:
                    logger.error("[v-p-k] %s: первая страница не открылась: %s",
                                 category, exc)
                    continue
                add(products_on(first))
                pages = [int(n) for n in re.findall(
                    r"PAGEN_1=(\d+)", first.decode("utf-8", "replace"))]
                last = min(max(pages), MAX_PAGES) if pages else 0
                if last <= 1:
                    logger.warning("[v-p-k] %s: число страниц не найдено, "
                                   "взята только первая", category)
                    continue
                logger.info("[v-p-k] %s: %d страниц, качаю по %d",
                            category, last, workers)

                def grab(p: int, _c=category) -> tuple[int, list[str]]:
                    try:
                        u = f"{_c}?PAGEN_1={p}"
                        return p, products_on(
                            self.client.get(u, timeout=page_timeout).content)
                    except Exception as exc:
                        logger.warning("[v-p-k] страница %d: %s", p, exc)
                        return p, []

                done = 0
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    # без sorted(): pool.map и так держит порядок аргументов, а
                    # sorted дождался бы всех страниц и убил отметки прогресса
                    for _p, found in pool.map(grab, range(2, last + 1)):
                        add(found)
                        done += 1
                        if done % 25 == 0:
                            logger.info("[v-p-k] обход: %d/%d страниц, %d товаров",
                                        done, last - 1, len(urls))
                logger.info("[v-p-k] finished %s — %d URL", category, len(urls))

            self._checkpoint["vpk_catalog_urls"] = urls
            # ключи частичного обхода больше не нужны: список либо собран
            # целиком, либо будет собран заново на следующем прогоне
            self._checkpoint.pop("vpk_partial_urls", None)
            self._checkpoint.pop("vpk_partial_last_page", None)
            self._save_ckpt()
        if os.getenv("SHUFFLE", "").strip() in ("1", "true", "yes"):
            import random
            random.shuffle(urls)
        if MAX_URLS:
            urls = urls[:MAX_URLS]
        logger.info("[v-p-k] %d product URLs from catalog pagination", len(urls))
        return urls

    def _save_ckpt(self) -> None:
        """Атомарно сохранить чекпоинт (через .tmp + rename)."""
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
