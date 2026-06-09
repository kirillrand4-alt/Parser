#!/usr/bin/env python3
"""Diagnose why a URL comes back as "страница-серия или нет данных".

Fetches a URL exactly the way the live checker does (same scraper, same
proxy from settings), and prints the real reason: HTTP status, body size,
whether it is a known WAF/block page, and whether a price was found.

Usage (run on the server, inside the project dir):

    python3 tools/diag_fetch.py https://www.pnevmoteh.ru/vintovoy-kompressor-comprag-av-30-10-bar
    python3 tools/diag_fetch.py URL1 URL2 ...

If the console mangles long pasted URLs, use search mode: give the site and
a slug fragment, the script finds the full URL in the sitemap itself:

    python3 tools/diag_fetch.py find aerocompressors.ru drb_30-50

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
        specs = product.specs or {}
        print(f"  характеристики: {len(specs)} шт.")
        for k, v in list(specs.items())[:40]:
            print(f"      {k}: {str(v)[:80]}")
        if len(specs) > 40:
            print(f"      … и ещё {len(specs) - 40}")
        if len(specs) < 5:
            _spec_markup_hunt(resp.content)


def _spec_markup_hunt(content: bytes) -> None:
    """Find where the characteristics actually live in the markup."""
    import re as _re
    soup = BeautifulSoup(content, "lxml")
    labels = _re.compile(
        r"^(Вид компрессора|Производительность|Максимальное давление|"
        r"Мощность двигателя|Тип привода|Масса)\b", _re.I)
    print("    — разведка разметки характеристик —")
    found = 0
    for el in soup.find_all(string=labels):
        node = el.parent
        # Show the ancestor chain: tag.class > tag.class > ...
        chain = []
        cur = node
        for _ in range(6):
            if cur is None or cur.name in ("body", "html"):
                break
            cls = ".".join(cur.get("class", [])) if cur.get("class") else ""
            chain.append(f"{cur.name}{('.' + cls) if cls else ''}")
            cur = cur.parent
        print("      метка:", str(el).strip()[:40])
        print("        цепочка:", " < ".join(chain))
        # Show the surrounding row markup (compact)
        row = node
        for _ in range(3):
            if row.parent is not None and row.parent.name not in ("body", "html"):
                row = row.parent
        html = _re.sub(r"\s+", " ", str(row))[:400]
        print("        HTML:", html)
        found += 1
        if found >= 3:
            break
    if not found:
        print("      метки характеристик в HTML не найдены — контент грузится JS-ом?")


def find_urls(site_key: str, fragment: str, env: dict[str, str]) -> list[str]:
    """Search the site's own URL listing for slugs containing fragment."""
    site_key = site_key.lstrip("www.")
    if site_key not in ALL_SCRAPERS:
        print(f"  ✗ Неизвестный сайт: {site_key}. Доступны: {', '.join(ALL_SCRAPERS)}")
        return []
    ukey = site_key.replace(".", "_").replace("-", "_").upper()
    proxy = env.get(f"PROXY__{ukey}") or env.get("PROXY", "")
    inst = ALL_SCRAPERS[site_key]()
    if proxy and not inst.client._using_proxy:
        inst.client._proxy_url = proxy
        inst.client._enable_proxy()
    inst.client._session.trust_env = False
    print(f"  получаю список URL сайта {site_key} (может занять минуту)…")
    urls: list[str] = []
    for listing in inst.discover():
        urls.extend(inst.fetch_listing(listing))
    frag = fragment.lower()
    hits = [u for u in urls if frag in u.lower()]
    print(f"  найдено {len(hits)} URL с «{fragment}» (из {len(urls)})")
    for u in hits[:10]:
        print("   ", u)
    return hits


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    env = _load_proxy_env()
    if args[0] == "find":
        if len(args) < 3:
            print("Использование: diag_fetch.py find <site> <фрагмент-слага>")
            sys.exit(1)
        hits = find_urls(args[1], args[2], env)
        # Diagnose the first few matches right away
        for url in hits[:3]:
            diagnose(url, env)
        print("=" * 78)
        return
    for url in args:
        diagnose(url, env)
    print("=" * 78)


if __name__ == "__main__":
    main()
