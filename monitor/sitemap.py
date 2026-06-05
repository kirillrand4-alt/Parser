"""Sitemap helpers: expand sitemap-index recursively, return product URLs."""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)


def _extract_locs(xml_bytes: bytes) -> list[str]:
    """Return all <loc> values; tolerant of namespaces and malformed XML."""
    try:
        root = ET.fromstring(xml_bytes)
        locs = [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]
        if locs:
            return locs
    except ET.ParseError:
        pass
    # Regex fallback
    return [m.strip() for m in _LOC_RE.findall(xml_bytes.decode("utf-8", "ignore"))]


def collect_product_urls(
    client,
    sitemap_url: str,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    max_urls: int | None = None,
) -> list[str]:
    """Fetch a sitemap (or sitemap-index), recurse into sub-sitemaps, and return
    page URLs filtered by include/exclude substrings.

    include: keep URL only if it contains at least one of these substrings.
    exclude: drop URL if it contains any of these substrings.
    """
    include = include or []
    exclude = exclude or []
    seen: set[str] = set()
    result: list[str] = []

    def want(url: str) -> bool:
        if include and not any(s in url for s in include):
            return False
        if exclude and any(s in url for s in exclude):
            return False
        return True

    def walk(url: str, depth: int = 0) -> None:
        if depth > 3 or url in seen:
            return
        seen.add(url)
        try:
            resp = client.get(url, timeout=40)
        except Exception as exc:
            logger.warning("[sitemap] fetch error %s: %s", url, exc)
            return

        locs = _extract_locs(resp.content)
        # Heuristic: a sitemap-index contains links to other .xml sitemaps
        sub_sitemaps = [u for u in locs if u.endswith(".xml") or "sitemap" in u.lower()]
        is_index = len(sub_sitemaps) > 0 and len(sub_sitemaps) >= len(locs) * 0.5

        if is_index:
            for sub in sub_sitemaps:
                if max_urls and len(result) >= max_urls:
                    return
                walk(sub, depth + 1)
        else:
            for u in locs:
                if u.endswith(".xml"):
                    continue
                if want(u) and u not in seen:
                    result.append(u)
                    seen.add(u)
                    if max_urls and len(result) >= max_urls:
                        return

    walk(sitemap_url)
    # Deduplicate preserving order
    return list(dict.fromkeys(result))
