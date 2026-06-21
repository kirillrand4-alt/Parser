"""Generate refreshed Excel match-reports from scraped CSV data.

For each product row in a source XLSX (brand or category report), look up
current competitor prices from the in-memory price index (already built from
data/*.csv) and produce a new XLSX in the same format with updated values.

Colour semantics (same as source files):
  FFE699  yellow  — cheapest competitor in this row
  FCE4D6  salmon  — product needs manual review (copied from source)
  F2F2F2  grey    — site has no listing for this product
"""
from __future__ import annotations

import io
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

# Competitor sites — columns always appear in this order in source files
SITES = [
    "compressortyt.ru",
    "aerocompressors.ru",
    "pnevmoteh.ru",
    "pnevmo-sklad.ru",
    "v-p-k.ru",
    "rutector.ru",
]

# ── Fills ────────────────────────────────────────────────────────────────────
FILL_YELLOW = PatternFill("solid", fgColor="FFE699")   # min competitor price
FILL_SALMON = PatternFill("solid", fgColor="FCE4D6")   # needs review
FILL_GREY   = PatternFill("solid", fgColor="F2F2F2")   # no data

_NORM_DROP = re.compile(r"[^A-Z0-9]")


def _normalize_key(brand: str, name: str) -> str:
    """Same logic as models.normalize_key: ASCII-only uppercase alphanumeric."""
    combined = f"{brand} {name}".upper()
    combined = unicodedata.normalize("NFKD", combined)
    combined = combined.encode("ascii", "ignore").decode("ascii")
    return _NORM_DROP.sub("", combined)


# Generic words that inflate the key without helping matching.
_NOISE = re.compile(
    r"\b(?:vintovoy|vintovoj|porshnevoy|spiralnyj?|bezmaslyanyj?|"
    r"dizelnyj?|benzin\w+|elektrich\w+|peredvizhn\w+|kompressornyj?|"
    r"kompressor|stanciya|ustanovka|vozdushnyj?|maslyanyj?|"
    r"priamoj?|remennoj?|s|na|s|dlya)\b",
    re.IGNORECASE,
)


def _extract_brand_from_name(name: str, known_brands: list[str]) -> str:
    low = name.lower()
    for b in sorted(known_brands, key=len, reverse=True):
        if b.lower() in low:
            return b
    return ""


def _lookup_price_for_site(
    price_index: dict,
    product_name: str,
    site: str,
    known_brands: list[str],
) -> tuple[Any, str]:
    """Return (price_value, product_url) for product on site.

    price_value is float, "По запросу", or None (not found).
    Searches price_index (key → [csv_row_dicts]).
    """
    brand = _extract_brand_from_name(product_name, known_brands)
    key_full = _normalize_key(brand, product_name)

    # 1) Exact match
    rows = price_index.get(key_full, [])

    # 2) Prefix/substring match on the normalised key
    if not rows:
        for k, v in price_index.items():
            if key_full and (key_full in k or k in key_full) and len(key_full) >= 6:
                rows = v
                break

    if not rows:
        return None, ""

    site_rows = [r for r in rows if r.get("site", "") == site]
    if not site_rows:
        return None, ""

    # Prefer numeric price; fall back to price_on_request flag
    best_price = None
    best_url = ""
    for r in site_rows:
        p_raw = r.get("price", "")
        url = r.get("product_url", "")
        if p_raw:
            try:
                p = float(p_raw)
                if best_price is None or p < best_price:
                    best_price = p
                    best_url = url
            except (ValueError, TypeError):
                pass

    if best_price is not None:
        return best_price, best_url

    if any(str(r.get("price_on_request", "")) == "1" for r in site_rows):
        return "По запросу", site_rows[0].get("product_url", "")

    return None, ""


def _refresh_row(
    row_values: list[Any],
    headers: list[str],
    price_index: dict,
    known_brands: list[str],
) -> tuple[list[Any], dict[int, float | str | None]]:
    """Return (new_values_list, {col_0based: price_or_por_or_none}) for one data row."""
    new_vals = list(row_values)

    name_col = next((i for i, h in enumerate(headers) if h == "Наш товар"), None)
    # Categories sheet has "Бренд" + "Наш товар"
    if name_col is None:
        name_col = next((i for i, h in enumerate(headers) if h and "товар" in str(h).lower()), 1)

    product_name = str(new_vals[name_col]) if name_col is not None and len(new_vals) > name_col else ""
    if not product_name or product_name in ("None", ""):
        return new_vals, {}

    # Find column indices for each site
    site_col_map: dict[str, int] = {}
    for i, h in enumerate(headers):
        if h in SITES:
            site_col_map[h] = i

    if not site_col_map:
        return new_vals, {}

    # Parallel lookup for all sites in this row
    fresh: dict[int, tuple[Any, str]] = {}

    def fetch_site(site: str, col: int):
        price, url = _lookup_price_for_site(price_index, product_name, site, known_brands)
        return col, price, url

    with ThreadPoolExecutor(max_workers=len(site_col_map)) as pool:
        futs = [pool.submit(fetch_site, site, col) for site, col in site_col_map.items()]
        for f in as_completed(futs):
            col, price, url = f.result()
            fresh[col] = (price, url)

    # Write fresh prices into new_vals
    col_to_price: dict[int, Any] = {}
    for col, (price, _url) in fresh.items():
        if price is not None:
            new_vals[col] = price
        else:
            new_vals[col] = None
        col_to_price[col] = price

    # Recalculate min конк.
    min_col = next(
        (i for i, h in enumerate(headers) if h and "min" in str(h).lower()), None
    )
    delta_col = next(
        (i for i, h in enumerate(headers) if h and "Δ" in str(h)), None
    )
    our_price_col = next(
        (i for i, h in enumerate(headers) if h == "Ваша цена"), None
    )

    numeric_prices = [
        float(p) for p in col_to_price.values()
        if p is not None and p != "По запросу" and p
    ]
    min_price = min(numeric_prices) if numeric_prices else None

    if min_col is not None:
        new_vals[min_col] = min_price

    if delta_col is not None:
        our_raw = new_vals[our_price_col] if our_price_col is not None else None
        if min_price and our_raw and our_raw not in ("нет цены", None, ""):
            try:
                our_p = float(our_raw)
                new_vals[delta_col] = round((our_p - min_price) / our_p * 100, 1)
            except (ValueError, TypeError):
                new_vals[delta_col] = None
        else:
            new_vals[delta_col] = None

    return new_vals, col_to_price


