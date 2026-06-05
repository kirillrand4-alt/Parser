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
    "/upload/yandex.xml",
    "/upload/export.yml",
    "/feed/yml",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PriceMonitor/1.0; +https://example.com)",
    "Accept": "text/html,application/xml,*/*",
}


def probe_site(base_url: str, timeout: int = 10) -> dict:
    results = {}
    # Server info
    try:
        r = requests.head(base_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        results["server"] = r.headers.get("Server", "?")
        results["x_powered"] = r.headers.get("X-Powered-By", "")
        results["status"] = r.status_code
    except Exception as exc:
        results["head_error"] = str(exc)

    # Feed probes
    feeds = {}
    for path in FEED_PATHS:
        url = base_url.rstrip("/") + path
        try:
            r = requests.head(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
            ct = r.headers.get("Content-Type", "")
            feeds[path] = {"status": r.status_code, "content_type": ct}
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
        print()

        for path, result in info.get("feeds", {}).items():
            status = result.get("status", "ERR")
            ct = result.get("content_type", "")
            error = result.get("error", "")
            marker = "✓" if status == 200 else " "
            is_xml = status == 200 and ("xml" in ct or path.endswith(".xml") or path.endswith(".yml"))
            print(f"  [{marker}] {path:35s} {status} {ct[:40]}{' [XML FEED!]' if is_xml else ''}{' ERR: ' + error if error else ''}")
