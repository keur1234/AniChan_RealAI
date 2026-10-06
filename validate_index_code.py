#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OBK Index Code Validator — พอร์ตจาก indexcode_validation.py + utils/utils.py (ผลตรงกับสคริปต์เดิม)

ใช้งาน:
  # ตรวจไฟล์ (xlsx/xls/csv หรือทั้งโฟลเดอร์) ด้วย reference bundle
  python validate_index_code.py --bundle obk_ref_bundle.json input1.xlsx input2.csv --out outputs/

  # ตรวจด้วยไฟล์ ref ดิบ (เหมือนสคริปต์เดิม)
  python validate_index_code.py --mapping "Point Details Report_CI_Z3_without_alarms.xlsm" \
      --location "Component-Zone-Floor-Progression 1_20250205 - Copy Roomname.xlsx" \
      --approve OBK-MainSystem_SubSystem.xlsx  input.xlsx

  # ตรวจโค้ดเดี่ยว
  python validate_index_code.py --bundle obk_ref_bundle.json --code C3A-001-ME01-AC-AHUS-000AHU-001

  # สร้าง bundle ใหม่จากไฟล์ ref ดิบ
  python validate_index_code.py --mapping ... --location ... --approve ... --build-bundle obk_ref_bundle.json

--approve รับ: .xlsx (หาชีทที่มีคอลัมน์ Equipement Code + Status เอง) / .csv / "gsheet" (ดึง Google Sheet ตรง ต้องมีเน็ต)
ผลสรุปพิมพ์ออกเป็น JSON บรรทัดสุดท้าย (ขึ้นต้นด้วย SUMMARY_JSON=) ให้ Claude อ่านไปรายงานต่อ
"""
import argparse, json, os, re, sys
from collections import Counter
import pandas as pd

# ================= CONFIG (เหมือนสคริปต์เดิม) =================
CANDIDATE_COLUMNS = [
    'Name', 'IndexCode', 'Index code', 'Asset ID', 'AssetID',
    'Index', 'INDEX', 'INDEX DEVICE 1', '* Equipment ID', 'Equipment ID', 'OBK.FM_25Digits_TAG', 'ItemName',
    '*Equipment ID', 'OBK.FM_25Digits_TAG\nString\nInstance\nIdentity Data', 'NEW INDEX CODE'
]
MAX_HEADER_ROWS = 5
SPECIAL_TYPE_OVERRIDES = [
    {'patterns': ['ICTN-002DS', 'ICTN-003DS'], 'type': 'N/A', 'match': 'contains'},
]
GSHEET_URL = os.getenv("OBK_APPROVE_GSHEET_URL")  # CSV export URL of the approve sheet
TYPE_ORDER = ['TYPE A', 'TYPE B', 'TYPE B OR C', 'TYPE C']

# ================= REFERENCE =================
def load_approve(src):
    """คืน list ของ 'MAIN-SUB-EQUIP' เฉพาะ Status == Active"""
    if not src:
        return []
    if src == 'gsheet':
        if not GSHEET_URL:
            raise SystemExit("ตั้งค่า OBK_APPROVE_GSHEET_URL ก่อนใช้ --approve gsheet")
        t = pd.read_csv(GSHEET_URL)
    elif src.lower().endswith('.csv'):
        t = pd.read_csv(src)
    else:
        t = None
        for sh in pd.ExcelFile(src).sheet_names:
            d = pd.read_excel(src, sheet_name=sh)
            if 'Equipement Code' in d.columns and 'Status' in d.columns:
                t = d; break
        if t is None:
            raise SystemExit(f"ไม่พบชีทที่มีคอลัมน์ Equipement Code + Status ใน {src}")
    t = t[t['Status'] == 'Active']
    keys = t[['MAIN SYSTEM CODE', 'SUB SYSTEM CODE', 'Equipement Code']].astype(str).agg('-'.join, axis=1)
    return sorted(set(keys))

def build_bundle(mapping_path, location_path, approve_src):
    m = pd.read_excel(mapping_path, header=3, sheet_name='Mapping Concept').dropna(axis=1, how='all')
    loc = pd.read_excel(location_path, header=1, sheet_name='Register Location')
    s = lambda col: sorted(set(col.dropna().astype(str)))
    clean = m.dropna(subset=['SUB SYSTEM CODE', 'SUB SYSTEM NAME'])
    code_to_name = {}
    for c, n in zip(clean['SUB SYSTEM CODE'].astype(str), clean['SUB SYSTEM NAME'].astype(str)):
        code_to_name.setdefault(c, n)          # เก็บลำดับตามไฟล์ (find_code ใช้ตัวแรกที่เจอ)
    return {
        'equipment_type': s(m['Equipment Type']),
        'equipment_code': s(m['Equipment Code']),
        'main_system': s(m['SYSTEM CODE']),
        'sub_system': s(m['SUB SYSTEM CODE']),
        'bim_id': s(loc['BIM ID']),
        'sub_keywords': list(code_to_name.keys()),
        'sub_names': list(code_to_name.values()),
        'approve': load_approve(approve_src),
    }

def master_from_bundle(b):
    """รวม reference + approve table → master_data (เหมือน load_master_data(drive=True))"""
    md = {k: set(b[k]) for k in ['equipment_type', 'equipment_code', 'main_system', 'sub_system', 'bim_id']}
    approved = set()
    for k in b.get('approve', []):
        p = k.split('-')
        if len(p) != 3:
            continue
        md['main_system'].add(p[0]); md['sub_system'].add(p[1]); md['equipment_code'].add(p[2])
        md['equipment_type'].add(k); approved.add(k)
    md['approved'] = approved
    md['code_to_name'] = dict(zip(b['sub_keywords'], b['sub_names']))
    md['keywords'] = list(b['sub_keywords'])
    return md

# ================= RULES (จาก utils.py) =================
_SUFFIX_RE = re.compile(r'\s*(?:-\s*[ATH]|-?\s*NONE)\s*$')

def has_exception_suffix(c):
    return False if pd.isna(c) else bool(_SUFFIX_RE.search(str(c)))

def strip_exception_suffix(c):
    if pd.isna(c):
        return ''
    return _SUFFIX_RE.sub('', str(c)).rstrip()

def has_allf_area(c):
    return False if pd.isna(c) else any('ALLF' in p for p in str(c).split('-')[:3])

def is_exception_code(c):
    return has_exception_suffix(c) or has_allf_area(c)

def build_parts(c):
    parts = ('' if pd.isna(c) else str(c)).split('-')
    while len(parts) < 7:
        parts.append('X')
    return parts[:7]

def build_running_key(p, room=False):
    if room:
        return '-'.join(p[:7])
    if p[3] == 'VT':
        return f"{p[0]}-{p[1]}-{p[3]}-{p[4]}-{p[6]}"
    return f"{p[0]}-{p[1]}-{p[3]}-{p[4]}-{p[5]}-{p[6]}"

def count_running_keys(codes, room=False):
    c = Counter()
    for code in codes:
        if is_exception_code(code):
            continue                      # ข้อยกเว้นไม่นับ กันชนกฎ 9
        c[build_running_key(build_parts(code), room)] += 1
    return c

PART_SPECS = [
    ("11", "Component", "Component", 3, True),
    ("12", "Floor", "Floor", 3, True),
    ("13", "Space", "Space", 4, True),
    ("4", "Main System", "Main System (AssetMainSys)", 2, False),
    ("5", "Sub System", "Sub System (AssetCategory)", 4, False),
    ("6", "Equipment", "Equipment Code", 6, False),
    ("7", "Running No.", "Running Number", 3, False),
]

def validate_index_code(code, md, running_counts=None, room=True):
    """คืน (result_en, result_th, TYPE, rules[])"""
    code = '' if pd.isna(code) else str(code)
    if has_allf_area(code):
        return "EXCEPTION (ALLF)", "ข้อยกเว้น (ALLF)", "N/A", ["EXCEPTION"]
    has_suffix = has_exception_suffix(code)
    chk = strip_exception_suffix(code) if has_suffix else code
    R, RT, cnt = [], [], []
    mandatory = planBoC = planB = 0
    parts = chk.split('-')

    if len(chk) != 31:
        R.append("Rule 15: Index code length is not 31 characters"); RT.append("กฏ 15: Index code ต้องมี 31 ตัวอักษร"); mandatory += 1; cnt.append("15")
    if not re.fullmatch(r'[A-Z0-9-]+', chk):
        R.append("Rule 14: Special characters are not allowed"); RT.append("กฏ 14: ห้ามมีตัวอักษรพิเศษ (อนุญาตเฉพาะ A-Z 0-9 และ -)"); mandatory += 1; cnt.append("14")
    if len(parts) != 7:
        R.append("Rule 16: Index code must be split into 7 parts by 6 dashes"); RT.append("กฏ 16: ต้องแบ่งด้วยขีด (-) จำนวน 6 ตัว ได้ 7 ส่วน"); cnt.append("16"); mandatory += 1
        while len(parts) < 7:
            parts.append("X")
        parts = parts[:7]

    for i, (tag, en, th, n, official) in enumerate(PART_SPECS):
        if len(parts[i]) != n:
            if official:
                R.append(f"Rule {tag}: {en} must be {n} characters"); RT.append(f"กฏ {tag}: {th} ต้องมี {n} ตัวอักษร")
            else:
                R.append(f"Length: {en} must be {n} characters"); RT.append(f"ความยาว: {th} ต้องมี {n} ตัวอักษร")
            cnt.append(tag); mandatory += 1

    bim = md['bim_id']
    k_eq = f"{parts[3]}-{parts[4]}-{parts[5]}"
    k_loc = f"{parts[0]}-{parts[1]}-{parts[2]}"
    k_floor = f"{parts[0]}-{parts[1]}"
    if parts[0] not in bim:
        R.append("Rule 1: Component is not in Reference Table"); RT.append("กฏ 1: ไม่พบ Component นี้ใน Reference Table"); mandatory += 1; cnt.append("1")
    if k_loc not in bim:
        R.append("Rule 4: Location is not in Reference Table"); RT.append("กฏ 4: ไม่พบ Location นี้ใน Reference Table")
        R.append("Rule 3: CCDD is not in Reference Table"); RT.append("กฏ 3: ไม่พบ CCDD นี้ใน Reference Table")
        cnt += ["4", "3"]; mandatory += 1
        if k_floor not in bim:
            R.append("Rule 2: Floor is not in Reference Table"); RT.append("กฏ 2: ไม่พบ Floor นี้ใน Reference Table"); mandatory += 1; cnt.append("2")
    if parts[5] not in md['equipment_code']:
        R.append("Rule 7: Equipment Type is not in Reference Table"); RT.append("กฏ 7: ไม่พบ Equipment Type นี้ใน Reference Table"); planBoC += 1; cnt.append("7")
    if k_eq not in md['equipment_type']:
        R.append("Rule 8: Asset Type is not in Reference Table"); RT.append("กฏ 8: ไม่พบ Asset Type นี้ใน Reference Table"); planBoC += 1; cnt.append("8")
    if parts[3] not in md['main_system']:
        R.append("Rule 5: AssetMainSys (Main System) is not in Reference Table"); RT.append("กฏ 5: ไม่พบ Main System นี้ใน Reference Table"); mandatory += 1; cnt.append("5")
    if parts[4] not in md['sub_system']:
        R.append("Rule 6: AssetCategory (Sub System) is not in Reference Table"); RT.append("กฏ 6: ไม่พบ Sub System นี้ใน Reference Table"); mandatory += 1; cnt.append("6")
    if running_counts is not None and running_counts.get(build_running_key(parts, room), 0) > 1:
        R.append("Rule 9: Running number is not unique"); RT.append("กฏ 9: Running number ซ้ำกันในชุดนี้"); planB += 1; cnt.append("9")

    t = ''
    if planB: t = 'TYPE B'
    if planBoC: t = 'TYPE B OR C'
    if mandatory: t = 'TYPE C'
    if R:
        return "; ".join(R), "; ".join(RT), t, cnt
    if has_suffix:
        return "EXCEPTION (suffix)", "ข้อยกเว้น (suffix)", "N/A", ["EXCEPTION"]
    return "VALID TYPE A", "ผ่าน TYPE A", "TYPE A", cnt

def check_index_code(c):
    """Correct BIM Format (BFV)"""
    if not isinstance(c, str) or len(c) != 31 or c.count('-') != 6:
        return False
    p = c.split('-')
    if len(p) != 7 or any(len(x) != n for x, n in zip(p, [3, 3, 4, 2, 4, 6, 3])):
        return False
    return bool(re.fullmatch(r'[A-Z0-9-]+', c))

def get_component(a):
    p = str(a).split('-'); return p[0] if len(p) >= 2 else None

def get_equipment(a):
    p = str(a).split('-'); return '-'.join(p[3:6]) if len(p) >= 6 else None

def apply_override(asset_id, t):
    if t != 'TYPE A':
        return t
    s = str(asset_id)
    for r in SPECIAL_TYPE_OVERRIDES:
        for p in r['patterns']:
            if (s == p) if r['match'] == 'exact' else (p in s):
                return r['type']
    return t

# ================= READ FILE =================
def find_check_column(df):
    df = df.loc[:, ~df.columns.duplicated(keep='first')]
    cmap = {str(c).strip().lower(): c for c in df.columns}
    return next((cmap[c.strip().lower()] for c in CANDIDATE_COLUMNS if c.strip().lower() in cmap), None)

def read_with_header_search(reader):
    """ลอง header row 0..4 จนกว่าจะเจอคอลัมน์ Asset ID"""
    first = None
    for h in range(MAX_HEADER_ROWS):
        try:
            d = reader(h)
        except (ValueError, IndexError, pd.errors.EmptyDataError):
            break
        if first is None:
            first = (d, h)
        if find_check_column(d) is not None:
            return d, h
    return first if first else (None, None)

def read_input(path, log):
    name = os.path.splitext(os.path.basename(path))[0]
    frames = []
    if path.lower().endswith('.csv'):
        def rd(h):
            try:
                return pd.read_csv(path, header=h)
            except UnicodeDecodeError:
                return pd.read_csv(path, header=h, encoding='utf-8-sig')
        d, h = read_with_header_search(rd)
        if d is not None and not d.empty:
            d['Source Sheet'] = name; frames.append(d)
            log.append(f"ไฟล์ CSV ใช้ header row = {h}")
    else:
        xls = pd.ExcelFile(path)
        for sh in xls.sheet_names:
            d, h = read_with_header_search(lambda h: pd.read_excel(xls, sheet_name=sh, header=h))
            if d is None or d.empty:
                log.append(f"ข้ามชีท '{sh}': ว่าง"); continue
            if find_check_column(d) is not None:
                log.append(f"ชีท '{sh}': คอลัมน์ '{find_check_column(d)}' header row = {h}")
            d['Source Sheet'] = sh; frames.append(d)
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)

# ================= SUMMARY =================
def all_types(df):
    return TYPE_ORDER + [t for t in df['TYPE'].dropna().unique() if t not in TYPE_ORDER]

def make_source_summary(df):
    ts = all_types(df)
    rows = []
    for s, g in df.groupby('Source Sheet'):
        d = {'Source Sheet': s}
        for t in ts:
            d[t] = int((g['TYPE'] == t).sum())
        d['NOT TYPE A'] = int((g['TYPE'] != 'TYPE A').sum()); d['TOTAL'] = len(g)
        d['TYPE A %'] = round(d['TYPE A'] / d['TOTAL'] * 100, 2) if d['TOTAL'] else 0
        rows.append(d)
    return pd.DataFrame(rows, columns=['Source Sheet'] + ts + ['NOT TYPE A', 'TOTAL', 'TYPE A %'])

def make_type_summary(df):
    total = len(df); vc = df['TYPE'].value_counts()
    return pd.DataFrame([{'TYPE': t, 'Count': int(vc.get(t, 0)),
                          'Percent': round(vc.get(t, 0) / total * 100, 2) if total else 0} for t in all_types(df)])

def write_book(path, main, dups):
    cnt = main.groupby(['TYPE', 'validation_result']).size().reset_index(name='count')
    sheets = [('Sheet1', main), ('Sheet2', cnt), ('Summary by Source', make_source_summary(main)),
              ('TYPE Breakdown', make_type_summary(main)), ('Duplicates', dups)]
    with pd.ExcelWriter(path, engine='openpyxl') as w:
        for n, d in sheets:
            d.to_excel(w, sheet_name=n, index=False)
            ws = w.sheets[n]; ws.freeze_panes = 'A2'
            if d.shape[1]:
                ws.auto_filter.ref = ws.dimensions

def save_pie(df, title, path):
    try:
        import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
    except ImportError:
        return None
    vc = df['TYPE'].value_counts()
    cmap = {'TYPE A': 'green', 'TYPE B': 'gray', 'TYPE C': 'red', 'TYPE B OR C': 'yellow'}
    fig, ax = plt.subplots(figsize=(8, 8))
    w, _, _ = ax.pie(vc, colors=[cmap.get(t, 'lightblue') for t in vc.index], explode=[0.05] * len(vc),
                     autopct=lambda p: f"{int(round(p / 100 * vc.sum()))} ({p:.1f}%)", startangle=45,
                     textprops={'fontsize': 12}, wedgeprops={'edgecolor': 'white'})
    ax.legend(w, vc.index, title="TYPE", loc="center left", bbox_to_anchor=(1, 0.5))
    ax.set_title(title, fontsize=14); ax.axis('equal'); plt.tight_layout(); plt.savefig(path); plt.close()
    return path

# ================= PIPELINE =================
def process_file(path, md, out_root, room=True, dup_mode='sheet'):
    log = []
    name = os.path.splitext(os.path.basename(path))[0]
    df_all = read_input(path, log)
    if df_all is None:
        return {'file': path, 'skipped': 'ไฟล์ว่าง', 'log': log}
    df_all = df_all.loc[:, ~df_all.columns.duplicated(keep='first')]
    col = find_check_column(df_all)
    if col is None:
        return {'file': path, 'skipped': 'ไม่พบคอลัมน์ Asset ID', 'columns': [str(c) for c in df_all.columns][:40], 'log': log}

    df = df_all.dropna(subset=[col]).copy()
    # ตัด _point ท้าย Asset ID
    df['Asset ID'] = df[col].astype(str).apply(lambda x: x if '_' not in x else '_'.join(x.split('_')[:-1]))
    df = df[(df['Asset ID'] != '') & (df['Asset ID'] != 'nan') & (df['Asset ID'].str.strip() != '')].copy()
    if df.empty:
        return {'file': path, 'skipped': 'ไม่มี Asset ID ที่ใช้ได้', 'log': log}

    # ตรวจจาก unique Asset ID แล้ว map กลับทุกแถว (get_results point=True)
    uniq = list(dict.fromkeys(df['Asset ID']))
    rc = count_running_keys(uniq, room=room)
    vmap = {a: validate_index_code(a, md, rc, room) for a in uniq}

    review = pd.DataFrame({'Asset ID': df['Asset ID'].values})
    review['TYPE'] = review['Asset ID'].map(lambda a: vmap[a][2])
    review['validation_result'] = review['Asset ID'].map(lambda a: vmap[a][0])
    review['validation_result_thai'] = review['Asset ID'].map(lambda a: vmap[a][1])
    review['Rules'] = review['Asset ID'].map(lambda a: ','.join(vmap[a][3]))
    review['Component'] = review['Asset ID'].apply(get_component)
    review['Correct BIM Format'] = review['Asset ID'].apply(check_index_code)
    review['Asset Type'] = review['Asset ID'].apply(get_equipment)
    review['Approved'] = review['Asset Type'].apply(lambda x: 'yes' if x in md['approved'] else 'no')
    review['TYPE'] = [apply_override(a, t) for a, t in zip(review['Asset ID'], review['TYPE'])]

    # Source Sheet = ชีทแรกที่พบ asset (เหมือน merge source_map เดิม)
    src = df[['Asset ID', 'Source Sheet']].drop_duplicates(subset='Asset ID', keep='first')
    review = review.merge(src, on='Asset ID', how='left')

    # ตรวจ Asset ID ซ้ำ จากแถวดิบ
    dup = (df.groupby('Asset ID').agg(**{
        'Dup Count': ('Source Sheet', 'size'),
        'Sheet Count': ('Source Sheet', 'nunique'),
        'Source Sheets': ('Source Sheet', lambda s: ', '.join(sorted(map(str, pd.unique(s.dropna()))))),
    }).reset_index())
    dup['Is Duplicate'] = (dup['Dup Count'] > 1) if dup_mode == 'count' else (dup['Sheet Count'] > 1)
    review = review.merge(dup, on='Asset ID', how='left')
    review['Is Duplicate'] = review['Is Duplicate'].fillna(False).astype(bool)

    def find_code(t):
        for k in md['keywords']:
            if k in str(t):
                return k
        return 'Un-categorized'
    review['Asset Category'] = review['Asset ID'].apply(find_code)
    review['Asset Category Full name'] = review['Asset Category'].map(lambda c: md['code_to_name'].get(c, 'Un-categorized'))

    asset = review.drop_duplicates(subset='Asset ID', keep='first').copy()
    dcols = ['Asset ID', 'Dup Count', 'Sheet Count', 'Source Sheets', 'TYPE', 'validation_result',
             'Correct BIM Format', 'Component', 'Asset Category', 'Asset Category Full name']
    dups = asset[asset['Is Duplicate']][dcols].sort_values('Dup Count', ascending=False).reset_index(drop=True)

    out_dir = os.path.join(out_root, name); os.makedirs(out_dir, exist_ok=True)
    p1 = os.path.join(out_dir, f"{name}_output.xlsx")
    p2 = os.path.join(out_dir, f"{name}_output_asset.xlsx")
    write_book(p1, review, dups); write_book(p2, asset, dups)
    png = save_pie(review, name, os.path.join(out_dir, f"{name}.png"))

    total = len(review)
    pct = lambda n: round(n / total * 100, 2) if total else 0
    vc = review['TYPE'].value_counts()
    rule_counter = Counter(r for rs in asset['Rules'] for r in rs.split(',') if r)
    worst = make_source_summary(review).sort_values('TYPE A %').head(5)
    return {
        'file': path, 'column_used': str(col), 'log': log,
        'records': total, 'unique_assets': len(asset),
        'QLT_type_a': {'count': int(vc.get('TYPE A', 0)), 'pct': pct(vc.get('TYPE A', 0))},
        'BFV_correct_format': {'count': int(review['Correct BIM Format'].sum()), 'pct': pct(review['Correct BIM Format'].sum())},
        'approved_asset_type': {'count': int((review['Approved'] == 'yes').sum()), 'pct': pct((review['Approved'] == 'yes').sum())},
        'types': {t: {'count': int(vc.get(t, 0)), 'pct': pct(vc.get(t, 0))} for t in all_types(review)},
        'duplicates': len(dups), 'dup_mode': dup_mode, 'room': room,
        'top_rules_unique_assets': rule_counter.most_common(8),
        'worst_sheets': worst[['Source Sheet', 'TYPE A %', 'TOTAL']].to_dict('records'),
        'sample_failures': asset[~asset['TYPE'].isin(['TYPE A', 'N/A'])][['Asset ID', 'TYPE', 'validation_result_thai']].head(10).to_dict('records'),
        'outputs': [p for p in [p1, p2, png] if p],
    }

def main():
    ap = argparse.ArgumentParser(description="OBK Index Code Validator")
    ap.add_argument('inputs', nargs='*', help='ไฟล์ .xlsx/.xls/.csv หรือโฟลเดอร์')
    ap.add_argument('--bundle', help='obk_ref_bundle.json')
    ap.add_argument('--mapping', help='Point Details Report_CI_Z3_without_alarms.xlsm')
    ap.add_argument('--location', help='Component-Zone-Floor-Progression ... Roomname.xlsx')
    ap.add_argument('--approve', help='approve table: .xlsx / .csv / gsheet')
    ap.add_argument('--build-bundle', help='สร้าง bundle JSON จากไฟล์ ref ดิบ แล้วจบ')
    ap.add_argument('--code', action='append', help='ตรวจโค้ดเดี่ยว (ใส่ได้หลายครั้ง)')
    ap.add_argument('--no-room', action='store_true', help='กฎ 9 ตัด Space ออก (room=False)')
    ap.add_argument('--dup-mode', choices=['sheet', 'count'], default='sheet')
    ap.add_argument('--out', default='Outputs')
    a = ap.parse_args()

    if a.mapping and a.location:
        bundle = build_bundle(a.mapping, a.location, a.approve)
    elif a.bundle:
        bundle = json.load(open(a.bundle, encoding='utf-8'))
        if a.approve:                       # แนบ approve ใหม่ทับของใน bundle
            bundle['approve'] = load_approve(a.approve)
    else:
        raise SystemExit("ต้องใส่ --bundle หรือ --mapping + --location")

    if a.build_bundle:
        json.dump(bundle, open(a.build_bundle, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
        print(f"สร้าง bundle แล้ว: {a.build_bundle} (bim_id {len(bundle['bim_id'])}, approve {len(bundle['approve'])})")
        return

    md = master_from_bundle(bundle)
    room = not a.no_room
    result = {'reference': {'bim_id': len(md['bim_id']), 'equipment_type': len(md['equipment_type']),
                            'approve_active': len(md['approved'])}}

    if a.code:
        result['codes'] = []
        for c in a.code:
            en, th, t, rules = validate_index_code(c.strip(), md, None, room)
            result['codes'].append({'code': c, 'TYPE': t, 'reasons_th': th, 'rules': rules,
                                    'correct_bim_format': check_index_code(c.strip()),
                                    'parts': c.strip().split('-')})

    files = []
    for p in a.inputs:
        if os.path.isdir(p):
            files += [os.path.join(p, f) for f in sorted(os.listdir(p)) if f.lower().endswith(('.csv', '.xlsx', '.xls'))]
        else:
            files.append(p)
    result['files'] = [process_file(f, md, a.out, room, a.dup_mode) for f in files]
    print("SUMMARY_JSON=" + json.dumps(result, ensure_ascii=False, default=str))

if __name__ == '__main__':
    main()
