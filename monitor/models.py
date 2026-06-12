"""Data models for scraped products."""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


# First price-looking token: digit groups with space thousands separators and
# an optional 1-2 digit decimal tail (",95" kopecks). A 3-digit tail after
# , / . would be a thousands separator, not kopecks, so it is excluded.
_PRICE_TOKEN = re.compile(r"\d+(?: \d{3})*(?:[.,]\d{1,2})?(?!\d)")
_NORM_KEY = re.compile(r"[^A-Z0-9]")
_WHITESPACE = re.compile(r"\s+")


# Unicode hyphen variants → ASCII "-". The non-breaking hyphen U+2011 shows up
# in ~550 pnevmoteh names ("F11 ‑ 10 бар") and breaks downstream regexes/search.
# En/em dashes are NOT touched — they legitimately mark ranges.
_HYPHENS = {0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2212: "-"}


def collapse_ws(text: str) -> str:
    """Collapse every run of whitespace (incl. tabs/newlines) to one space.

    Table cells can carry embedded tabs/newlines that survive get_text() and,
    once serialized into the specs JSON, break the CSV row layout. Unicode
    hyphen lookalikes are normalized to a plain "-" on the way out.
    """
    return _WHITESPACE.sub(" ", text.translate(_HYPHENS)).strip()


def clean_price(raw: str | None) -> float | None:
    """Parse the first price-looking number out of raw text.

    Handles NBSP / zero-width / thin-space thousands separators and a 1-2
    digit decimal tail (",95" kopecks). Taking only the FIRST token protects
    against markup where current and old prices live in one element (their
    digits used to concatenate into nonsense like 24984003123000).
    """
    if not raw:
        return None
    text = _WHITESPACE.sub(" ", str(raw).replace("\u200b", " "))
    m = _PRICE_TOKEN.search(text)
    if not m:
        return None
    token = m.group(0).replace(" ", "").replace(",", ".")
    try:
        return float(token)
    except ValueError:
        return None


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
    "Fiac", "Fini", "Dali", "Berg", "ECO", "Varisco", "CompAir",
    "Atmos", "Ozen", "Coaire", "Xeleron", "Comp Air",
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


# Boolean spec cells often carry an icon instead of text (a checkmark for
# "yes", a dash/minus/cross for "no"). Recognise both literal symbols and the
# typical icon class names so "Безмасляный: —" becomes "Безмасляный: нет".
_YES_TOKENS = ("check", "tick", "galka", "icon-yes", "icon_yes", "true", "plus")
_NO_TOKENS = ("minus", "icon-no", "icon_no", "cross", "close", "false", "dash")
_YES_SYMBOLS = {"✓", "✔", "+", "да", "есть"}
_NO_SYMBOLS = {"—", "–", "-", "−", "✗", "✕", "×", "нет"}


def cell_value(el) -> str:
    """Text of a spec value cell, with icon-only booleans mapped to да/нет."""
    text = collapse_ws(el.get_text(" ", strip=True))
    low = text.lower()
    if low in _YES_SYMBOLS:
        return "да"
    if low in _NO_SYMBOLS:
        return "нет"
    if text:
        return text
    # No text — look for an icon (svg/i/span/img) hinting yes/no
    blob = " ".join(
        " ".join(d.get("class", [])) + " " + (d.get("alt") or "") + " " + (d.get("src") or "")
        for d in el.find_all(True)
    ).lower()
    if any(t in blob for t in _YES_TOKENS):
        return "да"
    if any(t in blob for t in _NO_TOKENS):
        return "нет"
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
        texts = [cell_value(c) for c in cells]
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


# Keys that are clearly not product characteristics (navigation/junk captured by
# the generic harvester below).
_SPEC_JUNK_KEYS = (
    "корзина", "каталог", "доставка", "оплата", "контакты", "сравнение",
    "избранное", "отзывы", "вопрос", "статьи", "новости", "акции", "цена",
    "артикул",
)


