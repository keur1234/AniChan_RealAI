"""OBK index code validation for single codes typed in chat.

Lightweight port (no pandas) of validate_index_code.py; same rules and results.
The reference bundle (obk_ref_bundle.json) holds project data and is NOT committed;
point OBK_BUNDLE_PATH at it.
"""
import difflib
import json
import logging
import os
import re

log = logging.getLogger("anichan")

_SUFFIX_RE = re.compile(r'\s*(?:-\s*[ATH]|-?\s*NONE)\s*$')

SPECIAL_TYPE_OVERRIDES = [
    {'patterns': ['ICTN-002DS', 'ICTN-003DS'], 'type': 'N/A', 'match': 'contains'},
]

PART_SPECS = [
    ("11", "Component", "Component", 3, True),
    ("12", "Floor", "Floor", 3, True),
    ("13", "Space", "Space", 4, True),
    ("4", "Main System", "Main System (AssetMainSys)", 2, False),
    ("5", "Sub System", "Sub System (AssetCategory)", 4, False),
    ("6", "Equipment", "Equipment Code", 6, False),
    ("7", "Running No.", "Running Number", 3, False),
]
PART_NAMES_TH = ["Component", "Floor", "Space", "Main System", "Sub System", "Equipment", "Running No."]


def _t(lang, th, en):
    """Pick the Thai or English text (en is used for every non-Thai language)."""
    return th if lang == 'th' else en

# Anything with 4+ dashes between letters/digits looks like an index code worth checking
CODE_CANDIDATE_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.]*(?:-[A-Za-z0-9_.]+){4,}(?:\s+NONE\b)?')


def load_master(path):
    with open(path, encoding='utf-8') as f:
        b = json.load(f)
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
    # Pre-sorted lists for "did you mean" suggestions
    md['_locations'] = sorted(x for x in md['bim_id'] if x.count('-') == 2)
    md['_equipment_types'] = sorted(md['equipment_type'])
    return md


def has_exception_suffix(c):
    return bool(_SUFFIX_RE.search(c))


def strip_exception_suffix(c):
    return _SUFFIX_RE.sub('', c).rstrip()


def has_allf_area(c):
    return any('ALLF' in p for p in c.split('-')[:3])


def validate_index_code(code, md):
    """Returns (result_en, result_th, TYPE, rules[]) — same as the original script with running_counts=None."""
    if has_allf_area(code):
        return "EXCEPTION (ALLF)", "ข้อยกเว้น (ALLF)", "N/A", ["EXCEPTION"]
    has_suffix = has_exception_suffix(code)
    chk = strip_exception_suffix(code) if has_suffix else code
    R, RT, cnt = [], [], []
    mandatory = planBoC = 0
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
    # Rule 9 (unique running number) needs a whole file, so it is skipped for single codes

    t = ''
    if planBoC: t = 'TYPE B OR C'
    if mandatory: t = 'TYPE C'
    if R:
        return "; ".join(R), "; ".join(RT), t, cnt
    if has_suffix:
        return "EXCEPTION (suffix)", "ข้อยกเว้น (suffix)", "N/A", ["EXCEPTION"]
    return "VALID TYPE A", "ผ่าน TYPE A", "TYPE A", cnt


def check_index_code(c):
    """Correct BIM Format (BFV)"""
    if len(c) != 31 or c.count('-') != 6:
        return False
    p = c.split('-')
    if len(p) != 7 or any(len(x) != n for x, n in zip(p, [3, 3, 4, 2, 4, 6, 3])):
        return False
    return bool(re.fullmatch(r'[A-Z0-9-]+', c))


def apply_override(code, t):
    if t != 'TYPE A':
        return t
    for r in SPECIAL_TYPE_OVERRIDES:
        for p in r['patterns']:
            if (code == p) if r['match'] == 'exact' else (p in code):
                return r['type']
    return t


def find_codes(text):
    """Pull index-code-looking tokens out of free text."""
    return list(dict.fromkeys(m.group(0) for m in CODE_CANDIDATE_RE.finditer(text)))


def check_code(code, md):
    """Validate one code and add hints a human can act on."""
    code = code.strip()
    if '_' in code:                      # drop the _point suffix like the file pipeline does
        code = '_'.join(code.split('_')[:-1])
    en, th, t, rules = validate_index_code(code, md)
    t = apply_override(code, t)
    parts = (strip_exception_suffix(code) if has_exception_suffix(code) else code).split('-')
    result = {
        'code': code, 'TYPE': t, 'reasons_th': th, 'reasons_en': en, 'rules': rules,
        'correct_bim_format': check_index_code(code), 'parts': parts, 'hints': [],
    }
    # hints are (thai, english) pairs
    if len(parts) >= 6:
        asset_type = '-'.join(parts[3:6])
        result['approved'] = asset_type in md['approved']
        result['category'] = md['code_to_name'].get(parts[4])
        if '8' in rules:
            close = difflib.get_close_matches(asset_type, md['_equipment_types'], n=3, cutoff=0.6)
            if close:
                result['hints'].append((f"Asset Type ที่ใกล้เคียงใน Reference Table: {', '.join(close)}",
                                        f"Closest Asset Types in the Reference Table: {', '.join(close)}"))
    if code != code.upper() and t != 'TYPE A':
        upper_type = apply_override(code.upper(), validate_index_code(code.upper(), md)[2])
        if upper_type == 'TYPE A':
            result['hints'].append((f"หากแก้ไขเป็นตัวพิมพ์ใหญ่ ({code.upper()}) index code นี้จะผ่านเกณฑ์ TYPE A",
                                    f"In upper case ({code.upper()}) this index code passes as TYPE A"))
    if len(parts) >= 3 and '4' in rules:
        loc = '-'.join(parts[:3])
        close = difflib.get_close_matches(loc, md['_locations'], n=3, cutoff=0.7)
        if close:
            result['hints'].append((f"Location ที่ใกล้เคียงใน Reference Table: {', '.join(close)}",
                                    f"Closest Locations in the Reference Table: {', '.join(close)}"))
    return result


