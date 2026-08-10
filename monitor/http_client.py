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


def normalize_proxy(raw: str) -> str:
    """Bring a proxy string to the `scheme://user:pass@host:port` form requests needs.

    Proxy sellers hand out credentials as `host:port:user:pass`, and that is what
    gets pasted into the settings field. Feeding it to requests raises
    `InvalidURL: Failed to parse: socks5://bproxy.site:10917:DyC8yN:re...` on the
    FIRST request of every site — and InvalidURL is not a ConnectionError, so the
    retry loop in get() never sees it and the whole run dies at 0%.

    Accepted inputs (scheme optional, defaults to http):
      socks5://host:port:user:pass   → socks5://user:pass@host:port
      host:port:user:pass            → http://user:pass@host:port
      socks5h://user:pass@host:port  → unchanged (already valid)
      host:port                      → http://host:port
    Returns "" when the result still does not parse — the caller then stays on a
    direct connection instead of crashing mid-run.
    """
    from urllib.parse import quote, urlsplit

    raw = (raw or "").strip()
    if not raw:
        return ""
    scheme, sep, rest = raw.partition("://")
    if not sep:
        scheme, rest = "http", raw
    # Seller form is detected by shape, not by the absence of '@': a password
    # may itself contain '@' (host:1080:user:p@ss). Three colons + a numeric
    # second field can only be host:port:user:pass — `user:pass@host:port` has
    # two colons, so it never reaches four parts.
    parts = rest.split(":")
    if len(parts) == 4 and parts[1].isdigit():    # host:port:user:pass
        host, port, user, pw = parts
        rest = f"{user}:{pw}@{host}:{port}"
    if "@" in rest:
        creds, _, hostport = rest.rpartition("@")
        user, _, pw = creds.partition(":")
        # safe="%" keeps already-encoded creds intact instead of double-encoding
        # them; '@' or ':' inside a password would otherwise break the split.
        rest = f"{quote(user, safe='%')}:{quote(pw, safe='%')}@{hostport}"
    url = f"{scheme}://{rest}"
    try:
        parts_ = urlsplit(url)
        if not parts_.hostname or parts_.port is None:
            raise ValueError("host or port missing")
    except ValueError as exc:
        import logging as _l
        _l.getLogger(__name__).error(
            "proxy string %r is not usable (%s) — continuing DIRECT. Expected "
            "scheme://user:pass@host:port or host:port:user:pass", raw, exc)
        return ""
    return url

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    # Modern client-hint / fetch-metadata headers. Many WAFs (incl. the one
    # on pnevmoteh) flag requests that lack these as non-browser traffic.
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Cache-Control": "max-age=0",
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

        # Proxy config (optional). Global PROXY / PROXY_REFRESH, or per-site
        # PROXY__<SITE> / PROXY_REFRESH__<SITE> (dots/dashes → underscores).
        # If proxy is configured it is enabled immediately (every request goes
        # through it). Without proxy, direct access is used throughout.
        key = site_name.replace(".", "_").replace("-", "_").upper()
        self._proxy_url = normalize_proxy(
            os.getenv(f"PROXY__{key}") or os.getenv("PROXY") or "")
        self._proxy_refresh = (os.getenv(f"PROXY_REFRESH__{key}")
                               or os.getenv("PROXY_REFRESH") or "")
        self._using_proxy = False
        # Set when the proxy endpoint itself refuses/loses connections; blocks
        # re-enabling a known-dead proxy (e.g. the 500s-switch in pnevmo_sklad).
        self._proxy_dead = False
        self._proxy_fail_count = 0
        import logging as _l
        _log = _l.getLogger(__name__)
        if self._proxy_url:
            _log.info("[%s] proxy configured: %s", site_name,
                      self._proxy_url.split("@")[-1])  # hide credentials
            self._enable_proxy()
        else:
            _log.info("[%s] no proxy configured — direct access", site_name)
        # Retry knobs
        self._retry_attempts = int(os.getenv("HTTP_RETRY_ATTEMPTS", "3"))

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        delay = random.uniform(self.delay_min, self.delay_max)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request = time.monotonic()

    def _rotate_ua(self) -> None:
        self._session.headers["User-Agent"] = random.choice(USER_AGENTS)

    def _enable_proxy(self) -> None:
        # Re-normalize: webui's live price checker assigns _proxy_url directly
        # from the settings file, bypassing __init__.
        self._proxy_url = normalize_proxy(self._proxy_url)
        if not self._proxy_url or self._using_proxy or self._proxy_dead:
            return
        if self._proxy_refresh:
            try:
                requests.get(self._proxy_refresh, timeout=10)
                time.sleep(3)  # let provider rotate the exit IP
            except Exception:
                pass
        self._session.proxies.update({"http": self._proxy_url, "https": self._proxy_url})
        self._using_proxy = True
        import logging as _l
        _l.getLogger(__name__).info("[%s] switched to PROXY", self.site_name)

    def _disable_proxy(self) -> None:
        """Drop a dead proxy and continue DIRECT (endpoint refused/unreachable)."""
        self._session.proxies.clear()
        self._using_proxy = False
        self._proxy_dead = True
        import logging as _l
        _l.getLogger(__name__).warning(
            "[%s] proxy endpoint unreachable — falling back to DIRECT connection "
            "(update the proxy in settings; set PROXY_FALLBACK_DIRECT=0 to forbid "
            "direct fallback)", self.site_name)

    @staticmethod
    def _is_proxy_conn_error(exc: Exception) -> bool:
        """True when the failure is connecting to the proxy itself, not the site.

        requests raises ProxyError for HTTP proxies; for SOCKS the chain surfaces
        as a ConnectionError whose text carries the SOCKS connection class.
        """
        # InvalidURL = the proxy string itself is malformed. It is raised while
        # building the connection, so treat it as a proxy failure and fall back
        # to direct instead of killing the run.
        if isinstance(exc, (requests.exceptions.ProxyError,
                            requests.exceptions.InvalidURL)):
            return True
        text = str(exc).lower()
        return "socks" in text or "proxy" in text

    def _raw_get(self, url, params, headers, timeout, force_refresh, **kwargs):
        if force_refresh:
            with self._session.cache_disabled():
                self._throttle()
                return self._session.get(url, params=params, headers=headers,
                                         timeout=timeout, **kwargs)
        from_cache = self._session.cache.contains(url=url)
        if not from_cache:
            self._throttle()
        return self._session.get(url, params=params, headers=headers,
                                 timeout=timeout, **kwargs)

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
        # Fresh UA per request so requests don't share one fingerprint.
        self._rotate_ua()
        # Send a same-origin Referer so the request looks like in-site
        # navigation (a real user clicks through from a listing page).
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        if parts.scheme and parts.netloc:
            ref_headers = {"Referer": f"{parts.scheme}://{parts.netloc}/",
                           "Sec-Fetch-Site": "same-origin"}
            headers = {**ref_headers, **(headers or {})}
        last_exc: requests.HTTPError | None = None
        for attempt in range(self._retry_attempts):
            try:
                resp = self._raw_get(url, params, headers, timeout,
                                     force_refresh or attempt > 0, **kwargs)
            except (requests.ConnectionError,
                    requests.exceptions.InvalidURL) as exc:
                # A dead proxy endpoint would otherwise fail every request of
                # the whole run. After 2 proxy-connect failures switch this
                # client to direct access (opt out: PROXY_FALLBACK_DIRECT=0).
                if (self._using_proxy and self._is_proxy_conn_error(exc)
                        and os.getenv("PROXY_FALLBACK_DIRECT", "1") != "0"):
                    self._proxy_fail_count += 1
                    # A malformed proxy URL can never succeed — no point burning
                    # a second attempt on it, unlike a flaky endpoint.
                    if (self._proxy_fail_count >= 2
                            or isinstance(exc, requests.exceptions.InvalidURL)):
                        self._disable_proxy()
                        continue  # retry this attempt without the proxy
                raise
            self._proxy_fail_count = 0
            try:
                resp.raise_for_status()
                return resp
            except requests.HTTPError as exc:
                status = getattr(exc.response, "status_code", None)
                # 404 = page genuinely gone — skip immediately, no retry.
                if status == 404:
                    raise
                last_exc = exc
                self._rotate_ua()
                time.sleep(2 * (attempt + 1))
        raise last_exc  # type: ignore[misc]

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