def _spec_key_ok(k: str) -> bool:
    if not k or len(k) > 80:
        return False
    low = k.lower()
    return not any(j in low for j in _SPEC_JUNK_KEYS)


def harvest_specs(soup) -> dict:
    """Universal full-characteristics collector for a product page.

    Walks every structure that typically carries "name: value" pairs:
    - all 2-/3-column tables (via :func:`parse_spec_table`, skipping
      comparison matrices),
    - all <dl> dt/dd pairs,
    - list items / rows shaped "Название: значение".

    Used by scrapers as a fallback/enricher when the site-specific selector
    yields few items (layout changed, or specs live in another block).
    """
    specs: dict = {}

    # 1. Any table on the page (series comparison matrices are skipped)
    for table in soup.select("table"):
        rows = table.select("tr")
        if not rows:
            continue
        parsed, is_matrix = parse_spec_table(rows)
        if is_matrix:
            continue
        for k, v in parsed.items():
            if _spec_key_ok(k) and len(v) <= 300 and k not in specs:
                specs[k] = v

    # 2. Any dt/dd definition lists
    for dl in soup.select("dl"):
        for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
            k = collapse_ws(dt.get_text(" ", strip=True)).rstrip(":").strip()
            v = cell_value(dd)
            if _spec_key_ok(k) and v and len(v) <= 300 and k not in specs:
                specs[k] = v

    # 3. Paired name/value spans inside characteristic-like containers
    for item in soup.select(
        "[class*='propert'] [class*='name'], [class*='charact'] [class*='name'], "
        "[class*='spec'] [class*='name'], [class*='param'] [class*='name']"
    ):
        parent = item.parent
        if parent is None:
            continue
        val_el = parent.select_one("[class*='value'], [class*='val']")
        if val_el is None or val_el is item:
            continue
        k = collapse_ws(item.get_text(" ", strip=True)).rstrip(":").strip()
        v = cell_value(val_el)
        if _spec_key_ok(k) and v and len(v) <= 300 and k not in specs:
            specs[k] = v

    # 4. "Название: значение" list items inside spec-like blocks
    for li in soup.select(
        "[class*='charact'] li, [class*='spec'] li, [class*='param'] li, "
        "[class*='propert'] li"
    ):
        text = collapse_ws(li.get_text(" ", strip=True))
        if ":" not in text or len(text) > 200:
            continue
        k, _, v = text.partition(":")
        k, v = k.strip(), v.strip()
        if _spec_key_ok(k) and v and k not in specs:
            specs[k] = v

    return specs


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
    "оборудование", "генератор", "водяным", "впрыском", "впрыск",
    "охлаждения", "охлаждением", "воздушного", "воздушным", "винтовым",
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


# Sanity range for a single unit of compressor equipment. Values outside it
# are markup junk (an SKU read as a price, two prices concatenated) — they go
# to price_raw for diagnostics and never into the price column.
PRICE_MIN = 100.0
PRICE_MAX = 50_000_000.0


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
    price_on_request: int = 0
    price_raw: str = ""
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
        if self.price is not None and not (PRICE_MIN <= self.price <= PRICE_MAX):
            self.price_raw = f"{self.price:g}"
            self.price = None
        if self.old_price is not None and not (PRICE_MIN <= self.old_price <= PRICE_MAX):
            self.old_price = None
        if not (self.price and self.old_price and self.old_price > self.price):
            self.discount_pct = None
        # "Цена по запросу / под заказ" cards: empty price + explicit flag, so
        # the matcher can tell "no price published" from "price missing".
        if self.price is None:
            low = self.availability.lower()
            if "запрос" in low or "заказ" in low:
                self.price_on_request = 1
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
            "price_on_request", "price_raw",
            "availability", "series_status", "replacement_model",
            "specs", "category_path", "product_url", "image_url",
            "normalized_key", "scraped_at",
        ]
