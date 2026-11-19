"""/rawfinal: mark which index codes of the Final file ("..._Final_Index_Code_to_iviva...") are in a Raw File.

Every sheet of the Final file gets a column "Found in Map Raw File ?" with Y / N next to its index codes.
The Raw File's index codes are read like /check reads a file (any sheet, the usual index code column names,
"_point" suffix dropped); if no such column is found, every cell that looks like an index code is used.
"""
import os
import re

FOUND_HEADER = "Found in Map Raw File ?"
RAW_FOUND_HEADER = "Found in Final ?"
MARK_COLORS = {"Y": "1F6E3C", "N": "C00000", "-": "7F7F7F"}       # green / red / grey
FINAL_NAME_RE = re.compile(r"final.*index.*code|to[\s_-]*iviva", re.IGNORECASE)
HEADER_SCAN_ROWS = 5


def is_final_name(file_name):
    return bool(FINAL_NAME_RE.search(file_name or ""))


def _norm(code):
    return str(code).strip().upper()


def _cell_codes(frame):
    import obk_validator
    out = []
    for value in frame.stack().astype(str):
        out += [_norm(c) for c in obk_validator.find_codes(value.split("_")[0])]
    return out


def raw_entries(path):
    """({index code: sheet it was first seen in}, how they were found) from a Raw File of any layout.
    Each sheet is read on its own: the usual index code column (header row found automatically), else every cell
    that looks like an index code."""
    import pandas as pd
    import validate_index_code as vic
    entries, how = {}, []
    if path.lower().endswith(".csv"):
        sheets = {os.path.splitext(os.path.basename(path))[0]: (lambda h: pd.read_csv(path, header=h, dtype=str),
                                                                 lambda: pd.read_csv(path, header=None, dtype=str))}
    else:
        xls = pd.ExcelFile(path)
        sheets = {sh: ((lambda h, sh=sh: pd.read_excel(xls, sheet_name=sh, header=h, dtype=str)),
                       (lambda sh=sh: pd.read_excel(xls, sheet_name=sh, header=None, dtype=str)))
                  for sh in xls.sheet_names}
    for sheet, (reader, raw_reader) in sheets.items():
        df, _ = vic.read_with_header_search(reader)
        if df is None or df.empty:
            continue
        df = df.loc[:, ~df.columns.duplicated(keep="first")]
        col = vic.find_check_column(df)
        if col is not None:
            codes = [_norm(str(v).split("_")[0]) for v in df[col] if v is not None and str(v).strip().lower() not in ("", "nan")]
            how.append(f"{sheet}: คอลัมน์ {col}")
        else:
            codes = _cell_codes(raw_reader())
            if codes:
                how.append(f"{sheet}: ค้นจากทุกช่อง")
        for code in codes:
            if code:
                entries.setdefault(code, str(sheet))
    return entries, ", ".join(how) or "ไม่พบ index codes"


def raw_codes(path):
    """(set of index codes, how they were found) from a Raw File of any layout."""
    entries, how = raw_entries(path)
    return set(entries), how


def exception_note(code):
    """Why a code is N/A or an exception (shown as '-' instead of Y/N), or '' for a normal code."""
    import fas_rules
    import validate_index_code as vic
    if vic.has_allf_area(code):
        return "EXCEPTION (ALLF)"
    if vic.has_exception_suffix(code):
        return "EXCEPTION (suffix)"
    for rule in vic.SPECIAL_TYPE_OVERRIDES:
        for p in rule["patterns"]:
            if p in code:
                return f"N/A ({p})"
    if fas_rules.is_special(code, vic.get_equipment(code)):
        return fas_rules.SPECIAL_TYPE + " (FAS)"
    import compare_asset_id                      # same exempt equipment as the BIM check report (part 6)
    if compare_asset_id._is_exempt(code):
        return f"ยกเว้น ({code.split('-')[compare_asset_id.EXEMPT_PART_NO - 1]})"
    return ""


