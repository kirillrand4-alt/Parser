#!/usr/bin/env python3
"""Diagnose why a URL comes back as "страница-серия или нет данных".

Fetches a URL exactly the way the live checker does (same scraper, same
proxy from settings), and prints the real reason: HTTP status, body size,
whether it is a known WAF/block page, and whether a price was found.

Usage (run on the server, inside the project dir):

    python3 tools/diag_fetch.py https://www.pnevmoteh.ru/vintovoy-kompressor-comprag-av-30-10-bar
    python3 tools/diag_fetch.py URL1 URL2 ...

Reads the proxy from .runtime_settings.json (the same file the web UI
saves) or from the PROXY / PROXY__<SITE> environment variables.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bs4 import BeautifulSoup  # noqa: E402
from monitor.registry import ALL_SCRAPERS  # noqa: E402

BLOCK_MARKERS = (
    "spamfirewall", "ddos-guard", "attention required!",
    "доступ запрещ", "проверка браузера", "checking your browser",
    "are you human", "request blocked", "you have been blocked",
    "вы заблокированы",
)


def _load_proxy_env() -> dict[str, str]:
    """Mirror what the web UI injects: read .runtime_settings.json."""
    env: dict[str, str] = {}
    rs = ROOT / ".runtime_settings.json"
    if rs.exists():
        try:
            data = json.loads(rs.read_text())
            if isinstance(data, dict):
                env.update({k: str(v) for k, v in data.items() if v})
        except Exception as e:
            print(f"! не смог прочитать .runtime_settings.json: {e}")
    # Real environment overrides the file
    for k, v in os.environ.items():
        if k.startswith("PROXY"):
            env[k] = v
    return env


def site_key_for(url: str) -> str | None:
    host = urlsplit(url).netloc.lstrip("www.")
    return next((k for k in ALL_SCRAPERS if host == k or host.endswith("." + k)), None)


def diagnose(url: str, env: dict[str, str]) -> None:
    print("=" * 78)
    print("URL:", url)
    site_key = site_key_for(url)
    if not site_key:
        print("  ✗ Сайт не поддерживается ни одним скрапером")
        return
    print("  сайт:", site_key)

    ukey = site_key.replace(".", "_").replace("-", "_").upper()
    proxy = env.get(f"PROXY__{ukey}") or env.get("PROXY", "")
    refresh = env.get(f"PROXY_REFRESH__{ukey}") or env.get("PROXY_REFRESH", "")
    print("  прокси:", (proxy.split("@")[-1] if proxy else "НЕТ (прямое соединение)"))

    inst = ALL_SCRAPERS[site_key]()
    if proxy and not inst.client._using_proxy:
        inst.client._proxy_url = proxy
        if refresh:
            inst.client._proxy_refresh = refresh
        inst.client._enable_proxy()
    inst.client._session.trust_env = False

    # Raw fetch (bypass cache) so we see the live response
    try:
        resp = inst.client._session.get(url, timeout=25)
    except Exception as e:
        print(f"  ✗ ОШИБКА СОЕДИНЕНИЯ: {type(e).__name__}: {str(e)[:200]}")
        print("    → прокси не подключился или сайт недоступен")
        return

    body = resp.text
    low = body.lower()
    print(f"  HTTP {resp.status_code} · {len(resp.content)} байт")

    # Show exit IP if the request went to an IP echo (sanity)
    markers = [m for m in BLOCK_MARKERS if m in low]
    if markers:
        print(f"  🚫 ПОХОЖЕ НА БЛОКИРОВКУ — найдены маркеры: {markers}")
    if resp.status_code == 403:
        print("  🚫 403 Forbidden — сайт заблокировал запрос")
    if len(resp.content) < 1500:
        print(f"  ⚠ Очень короткий ответ ({len(resp.content)} б) — обычно это заглушка/блок")
        print("    первые 300 символов:")
        print("   ", body[:300].replace("\n", " "))

    # Did the scraper find a price?
    try:
        product = inst.parse_product(url)
    except Exception as e:
        print(f"  parse_product упал: {type(e).__name__}: {str(e)[:160]}")
        product = None
    if product is None:
        print("  → parse_product вернул None (цена не найдена)")
        soup = BeautifulSoup(resp.content, "lxml")
        h1 = soup.select_one("h1")
        title = soup.select_one("title")
        print("    <title>:", (title.get_text(strip=True)[:80] if title else "нет"))
        print("    <h1>:   ", (h1.get_text(strip=True)[:80] if h1 else "нет"))
        # Hunt for the real price markup so we can fix the selector.
        print("    — поиск цены на странице —")
        import re as _re
        # 1) any element with itemprop=price / class containing 'price'
        for el in soup.select("[itemprop='price'], [class*='price'], [class*='Price'], "
                               "[class*='cost'], [class*='cena'], [id*='price']")[:12]:
            txt = el.get_text(" ", strip=True)[:60]
            content = el.get("content", "")
            cls = ".".join(el.get("class", [])) or el.get("id", "")
            print(f"      <{el.name} {cls}> content={content!r} text={txt!r}")
        # 2) raw text fragments that look like a RUB price
        price_re = _re.compile(r"[\d][\d  ]{2,}\s*(?:₽|руб|р\.)", _re.I)
        hits = price_re.findall(soup.get_text(" ", strip=True))
        if hits:
            print("      текстовые цены:", hits[:6])
        else:
            print("      ни одной цены в тексте — вероятно 'цена по запросу'")
    else:
        print(f"  ✓ ТОВАР НАЙДЕН: {product.name[:60]} — цена {product.price}")


def main() -> None:
    urls = sys.argv[1:]
    if not urls:
        print(__doc__)
        sys.exit(1)
    env = _load_proxy_env()
    for url in urls:
        diagnose(url, env)
    print("=" * 78)


if __name__ == "__main__":
    main()
