"""HTTP client with caching, retries, rate-limiting and realistic headers."""
from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

import requests
import requests_cache
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


class HttpClient:
    """Thin wrapper over requests with cache, retries and polite delays."""

    def __init__(
        self,
        site_name: str,
        delay_min: float = 1.0,
        delay_max: float = 2.5,
        expire_after: int = 3600 * 6,  # 6 hours
    ) -> None:
        self.site_name = site_name
        self.delay_min = delay_min
        self.delay_max = delay_max
        self._last_request = 0.0

        cache_path = CACHE_DIR / f"{site_name}.sqlite"
        self._session = requests_cache.CachedSession(
            str(cache_path),
            expire_after=expire_after,
            allowable_codes=[200],
            stale_if_error=True,
        )
        self._session.headers.update(DEFAULT_HEADERS)
        self._session.headers["User-Agent"] = random.choice(USER_AGENTS)

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        delay = random.uniform(self.delay_min, self.delay_max)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request = time.monotonic()

    @retry(
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: int = 20,
        force_refresh: bool = False,
        **kwargs: Any,
    ) -> requests.Response:
        if force_refresh:
            with self._session.cache_disabled():
                self._throttle()
                resp = self._session.get(url, params=params, headers=headers, timeout=timeout, **kwargs)
        else:
            from_cache = self._session.cache.contains(url=url)
            if not from_cache:
                self._throttle()
            resp = self._session.get(url, params=params, headers=headers, timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def check_robots(self, base_url: str, path: str) -> bool:
        """Return True if path is allowed by robots.txt (simple check)."""
        try:
            from urllib.robotparser import RobotFileParser
            rp = RobotFileParser()
            robots_url = base_url.rstrip("/") + "/robots.txt"
            resp = self.get(robots_url)
            rp.parse(resp.text.splitlines())
            return rp.can_fetch("*", base_url.rstrip("/") + path)
        except Exception:
            return True  # assume allowed on error
