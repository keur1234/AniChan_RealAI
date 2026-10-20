#!/usr/bin/env python3
"""
Asset ID Comparison Tool
Compares Asset IDs and TYPE between two Excel sheets (Sub vs BIM)

อัปเดต:
1) ถ้าไฟล์มีคอลัมน์ 'NEW TYPE' และแถวไหนมีค่าเป็น 'N/A'
   จะข้ามไม่เอาไปตรวจ แล้วแยกไปเก็บในชีท "N-A (Skipped)" ต่างหาก
2) ถ้าไฟล์มีคอลัมน์ 'validation_result' จะดึงค่ามาแมปให้ Asset ID แต่ละตัว
   แล้วโชว์ในทุกชีท (Master Table / Subsystem / BIM / N-A) + สรุปจำนวนใน Summary
"""

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import datetime
import sys
import os
import re

VR_COL = "validation_result"   # ชื่อคอลัมน์ผลตรวจจากไฟล์ input (match แบบไม่สนตัวพิมพ์)

# ── เงื่อนไขยกเว้น (ดูเฉพาะ part ที่ 6 ของ Asset ID) ──────────────────────
# แยก Asset ID ด้วย "-" แล้วดู part ที่ 6 (นับจาก 1)
# ถ้า BIM "ไม่เจอใน Subsystem" และ part 6 เป็นค่าในลิสต์นี้ → นับเป็น
# "Not found in Subsystem (Correct)" (ไม่นับเป็น defect)
EXEMPT_PART_NO = 6                       # part ที่จะเช็ค (นับจาก 1)
EXEMPT_PART_VALUES = ["FASRMX", "CONTPC", "SCUNIT", "MCUNIT", "RRDETC",'0FAAPS','TLJACK','ADSTOB','FSPEAK','EMERBG']          # ค่าที่ยกเว้น (เทียบตรงตัว ไม่สนตัวพิมพ์)


def _is_exempt(aid):
    """เช็คว่า part ที่ 6 ของ Asset ID ตรงกับค่ายกเว้นไหม
    ถ้า Asset ID มีไม่ถึง 6 part → ไม่ยกเว้น"""
    parts = str(aid).strip().split("-")
    if len(parts) < EXEMPT_PART_NO:
        return False
    p = parts[EXEMPT_PART_NO - 1].strip().upper()
    return p in {v.strip().upper() for v in EXEMPT_PART_VALUES}


def thin_border():
    s = Side(style="thin", color="CCCCCC")
    return Border(left=s, right=s, top=s, bottom=s)


def hdr_cell(ws, row, col, val, bg):
    cell = ws.cell(row, col)
    cell.value = val
    cell.font = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    cell.fill = PatternFill("solid", start_color=bg)
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    cell.border = thin_border()


def data_cell(ws, row, col, val, bg=None, align="left", bold=False, color=None):
    cell = ws.cell(row, col)
    cell.value = val
    cell.font = Font(name="Arial", size=10, bold=bold, color=color)
    if bg:
        cell.fill = PatternFill("solid", start_color=bg)
    cell.alignment = Alignment(horizontal=align, vertical="center")
    cell.border = thin_border()


def _style_tail(ws, row, col, bg=None):
    """ใส่เส้นขอบ/สีพื้นให้ช่องที่โดน merge (ช่องที่ไม่ใช่หัวมุมซ้าย) โดยไม่แตะ value
    เพราะ MergedCell ห้ามเขียน .value จะ error"""
    cell = ws.cell(row, col)
    cell.border = thin_border()
    if bg:
        cell.fill = PatternFill("solid", start_color=bg)


def _vr_style(val):
    """เดาสีของช่อง validation_result จากข้อความ
    ผ่าน→เขียว, ไม่ผ่าน→แดง, อื่นๆ/ว่าง→ไม่ใส่สี"""
    t = str(val).strip().upper()
    if t in ("", "—", "NAN", "NONE"):
        return (None, None)
    if any(k in t for k in ("PASS", "OK", "VALID", "CORRECT", "MATCH", "TRUE", "Y")):
        if not any(k in t for k in ("INVALID", "NOT ", "MISMATCH", "FAIL")):
            return ("E2EFDA", "1F6E3C")          # เขียวอ่อน / เขียวเข้ม
    if any(k in t for k in ("FAIL", "ERROR", "INVALID", "WRONG", "MISMATCH", "FALSE")):
        return ("FCE4D6", "C00000")              # แดงอ่อน / แดงเข้ม
    return (None, None)


