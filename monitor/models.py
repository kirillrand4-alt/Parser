"""Data models for scraped products."""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


_PRICE_GARBAGE = re.compile(r"[^\d]")
_NORM_KEY = re.compile(r"[^A-Z0-9]")


def clean_price(raw: str | None) -> float | None:
    """Strip all non-digit chars (including nbsp, zero-width spaces) and convert."""
    if not raw:
        return None
    digits = _PRICE_GARBAGE.sub("", raw)
    return float(digits) if digits else None


def normalize_key(brand: str, model: str) -> str:
    """Return BRAND+MODEL uppercased with all non-alphanumeric stripped."""
    combined = f"{brand} {model}".upper()
    # Transliterate Cyrillic so keys are ASCII-safe
    combined = unicodedata.normalize("NFKD", combined)
    combined = combined.encode("ascii", "ignore").decode("ascii")
    return _NORM_KEY.sub("", combined)


SERIES_STATUS_MAP: dict[str, str] = {
    # Discontinued
    "снято с производства": "снято",
    "снят с производства": "снято",
    "архивный": "снято",
    "архив": "снято",
    "выведен из ассортимента": "снято",
    "устаревшая модель": "снято",
    "устаревший": "снято",
    "не производится": "снято",
    # Order
    "под заказ": "под заказ",
    "ожидается": "под заказ",
    "срок поставки": "под заказ",
    "поставка под заказ": "под заказ",
    # Out of stock
    "нет в наличии": "нет в наличии",
    "распродано": "нет в наличии",
    "нет на складе": "нет в наличии",
    "отсутствует": "нет в наличии",
}


def detect_series_status(page_text: str) -> str:
    """Scan page text for status signals and return normalized status."""
    lower = page_text.lower()
    for signal, status in SERIES_STATUS_MAP.items():
        if signal in lower:
            return status
    return "неизвестно"


@dataclass
class Product:
    site: str
    brand: str = ""
    series: str = ""
    name: str = ""
    model: str = ""
    sku: str = ""
    price: float | None = None
    old_price: float | None = None
    discount_pct: float | None = None
    currency: str = "RUB"
    availability: str = ""
    series_status: str = "неизвестно"
    replacement_model: str = ""
    specs: dict[str, Any] = field(default_factory=dict)
    category_path: str = ""
    product_url: str = ""
    image_url: str = ""
    normalized_key: str = ""
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        if not self.normalized_key:
            self.normalized_key = normalize_key(self.brand, self.model or self.name)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["specs"] = json.dumps(d["specs"], ensure_ascii=False)
        return d

    @classmethod
    def csv_headers(cls) -> list[str]:
        return [
            "site", "brand", "series", "name", "model", "sku",
            "price", "old_price", "discount_pct", "currency",
            "availability", "series_status", "replacement_model",
            "specs", "category_path", "product_url", "image_url",
            "normalized_key", "scraped_at",
        ]
