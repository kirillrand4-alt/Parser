"""
Build a price-comparison Excel table.

Columns:
  Бренд | Серия | Модель | normalized_key
  | Наша цена | Наша ссылка
  | [per competitor site] Цена | Ссылка | Характеристики
  | _match_type   (key / no_match)

Usage:
  python tools/make_matching.py \
      --scraped data/all_prices_*.csv \
      --our     products_export.csv \
      --out     matching_YYYYMMDD.xlsx

  # or shortest form (auto-detects latest files in data/):
  python tools/make_matching.py
"""
from __future__ import annotations

import argparse
import csv
import glob
import html
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# ── openpyxl is needed ──────────────────────────────────────────────────────
try:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("pip install openpyxl")


# ── normalisation ────────────────────────────────────────────────────────────
def norm_key(s: str) -> str:
    return re.sub(r"[^A-ZА-ЯЁ0-9]", "", s.upper())


def extract_our_brand_model(raw_name: str) -> tuple[str, str]:
    """prokompressor.ru: 'Category "Brand" Model code' → (brand, model)."""
    name = html.unescape(raw_name)
    m = re.search(r'"([^"]+)"\s+(.+)', name)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", name.strip()


def format_specs(specs_raw: str) -> str:
    """JSON specs dict → human-readable 'key: value; ...' string."""
    if not specs_raw or specs_raw.strip() in ("", "{}", "null"):
        return ""
    try:
        d = json.loads(specs_raw)
        if not isinstance(d, dict):
            return specs_raw
        parts = []
        for k, v in d.items():
            if v and str(v).strip():
                parts.append(f"{k}: {v}")
        return "; ".join(parts)
    except Exception:
        return specs_raw.strip()


# ── load scraped CSVs ────────────────────────────────────────────────────────
def load_scraped(paths: list[str]) -> dict[str, dict[str, dict]]:
    """
    Returns {normalized_key: {site: row_dict}}.
    If multiple rows share same (key, site) keep the one with highest price
    (or the latest scraped_at when prices are equal / missing).
    """
    by_key_site: dict[str, dict[str, dict]] = defaultdict(dict)

    for path in paths:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                key = row.get("normalized_key", "").strip()
                site = row.get("site", "").strip()
                if not key or not site:
                    continue
                existing = by_key_site[key].get(site)
                if existing is None:
                    by_key_site[key][site] = row
                else:
                    # prefer row with actual price
                    old_p = _to_float(existing.get("price", ""))
                    new_p = _to_float(row.get("price", ""))
                    if new_p is not None and (old_p is None or new_p > old_p):
                        by_key_site[key][site] = row
    return by_key_site


def _to_float(s: str) -> float | None:
    s = s.strip().replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


# ── load our site CSV ────────────────────────────────────────────────────────
def load_our(path: str) -> dict[str, dict]:
    """Returns {normalized_key: row_dict}."""
    result: dict[str, dict] = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh, delimiter=";"):
            raw = row.get("Название", "").strip()
            brand, model = extract_our_brand_model(raw)
            key = norm_key(brand + model)
            if not key:
                continue
            row["_brand"] = brand
            row["_model"] = model
            row["_key"] = key
            # keep cheapest / first (our site has single price)
            if key not in result:
                result[key] = row
    return result