def add_raw_sheet(out_path, entries, raw_name, final_by_sheet):
    """Sheet with the Raw File's index codes and whether each is in the Final file (Y / N, '-' for N/A and exceptions)."""
    from copy import copy
    from openpyxl import load_workbook
    from openpyxl.styles import Alignment, Font
    wb = load_workbook(out_path)
    first = wb.worksheets[0]
    style = next((c for c in first[1] if c.has_style and c.value), None)
    title = "Raw File"
    while title in wb.sheetnames:
        title += "+"
    ws = wb.create_sheet(title)
    final_set = set().union(*final_by_sheet.values()) if final_by_sheet else set()
    headers = ["#", f"Index Code - Raw File ({raw_name})"[:120], "Source Sheet", RAW_FOUND_HEADER] + \
              [f"In Final: {name}" for name in final_by_sheet] + ["Note"]
    for i, h in enumerate(headers, 1):
        cell = ws.cell(1, i, h)
        if style is not None:
            cell.font, cell.fill, cell.border = copy(style.font), copy(style.fill), copy(style.border)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    counts = {"Y": 0, "N": 0, "-": 0}
    for n, (code, sheet) in enumerate(entries.items(), 1):
        note = exception_note(code)
        mark = "-" if note else ("Y" if code in final_set else "N")
        per_sheet = ["-" if note else ("Y" if code in codes else "N") for codes in final_by_sheet.values()]
        counts[mark] += 1
        ws.append([n, code, sheet, mark] + per_sheet + [note])
        for i, m in enumerate([mark] + per_sheet):
            cell = ws.cell(n + 1, 4 + i)
            cell.alignment = Alignment(horizontal="center")
            cell.font = Font(bold=True, color=MARK_COLORS[m])
    from openpyxl.utils import get_column_letter
    widths = [7, 42, 18, 18] + [20] * len(final_by_sheet) + [26]
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(out_path)
    return title, counts


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
    """Add the Y / N / - column to every sheet of the Final file. Returns [{sheet, header, found, missing, skipped, no_column}]."""
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
        found = missing = skipped = 0
        for r in range(header_row + 1, ws.max_row + 1):
            value = ws.cell(r, col).value
            if value is None or not str(value).strip():
                continue
            code = _norm(value)
            mark = "-" if exception_note(code) else ("Y" if code in codes else "N")
            found, missing, skipped = found + (mark == "Y"), missing + (mark == "N"), skipped + (mark == "-")
            cell = ws.cell(r, new_col, mark)
            cell.font = Font(bold=True, color=MARK_COLORS[mark])
            cell.alignment = Alignment(horizontal="center")
            src = ws.cell(r, col)
            if src.has_style:
                cell.border, cell.fill = copy(src.border), copy(src.fill)
        ws.column_dimensions[get_column_letter(new_col)].width = 22
        if ws.auto_filter.ref:
            ws.auto_filter.ref = ws.dimensions
        report.append({"sheet": ws.title, "header": str(ref.value or ""), "found": found, "missing": missing, "skipped": skipped})
    wb.save(out_path)
    return report


def final_codes_by_sheet(final_path):
    """{sheet name: set of its index codes} for the Final file."""
    from openpyxl import load_workbook
    wb = load_workbook(final_path, read_only=True)
    out = {}
    for ws in wb.worksheets:
        header_row, col = _code_column(ws)
        if col:
            out[ws.title] = {_norm(v) for (v,) in ws.iter_rows(min_row=header_row + 1, min_col=col, max_col=col, values_only=True)
                             if v is not None and str(v).strip() and str(v).strip() != FOUND_HEADER}
    wb.close()
    return out


def final_codes(final_path):
    return set().union(*final_codes_by_sheet(final_path).values())


def system_name(final_name):
    """'FAS' from 'FAS-C3A-Final_Index_Code_to_iviva_v.1.xlsx' ('' if the name doesn't start with a system)."""
    m = re.match(r"([A-Za-z]{2,6})[-_ ]", os.path.basename(final_name or ""))
    return m.group(1).upper() if m and m.group(1).lower() != "final" else ""


def component_name(final_name, final_set):
    """'C3A' from 'FAS-C3A-Final_...'; otherwise the most common component of the Final index codes."""
    m = re.match(r"[A-Za-z]{2,6}[-_ ]([A-Za-z0-9]{3})[-_ ]", os.path.basename(final_name or ""))
    if m and m.group(1).lower() not in ("fin",):
        return m.group(1).upper()
    from collections import Counter
    comps = Counter(c.split("-")[0] for c in final_set if c.count("-") >= 6)
    return comps.most_common(1)[0][0] if comps else ""


