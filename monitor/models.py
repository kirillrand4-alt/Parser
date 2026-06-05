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
_WHITESPACE = re.compile(r"\s+")


def collapse_ws(text: str) -> str:
    """Collapse every run of whitespace (incl. tabs/newlines) to one space.

    Table cells can carry embedded tabs/newlines that survive get_text() and,
    once serialized into the specs JSON, break the CSV row layout.
    """
    return _WHITESPACE.sub(" ", text).strip()


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


def parse_spec_table(rows) -> tuple[dict, bool]:
    """Parse a list of <tr> elements into a specs dict.

    Returns ``(specs, is_matrix)``. ``is_matrix`` is True only for a real
    multi-variant comparison matrix (a "series" page, e.g. one page covering a
    whole CompAir L26 family with a column per sub-model). Such tables have a
    label plus *several* variant columns, so a row with >= 4 non-empty cells is
    the signal; when >= 2 such rows are found the caller should skip the product.

    Normal "label | value" tables (2 cells) and "label | value | unit" tables
    (3 cells) are parsed: first non-empty cell is the key, the rest joined is the
    value. Empty filler cells are ignored so they cannot be mistaken for columns.
    """
    parsed: list[list[str]] = []
    for row in rows:
        cells = row.select("td, th")
        texts = [collapse_ws(c.get_text(" ", strip=True)) for c in cells]
        parsed.append([t for t in texts if t])  # keep only non-empty cells

    # A comparison matrix has several variant columns: >= 4 non-empty cells in
    # >= 2 rows. Ordinary 2-/3-column spec tables never reach this.
    if sum(1 for r in parsed if len(r) >= 4) >= 2:
        return {}, True

    specs: dict = {}
    for r in parsed:
        if len(r) >= 2:
            k = r[0].rstrip(":").strip()
            v = " ".join(r[1:])
            if k and v and k != v:
                specs[k] = v
    return specs, False


# Generic type / description words to strip when reducing a product name down to
# its bare model code (e.g. "Винтовой компрессор Atlas Copco XATS 487" → "XATS 487").
_MODEL_NOISE_WORDS = (
    "винтовой", "винтовая", "поршневой", "поршневая", "спиральный", "спиральная",
    "безмасляный", "безмасляная", "маслозаполненный", "маслозаполненная",
    "маслосмазываемый", "дизельный", "дизельная", "бензиновый", "бензиновая",
    "электрический", "электрическая", "передвижной", "передвижная", "мобильный",
    "мобильная", "стационарный", "стационарная", "промышленный", "промышленная",
    "компрессорная", "компрессорный", "компрессор", "станция", "установка",
    "воздушный", "воздушная", "масляный", "масляная", "привод", "прямой",
    "ременной", "ременная", "с", "на", "ресивере", "электродвигателем",
    "оборудование", "генератор",
)
_MODEL_NOISE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _MODEL_NOISE_WORDS) + r")\b",
    re.IGNORECASE,
)


def extract_model_from_name(name: str, brand: str = "") -> str:
    """Best-effort short model code: strip brand and generic type words.

    "Винтовой компрессор Atlas Copco XATS 487 дизельный" → "XATS 487".
    Falls back to the full name when nothing recognisable remains.
    """
    if not name:
        return ""
    s = name
    if brand:
        s = re.sub(re.escape(brand), " ", s, flags=re.IGNORECASE)
    s = _MODEL_NOISE_RE.sub(" ", s)
    # Collapse leftover punctuation/space noise
    s = re.sub(r"\s+", " ", s).strip(" -–—,.")
    return s or name


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
        # Sanitize specs (collapse embedded tabs/newlines) before serializing.
        specs = {
            collapse_ws(str(k)): collapse_ws(str(v))
            for k, v in d["specs"].items()
        }
        d["specs"] = json.dumps(specs, ensure_ascii=False)
        # Defensive: no free-text field may carry a newline/tab that would
        # break the CSV row (URLs/timestamps have none, but names/paths can).
        for key, val in d.items():
            if key != "specs" and isinstance(val, str):
                d[key] = collapse_ws(val)
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