def _read_asset_file(path, label):
    """อ่านไฟล์ Excel ชีทแรก ต้องมีคอลัมน์ 'Asset ID' และ 'TYPE'
    ถ้ามี 'NEW TYPE' จะอ่านมาเพื่อกรองแถว N/A
    ถ้ามี 'validation_result' (ไม่สนตัวพิมพ์) จะอ่านมาแมปให้ Asset ID
    คืนค่า (dataframe, has_new_type, has_validation_result)"""
    print(f"Reading {label} file: {path}...")
    # keep_default_na=False: กัน pandas แปลงข้อความ "N/A" เป็น NaN อัตโนมัติ
    # (ต้องเก็บค่า N/A ไว้ตรวจ) ส่วนช่องว่างจริงจะกลายเป็น "" ซึ่งแยกออกจาก "N/A" ได้
    df = pd.read_excel(path, sheet_name=0, keep_default_na=False)
    if "Asset ID" not in df.columns:
        raise ValueError(f"'Asset ID' column not found in {label} file: {path}")
    if "TYPE" not in df.columns:
        raise ValueError(
            f"'TYPE' column not found in {label} file: {path}\n"
            f"  Columns found: {list(df.columns)}"
        )

    has_new_type = "NEW TYPE" in df.columns          # มีคอลัมน์ NEW TYPE ไหม

    # หา validation_result แบบไม่สนตัวพิมพ์/ช่องว่าง แล้ว rename เป็นชื่อมาตรฐาน
    vr_src = None
    for c in df.columns:
        if str(c).strip().lower().replace(" ", "_") == VR_COL:
            vr_src = c
            break
    has_vr = vr_src is not None
    if has_vr and vr_src != VR_COL:
        df = df.rename(columns={vr_src: VR_COL})

    keep = ["Asset ID", "TYPE"]
    if has_new_type:
        keep.append("NEW TYPE")
    if has_vr:
        keep.append(VR_COL)
    return df[keep].copy(), has_new_type, has_vr


def _split_na(df, has_new_type, source):
    """แยกแถวที่ NEW TYPE == 'N/A' ออกไป (ข้ามไม่ตรวจ)
    คืนค่า (df_ที่เอาไปเทียบต่อ, df_ที่ถูกข้าม + คอลัมน์ Source)"""
    if not has_new_type:                             # ไม่มีคอลัมน์ NEW TYPE → ไม่ต้องแยก
        empty = df.iloc[0:0].copy()
        empty["Source"] = pd.Series(dtype=object)
        return df, empty
    # ปรับให้เทียบแบบไม่สนตัวพิมพ์/ช่องว่าง/จุด: N/A, n/a, NA, N.A., "N / A" ถือว่า N/A หมด
    nt = (df["NEW TYPE"].astype(str)
          .str.upper()
          .str.replace(r"\s+", "", regex=True)       # เอาช่องว่างออก
          .str.replace(".", "", regex=False))        # เอาจุดออก (N.A. -> NA)
    na_mask = nt.isin(["N/A", "NA"])
    df_na = df[na_mask].copy()
    df_na["Source"] = source
    df_keep = df[~na_mask].copy()
    return df_keep, df_na


