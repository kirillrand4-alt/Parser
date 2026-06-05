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


# Strong "discontinued" markers — safe to scan across a whole page because they
# rarely appear by accident (unlike "нет в наличии" which shows up in sidebars).
_DISCONTINUED_SIGNALS = (
    "снято с производства", "снят с производства", "архивный товар", "архивная модель",
    "выведен из ассортимента", "устаревшая модель", "не производится",
)


def has_discontinued_signal(page_text: str) -> bool:
    lower = page_text.lower()
    return any(s in lower for s in _DISCONTINUED_SIGNALS)


def status_from_availability(availability: str) -> str:
    """Map a product's own availability text to a normalized status."""
    low = availability.lower()
    if "в наличии" in low or "есть" in low or "на складе" in low:
        return "в наличии"
    if "под заказ" in low or "ожидается" in low or "срок поставки" in low:
        return "под заказ"
    if "нет" in low or "распродан" in low or "отсутств" in low:
        return "нет в наличии"
    return "неизвестно"


# Known compressor / pneumatic-equipment brands, longest-first so multi-word
# brands ("Chicago Pneumatic") match before single-word substrings.
KNOWN_BRANDS = [
    # International compressor / pneumatic majors
    "Atlas Copco", "Chicago Pneumatic", "Ingersoll Rand", "Ingersoll-Rand",
    "Gardner Denver", "Pneumatech", "Comprag", "Ceccato", "Dalgakiran",
    "Mattei", "Boge", "Kaeser", "Almig", "Quincy", "Sullair", "Rotorcomp",
    "Airman", "Doosan", "Kaishan", "Worthington", "Omi", "Abac", "ABAC",
    "Fiac", "Fini", "Dali", "Berg", "ECO", "Varisco",
    # Russian / CIS brands
    "ET-Compressors", "ET Compressors", "KraftMachine", "Kraftmann",
    "Remeza", "Aircast", "Comaro", "Frosp", "Zitrek", "Voltel",
    "Mainpack", "For-Est", "Megapromtech", "Ariacom", "DENAIR", "Denair",
    "РКЗ", "Кратон", "Зубр", "Вектор", "Беламос", "Калибр",
    # General power tools
    "Fubag", "Metabo", "Wester", "Aurora", "Denzel", "Resanta",
    "Hyundai", "Patriot", "Scheppach", "Elitech", "Kraftmann",
    "Spitzenreiter", "GMP", "Lupamat", "MERAN",
]
_BRANDS_SORTED = sorted(KNOWN_BRANDS, key=len, reverse=True)


def extract_brand_from_name(name: str) -> str:
    """Find a known brand as a substring of a product name (case-insensitive)."""
    low = name.lower()
    for brand in _BRANDS_SORTED:
        if brand.lower() in low:
            return brand
    return ""


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
