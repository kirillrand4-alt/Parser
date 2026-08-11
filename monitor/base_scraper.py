"""Abstract base class for all site scrapers."""
from __future__ import annotations

import json
import os
import time
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

import requests

try:
    from tqdm.auto import tqdm
except Exception:  # tqdm optional — degrade to a no-op progress bar
    class tqdm:  # type: ignore
        """Stand-in for tqdm when it is not installed.

        It must behave like a BAR, not like an iterator: callers do
        `bar = tqdm(total=...)` then `bar.update()` / `bar.set_postfix()` /
        `bar.close()`. Returning a bare iterator here (the previous stub) made
        every scrape crash with AttributeError on the first update whenever
        tqdm was missing, since tqdm is only an optional dependency.
        """

        def __init__(self, iterable=None, **_):
            self._iterable = iterable

        def __iter__(self):
            return iter(self._iterable if self._iterable is not None else ())

        def update(self, _n=1): pass

        def set_postfix(self, *_a, **_k): pass

        def close(self): pass

        def __enter__(self): return self

        def __exit__(self, *_): return False

from .http_client import HttpClient
from .models import Product

logger = logging.getLogger(__name__)

# HTTP statuses that signal anti-bot blocking / rate-limiting rather than a
# genuine "page not found". Used to detect when a site starts pushing back.
BLOCK_STATUSES = {403, 429, 503}

# Abort a site after this many *consecutive* blocked responses — there is no
# point hammering a host that is rejecting us. Progress is saved so a later
# run (e.g. once proxies are configured) resumes from exactly here.
MAX_CONSECUTIVE_BLOCKS = int(os.getenv("MAX_CONSECUTIVE_BLOCKS", "12"))