def compare_asset_id(sub_file, bim_file, output_file=None):
    """
    Compare Asset IDs between Subsystem and BIM files

    Parameters:
    -----------
    sub_file : str
        Path to Subsystem Excel file (needs Asset ID column)
    bim_file : str
        Path to BIM Excel file (needs Asset ID and TYPE columns)
    output_file : str, optional
        Path to output Excel file. If None, will use "comparison_result.xlsx"
    """

    # ── ตัวเลือก ─────────────────────────────────────────────────────────
    # True  = ถ้า Asset ID ไหนถูกมาร์ก NEW TYPE=N/A (ในไฟล์ใดไฟล์หนึ่ง)
    #         ให้ตัด id นั้นออกจากการเทียบ "ทั้งสองฝั่ง" กันขึ้น Sub only/BIM only หลอก
    # False = ตัดเฉพาะแถวในไฟล์ที่มาร์ก N/A เท่านั้น (ตรงตามคำสั่งแบบตรงตัว) ← ค่าเริ่มต้น
    EXCLUDE_NA_IDS_GLOBALLY = False

    # รูปแบบ "-xx" ที่จะนับ — เจอที่ไหนก็ได้ใน Asset ID (contains ไม่ต้องอยู่ท้ายสุด)
    XX_PATTERN = r"-[Xx]{2}"
    # ────────────────────────────────────────────────────────────────────

    # ---- Read input ----
    sub, sub_has_nt, sub_has_vr = _read_asset_file(sub_file, "Subsystem")
    bim, bim_has_nt, bim_has_vr = _read_asset_file(bim_file, "BIM")
    has_vr = sub_has_vr or bim_has_vr                # มีคอลัมน์ผลตรวจอย่างน้อยฝั่งเดียว

    # ---- Get file info ----
    sub_name = os.path.basename(sub_file)
    bim_name = os.path.basename(bim_file)
    file_name = f"{sub_name} + {bim_name}"
    proc_date = datetime.date.today().strftime("%d/%m/%Y")

    # ---- Prepare data ----
    # ตัดแถวที่ Asset ID ว่าง (dropna กัน NaN, แล้วกรอง "" ที่มาจาก keep_default_na=False)
    sub = sub.dropna(subset=["Asset ID"])
    sub = sub[sub["Asset ID"].astype(str).str.strip() != ""]
    bim = bim.dropna(subset=["Asset ID"])
    bim = bim[bim["Asset ID"].astype(str).str.strip() != ""]

    # ---- แยกแถว NEW TYPE == 'N/A' ออก (ข้ามไม่ตรวจ) ----
    sub, sub_na = _split_na(sub, sub_has_nt, "Subsystem")
    bim, bim_na = _split_na(bim, bim_has_nt, "BIM")

    # ตัดซ้ำในกลุ่มที่ถูกข้าม (ยึด Asset ID) เพื่อให้นับ/โชว์ไม่ซ้ำ
    if not sub_na.empty:
        sub_na = sub_na.drop_duplicates(subset=["Asset ID"]).reset_index(drop=True)
    if not bim_na.empty:
        bim_na = bim_na.drop_duplicates(subset=["Asset ID"]).reset_index(drop=True)

    sub_skipped = len(sub_na)
    bim_skipped = len(bim_na)
    skipped = pd.concat([sub_na, bim_na], ignore_index=True)   # รวมลงชีทเดียว
    skipped_count = len(skipped)

    # (ตัวเลือก) ตัด id ที่เป็น N/A ออกจากทั้งสองฝั่ง
    if EXCLUDE_NA_IDS_GLOBALLY:
        na_ids = set(sub_na["Asset ID"]) | set(bim_na["Asset ID"])
        if na_ids:
            sub = sub[~sub["Asset ID"].isin(na_ids)].copy()
            bim = bim[~bim["Asset ID"].isin(na_ids)].copy()

    # Keep track of total and unique counts (นับหลังตัด N/A แล้ว = เฉพาะที่เอาไปเทียบ)
    sub_total = len(sub)
    bim_total = len(bim)

    # Use only unique Asset IDs
    sub = sub.drop_duplicates(subset=["Asset ID"]).reset_index(drop=True)
    bim = bim.drop_duplicates(subset=["Asset ID"]).reset_index(drop=True)

    sub_unique = len(sub)
    bim_unique = len(bim)

    # ---- นับ Asset ID ที่มี "-xx" อยู่ในโค้ด (contains, แยก Sub/BIM จากรายการที่เอาไปเทียบ) ----
    xx_re = re.compile(XX_PATTERN)
    sub_xx = sum(1 for aid in sub["Asset ID"] if xx_re.search(str(aid)))
    bim_xx = sum(1 for aid in bim["Asset ID"] if xx_re.search(str(aid)))
    xx_count = sub_xx + bim_xx

    ids_sub = set(sub["Asset ID"])
    ids_bim = set(bim["Asset ID"])
    sub_type_map = dict(zip(sub["Asset ID"], sub["TYPE"]))
    bim_type_map = dict(zip(bim["Asset ID"], bim["TYPE"]))

    # ---- แมป validation_result ให้ Asset ID แต่ละตัว (ถ้าไฟล์นั้นมีคอลัมน์) ----
    sub_vr_map = dict(zip(sub["Asset ID"], sub[VR_COL])) if sub_has_vr else {}
    bim_vr_map = dict(zip(bim["Asset ID"], bim[VR_COL])) if bim_has_vr else {}

    def _vr(map_, aid):
        """ดึงค่า validation_result ของ Asset ID ตัวนั้น ไม่มี/ว่าง → '—'"""
        v = map_.get(aid, "")
        s = str(v).strip()
        return s if s and s.lower() != "nan" else "—"

    # ---- Subsystem records ----
    sub_records = []
    for aid in sub["Asset ID"]:
        found_bim = aid in ids_bim
        is_typeA = sub_type_map.get(aid) == "TYPE A"  # TYPE จาก Sub เอง
        sub_records.append((aid, found_bim, is_typeA, _vr(sub_vr_map, aid)))

    sub_records_count = len(sub_records)
    sub_correct_total = sum(r[2] for r in sub_records)
    sub_matched = [r for r in sub_records if r[1]]
    sub_notfound = [r for r in sub_records if not r[1]]

    sub_summary_rows = [
        ("Total", sub_correct_total, sub_records_count-sub_correct_total, sub_records_count),
        ("Matched with BIM", sum(r[2] for r in sub_matched), len(sub_matched)-sum(r[2] for r in sub_matched), len(sub_matched)),
        ("Not found in BIM", sum(r[2] for r in sub_notfound), len(sub_notfound)-sum(r[2] for r in sub_notfound), len(sub_notfound)),
    ]

    # ---- BIM records ----
    bim_list = list(bim["Asset ID"])
    bim_records_count = len(bim)
    bim_correct_total = (bim["TYPE"]=="TYPE A").sum()
    matched_mask = bim["Asset ID"].isin(ids_sub)
    notmatched_mask = ~matched_mask

    # แยก Not found in Subsystem → Correct (อยู่ในลิสต์ยกเว้น) / Defect (ที่เหลือ)
    exempt_mask = bim["Asset ID"].apply(_is_exempt)
    nf_correct_mask = notmatched_mask & exempt_mask
    nf_defect_mask = notmatched_mask & ~exempt_mask

    def _bim_row(label, mask):
        """สร้างแถวสรุป BIM: (label, TYPE A, ไม่ใช่ TYPE A, รวม)"""
        return (label,
                int((bim.loc[mask, "TYPE"] == "TYPE A").sum()),
                int((bim.loc[mask, "TYPE"] != "TYPE A").sum()),
                int(mask.sum()))

    bim_summary_rows = [
        ("Total", int(bim_correct_total), int(bim_records_count-bim_correct_total), bim_records_count),
        _bim_row("Matched with Subsystem", matched_mask),
        _bim_row("Not found in Subsystem (Correct)", nf_correct_mask),
        _bim_row("Not found in Subsystem (Defect)", nf_defect_mask),
    ]

    # ---- Master records (รวมทุก Asset ID จากทั้ง Sub และ BIM) ----
    all_ids = ids_sub | ids_bim          # union ทั้งหมด
    master_records = []
    for aid in all_ids:
        in_sub = aid in ids_sub
        in_bim = aid in ids_bim
        if in_sub and in_bim:
            status = "Both"              # เจอทั้งคู่
        elif in_sub:
            status = "Sub only"          # เจอเฉพาะ Sub
        else:
            status = "BIM only"          # เจอเฉพาะ BIM
        type_sub = sub_type_map.get(aid, "—")   # ถ้าไม่มีใน Sub ใส่ขีด
        type_bim = bim_type_map.get(aid, "—")   # ถ้าไม่มีใน BIM ใส่ขีด
        vr_sub = _vr(sub_vr_map, aid) if in_sub else "—"
        vr_bim = _vr(bim_vr_map, aid) if in_bim else "—"
        master_records.append((aid, status, type_sub, type_bim, vr_sub, vr_bim))

    # เรียง: Both -> Sub only -> BIM only แล้วเรียงตาม Asset ID ในแต่ละกลุ่ม
    status_order = {"Both": 0, "Sub only": 1, "BIM only": 2}
    master_records.sort(key=lambda r: (status_order[r[1]], str(r[0])))

    # นับจำนวนแต่ละสถานะไว้โชว์หัวชีท
    n_both = sum(1 for r in master_records if r[1] == "Both")
    n_sub_only = sum(1 for r in master_records if r[1] == "Sub only")
    n_bim_only = sum(1 for r in master_records if r[1] == "BIM only")

    # ---- สรุปจำนวนแต่ละค่าของ validation_result (Sub / BIM) ----
    vr_summary_rows = []
    if has_vr:
        vr_vals = sorted({v for v in list(sub_vr_map.values()) + list(bim_vr_map.values())
                          if str(v).strip() and str(v).strip().lower() != "nan"},
                         key=lambda x: str(x))
        for v in vr_vals:
            n_s = sum(1 for aid in ids_sub if _vr(sub_vr_map, aid) == str(v).strip())
            n_b = sum(1 for aid in ids_bim if _vr(bim_vr_map, aid) == str(v).strip())
            vr_summary_rows.append((str(v).strip(), n_s, n_b, n_s + n_b))
        # แถวที่ไม่มีค่า (blank)
        n_s_blank = sum(1 for aid in ids_sub if _vr(sub_vr_map, aid) == "—")
        n_b_blank = sum(1 for aid in ids_bim if _vr(bim_vr_map, aid) == "—")
        if n_s_blank or n_b_blank:
            vr_summary_rows.append(("(no value)", n_s_blank, n_b_blank, n_s_blank + n_b_blank))

    # ---- Create workbook ----
    if output_file is None:
        date_str = datetime.date.today().strftime("%Y%m%d")
        output_file = f"comparison_result_{date_str}.xlsx"

    print(f"Creating {output_file}...")
    wb = Workbook()

    # ============================================================
    # SHEET 1: Summary
    # ============================================================
    ws = wb.active
    ws.title = "Summary"
    ws.sheet_view.showGridLines = True

    ws.merge_cells("B2:E2")
    ws["B2"].value = "Asset ID Comparison Report"
    ws["B2"].font = Font(name="Arial", bold=True, size=14, color="1F4E79")
    ws["B2"].alignment = Alignment(horizontal="center")
    ws.row_dimensions[2].height = 24

    data_cell(ws, 4, 2, "File Name", None, bold=True)
    ws.merge_cells("C4:E4")
    data_cell(ws, 4, 3, file_name, None)

    data_cell(ws, 5, 2, "Processing Date", None, bold=True)
    data_cell(ws, 5, 3, proc_date, None)

    data_cell(ws, 6, 2, "Subsystem Data", None, bold=True)
    ws.merge_cells("C6:E6")
    data_cell(ws, 6, 3, f"Total: {sub_total}  |  Unique: {sub_unique}", None)

    data_cell(ws, 7, 2, "BIM Data", None, bold=True)
    ws.merge_cells("C7:E7")
    data_cell(ws, 7, 3, f"Total: {bim_total}  |  Unique: {bim_unique}", None)

    # แถวสรุปเสริม (ต่อจาก BIM Data ที่แถว 7) โชว์เฉพาะรายการที่มีค่า แล้วค่อยขยับตารางลง
    info_row = 8
    if skipped_count > 0:                              # จำนวนแถวที่ข้ามเพราะ NEW TYPE=N/A
        data_cell(ws, info_row, 2, "Skipped (NEW TYPE=N/A)", None, bold=True, color="C00000")
        ws.merge_cells(f"C{info_row}:E{info_row}")
        data_cell(ws, info_row, 3,
                  f"Sub: {sub_skipped}  |  BIM: {bim_skipped}  |  Total: {skipped_count}",
                  None, color="C00000")
        info_row += 1
    if xx_count > 0:                                   # จำนวน Asset ID ที่เข้ารูปแบบ -xx
        data_cell(ws, info_row, 2, "-xx found (contains)", None, bold=True, color="C00000")
        ws.merge_cells(f"C{info_row}:E{info_row}")
        data_cell(ws, info_row, 3,
                  f"Sub: {sub_xx}  |  BIM: {bim_xx}  |  Total: {xx_count}",
                  None, color="C00000")
        info_row += 1
    if has_vr:                                         # บอกว่าเจอคอลัมน์ผลตรวจจากฝั่งไหน
        srcs = []
        if sub_has_vr: srcs.append("Sub")
        if bim_has_vr: srcs.append("BIM")
        data_cell(ws, info_row, 2, "validation_result", None, bold=True, color="1F4E79")
        ws.merge_cells(f"C{info_row}:E{info_row}")
        data_cell(ws, info_row, 3, f"found in: {', '.join(srcs)}", None, color="1F4E79")
        info_row += 1
    r0 = info_row + 1                                  # เว้น 1 แถวก่อนตาราง Subsystem Summary

    # Subsystem Summary table
    ws.merge_cells(start_row=r0, start_column=2, end_row=r0, end_column=5)
    ws.cell(r0,2).value = "Subsystem Summary"
    ws.cell(r0,2).font = Font(name="Arial", bold=True, size=12, color="FFFFFF")
    ws.cell(r0,2).fill = PatternFill("solid", start_color="1F4E79")
    ws.cell(r0,2).alignment = Alignment(horizontal="left")
    ws.row_dimensions[r0].height = 20

    hdr_row = r0 + 1
    headers = ["Result", "Correct", "Wrong", "Total"]
    for c, h in enumerate(headers, 2):
        hdr_cell(ws, hdr_row, c, h, "1F6E3C")
    ws.row_dimensions[hdr_row].height = 20

    for i, (label, corr, wrong, total) in enumerate(sub_summary_rows, hdr_row+1):
        bg = "F2F2F2" if (i-hdr_row)%2==0 else "FFFFFF"
        bold = (label=="Total")
        data_cell(ws, i, 2, label, bg, bold=bold)
        data_cell(ws, i, 3, corr, bg, "center", bold=bold)
        data_cell(ws, i, 4, wrong, bg, "center", bold=bold)
        data_cell(ws, i, 5, total, bg, "center", bold=bold)

    # BIM Summary table
    r1 = hdr_row + len(sub_summary_rows) + 3
    ws.merge_cells(start_row=r1, start_column=2, end_row=r1, end_column=5)
    ws.cell(r1,2).value = "BIM Summary"
    ws.cell(r1,2).font = Font(name="Arial", bold=True, size=12, color="FFFFFF")
    ws.cell(r1,2).fill = PatternFill("solid", start_color="1F4E79")
    ws.cell(r1,2).alignment = Alignment(horizontal="left")
    ws.row_dimensions[r1].height = 20

    hdr_row2 = r1 + 1
    for c, h in enumerate(headers, 2):
        hdr_cell(ws, hdr_row2, c, h, "1F4E79")
    ws.row_dimensions[hdr_row2].height = 20

    for i, (label, corr, wrong, total) in enumerate(bim_summary_rows, hdr_row2+1):
        bg = "F2F2F2" if (i-hdr_row2)%2==0 else "FFFFFF"
        bold = (label=="Total")
        data_cell(ws, i, 2, label, bg, bold=bold)
        data_cell(ws, i, 3, corr, bg, "center", bold=bold)
        data_cell(ws, i, 4, wrong, bg, "center", bold=bold)
        data_cell(ws, i, 5, total, bg, "center", bold=bold)

    # Master Table Summary table (สรุปสถานะการ match ทั้งหมด)
    r2 = hdr_row2 + len(bim_summary_rows) + 3
    ws.merge_cells(start_row=r2, start_column=2, end_row=r2, end_column=5)
    ws.cell(r2,2).value = "Master Table Summary"
    ws.cell(r2,2).font = Font(name="Arial", bold=True, size=12, color="FFFFFF")
    ws.cell(r2,2).fill = PatternFill("solid", start_color="1F4E79")
    ws.cell(r2,2).alignment = Alignment(horizontal="left")
    ws.row_dimensions[r2].height = 20

    hdr_row3 = r2 + 1
    # หัวตาราง: Status | Description (รวม C:D) | Count
    hdr_cell(ws, hdr_row3, 2, "Status", "404040")
    ws.merge_cells(start_row=hdr_row3, start_column=3, end_row=hdr_row3, end_column=4)
    hdr_cell(ws, hdr_row3, 3, "Description", "404040")   # เขียนที่ช่องหัวมุมซ้าย (C)
    _style_tail(ws, hdr_row3, 4, "404040")               # ช่อง D ที่โดน merge ใส่แค่ขอบ/สี
    hdr_cell(ws, hdr_row3, 5, "Count", "404040")
    ws.row_dimensions[hdr_row3].height = 20

    master_summary_rows = [
        ("Both",     "In Sub & BIM",      n_both,     "E2EFDA", "1F6E3C"),
        ("Sub only", "In Subsystem only", n_sub_only, "FCE4D6", "C05C00"),
        ("BIM only", "In BIM only",       n_bim_only, "DDEBF7", "1F4E79"),
    ]
    for k, (label, desc, cnt, sbg, sfg) in enumerate(master_summary_rows):
        rr = hdr_row3 + 1 + k
        data_cell(ws, rr, 2, label, sbg, "left", bold=True, color=sfg)
        ws.merge_cells(start_row=rr, start_column=3, end_row=rr, end_column=4)
        data_cell(ws, rr, 3, desc, None)        # เขียนที่ช่อง C (หัวมุมซ้าย)
        _style_tail(ws, rr, 4, None)            # ช่อง D ใส่แค่ขอบ
        data_cell(ws, rr, 5, cnt, None, "center", bold=True)
    # แถวรวมทั้งหมด
    rr_total = hdr_row3 + 1 + len(master_summary_rows)
    data_cell(ws, rr_total, 2, "Total (unique)", "F2F2F2", "left", bold=True)
    ws.merge_cells(start_row=rr_total, start_column=3, end_row=rr_total, end_column=4)
    data_cell(ws, rr_total, 3, "", "F2F2F2")
    _style_tail(ws, rr_total, 4, "F2F2F2")
    data_cell(ws, rr_total, 5, len(master_records), "F2F2F2", "center", bold=True)

    # Validation Result Summary table (นับจำนวนแต่ละค่าของ validation_result)
    if vr_summary_rows:
        r3 = rr_total + 3
        ws.merge_cells(start_row=r3, start_column=2, end_row=r3, end_column=5)
        ws.cell(r3,2).value = "Validation Result Summary"
        ws.cell(r3,2).font = Font(name="Arial", bold=True, size=12, color="FFFFFF")
        ws.cell(r3,2).fill = PatternFill("solid", start_color="1F4E79")
        ws.cell(r3,2).alignment = Alignment(horizontal="left")
        ws.row_dimensions[r3].height = 20

        hdr_row4 = r3 + 1
        for c, h in enumerate(["validation_result", "Sub", "BIM", "Total"], 2):
            hdr_cell(ws, hdr_row4, c, h, "7F5F00")
        ws.row_dimensions[hdr_row4].height = 20

        for k, (val, n_s, n_b, n_t) in enumerate(vr_summary_rows):
            rr = hdr_row4 + 1 + k
            v_bg, v_fg = _vr_style(val)
            data_cell(ws, rr, 2, val, v_bg, "left", bold=True, color=v_fg)
            data_cell(ws, rr, 3, n_s, None, "center")
            data_cell(ws, rr, 4, n_b, None, "center")
            data_cell(ws, rr, 5, n_t, None, "center", bold=True)

    ws.column_dimensions["A"].width = 2
    ws.column_dimensions["B"].width = 34       # กว้างพอสำหรับ "Not found in Subsystem (Correct)"
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 12
    ws.column_dimensions["E"].width = 12

    # ============================================================
    # SHEET 2: Master Table (รวมทุก Asset ID + สถานะ + validation_result)
    # ============================================================
    ws_m = wb.create_sheet("Master Table", 1)   # แทรกเป็นชีทที่ 2 (หลัง Summary)
    ws_m.sheet_view.showGridLines = False

    cols_m = [("#", "404040", 5),
              ("Index Code", "1F4E79", 42),
              ("Status", "404040", 14),
              ("TYPE (Sub)", "1F6E3C", 14),
              ("TYPE (BIM)", "C05C00", 14)]
    if has_vr:                                   # ต่อท้ายเฉพาะตอนมีคอลัมน์ผลตรวจ
        cols_m += [("validation_result (Sub)", "7F5F00", 24),
                   ("validation_result (BIM)", "7F5F00", 24)]
    last_col_m = get_column_letter(len(cols_m))

    # แถวหัวเรื่อง + สรุปจำนวนแต่ละสถานะแบบ inline
    ws_m.merge_cells(f"A1:{last_col_m}1")
    ws_m["A1"].value = (f"Master Table — All Asset IDs        "
                        f"Both: {n_both}   |   Sub only: {n_sub_only}   |   "
                        f"BIM only: {n_bim_only}   |   Total: {len(master_records)}")
    ws_m["A1"].font = Font(name="Arial", bold=True, size=11, color="FFFFFF")
    ws_m["A1"].fill = PatternFill("solid", start_color="1F4E79")
    ws_m["A1"].alignment = Alignment(horizontal="left", vertical="center")
    ws_m.row_dimensions[1].height = 22

    for c, (h, bg, w) in enumerate(cols_m, 1):
        hdr_cell(ws_m, 2, c, h, bg)
        ws_m.column_dimensions[get_column_letter(c)].width = w
    ws_m.row_dimensions[2].height = 20

    # สีพื้น/สีตัวอักษรของช่อง Status ตามแต่ละสถานะ
    status_style = {
        "Both":     ("E2EFDA", "1F6E3C"),   # เขียวอ่อน / เขียวเข้ม
        "Sub only": ("FCE4D6", "C05C00"),   # ส้มอ่อน / ส้มเข้ม
        "BIM only": ("DDEBF7", "1F4E79"),   # ฟ้าอ่อน / น้ำเงินเข้ม
    }

    for i, (aid, status, type_sub, type_bim, vr_sub, vr_bim) in enumerate(master_records, 1):
        row = i + 2
        stripe = "F2F2F2" if i % 2 == 0 else None   # สลับสีแถว
        data_cell(ws_m, row, 1, i, stripe, "center")
        data_cell(ws_m, row, 2, aid, stripe)
        s_bg, s_fg = status_style[status]            # ช่อง Status ใช้สีเฉพาะ
        data_cell(ws_m, row, 3, status, s_bg, "center", bold=True, color=s_fg)
        data_cell(ws_m, row, 4, type_sub, stripe, "center")
        data_cell(ws_m, row, 5, type_bim, stripe, "center")
        if has_vr:
            for off, vr in ((6, vr_sub), (7, vr_bim)):
                v_bg, v_fg = _vr_style(vr)           # ผ่าน=เขียว / ไม่ผ่าน=แดง
                data_cell(ws_m, row, off, vr, v_bg or stripe, "center",
                          bold=bool(v_fg), color=v_fg)

    ws_m.freeze_panes = "A3"

    # ============================================================
    # SHEET 3: N-A (Skipped)  — แถวที่ NEW TYPE=N/A ไม่เอาไปตรวจ
    #   สร้างเฉพาะตอนมีรายการที่ถูกข้าม วางไว้หลัง Master Table
    # ============================================================
    if skipped_count > 0:
        ws_na = wb.create_sheet("N-A (Skipped)", 2)   # แทรกเป็นชีทที่ 3
        ws_na.sheet_view.showGridLines = False

        cols_na = [("#", "404040", 5),
                   ("Index Code", "1F4E79", 42),
                   ("TYPE", "1F6E3C", 14),
                   ("NEW TYPE", "C05C00", 14)]
        if has_vr:
            cols_na.append(("validation_result", "7F5F00", 24))
        cols_na.append(("Source", "7F7F7F", 14))
        last_col_na = get_column_letter(len(cols_na))

        ws_na.merge_cells(f"A1:{last_col_na}1")
        ws_na["A1"].value = (f"Skipped — NEW TYPE = N/A (ข้ามไม่ตรวจ)        "
                             f"Sub: {sub_skipped}   |   BIM: {bim_skipped}   |   "
                             f"Total: {skipped_count}")
        ws_na["A1"].font = Font(name="Arial", bold=True, size=11, color="FFFFFF")
        ws_na["A1"].fill = PatternFill("solid", start_color="7F7F7F")   # เทา = ยกเว้น
        ws_na["A1"].alignment = Alignment(horizontal="left", vertical="center")
        ws_na.row_dimensions[1].height = 22

        for c, (h, bg, w) in enumerate(cols_na, 1):
            hdr_cell(ws_na, 2, c, h, bg)
            ws_na.column_dimensions[get_column_letter(c)].width = w
        ws_na.row_dimensions[2].height = 20

        # เตรียมค่าเป็น list (กัน itertuples เปลี่ยนชื่อคอลัมน์ที่มีช่องว่าง)
        has_nt_col = "NEW TYPE" in skipped.columns
        vr_series = (skipped[VR_COL] if VR_COL in skipped.columns
                     else [""] * skipped_count)
        skipped_rows = list(zip(
            skipped["Asset ID"],
            skipped["TYPE"],
            skipped["NEW TYPE"] if has_nt_col else [""] * skipped_count,
            vr_series,
            skipped["Source"],
        ))
        # สี Source: Sub=ส้ม, BIM=ฟ้า
        src_style = {"Subsystem": ("FCE4D6", "C05C00"), "BIM": ("DDEBF7", "1F4E79")}
        for i, (aid, typ, ntype, vr, src) in enumerate(skipped_rows, 1):
            row = i + 2
            stripe = "F2F2F2" if i % 2 == 0 else None
            s_bg, s_fg = src_style.get(src, ("F2F2F2", "404040"))
            data_cell(ws_na, row, 1, i, stripe, "center")
            data_cell(ws_na, row, 2, aid, stripe)
            data_cell(ws_na, row, 3, typ, stripe, "center")
            data_cell(ws_na, row, 4, ntype, stripe, "center")
            col = 5
            if has_vr:
                vr_txt = str(vr).strip()
                vr_txt = vr_txt if vr_txt and vr_txt.lower() != "nan" else "—"
                v_bg, v_fg = _vr_style(vr_txt)
                data_cell(ws_na, row, col, vr_txt, v_bg or stripe, "center",
                          bold=bool(v_fg), color=v_fg)
                col += 1
            data_cell(ws_na, row, col, src, s_bg, "center", bold=True, color=s_fg)

        ws_na.freeze_panes = "A3"

    # ============================================================
    # SHEET 4: Subsystem
    # ============================================================
    ws2 = wb.create_sheet("Subsystem")
    ws2.sheet_view.showGridLines = False

    cols2 = [("#","404040",5),
             ("Index Code - Sub System","1F4E79",42),
             ("Found in BIM (Y/N)","1F6E3C",18),
             ("Correct Index Code (Y/N)\n(Type A)","C05C00",22)]
    if has_vr:
        cols2.append(("validation_result","7F5F00",24))
    for c,(h,bg,w) in enumerate(cols2,1):
        hdr_cell(ws2, 1, c, h, bg)
        ws2.column_dimensions[get_column_letter(c)].width = w
    ws2.row_dimensions[1].height = 34

    for i, (aid, found_bim, is_typeA, vr) in enumerate(sub_records, 1):
        bg = "F2F2F2" if i%2==0 else None
        data_cell(ws2, i+1, 1, i, bg, "center")
        data_cell(ws2, i+1, 2, aid, bg)
        fb = "Y" if found_bim else "N"
        data_cell(ws2, i+1, 3, fb, bg, "center", bold=True, color="C00000" if fb=="N" else "1F6E3C")
        ci = "Y" if is_typeA else "N"
        data_cell(ws2, i+1, 4, ci, bg, "center", bold=True, color="C00000" if ci=="N" else "1F6E3C")
        if has_vr:
            v_bg, v_fg = _vr_style(vr)
            data_cell(ws2, i+1, 5, vr, v_bg or bg, "center", bold=bool(v_fg), color=v_fg)

    ws2.freeze_panes = "A2"

    # ============================================================
    # SHEET 5: BIM
    # ============================================================
    ws3 = wb.create_sheet("BIM")
    ws3.sheet_view.showGridLines = False

    cols3 = [("#","404040",5),
             ("Index Code - BIM","1F4E79",42),
             ("Found in Subsystem (Y/N)","1F6E3C",20),
             ("Defect (Y/N)\n(Not found & not exempt)","C00000",22),
             ("Correct Index Code (Y/N)\n(Type A)","C05C00",22)]
    if has_vr:
        cols3.append(("validation_result","7F5F00",24))
    for c,(h,bg,w) in enumerate(cols3,1):
        hdr_cell(ws3, 1, c, h, bg)
        ws3.column_dimensions[get_column_letter(c)].width = w
    ws3.row_dimensions[1].height = 34

    for i, aid in enumerate(bim_list, 1):
        typ = bim_type_map.get(aid, "—")
        found_sub = aid in ids_sub
        is_typeA = (typ == "TYPE A")

        bg = "F2F2F2" if i%2==0 else None
        data_cell(ws3, i+1, 1, i, bg, "center")
        data_cell(ws3, i+1, 2, aid, bg)
        fs = "Y" if found_sub else "N"
        data_cell(ws3, i+1, 3, fs, bg, "center", bold=True, color="C00000" if fs=="N" else "1F6E3C")
        # Defect = ไม่เจอใน Sub และ part 6 ไม่อยู่ในลิสต์ยกเว้น
        df_ = "Y" if (not found_sub and not _is_exempt(aid)) else "N"
        data_cell(ws3, i+1, 4, df_, bg, "center", bold=True, color="C00000" if df_=="Y" else "1F6E3C")
        ci = "Y" if is_typeA else "N"
        data_cell(ws3, i+1, 5, ci, bg, "center", bold=True, color="C00000" if ci=="N" else "1F6E3C")
        if has_vr:
            vr = _vr(bim_vr_map, aid)
            v_bg, v_fg = _vr_style(vr)
            data_cell(ws3, i+1, 6, vr, v_bg or bg, "center", bold=bool(v_fg), color=v_fg)

    ws3.freeze_panes = "A2"

    wb.save(output_file)
    print(f"✅ Done! Saved to: {output_file}")

    # Print summary
    print()
    print("=== Summary ===")
    print("Subsystem:")
    for label, corr, wrong, total in sub_summary_rows:
        print(f"  {label}: {corr} correct, {wrong} wrong, {total} total")
    print()
    print("BIM:")
    for label, corr, wrong, total in bim_summary_rows:
        print(f"  {label}: {corr} correct, {wrong} wrong, {total} total")
    print()
    print("Master Table:")
    print(f"  Both (in Sub & BIM): {n_both}")
    print(f"  Sub only: {n_sub_only}")
    print(f"  BIM only: {n_bim_only}")
    print(f"  Total unique: {len(master_records)}")
    if skipped_count > 0:
        print()
        print("Skipped (NEW TYPE = N/A):")
        print(f"  Sub: {sub_skipped}  |  BIM: {bim_skipped}  |  Total: {skipped_count}")
    if xx_count > 0:
        print()
        print("-xx found (contains):")
        print(f"  Sub: {sub_xx}  |  BIM: {bim_xx}  |  Total: {xx_count}")
    if vr_summary_rows:
        print()
        print("validation_result:")
        for val, n_s, n_b, n_t in vr_summary_rows:
            print(f"  {val}: Sub {n_s}  |  BIM {n_b}  |  Total {n_t}")

    return output_file


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 compare_asset_id.py <subsystem_file> <bim_file> [output_file]")
        print()
        print("Example:")
        print("  python3 compare_asset_id.py sub.xlsx bim.xlsx")
        print("  python3 compare_asset_id.py sub.xlsx bim.xlsx output.xlsx")
        sys.exit(1)

    sub_file = sys.argv[1]
    bim_file = sys.argv[2]
    output_file = sys.argv[3] if len(sys.argv) > 3 else None

    compare_asset_id(sub_file, bim_file, output_file)