def write_mismatch(marked_path, out_path):
    """Only the N rows, one sheet per side: each Final sheet, then the Raw File sheet. Returns {sheet: rows}."""
    from copy import copy
    from openpyxl import Workbook, load_workbook
    from openpyxl.utils import get_column_letter
    src = load_workbook(marked_path)
    out = Workbook()
    out.remove(out.active)
    counts = {}
    for ws in src.worksheets:
        is_raw = ws.title.startswith("Raw File")
        header_row = 1 if is_raw else (_code_column(ws)[0] or 1)
        headers = [c.value for c in ws[header_row]]
        mark_name = RAW_FOUND_HEADER if is_raw else FOUND_HEADER
        if mark_name not in headers:
            continue
        mark_col = headers.index(mark_name)
        title = (f"{ws.title} (N)" if is_raw else f"Final - {ws.title} (N)")[:31]
        dst = out.create_sheet(title)
        for i, cell in enumerate(ws[header_row], 1):
            new = dst.cell(1, i, cell.value)
            if cell.has_style:
                new.font, new.fill, new.border, new.alignment = copy(cell.font), copy(cell.fill), copy(cell.border), copy(cell.alignment)
        n = 0
        for row in ws.iter_rows(min_row=header_row + 1):
            if row[mark_col].value == "N":
                n += 1
                for i, cell in enumerate(row, 1):
                    new = dst.cell(n + 1, i, cell.value)
                    if cell.has_style:
                        new.font, new.alignment = copy(cell.font), copy(cell.alignment)
        for i in range(1, len(headers) + 1):
            letter = get_column_letter(i)
            dst.column_dimensions[letter].width = ws.column_dimensions[letter].width or 14
        dst.freeze_panes = "A2"
        if n:
            dst.auto_filter.ref = dst.dimensions
        counts[title] = n
    out.save(out_path)
    return counts


def run(final_path, final_name, raw_path, raw_name, out_dir, lang="th"):
    """Mark the Final file and save the mismatches. Returns (reply text, [output paths])."""
    t = (lambda th, en: th) if lang == "th" else (lambda th, en: en)
    entries, how = raw_entries(raw_path)
    codes = set(entries)
    if not codes:
        return t(f"หนูไม่พบ index codes ในไฟล์ Raw {raw_name} ค่ะ", f"I found no index codes in the Raw File {raw_name}."), []
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
            total = r["found"] + r["missing"]          # '-' (N/A, exceptions) not counted
            pct = f" ({r['found'] / total * 100:.2f}% Y)" if total else ""
            lines.append(f"- {r['sheet']} ({r['header']}): Y {r['found']:,} | N {r['missing']:,} | - {r['skipped']:,}{pct}")
    final_by_sheet = final_codes_by_sheet(out_path)
    final_set = set().union(*final_by_sheet.values()) if final_by_sheet else set()
    sheet, counts = add_raw_sheet(out_path, entries, raw_name, final_by_sheet)
    lines.append(t(f"เพิ่มชีท \"{sheet}\": index codes ของ Raw เจอใน Final ไหม: Y {counts['Y']:,} | N {counts['N']:,} | - {counts['-']:,} (N/A และข้อยกเว้น)",
                   f"Added sheet \"{sheet}\": Raw index codes in the Final file: Y {counts['Y']:,} | N {counts['N']:,} | - {counts['-']:,} (N/A and exceptions)"))
    raw_only = sorted(c for c in codes - final_set if not exception_note(c))
    lines.append(t(f"index codes ใน Raw ที่ไม่มีใน Final (N): {len(raw_only):,}", f"Raw index codes not in the Final file (N): {len(raw_only):,}"))
    lines += [f"  {c}" for c in raw_only[:10]]
    if len(raw_only) > 10:
        lines.append(t(f"  และอีก {len(raw_only) - 10:,} index codes", f"  and {len(raw_only) - 10:,} more"))
    prefix = "".join(f"{p}-" for p in (system_name(final_name), component_name(final_name, final_set)) if p)
    mismatch_path = os.path.join(out_dir, f"{prefix}Mismatch-RawFile.xlsx")         # e.g. ACS-C3A-Mismatch-RawFile.xlsx
    mismatch = write_mismatch(out_path, mismatch_path)
    lines.append(t(f"ไฟล์ {os.path.basename(mismatch_path)}: เฉพาะ N แยกชีท ", f"{os.path.basename(mismatch_path)}: N rows only, ")
                 + " | ".join(f"{name} {n:,}" for name, n in mismatch.items()))
    return "\n".join(lines), [out_path, mismatch_path]
