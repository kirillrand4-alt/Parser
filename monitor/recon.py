"""Quick reconnaissance: probe common feed paths for each site."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

SITES = {
    "pnevmo-sklad.ru": "https://pnevmo-sklad.ru",
    "pnevmoteh.ru": "https://pnevmoteh.ru",
    "rutector.ru": "https://rutector.ru",
    "aerocompressors.ru": "https://aerocompressors.ru",
    "v-p-k.ru": "https://v-p-k.ru",
    "compressortyt.ru": "https://compressortyt.ru",
}

FEED_PATHS = [
    "/robots.txt",
    "/sitemap.xml",
    "/yml/",
    "/yml.php",
    "/export.yml",
    "/catalog.yml",
    "/price.xml",
    "/yandex-market.xml",
    "/yandex_market.xml",
    "/yandex.xml",
    "/market.xml",
    "/upload/yandex.xml",
    "/upload/export.yml",
    "/feed/yml",
    "/feed.xml",
    "/feeds/yml.xml",
    "/export/yml",
    "/export/yandex.xml",
    "/google-merchant.xml",
    "/products.xml",
    "/offers.xml",
    # 1С-Битрикс standard catalog export locations (pnevmo-sklad/rutector/v-p-k)
    "/bitrix/catalog_export/yandex.php",
    "/bitrix/catalog_export/yandex.xml",
    "/bitrix/catalog_export/export.xml",
    "/bitrix/catalog_export/google.xml",
    "/local/catalog_export/yandex.xml",
    # Drupal Commerce / others (pnevmoteh)
    "/yml_export",
    "/sitemap_index.xml",
    "/index.php?route=feed/yandex_yml",
]

HEADERS = {
    # Use a real browser UA — some hosts 403 obvious bots even for feeds.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# A real YML feed starts with these markers near the top of the body.
_YML_MARKERS = ("<yml_catalog", "<shop>", "<offers", "<offer ")


def _looks_like_feed(text: str) -> bool:
    head = text[:2000].lower()
    return any(m in head for m in _YML_MARKERS)


def _robots_sitemaps(base_url: str, timeout: int) -> list[str]:
    """Return Sitemap: URLs declared in robots.txt (feeds are often listed)."""
    out: list[str] = []
    try:
        r = requests.get(base_url.rstrip("/") + "/robots.txt",
                         headers=HEADERS, timeout=timeout)
        for line in r.text.splitlines():
            if line.lower().startswith("sitemap:"):
                out.append(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return out


def probe_site(base_url: str, timeout: int = 12) -> dict:
    results = {}
    # Server info
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        results["server"] = r.headers.get("Server", "?")
        results["x_powered"] = r.headers.get("X-Powered-By", "")
        results["status"] = r.status_code
    except Exception as exc:
        results["head_error"] = str(exc)

    # Sitemaps declared in robots.txt
    results["robots_sitemaps"] = _robots_sitemaps(base_url, timeout)

    # Feed probes — GET (some feeds 200 only on GET), inspect body for YML.
    feeds = {}
    for path in FEED_PATHS:
        url = base_url.rstrip("/") + path
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout,
                             allow_redirects=True, stream=False)
            ct = r.headers.get("Content-Type", "")
            body_is_feed = _looks_like_feed(r.text) if r.status_code == 200 else False
            feeds[path] = {
                "status": r.status_code,
                "content_type": ct,
                "size": len(r.content),
                "is_feed": body_is_feed,
            }
        except Exception as exc:
            feeds[path] = {"error": str(exc)}

    results["feeds"] = feeds
    return results


def run_recon() -> None:
    print(f"\n{'=' * 60}")
    print(f"Recon run: {datetime.now(timezone.utc).isoformat()}")
    print(f"{'=' * 60}\n")

    for name, base in SITES.items():
        print(f"\n## {name}")
        try:
            info = probe_site(base)
        except Exception as exc:
            print(f"  ERROR: {exc}")
            continue

        print(f"  Server:     {info.get('server', '?')}")
        print(f"  X-Powered:  {info.get('x_powered', '?')}")
        print(f"  HTTP:       {info.get('status', '?')}")
        sm = info.get("robots_sitemaps", [])
        if sm:
            print(f"  robots Sitemap:")
            for s in sm:
                print(f"      {s}")
        print()

        found_feed = False
        for path, result in info.get("feeds", {}).items():
            status = result.get("status", "ERR")
            ct = result.get("content_type", "")
            error = result.get("error", "")
            size = result.get("size", 0)
            is_feed = result.get("is_feed", False)
            marker = "✓" if status == 200 else " "
            tag = ""
            if is_feed:
                tag = f"  ◀── YML-ФИД! ({size // 1024} КБ)"
                found_feed = True
            print(f"  [{marker}] {path:40s} {status} {ct[:30]}{tag}"
                  f"{' ERR: ' + error if error else ''}")
        if not found_feed:
            print(f"\n  → YML-фид НЕ найден среди проверенных путей.")
