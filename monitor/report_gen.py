"""Generate refreshed Excel match-reports by re-fetching competitor pages live.

Each price cell in a source XLSX (brand or category report) carries a hyperlink
to the exact competitor product page. This module:
  1. extracts those links (extract_links),
  2. lets the caller fetch fresh prices for them (live, via the site scrapers),
  3. rebuilds the XLSX in the same format with updated prices + colours
     (generate_report).

Colour semantics (same as source files):
  FFE699  yellow  — cheapest competitor in this row
  FCE4D6  salmon  — product flagged for manual review (preserved from source)
  F2F2F2  grey    — site has no listing / price unavailable
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Callable

import openpyxl
from openpyxl.styles import PatternFill, Font
from openpyxl.utils import get_column_letter

# Competitor site columns, in the order they appear in source files.
SITES = [
    "compressortyt.ru",
    "aerocompressors.ru",
    "pnevmoteh.ru",
    "pnevmo-sklad.ru",
    "v-p-k.ru",
    "rutector.ru",
]

FILL_YELLOW = PatternFill("solid", fgColor="FFE699")   # min competitor price
FILL_SALMON = PatternFill("solid", fgColor="FCE4D6")   # needs review
FILL_GREY   = PatternFill("solid", fgColor="F2F2F2")   # no data / unavailable

# Our own-site column ("Ваша цена") links to prokompressor.ru — not refreshed.
OUR_PRICE_HEADER = "Ваша цена"


def _site_columns(headers: list) -> dict[str, int]:
    """Map competitor site name → 0-based column index for this sheet."""
    return {h: i for i, h in enumerate(headers) if h in SITES}


def extract_links(source_xlsx: Path) -> set[str]:
    """Return every competitor product URL referenced by price-cell hyperlinks.

    Only cells in the 6 competitor site columns are considered (our own
    prokompressor.ru links in 'Ваша цена' are skipped).
    """
    wb = openpyxl.load_workbook(source_xlsx)
    urls: set[str] = set()
    for ws in wb.worksheets:
        headers = [c.value for c in ws[1]]
        site_cols = set(_site_columns(headers).values())
        if not site_cols:
            continue
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                if (cell.column - 1) in site_cols and cell.hyperlink:
                    target = cell.hyperlink.target
                    if target:
                        urls.add(target.strip())
    wb.close()
    return urls


def generate_report(
    source_xlsx: Path,
    price_by_url: dict[str, Any],
    only_fetched: bool = False,
) -> bytes:
    """Rebuild the XLSX with fresh prices pulled from ``price_by_url``.

    ``price_by_url`` maps a competitor URL → one of:
      - float            (fresh numeric price)
      - "По запросу"     (page exists, price on request)
      - None / missing   (not fetched / unavailable → grey, no value)

    ``only_fetched`` controls what happens to competitor cells whose URL is NOT
    present in ``price_by_url`` (i.e. not yet checked — relevant for a partial
    file built after Stop):
      - False (default): keep the original price from the source file.
      - True:            blank the cell (→ grey), so the file shows only the
                         prices that were actually re-checked this run.

    All sheets, hyperlinks, the 'Почему сцепилось' explanation column, and the
    salmon review markers are preserved; only competitor prices, the per-row
    minimum, Δ% and the cheapest-cell highlight are recomputed.
    """
    wb_in = openpyxl.load_workbook(source_xlsx)
    wb_out = openpyxl.Workbook()
    wb_out.remove(wb_out.active)

    for sheet_name in wb_in.sheetnames:
        ws_in = wb_in[sheet_name]
        ws_out = wb_out.create_sheet(sheet_name)
        headers = [c.value for c in ws_in[1]]
        site_cols = _site_columns(headers)
        site_col_idx = set(site_cols.values())

        min_col = next((i for i, h in enumerate(headers)
                        if h and "min" in str(h).lower()), None)
        delta_col = next((i for i, h in enumerate(headers)
                          if h and "Δ" in str(h)), None)
        our_col = next((i for i, h in enumerate(headers)
                        if h == OUR_PRICE_HEADER), None)

        # Header row (bold)
        for cell in ws_in[1]:
            out = ws_out.cell(row=1, column=cell.column, value=cell.value)
            out.font = Font(bold=True)

        # Sheets without competitor columns: copy verbatim (values + fills + links)
        if not site_cols:
            for row in ws_in.iter_rows(min_row=2):
                for cell in row:
                    out = ws_out.cell(row=cell.row, column=cell.column, value=cell.value)
                    if cell.hyperlink:
                        out.hyperlink = cell.hyperlink.target
                    if cell.fill and cell.fill.fill_type not in (None, "none"):
                        out.fill = cell.fill
            _auto_col_widths(ws_out)
            continue

        for row in ws_in.iter_rows(min_row=2):
            out_row = row[0].row
            # 1) Refresh competitor price cells, collect numeric values for min
            fresh_numeric: dict[int, float] = {}
            for cell in row:
                j = cell.column - 1
                out = ws_out.cell(row=out_row, column=cell.column, value=cell.value)

                if j in site_col_idx:
                    link = cell.hyperlink.target if cell.hyperlink else None
                    if link and link in price_by_url:
                        new_price = price_by_url[link]
                        out.value = new_price if new_price is not None else None
                    elif only_fetched and link:
                        # not yet checked → blank (grey) so the partial file
                        # shows only freshly re-checked prices
                        out.value = None
                    # else: no link / not fetched → keep original value
                    # Keep the hyperlink only when the cell still shows something;
                    # an empty cell + hyperlink makes openpyxl render the URL text.
                    if link and out.value is not None:
                        out.hyperlink = link
                    val = out.value
                    if isinstance(val, (int, float)):
                        fresh_numeric[j] = float(val)
                else:
                    if cell.hyperlink and cell.value is not None:
                        out.hyperlink = cell.hyperlink.target

            min_price = min(fresh_numeric.values()) if fresh_numeric else None

            # 2) Recompute "min конк." and "Δ%"
            if min_col is not None:
                ws_out.cell(row=out_row, column=min_col + 1, value=min_price)
            if delta_col is not None:
                our_raw = row[our_col].value if our_col is not None else None
                delta = None
                if min_price and isinstance(our_raw, (int, float)) and our_raw:
                    delta = round((float(our_raw) - min_price) / float(our_raw) * 100, 1)
                ws_out.cell(row=out_row, column=delta_col + 1, value=delta)

            # 3) Colours
            for cell in row:
                j = cell.column - 1
                out = ws_out.cell(row=out_row, column=cell.column)
                if j in site_col_idx:
                    val = out.value
                    if val is None:
                        out.fill = FILL_GREY
                    elif isinstance(val, (int, float)) and min_price is not None \
                            and abs(float(val) - min_price) < 0.5:
                        out.fill = FILL_YELLOW
                    # "По запросу" / other text → no highlight
                else:
                    orig = cell.fill
                    if orig and orig.fill_type not in (None, "none"):
                        rgb = orig.fgColor.rgb if orig.fgColor else ""
                        if rgb in ("00FCE4D6", "FFFCE4D6"):
                            out.fill = FILL_SALMON

        _auto_col_widths(ws_out)

    wb_in.close()
    buf = io.BytesIO()
    wb_out.save(buf)
    return buf.getvalue()


def _auto_col_widths(ws) -> None:
    for col_cells in ws.columns:
        max_len = 0
        letter = get_column_letter(col_cells[0].column)
        for cell in col_cells:
            if cell.value is not None:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[letter].width = min(max_len + 2, 60)
