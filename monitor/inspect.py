"""Diagnostics to speed up per-site recon.

  python -m monitor urls --site <name> [--n 5]    # sample product URLs
  python -m monitor inspect <url>                 # dump price/specs/availability
"""
from __future__ import annotations

import re

import requests
from bs4 import BeautifulSoup

from .sitemap import collect_product_urls
from .http_client import HttpClient

# Per-site sitemap + product-path filters used only for recon sampling.
SITE_SITEMAP: dict[str, dict] = {
    "pnevmo-sklad.ru": {
        "sitemap": "https://www.pnevmo-sklad.ru/sitemap.xml",
        "include": ["/shop/"], "exclude": ["ulyanovsk.", "/sitemap"],
    },
    "pnevmoteh.ru": {
        "sitemap": "https://www.pnevmoteh.ru/sitemap.xml",
        "include": [], "exclude": ["sitemap", "?", "/user", "/cart", "/search"],
    },
    "rutector.ru": {
        "sitemap": "https://rutector.ru/sitemap-iblock-4.xml",
        "include": ["/products/"], "exclude": ["/sitemap"],
    },
    "v-p-k.ru": {
        "sitemap": "https://www.v-p-k.ru/sitemap-iblock-248.xml",
        "include": ["/product/"], "exclude": ["/sitemap"],
    },
    "aerocompressors.ru": {
        "sitemap": "https://aerocompressors.ru/sitemap.xml",
        "include": ["/catalog/", "/product/"], "exclude": ["/sitemap"],
    },
}

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def sample_urls(site: str, n: int = 5) -> list[str]:
    cfg = SITE_SITEMAP.get(site)
    if not cfg:
        raise ValueError(f"No sitemap config for {site}")
    client = HttpClient(f"recon-{site}")
    try:
        urls = collect_product_urls(
            client, cfg["sitemap"],
            include=cfg["include"], exclude=cfg["exclude"], max_urls=n * 4,
        )
    finally:
        client.close()
    return urls[:n]


def inspect_site(site: str, n: int = 8) -> None:
    """Sample product URLs from a site's sitemap and inspect the first few."""
    urls = sample_urls(site, n)
    print(f">>> {len(urls)} sample URLs for {site}:")
    for u in urls:
        print("   ", u)
    if not urls:
        print("   (none — sitemap filter found nothing)")
        return
    print("\n" + "=" * 70)
    print(">>> Inspecting first URL:")
    print("=" * 70)
    inspect_url(urls[0])


def inspect_url(url: str) -> None:
    r = requests.get(url, headers=_HEADERS, timeout=30)
    print(f"URL: {url}")
    print(f"HTTP {r.status_code}  |  {len(r.content)} bytes\n")
    soup = BeautifulSoup(r.content, "lxml")

    h1 = soup.select_one("h1")
    print("H1:", h1.get_text(strip=True) if h1 else "—")

    print("\n=== PRICE candidates (class contains price/cost/руб) ===")
    seen = set()
    for el in soup.find_all(attrs={"class": re.compile("price|cost|цена", re.I)}):
        cls = " ".join(el.get("class", []))
        if cls in seen:
            continue
        seen.add(cls)
        txt = el.get_text(" ", strip=True)[:50]
        if txt:
            print(f"  .{cls:40s} | {txt}")
    for el in soup.select("[itemprop='price'], [data-price], meta[itemprop='price']")[:5]:
        print(f"  itemprop/data: {el.get('content') or el.get('data-price') or el.get_text(strip=True)[:40]}")

    print("\n=== AVAILABILITY candidates (class contains stock/нали/avail) ===")
    seen = set()
    for el in soup.find_all(attrs={"class": re.compile("stock|нали|avail|instock", re.I)}):
        cls = " ".join(el.get("class", []))
        if cls in seen:
            continue
        seen.add(cls)
        txt = el.get_text(" ", strip=True)[:40]
        if txt:
            print(f"  .{cls:40s} | {txt}")

    print("\n=== TABLES (label | value) ===")
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        cls = " ".join(t.get("class", [])) or "(no-class)"
        if len(rows) < 2:
            continue
        print(f"  table.{cls}  ({len(rows)} rows):")
        for tr in rows[:4]:
            cells = tr.find_all(["td", "th"])
            if len(cells) >= 2:
                print(f"     {cells[0].get_text(' ', strip=True)[:25]} | {cells[-1].get_text(' ', strip=True)[:35]}")

    print("\n=== DL lists ===")
    for dl in soup.find_all("dl")[:2]:
        cls = " ".join(dl.get("class", [])) or "(no-class)"
        dts, dds = dl.find_all("dt"), dl.find_all("dd")
        print(f"  dl.{cls}  ({len(dts)} dt / {len(dds)} dd)")
        for dt, dd in list(zip(dts, dds))[:3]:
            print(f"     {dt.get_text(' ', strip=True)[:25]} | {dd.get_text(' ', strip=True)[:35]}")
