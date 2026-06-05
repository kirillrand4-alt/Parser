"""Abstract base class for all site scrapers."""
from __future__ import annotations

import json
import os
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

import requests

try:
    from tqdm.auto import tqdm
except Exception:  # tqdm optional — degrade to a no-op wrapper
    def tqdm(iterable=None, **_):  # type: ignore
        return iterable if iterable is not None else iter(())

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
        self._checkpoint_path = Path(f"cache/{self.site}.checkpoint.json")
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

        Progress is tracked in a per-site checkpoint (``done_urls`` /
        ``failed_urls``). With ``RESUME=1`` set, URLs already in ``done_urls``
        are skipped, so a run interrupted by blocking (or one re-launched once
        proxies are configured) picks up exactly where it left off and retries
        the URLs that failed.

        A live tqdm progress bar shows parsed / errors / blocked counts.
        ``position`` lets concurrent site bars stack without overwriting.
        """
        resume = os.getenv("RESUME", "").strip().lower() in ("1", "true", "yes")
        done_urls: set[str] = set(self._checkpoint.get("done_urls", [])) if resume else set()
        failed_urls: set[str] = set(self._checkpoint.get("failed_urls", [])) if resume else set()

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

    def _save_progress(self, done_urls: set[str], failed_urls: set[str]) -> None:
        """Persist resume index: successfully parsed and failed URLs.

        ``failed_urls`` is what a proxy-enabled re-run (RESUME=1) should retry;
        ``done_urls`` is skipped on resume.
        """
        self._checkpoint["done_urls"] = sorted(done_urls)
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