def generate_report(
    source_xlsx: Path,
    price_index: dict,
    known_brands: list[str] | None = None,
) -> bytes:
    """Build a refreshed XLSX from source_xlsx, returning bytes for download."""
    if known_brands is None:
        known_brands = []

    wb_in = openpyxl.load_workbook(source_xlsx)
    wb_out = openpyxl.Workbook()
    wb_out.remove(wb_out.active)  # remove default sheet

    for sheet_name in wb_in.sheetnames:
        ws_in = wb_in[sheet_name]
        ws_out = wb_out.create_sheet(sheet_name)

        headers = [c.value for c in ws_in[1]]
        site_cols = {i for i, h in enumerate(headers) if h in SITES}

        # ── Header row ───────────────────────────────────────────────────────
        for cell in ws_in[1]:
            out = ws_out.cell(row=1, column=cell.column, value=cell.value)
            if cell.font:
                out.font = Font(bold=True)

        # Sheets without site price columns are copied as-is
        if not site_cols:
            for row in ws_in.iter_rows(min_row=2):
                for cell in row:
                    out = ws_out.cell(row=cell.row, column=cell.column, value=cell.value)
                    _copy_fill(cell, out)
            _auto_col_widths(ws_out)
            continue

        # ── Collect all data rows, refresh in parallel across rows ───────────
        data_rows = list(ws_in.iter_rows(min_row=2, values_only=False))
        orig_fills: list[list] = [
            [c.fill if (c.fill and c.fill.fill_type not in (None, "none")) else None
             for c in row]
            for row in data_rows
        ]

        results_by_idx: dict[int, tuple[list, dict]] = {}

        def process_row(idx: int, row):
            vals = [c.value for c in row]
            if not any(v for v in vals if v is not None):
                return idx, vals, {}
            new_vals, col_prices = _refresh_row(vals, headers, price_index, known_brands)
            return idx, new_vals, col_prices

        with ThreadPoolExecutor(max_workers=12) as pool:
            futs = {pool.submit(process_row, i, row): i for i, row in enumerate(data_rows)}
            for f in as_completed(futs):
                idx, new_vals, col_prices = f.result()
                results_by_idx[idx] = (new_vals, col_prices)

        # ── Write rows with colours ──────────────────────────────────────────
        for i in range(len(data_rows)):
            new_vals, col_prices = results_by_idx.get(i, ([c.value for c in data_rows[i]], {}))
            out_row = i + 2

            # Find min competitor price for yellow highlight
            numeric = [
                float(p) for p in col_prices.values()
                if p is not None and p != "По запросу"
            ]
            min_price = min(numeric) if numeric else None

            for j, val in enumerate(new_vals):
                col = j + 1
                out_cell = ws_out.cell(row=out_row, column=col, value=val)

                # Site price column → apply fresh colour logic
                if j in site_cols:
                    price = col_prices.get(j)
                    if price is None:
                        out_cell.fill = FILL_GREY
                    elif price == "По запросу":
                        pass  # no highlight
                    elif min_price is not None and abs(float(price) - min_price) < 0.5:
                        out_cell.fill = FILL_YELLOW
                else:
                    # Non-site columns: preserve original fill (salmon review marker, etc.)
                    orig = orig_fills[i][j] if j < len(orig_fills[i]) else None
                    if orig and orig.fill_type not in (None, "none"):
                        rgb = orig.fgColor.rgb if orig.fgColor else ""
                        if rgb == "00FCE4D6":
                            out_cell.fill = FILL_SALMON

        _auto_col_widths(ws_out)

    buf = io.BytesIO()
    wb_out.save(buf)
    return buf.getvalue()


def _copy_fill(src_cell, dst_cell) -> None:
    if src_cell.fill and src_cell.fill.fill_type not in (None, "none"):
        dst_cell.fill = src_cell.fill


def _auto_col_widths(ws) -> None:
    """Set column widths based on max content length (capped at 60)."""
    for col_cells in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col_cells[0].column)
        for cell in col_cells:
            if cell.value is not None:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_len + 2, 60)