# ── build merged table ───────────────────────────────────────────────────────
def build_table(
    scraped: dict[str, dict[str, dict]],
    our: dict[str, dict],
    competitor_sites: list[str],
) -> list[dict]:
    all_keys = set(scraped) | set(our)
    rows = []
    for key in sorted(all_keys):
        our_row = our.get(key)
        sc_sites = scraped.get(key, {})

        # meta from scraped (any site that has brand)
        brand, series, model_str = "", "", ""
        for site in competitor_sites:
            if site in sc_sites:
                r = sc_sites[site]
                brand = brand or r.get("brand", "")
                series = series or r.get("series", "")
                model_str = model_str or r.get("model", "")
                if brand and model_str:
                    break

        if our_row:
            brand = brand or our_row.get("_brand", "")
            model_str = model_str or our_row.get("_model", "")

        our_price_raw = our_row.get("Цена", "").strip() if our_row else ""
        our_price = _to_float(our_price_raw) if our_price_raw else None
        our_url = our_row.get("Ссылка", "").strip() if our_row else ""

        has_competitor = any(s in sc_sites for s in competitor_sites)
        match_type = (
            "matched" if (our_row and has_competitor)
            else "only_ours" if our_row
            else "only_competitor"
        )

        rec: dict = {
            "Бренд": brand,
            "Серия": series,
            "Модель": model_str,
            "normalized_key": key,
            "match_type": match_type,
            "Наша цена": our_price if our_price is not None else ("по запросу" if our_row else ""),
            "Наша ссылка": our_url,
        }

        for site in competitor_sites:
            prefix = site.replace(".", "_").replace("-", "_")
            if site in sc_sites:
                sr = sc_sites[site]
                p = _to_float(sr.get("price", ""))
                rec[f"{prefix}_цена"] = p if p is not None else ("по запросу" if sr.get("availability") else "")
                rec[f"{prefix}_ссылка"] = sr.get("product_url", "").strip()
                rec[f"{prefix}_хар_ки"] = format_specs(sr.get("specs", ""))
            else:
                rec[f"{prefix}_цена"] = ""
                rec[f"{prefix}_ссылка"] = ""
                rec[f"{prefix}_хар_ки"] = ""

        rows.append(rec)

    return rows


# ── Excel export ─────────────────────────────────────────────────────────────
GOLD = "FFC200"
DARK = "1A1A2E"
LIGHT_BLUE = "D6E4F7"
LIGHT_GREEN = "D6F7DC"
LIGHT_RED = "F7D6D6"
GREY = "F2F2F2"


def _fill(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color)


def _font(bold=False, color="000000", size=10) -> Font:
    return Font(bold=bold, color=color, size=size)


def _side() -> Side:
    return Side(style="thin", color="CCCCCC")


def _border() -> Border:
    s = _side()
    return Border(left=s, right=s, top=s, bottom=s)


