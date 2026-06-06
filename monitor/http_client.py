"""HTTP client with caching, retries, rate-limiting and realistic headers."""
from __future__ import annotations

import os
import random
import time
from pathlib import Path
from typing import Any

import requests
import requests_cache
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# HTTP cache can grow to many GB on a full scrape. It is purely an
# intra-session optimization (resume relies on checkpoints, not this cache),
# so it can live on ephemeral disk: set HTTP_CACHE_DIR=/content/http_cache to
# keep it off a size-limited Google Drive. Defaults to ./cache.
CACHE_DIR = Path(os.getenv("HTTP_CACHE_DIR", "cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

def _build_user_agents() -> list[str]:
    """Generate a large pool (~1000+) of realistic desktop browser UA strings
    by combining current Chrome/Firefox/Edge/Safari versions with common OS
    platforms. Far more fingerprint variety than a handful of hardcoded lines."""
    platforms = [
        "Windows NT 10.0; Win64; x64",
        "Windows NT 11.0; Win64; x64",
        "Windows NT 10.0; WOW64",
        "Macintosh; Intel Mac OS X 10_15_7",
        "Macintosh; Intel Mac OS X 13_5",
        "Macintosh; Intel Mac OS X 14_4",
        "X11; Linux x86_64",
        "X11; Ubuntu; Linux x86_64",
    ]
    # Recent-ish major versions (kept plausible; exact build suffix .0.0)
    chrome_majors = list(range(112, 134))   # 112..133
    firefox_majors = list(range(112, 134))
    # A few plausible build numbers to multiply variety per major version.
    chrome_builds = ("0.0.0", "0.6099.109", "0.6045.199", "0.5993.88")
    webkit = "AppleWebKit/537.36 (KHTML, like Gecko)"

    uas: list[str] = []
    for plat in platforms:
        for v in chrome_majors:
            for b in chrome_builds:
                # Chrome
                uas.append(f"Mozilla/5.0 ({plat}) {webkit} Chrome/{v}.{b} Safari/537.36")
            # Edge (Chromium)
            uas.append(f"Mozilla/5.0 ({plat}) {webkit} Chrome/{v}.0.0.0 Safari/537.36 "
                       f"Edg/{v}.0.0.0")
        for v in firefox_majors:
            rv = plat if "rv:" in plat else f"{plat}; rv:{v}.0"
            uas.append(f"Mozilla/5.0 ({rv}) Gecko/20100101 Firefox/{v}.0")
    # Safari on macOS (a few)
    for sv in ("16.5", "17.0", "17.4", "16.6"):
        uas.append(
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            f"AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{sv} Safari/605.1.15")
    # De-dup while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for ua in uas:
        if ua not in seen:
            seen.add(ua)
            out.append(ua)
    return out


USER_AGENTS = _build_user_agents()


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