TYPE_LABEL_TH = {
    'TYPE A': 'ผ่านเกณฑ์',
    'TYPE B': 'Running Number ซ้ำ',
    'TYPE B OR C': 'ต้องตรวจสอบ Equipment / Asset Type',
    'TYPE C': 'ไม่ผ่านเกณฑ์บังคับ',
    'N/A': 'ข้อยกเว้น',
}
TYPE_LABEL_EN = {
    'TYPE A': 'Passed',
    'TYPE B': 'Duplicate running number',
    'TYPE B OR C': 'Equipment / Asset Type needs review',
    'TYPE C': 'Failed mandatory rules',
    'N/A': 'Exception',
}


def reason_text(reason):
    """Display form of a rule message ('กฏ 8: ...' -> 'กฎข้อ 8: ...')."""
    return re.sub(r'^กฏ\s*', 'กฎข้อ ', reason)


def type_label(t, lang='th'):
    label = (TYPE_LABEL_TH if lang == 'th' else TYPE_LABEL_EN).get(t)
    return f"{t} ({label})" if label else (t or '-')


def format_report(results, lang='th'):
    """Formal plain-text report for LINE (no Markdown, no emoji)."""
    lines = [_t(lang, "รายงานผลการตรวจสอบ index code", "Index code validation report"), ""]
    code_label = "Index code"
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {code_label}: {r['code']}" if len(results) > 1 else f"{code_label}: {r['code']}")
        lines.append(_t(lang, "ผลการตรวจสอบ", "Result") + f": {type_label(r['TYPE'], lang)}")
        reasons = r['reasons_th'] if lang == 'th' else r['reasons_en']
        if r['TYPE'] != 'TYPE A' and reasons:
            lines.append(_t(lang, "รายการที่ไม่เป็นไปตามเกณฑ์:", "Issues found:"))
            lines += [f"- {reason_text(reason)}" for reason in reasons.split('; ')]
        lines.append(_t(lang, "รูปแบบ BIM (BFV): ", "BIM format (BFV): ")
                     + (_t(lang, "ถูกต้อง", "Correct") if r['correct_bim_format'] else _t(lang, "ไม่ถูกต้อง", "Incorrect")))
        if 'approved' in r:
            lines.append(_t(lang, "Asset Type ในรายการที่อนุมัติ: ", "Asset Type in approved list: ")
                         + (_t(lang, "อยู่ในรายการ", "Yes") if r['approved'] else _t(lang, "ไม่อยู่ในรายการ", "No")))
        if r.get('category'):
            lines.append(_t(lang, "หมวดระบบ", "System category") + f": {r['category'].strip()}")
        if len(r['parts']) == 7:
            lines.append(_t(lang, "องค์ประกอบ index code: ", "Index code parts: ")
                         + " | ".join(f"{n} {p}" for n, p in zip(PART_NAMES_TH, r['parts'])))
        if r['hints']:
            lines.append(_t(lang, "ข้อเสนอแนะ:", "Suggestions:"))
            lines += [f"- {h[0] if lang == 'th' else h[1]}" for h in r['hints']]
        lines.append("")
    lines.append(_t(lang,
                    "หมายเหตุ: การตรวจสอบ index code รายตัวไม่ครอบคลุมกฎข้อ 9 (Running Number ซ้ำ) ซึ่งต้องตรวจสอบจากไฟล์ทั้งชุด",
                    "Note: checking single index codes does not cover Rule 9 (duplicate running number), which needs the whole file."))
    return "\n".join(lines).strip()


_master = None


def get_master():
    """Load the reference bundle once; None if it isn't configured."""
    global _master
    if _master is None:
        app_dir = os.path.dirname(os.path.abspath(__file__))
        path = os.getenv("OBK_BUNDLE_PATH") or "obk_ref_bundle.json"
        # A relative path may be relative to where python was started or to the project folder
        if not os.path.isabs(path) and not os.path.isfile(path):
            path = os.path.join(app_dir, path)
        path = os.path.abspath(path)
        if os.path.isfile(path):
            _master = load_master(path)
        else:
            log.warning("OBK reference bundle not found at %s", path)
    return _master
