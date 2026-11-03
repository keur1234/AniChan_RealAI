"""FAS (fire alarm) special rules, used only when the user asks to check FAS (e.g. "/check FAS").

Rows are filtered to Asset IDs containing CM-FIRE; fire assets that meet rule 1 or rule 2
become "N/A กรณีพิเศษ", are counted separately and are not looked up in BIM.
"""
FILTER_ASSET_ID_CONTAINS = "CM-FIRE"        # กรองแถว: เก็บเฉพาะ Asset ID ที่มีคำนี้ (None = ไม่กรอง)
FIRE_PREFIXES  = ("CM-FIRE", "FP")          # กฎ 1: prefix ของ asset ระบบดับเพลิง
SUFFIX_OK      = {"A", "T", "L", "R", "H"}  # กฎ 1: ตัวอักษรท้าย Asset ID ที่ถือว่าผ่าน
CMFIRE         = "CM-FIRE"                   # กฎ 2: เฉพาะ CM-FIRE
SPECIAL_TYPE   = "N/A กรณีพิเศษ"             # TYPE ของ asset ที่เข้ากฎ 1 หรือ 2
RULE2_TYPES    = {"0KEYSW", "ADKEYS", "TBLAMP", "FSPEAK", "TLJACK", "ADSTOB", "FARACK", "FIRECP", "MONITR"}  # + "ADMANL"?


def is_fire(asset_type):
    t = str(asset_type or "").strip().upper()
    return any(t.startswith(p) for p in FIRE_PREFIXES)


def rule1_suffix(asset_id):
    """กฎ 1: ตัวท้ายสุดของ Asset ID เป็นตัวอักษรเดี่ยวในชุด A/T/L/R/H"""
    last = str(asset_id or "").strip().rsplit("-", 1)[-1].upper()
    return len(last) == 1 and last in SUFFIX_OK


def rule2_type(asset_type):
    """กฎ 2: CM-FIRE ชนิด 0KEYSW / INT* / INI* / *MOD / ADKEYS / TBLAMP / FSPEAK / TLJACK / ADSTOB / FARACK / FIRECP / MONITR"""
    t = str(asset_type or "").strip().upper()
    if not t.startswith(CMFIRE):
        return False
    tail = t[len(CMFIRE):].lstrip("-")
    return tail in RULE2_TYPES or tail.startswith("INT") or tail.startswith("INI") or tail.endswith("MOD")


def keep_row(asset_id):
    return FILTER_ASSET_ID_CONTAINS is None or FILTER_ASSET_ID_CONTAINS in str(asset_id).upper()


def is_special(asset_id, asset_type):
    return is_fire(asset_type) and (rule1_suffix(asset_id) or rule2_type(asset_type))


# ---- Check scopes: "/check FAS", "/check ACS", "/check BMS" ----
# contains:        keep only index codes containing this text, in the file and in BIM ("" = keep all)
# special_rules:   apply the FAS special cases above (N/A กรณีพิเศษ)
# bim_asset_types: BIM side only: keep BIM index codes whose Asset Type (parts 4-6) is in this list
#                  (the file itself is not filtered); see bms_asset_types.py
SCOPES = {
    "FAS": {"contains": FILTER_ASSET_ID_CONTAINS, "special_rules": True},
    "ACS": {"contains": "CM-ACCS", "special_rules": False},
    "BMS": {"contains": "", "special_rules": False, "bim_asset_types": "bms"},
}
_asset_type_lists = {}


def bim_asset_types(scope):
    """The Asset Types BIM is limited to for this scope, or None."""
    name = (SCOPES.get(scope) or {}).get("bim_asset_types")
    if not name:
        return None
    if name not in _asset_type_lists:
        import bms_asset_types
        _asset_type_lists[name] = bms_asset_types.load()
    return _asset_type_lists[name]


def bim_in_scope(tag, scope):
    """Is this BIM index code part of the comparison for this scope?"""
    types = bim_asset_types(scope)
    if types is not None:
        return "-".join(str(tag).upper().split("-")[3:6]) in types
    return in_scope(tag, scope)


def scope_description(scope, lang="th"):
    """'FAS check: ตรวจเฉพาะ index codes ที่มี CM-FIRE' etc. ('' for a normal check)."""
    if not scope:
        return ""
    types = bim_asset_types(scope)
    if types is not None:
        return (f"{scope} check: ฝั่ง BIM เทียบเฉพาะ Asset Type ของ {scope} ({len(types)} ชนิด) ไฟล์ต้นทางไม่กรอง"
                if lang == "th" else
                f"{scope} check: BIM limited to {len(types)} {scope} Asset Types; the file is not filtered")
    return (f"{scope} check: ตรวจเฉพาะ index codes ที่มี {scope_filter(scope)}" if lang == "th" else
            f"{scope} check: only index codes containing {scope_filter(scope)}")


def scope_filter(scope):
    """The text an index code must contain for this scope ('' = no filter)."""
    return (SCOPES.get(scope) or {}).get("contains") or ""


def in_scope(asset_id, scope):
    f = scope_filter(scope)
    return not f or f in str(asset_id).upper()


def has_special_rules(scope):
    return bool((SCOPES.get(scope) or {}).get("special_rules"))