class BaseScraper(ABC):
    """Common interface every site scraper must implement."""

    site: str  # domain, e.g. "compressortyt.ru"
    base_url: str  # e.g. "https://compressortyt.ru"

    # Shared across all scraper instances: SEED_DONE_CSV_DIR → {site: {urls}}.
    # Parsed once per directory so 6 sites don't each re-read every CSV.
    _seed_cache: dict = {}

    # Per-site polite delay between requests (seconds). Robust sites can set
    # these lower; sites behind anti-bot (ddos-guard) keep them higher.
    delay_min: float = 1.0
    delay_max: float = 2.5

    def __init__(self) -> None:
        # Global env override lets you tune requests/sec without code changes:
        #   DELAY_MIN / DELAY_MAX (seconds), or per-site
        #   DELAY_MIN__<SITE> / DELAY_MAX__<SITE> (dots/dashes → underscores).
        key = self.site.replace(".", "_").replace("-", "_").upper()
        dmin = os.getenv(f"DELAY_MIN__{key}") or os.getenv("DELAY_MIN")
        dmax = os.getenv(f"DELAY_MAX__{key}") or os.getenv("DELAY_MAX")
        delay_min = float(dmin) if dmin else self.delay_min
        delay_max = float(dmax) if dmax else self.delay_max
        self.client = HttpClient(self.site, delay_min=delay_min, delay_max=delay_max)
        checkpoint_dir = os.getenv("CHECKPOINT_DIR", "cache")
        self._checkpoint_path = Path(checkpoint_dir) / f"{self.site}.checkpoint.json"
        self._checkpoint: dict = self._load_checkpoint()

    # ------------------------------------------------------------------
    # Interface
    # ------------------------------------------------------------------

    @abstractmethod
    def discover(self) -> list[str]:
        """Return list of category/listing URLs to crawl."""
        ...

    @abstractmethod
    def fetch_listing(self, url: str) -> list[str]:
        """Return product URLs from a listing/category page (handles pagination)."""
        ...

    @abstractmethod
    def parse_product(self, url: str) -> Product | None:
        """Fetch and parse a single product page. Return None on skip."""
        ...

    def scrape(self, position: int = 0) -> Iterator[Product]:
        """Main entry point: discover → fetch listings → parse products.

        A fresh run re-scrapes every product (a price monitor wants current
        prices each time); the HTTP cache — not the checkpoint — protects the
        site from repeated load.

        Progress is tracked in a per-site checkpoint (``done_urls`` / ``done_at``
        / ``failed_urls``). С ``RESUME=1`` пропускаются не все собранные когда-то
        URL, а только собранные НЕДАВНО — моложе ``FRESH_WITHIN_DAYS`` (по
        умолчанию 2 дня, см. ``_recent_done``). Прогон, оборванный на середине,
        продолжается с места обрыва, а карточки, снятые неделю назад, идут
        заново — иначе их цены застывают навсегда. Неудачные URL повторяются.

        A live tqdm progress bar shows parsed / errors / blocked counts.
        ``position`` lets concurrent site bars stack without overwriting.
        """
        resume = os.getenv("RESUME", "").strip().lower() in ("1", "true", "yes")
        done_urls: set[str] = self._recent_done() if resume else set()
        failed_urls: set[str] = set(self._checkpoint.get("failed_urls", [])) if resume else set()
        # Что уже было собрано ДО этого прогона — чтобы при сохранении не
        # обновлять их отметки времени: они собраны не сейчас.
        self._initial_done = set(done_urls)

        # 1) Build the full work list first so the bar has a real total.
        # For scrapers that do expensive discovery (e.g. paginated catalog walk),
        # cache the URL list in the checkpoint so subsequent runs skip the crawl.
        category_urls = self.discover()
        logger.info("[%s] %d categories to crawl", self.site, len(category_urls))

        product_urls: list[str] = []
        cached_urls: list[str] = self._checkpoint.get("product_urls", [])
        if resume and cached_urls:
            product_urls = cached_urls
            logger.info("[%s] using %d cached product URLs from checkpoint",
                        self.site, len(product_urls))
        else:
            seen: set[str] = set()
            for cat_url in category_urls:
                try:
                    for u in self.fetch_listing(cat_url):
                        if u not in seen:
                            seen.add(u)
                            product_urls.append(u)
                except Exception as exc:
                    logger.warning("[%s] listing error %s: %s", self.site, cat_url, exc)
            # Persist URL list so next RESUME run skips discovery
            self._checkpoint["product_urls"] = product_urls
            self._save_progress(done_urls, failed_urls)

        # SEED_DONE_CSV_DIR: skip URLs already present in prior-run CSVs.
        # We fold them into done_urls and persist immediately so subsequent
        # runs load them from the checkpoint (O(1)) instead of re-reading
        # every CSV again (which can take hours on slow network drives).
        if resume:
            csv_seeded = self._checkpoint.get("csv_seeded", False)
            if not csv_seeded:
                seed_done = self._load_seed_done()
                if seed_done:
                    logger.info("[%s] seeded %d done URLs from prior CSV output",
                                self.site, len(seed_done))
                    done_urls |= seed_done
                    # Их собрали КОГДА-ТО (по старым CSV), а не сейчас — иначе
                    # при сохранении они получат сегодняшнюю отметку и навсегда
                    # будут считаться свежими.
                    self._initial_done |= seed_done
                    self._checkpoint["csv_seeded"] = True
                    self._save_progress(done_urls, failed_urls)
            else:
                logger.info("[%s] CSV seed already in checkpoint — skipping CSV scan",
                            self.site)

        pending = [u for u in product_urls if u not in done_urls]
        if resume and len(pending) < len(product_urls):
            logger.info("[%s] resume: skipping %d already-done URLs",
                        self.site, len(product_urls) - len(pending))

        # SHUFFLE=1 randomises the order so test runs sample the full catalog
        # instead of always hitting the first N URLs from the sitemap.
        # In production (no SHUFFLE) order is preserved for reproducibility.
        if os.getenv("SHUFFLE", "").strip() in ("1", "true", "yes"):
            import random
            random.shuffle(pending)

        # 2) Parse each product, tracking errors and blocking.
        # WORKERS (or per-site WORKERS__<SITE>) sets concurrent requests per
        # site. Default 1 = strictly sequential (unchanged behaviour). Robust
        # nginx hosts tolerate 8-16; sites behind anti-bot should stay low.
        key = self.site.replace(".", "_").replace("-", "_").upper()
        workers = int(os.getenv(f"WORKERS__{key}") or os.getenv("WORKERS") or "1")
        workers = max(1, workers)

        stats = {"errors": 0, "blocked": 0, "consecutive_blocks": 0, "i": 0}
        bar = tqdm(total=len(pending), desc=f"{self.site:<20}", position=position,
                   unit="prod", leave=True, dynamic_ncols=True)

        def handle(prod_url: str, product, exc: Exception | None):
            """Update counters/checkpoint for one finished URL. Return product
            to yield (or None). Sets stats['stop']=True on block threshold."""
            if exc is None:
                done_urls.add(prod_url)
                failed_urls.discard(prod_url)
                stats["consecutive_blocks"] = 0
                return product
            if isinstance(exc, requests.HTTPError):
                status = getattr(exc.response, "status_code", None)
                failed_urls.add(prod_url)
                if status in BLOCK_STATUSES:
                    stats["blocked"] += 1
                    stats["consecutive_blocks"] += 1
                    logger.warning("[%s] BLOCKED %s on %s", self.site, status, prod_url)
                    if stats["consecutive_blocks"] >= MAX_CONSECUTIVE_BLOCKS:
                        logger.error(
                            "[%s] %d consecutive blocks — stopping. "
                            "Re-run with RESUME=1 (and proxies) to continue.",
                            self.site, stats["consecutive_blocks"])
                        stats["stop"] = True
                else:
                    stats["errors"] += 1
                    logger.warning("[%s] HTTP %s on %s", self.site, status, prod_url)
            else:
                stats["errors"] += 1
                failed_urls.add(prod_url)
                logger.warning("[%s] product error %s: %s", self.site, prod_url, exc)
            return None

        def advance():
            stats["i"] += 1
            bar.update(1)
            bar.set_postfix(err=stats["errors"], blocked=stats["blocked"], refresh=False)
            if stats["i"] % 25 == 0:
                self._save_progress(done_urls, failed_urls)

        try:
            if workers == 1:
                for prod_url in pending:
                    try:
                        product = self.parse_product(prod_url)
                        out = handle(prod_url, product, None)
                    except Exception as exc:
                        out = handle(prod_url, None, exc)
                    if out:
                        yield out
                    advance()
                    if stats.get("stop"):
                        break
            else:
                from concurrent.futures import ThreadPoolExecutor
                logger.info("[%s] parsing with %d concurrent workers", self.site, workers)
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    it = iter(pending)
                    in_flight: dict = {}
                    # Prime the pool, then refill as each future completes so we
                    # never hold the whole work list in memory at once.
                    for _ in range(workers):
                        u = next(it, None)
                        if u is None:
                            break
                        in_flight[pool.submit(self.parse_product, u)] = u
                    from concurrent.futures import wait, FIRST_COMPLETED
                    while in_flight:
                        done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                        for fut in done:
                            u = in_flight.pop(fut)
                            try:
                                out = handle(u, fut.result(), None)
                            except Exception as exc:
                                out = handle(u, None, exc)
                            if out:
                                yield out
                            advance()
                        if stats.get("stop"):
                            for fut in in_flight:
                                fut.cancel()
                            break
                        # Refill one slot per completed future
                        for _ in range(len(done)):
                            u = next(it, None)
                            if u is None:
                                break
                            in_flight[pool.submit(self.parse_product, u)] = u
        finally:
            bar.close()
            self._save_progress(done_urls, failed_urls)
            if stats["blocked"]:
                logger.warning("[%s] finished with %d blocked, %d other errors",
                               self.site, stats["blocked"], stats["errors"])

    # ------------------------------------------------------------------
    # Checkpoint helpers
    # ------------------------------------------------------------------

    def _load_seed_done(self) -> set[str]:
        """Collect product URLs for this site from prior-run CSVs.

        Controlled by the SEED_DONE_CSV_DIR env var (e.g. a Drive folder of
        prices_*.csv). Returns a set used to skip already-captured URLs for the
        current run only; never written back to the checkpoint. Cached per
        directory so the 6 site scrapers don't each re-read every CSV.
        """
        import csv
        import glob

        seed_dir = os.getenv("SEED_DONE_CSV_DIR")
        if not seed_dir or not os.path.isdir(seed_dir):
            return set()

        cache = BaseScraper._seed_cache
        if seed_dir not in cache:
            by_site: dict[str, set[str]] = {}
            # CSV specs cells can be large; lift the field-size limit.
            try:
                csv.field_size_limit(2**24)
            except Exception:
                pass
            for path in sorted(glob.glob(os.path.join(seed_dir, "*.csv"))):
                try:
                    with open(path, newline="", encoding="utf-8-sig") as f:
                        for row in csv.DictReader(f):
                            site = row.get("site")
                            url = row.get("product_url")
                            if site and url:
                                by_site.setdefault(site, set()).add(url)
                except Exception as exc:
                    logger.warning("seed CSV read error %s: %s", path, exc)
            cache[seed_dir] = by_site
        return cache[seed_dir].get(self.site, set())

    def _load_checkpoint(self) -> dict:
        if self._checkpoint_path.exists():
            try:
                return json.loads(self._checkpoint_path.read_text())
            except Exception:
                pass
        return {}

    def _save_checkpoint(self, done_urls: set[str]) -> None:
        self._checkpoint["done_urls"] = list(done_urls)
        self._checkpoint_path.write_text(json.dumps(self._checkpoint))

    def _recent_done(self) -> set[str]:
        """URL, собранные достаточно НЕДАВНО, чтобы их можно было пропустить.

        Прежде «продолжить» пропускал всё, что собиралось когда-либо, и цены на
        этих карточках навсегда оставались июльскими. А «с нуля» перебирал даже
        то, что снято десять минут назад, — после любого обрыва приходилось
        начинать сначала.

        Теперь чекпоинт помнит время сбора каждой ссылки (`done_at`), и
        пропускаются только те, что моложе FRESH_WITHIN_DAYS (по умолчанию 2
        дня). Всё, что старше, перезапрашивается — цена обновится.

        У ссылок из старых чекпоинтов отметки времени нет: возраст неизвестен,
        поэтому считаем их устаревшими и собираем заново. Это ровно то
        поведение, которое нужно для июльских данных.
        FRESH_WITHIN_DAYS=0 возвращает прежнюю логику «пропускать всё собранное».
        """
        days = float(os.getenv("FRESH_WITHIN_DAYS", "2") or 0)
        done_at: dict = self._checkpoint.get("done_at", {}) or {}
        if days <= 0:
            return set(self._checkpoint.get("done_urls", []))
        cutoff = time.time() - days * 86400
        recent = {u for u, t in done_at.items() if isinstance(t, (int, float)) and t >= cutoff}
        stale = len(self._checkpoint.get("done_urls", [])) - len(recent)
        if stale > 0:
            logger.info("[%s] пропускаю %d свежих (моложе %g дн.), "
                        "%d устаревших пойдут заново", self.site, len(recent), days, stale)
        return recent

    def _save_progress(self, done_urls: set[str], failed_urls: set[str]) -> None:
        """Persist resume index: successfully parsed and failed URLs.

        ``failed_urls`` is what a proxy-enabled re-run (RESUME=1) should retry;
        ``done_urls`` is skipped on resume.

        Done URLs are merged with whatever is already on disk so a stale or
        concurrent writer (or a checkpoint loaded before an updated one was
        uploaded) can never shrink the recorded progress.
        """
        on_disk: set[str] = set()
        done_at: dict = {}
        if self._checkpoint_path.exists():
            try:
                prev = json.loads(self._checkpoint_path.read_text())
                on_disk = set(prev.get("done_urls", []))
                done_at = prev.get("done_at", {}) or {}
            except Exception:
                pass
        merged_done = set(done_urls) | on_disk
        # Отметку времени ставим ТОЛЬКО тем, что собрано в этом прогоне.
        # Пропущенные при возобновлении лежат в done_urls тоже, и без этой
        # проверки их «свежесть» продлевалась бы бесконечно: карточка,
        # собранная в июле, каждый прогон выглядела бы вчерашней.
        now = time.time()
        for u in set(done_urls) - getattr(self, "_initial_done", set()):
            done_at[u] = now
        self._checkpoint["done_at"] = done_at
        self._checkpoint["done_urls"] = sorted(merged_done)
        self._checkpoint["failed_urls"] = sorted(failed_urls)
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._checkpoint_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._checkpoint, ensure_ascii=False))
        tmp.replace(self._checkpoint_path)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "BaseScraper":
        return self

    def __exit__(self, *_) -> None:
        self.close()