def write_excel(rows: list[dict], competitor_sites: list[str], out_path: str) -> None:
    wb = openpyxl.Workbook()

    # ── Sheet 1: all matched ──────────────────────────────────────────────
    ws = wb.active
    ws.title = "Сравнение цен"

    # Build column list
    base_cols = ["Бренд", "Серия", "Модель", "Наша цена", "Наша ссылка"]
    comp_cols = []
    for site in competitor_sites:
        p = site.replace(".", "_").replace("-", "_")
        comp_cols += [f"{p}_цена", f"{p}_ссылка", f"{p}_хар_ки"]
    extra_cols = ["normalized_key", "match_type"]
    all_cols = base_cols + comp_cols + extra_cols

    # Header row 1: group labels (merged)
    ws.append([])  # will fill below
    # Header row 2: column names
    ws.append(all_cols)

    # Style header
    header_row = 2
    for ci, col in enumerate(all_cols, 1):
        cell = ws.cell(row=header_row, column=ci)
        cell.value = col
        cell.font = _font(bold=True, color="FFFFFF", size=10)
        cell.fill = _fill(DARK)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = _border()

    # Write data
    for ri, rec in enumerate(rows, start=header_row + 1):
        mt = rec.get("match_type", "")
        row_fill = (
            _fill(LIGHT_GREEN) if mt == "matched"
            else _fill(LIGHT_RED) if mt == "only_competitor"
            else None
        )
        for ci, col in enumerate(all_cols, 1):
            cell = ws.cell(row=ri, column=ci)
            val = rec.get(col, "")
            # hyperlink for URL columns
            if col.endswith("_ссылка") or col == "Наша ссылка":
                if val and str(val).startswith("http"):
                    cell.hyperlink = val
                    cell.value = "🔗 Ссылка"
                    cell.font = Font(color="1155CC", underline="single", size=10)
                else:
                    cell.value = ""
            else:
                cell.value = val if val != "" else ""
            cell.alignment = Alignment(vertical="top", wrap_text=(col.endswith("_хар_ки")))
            cell.border = _border()
            if row_fill and not col.endswith("_ссылка") and col != "Наша ссылка":
                cell.fill = row_fill

    # Column widths
    widths = {"Бренд": 18, "Серия": 14, "Модель": 28, "Наша цена": 12, "Наша ссылка": 10, "normalized_key": 22, "match_type": 14}
    for site in competitor_sites:
        p = site.replace(".", "_").replace("-", "_")
        widths[f"{p}_цена"] = 14
        widths[f"{p}_ссылка"] = 10
        widths[f"{p}_хар_ки"] = 40
    for ci, col in enumerate(all_cols, 1):
        ws.column_dimensions[get_column_letter(ci)].width = widths.get(col, 14)

    # Freeze header + first 3 cols
    ws.freeze_panes = ws.cell(row=header_row + 1, column=4)

    # Auto-filter
    ws.auto_filter.ref = ws.dimensions

    # ── Sheet 2: only matched (both sides) ───────────────────────────────
    ws2 = wb.create_sheet("Только совпадения")
    ws2.append(all_cols)
    for ci, col in enumerate(all_cols, 1):
        cell = ws2.cell(row=1, column=ci)
        cell.value = col
        cell.font = _font(bold=True, color="FFFFFF")
        cell.fill = _fill(DARK)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for ri, rec in enumerate((r for r in rows if r.get("match_type") == "matched"), start=2):
        for ci, col in enumerate(all_cols, 1):
            cell = ws2.cell(row=ri, column=ci)
            val = rec.get(col, "")
            if col.endswith("_ссылка") or col == "Наша ссылка":
                if val and str(val).startswith("http"):
                    cell.hyperlink = val
                    cell.value = "🔗 Ссылка"
                    cell.font = Font(color="1155CC", underline="single")
                else:
                    cell.value = ""
            else:
                cell.value = val
            cell.alignment = Alignment(vertical="top", wrap_text=col.endswith("_хар_ки"))
    for ci, col in enumerate(all_cols, 1):
        ws2.column_dimensions[get_column_letter(ci)].width = widths.get(col, 14)
    ws2.freeze_panes = ws2.cell(row=2, column=4)
    ws2.auto_filter.ref = ws2.dimensions

    wb.save(out_path)
    print(f"Saved: {out_path}")


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Build price-comparison Excel")
    parser.add_argument("--scraped", nargs="*", help="Scraped CSV file(s). Glob patterns ok.")
    parser.add_argument("--our", help="prokompressor.ru export CSV (semicolon-separated)")
    parser.add_argument("--out", help="Output .xlsx path")
    args = parser.parse_args()

    # auto-detect scraped CSVs
    scraped_paths: list[str] = []
    if args.scraped:
        for pat in args.scraped:
            scraped_paths += glob.glob(pat)
    else:
        scraped_paths = glob.glob("data/all_prices_*.csv") or glob.glob("data/prices_*.csv")
    if not scraped_paths:
        sys.exit("No scraped CSV files found. Use --scraped or put files in data/")

    # auto-detect our CSV
    our_path = args.our
    if not our_path:
        candidates = glob.glob("data/products_export*.csv") + glob.glob("products_export*.csv")
        if candidates:
            our_path = candidates[0]
        else:
            sys.exit("Our site CSV not found. Use --our path/to/products_export.csv")

    out_path = args.out or f"data/matching_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    print(f"Loading scraped: {len(scraped_paths)} file(s)...")
    scraped = load_scraped(scraped_paths)
    print(f"  Unique (key, site) combinations: {sum(len(v) for v in scraped.values())}")
    print(f"  Unique products: {len(scraped)}")

    print(f"Loading our site: {our_path}...")
    our = load_our(our_path)
    print(f"  Unique products: {len(our)}")

    # determine competitor sites order by row count
    site_counts: dict[str, int] = defaultdict(int)
    for sites_dict in scraped.values():
        for s in sites_dict:
            site_counts[s] += 1
    competitor_sites = [s for s, _ in sorted(site_counts.items(), key=lambda x: -x[1])]
    print(f"Competitor sites ({len(competitor_sites)}): {competitor_sites}")

    rows = build_table(scraped, our, competitor_sites)

    matched = sum(1 for r in rows if r["match_type"] == "matched")
    only_ours = sum(1 for r in rows if r["match_type"] == "only_ours")
    only_comp = sum(1 for r in rows if r["match_type"] == "only_competitor")
    print(f"\nResults:")
    print(f"  Matched (both sides): {matched}")
    print(f"  Only in our site:     {only_ours}")
    print(f"  Only in competitors:  {only_comp}")
    print(f"  Total rows:           {len(rows)}")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    write_excel(rows, competitor_sites, out_path)


if __name__ == "__main__":
    main()
