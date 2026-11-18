"""/rawfinal: mark which index codes of the Final file ("..._Final_Index_Code_to_iviva...") are in a Raw File.

Every sheet of the Final file gets a column "Found in Map Raw File ?" with Y / N next to its index codes.
The Raw File's index codes are read like /check reads a file (any sheet, the usual index code column names,
"_point" suffix dropped); if no such column is found, every cell that looks like an index code is used.
"""
import os
import re

FOUND_HEADER = "Found in Map Raw File ?"
FINAL_NAME_RE = re.compile(r"final.*index.*code|to[\s_-]*iviva", re.IGNORECASE)
HEADER_SCAN_ROWS = 5


def is_final_name(file_name):
    return bool(FINAL_NAME_RE.search(file_name or ""))


def _norm(code):
    return str(code).strip().upper()


def raw_codes(path):
    """(set of index codes, how they were found) from a Raw File of any layout."""
    import validate_index_code as vic
    import obk_validator
    log = []
    df = vic.read_input(path, log)
    if df is not None:
        df = df.loc[:, ~df.columns.duplicated(keep="first")]
        col = vic.find_check_column(df)
        if col is not None:
            values = df[col].dropna().astype(str).map(lambda x: x.split("_")[0])     # index code before the first _
            codes = {_norm(v) for v in values if v.strip() and v.strip().lower() != "nan"}
            if codes:
                return codes, f"คอลัมน์ {col}"
    # Unknown layout: take every cell that looks like an index code
    import pandas as pd
    frames = {"csv": pd.read_csv(path, dtype=str, header=None)} if path.lower().endswith(".csv") else \
        pd.read_excel(path, sheet_name=None, dtype=str, header=None)
    codes = set()
    for frame in frames.values():
        for value in frame.stack().astype(str):
            for code in obk_validator.find_codes(value.split("_")[0] if "_" in value else value):
                codes.add(_norm(code))
    return codes, "ค้นจากทุกช่องในไฟล์ (ไม่พบคอลัมน์ index codes)"


def _code_column(ws):
    """(header row, column) of the index code column in a Final sheet, or (None, None)."""
    import obk_validator
    for r in range(1, min(HEADER_SCAN_ROWS, ws.max_row) + 1):
        for cell in ws[r]:
            if cell.value and "index" in str(cell.value).lower() and "code" in str(cell.value).lower():
                return r, cell.column
    # No header naming it: the column with the most index-code-looking values
    best, best_hits = None, 0
    for col in range(1, ws.max_column + 1):
        hits = sum(1 for (v,) in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 200), min_col=col, max_col=col, values_only=True)
                   if v and obk_validator.find_codes(str(v)))
        if hits > best_hits:
            best, best_hits = col, hits
    return (1, best) if best else (None, None)


def mark_final(final_path, codes, out_path):
    """Add the Y/N column to every sheet of the Final file. Returns [{sheet, header, found, missing, no_column}]."""
    from copy import copy
    from openpyxl import load_workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    wb = load_workbook(final_path)
    report = []
    for ws in wb.worksheets:
        header_row, col = _code_column(ws)
        if not col:
            report.append({"sheet": ws.title, "no_column": True})
            continue
        headers = [c.value for c in ws[header_row]]
        if FOUND_HEADER in headers:                       # run again on an already marked file: reuse the column
            new_col = headers.index(FOUND_HEADER) + 1
        else:
            new_col = ws.max_column + 1
        head = ws.cell(header_row, new_col, FOUND_HEADER)
        ref = ws.cell(header_row, col)
        if ref.has_style:
            head.font, head.fill, head.border = copy(ref.font), copy(ref.fill), copy(ref.border)
            head.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        found = missing = 0
        for r in range(header_row + 1, ws.max_row + 1):
            value = ws.cell(r, col).value
            if value is None or not str(value).strip():
                continue
            hit = _norm(value) in codes
            found, missing = found + hit, missing + (not hit)
            cell = ws.cell(r, new_col, "Y" if hit else "N")
            cell.font = Font(bold=True, color="1F6E3C" if hit else "C00000")
            cell.alignment = Alignment(horizontal="center")
            src = ws.cell(r, col)
            if src.has_style:
                cell.border, cell.fill = copy(src.border), copy(src.fill)
        ws.column_dimensions[get_column_letter(new_col)].width = 22
        if ws.auto_filter.ref:
            ws.auto_filter.ref = ws.dimensions
        report.append({"sheet": ws.title, "header": str(ref.value or ""), "found": found, "missing": missing})
    wb.save(out_path)
    return report


def final_codes(final_path):
    from openpyxl import load_workbook
    wb = load_workbook(final_path, read_only=True)
    out = set()
    for ws in wb.worksheets:
        header_row, col = _code_column(ws)
        if col:
            for (v,) in ws.iter_rows(min_row=header_row + 1, min_col=col, max_col=col, values_only=True):
                if v is not None and str(v).strip():
                    out.add(_norm(v))
    wb.close()
    return out


def run(final_path, final_name, raw_path, raw_name, out_dir, lang="th"):
    """Mark the Final file. Returns (reply text, output path or None)."""
    t = (lambda th, en: th) if lang == "th" else (lambda th, en: en)
    codes, how = raw_codes(raw_path)
    if not codes:
        return t(f"หนูไม่พบ index codes ในไฟล์ Raw {raw_name} ค่ะ", f"I found no index codes in the Raw File {raw_name}."), None
    stem = os.path.splitext(os.path.basename(final_name))[0]
    out_path = os.path.join(out_dir, f"{stem}_RawCheck.xlsx")
    report = mark_final(final_path, codes, out_path)
    lines = [t("เทียบ Raw File กับ Final Index Code", "Raw File vs Final Index Code"),
             f"Final: {final_name}",
             f"Raw: {raw_name} ({how}, {len(codes):,} index codes {t('ไม่ซ้ำ', 'unique')})",
             t(f"เพิ่มคอลัมน์ \"{FOUND_HEADER}\" ทุกชีท", f"Added \"{FOUND_HEADER}\" to every sheet")]
    for r in report:
        if r.get("no_column"):
            lines.append(t(f"- {r['sheet']}: ไม่พบคอลัมน์ index codes (ข้าม)", f"- {r['sheet']}: no index code column (skipped)"))
        else:
            total = r["found"] + r["missing"]
            pct = f"{r['found'] / total * 100:.2f}%" if total else "-"
            lines.append(f"- {r['sheet']} ({r['header']}): Y {r['found']:,} | N {r['missing']:,} ({pct} Y)")
    raw_only = sorted(codes - final_codes(out_path))
    lines.append(t(f"index codes ใน Raw ที่ไม่มีใน Final: {len(raw_only):,}", f"Raw index codes not in the Final file: {len(raw_only):,}"))
    lines += [f"  {c}" for c in raw_only[:10]]
    if len(raw_only) > 10:
        lines.append(t(f"  และอีก {len(raw_only) - 10:,} index codes", f"  and {len(raw_only) - 10:,} more"))
    return "\n".join(lines), out_path
