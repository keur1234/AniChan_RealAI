"""Reference tables for index codes validation, kept up to date from their real sources.

Sources (each optional; whatever is missing comes from the last snapshot or obk_ref_bundle.json):
- OBK_EQUIPMENT_SHEET_URL  Google Sheet with the approved equipment list
                           (columns MAIN SYSTEM CODE, SUB SYSTEM CODE, Equipement Code, Status)
                           re-downloaded every OBK_REF_REFRESH_MINUTES (default 30)
- OBK_AREA_FILE            Excel file with sheet 'Register Location' (BIM ID = area codes)
- OBK_MAPPING_FILE         Excel file with sheet 'Mapping Concept' (systems / equipment types)
                           local files are re-read as soon as they are saved

Every successful update is saved to obk_ref_cache.json, so the bot still works offline
or after a restart with the last data it had.
"""
import copy
import io
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import obk_validator

log = logging.getLogger("vicky")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
BANGKOK = timezone(timedelta(hours=7))

EQUIPMENT_SHEET_URL = os.getenv("OBK_EQUIPMENT_SHEET_URL") or os.getenv("OBK_APPROVE_GSHEET_URL") or ""
AREA_FILE = os.getenv("OBK_AREA_FILE", "")
MAPPING_FILE = os.getenv("OBK_MAPPING_FILE", "")
REFRESH_SECONDS = int(os.getenv("OBK_REF_REFRESH_MINUTES", "30")) * 60
CACHE_PATH = os.getenv("OBK_REF_CACHE") or os.path.join(APP_DIR, "obk_ref_cache.json")

_lock = threading.Lock()
_state = {
    "bundle": None,          # merged reference dict
    "master": None,          # lookup sets built from the bundle
    "sheet_checked": 0.0,    # when the Google Sheet was last fetched (success or not)
    "mtimes": {},            # local file -> mtime last loaded
    "sources": {},           # name -> {"status", "detail", "time"}
    "refreshing": False,
}


def _now():
    return datetime.now(BANGKOK).strftime("%Y-%m-%d %H:%M")


def _resolve(path):
    if path and not os.path.isabs(path) and not os.path.isfile(path):
        path = os.path.join(APP_DIR, path)
    return path


def _bundle_path():
    return _resolve(os.getenv("OBK_BUNDLE_PATH") or "obk_ref_bundle.json")


def _note(name, status, detail):
    _state["sources"][name] = {"status": status, "detail": detail, "time": _now()}
    (log.info if status == "ok" else log.warning)("Reference %s: %s %s", name, status, detail)


def sheet_csv_url(url):
    """Any Google Sheets link -> its CSV export link (keeps the tab given by gid)."""
    if "export?format=csv" in url or "output=csv" in url:
        return url
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", url)
    if not m:
        return url
    gid = re.search(r"[#?&]gid=(\d+)", url)
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + (f"&gid={gid.group(1)}" if gid else "")


def _load_base():
    """The newer of the last snapshot and obk_ref_bundle.json (so dropping in a new bundle file wins)."""
    candidates = [(os.path.getmtime(p), p, label) for p, label in ((CACHE_PATH, "snapshot"), (_bundle_path(), "bundle"))
                  if p and os.path.isfile(p)]
    if not candidates:
        return None
    _, path, label = max(candidates)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    _note("base", "ok", f"{label} {os.path.basename(path)}")
    return data.get("bundle", data)


def _fetch_equipment(bundle):
    import pandas as pd
    import requests
    import validate_index_code as vic
    r = requests.get(sheet_csv_url(EQUIPMENT_SHEET_URL), timeout=60)
    r.raise_for_status()
    if r.text.lstrip().startswith("<"):
        raise RuntimeError("got a web page instead of CSV; share the sheet as 'Anyone with the link can view'")
    approve = vic.approve_from_table(pd.read_csv(io.StringIO(r.content.decode("utf-8-sig"))))
    if not approve:
        raise RuntimeError("no Active rows found")
    bundle["approve"] = approve
    return f"{len(approve)} active equipment types"


def _file_changed(path):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False
    return _state["mtimes"].get(path) != mtime


def refresh(force=False):
    """Bring the reference data up to date. Returns True if anything changed."""
    import validate_index_code as vic
    with _lock:
        bundle = copy.deepcopy(_state["bundle"]) if _state["bundle"] else _load_base()
        changed = _state["bundle"] is None and bundle is not None
        bundle = bundle or {}

        mapping, area = _resolve(MAPPING_FILE), _resolve(AREA_FILE)
        if mapping:
            if not os.path.isfile(mapping):
                _note("mapping", "error", f"file not found: {mapping}")
            elif force or _file_changed(mapping):
                try:
                    bundle.update(vic.mapping_part(mapping))
                    _state["mtimes"][mapping] = os.path.getmtime(mapping)
                    _note("mapping", "ok", f"{os.path.basename(mapping)}: {len(bundle['equipment_type'])} equipment types")
                    changed = True
                except Exception as e:
                    _note("mapping", "error", f"{os.path.basename(mapping)}: {e}")
        if area:
            if not os.path.isfile(area):
                _note("area", "error", f"file not found: {area}")
            elif force or _file_changed(area):
                try:
                    bundle["bim_id"] = vic.location_part(area)
                    _state["mtimes"][area] = os.path.getmtime(area)
                    _note("area", "ok", f"{os.path.basename(area)}: {len(bundle['bim_id'])} area codes")
                    changed = True
                except Exception as e:
                    _note("area", "error", f"{os.path.basename(area)}: {e}")
        if EQUIPMENT_SHEET_URL and (force or time.time() - _state["sheet_checked"] >= REFRESH_SECONDS):
            _state["sheet_checked"] = time.time()
            try:
                _note("equipment", "ok", "Google Sheet: " + _fetch_equipment(bundle))
                changed = True
            except Exception as e:
                _note("equipment", "error", f"Google Sheet: {e}")

        required = ("equipment_type", "equipment_code", "main_system", "sub_system", "bim_id", "sub_keywords", "sub_names")
        missing = [k for k in required if k not in bundle]
        if missing:
            if _state["master"] is None:
                log.warning("Reference data incomplete (missing %s); validation is unavailable", ", ".join(missing))
            return False
        if changed:
            bundle.setdefault("approve", [])
            _state["bundle"], _state["master"] = bundle, obk_validator.master_from_dict(bundle)
            try:
                tmp = CACHE_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"saved": _now(), "bundle": bundle}, f, ensure_ascii=False)
                os.replace(tmp, CACHE_PATH)
            except OSError as e:
                log.warning("Could not save reference snapshot: %s", e)
        return changed


def _stale():
    if EQUIPMENT_SHEET_URL and time.time() - _state["sheet_checked"] >= REFRESH_SECONDS:
        return True
    return any(p and _file_changed(p) for p in (_resolve(MAPPING_FILE), _resolve(AREA_FILE)) if p and os.path.isfile(p))


def _refresh_in_background():
    def run():
        try:
            refresh()
        finally:
            _state["refreshing"] = False
    _state["refreshing"] = True
    threading.Thread(target=run, daemon=True).start()


def get_master():
    """Reference lookups for validation. The first call loads synchronously; later updates happen in the background."""
    if _state["master"] is None:
        refresh()
    elif _stale() and not _state["refreshing"]:
        _refresh_in_background()
    return _state["master"]


def status_text(lang="th"):
    """Where the reference data came from and when, for the /refs command."""
    b = _state["bundle"] or {}
    th = lang == "th"
    lines = ["Reference data"]
    if b:
        lines.append((f"- Area codes: {len(b.get('bim_id', [])):,}" ) +
                     f"\n- Equipment types: {len(b.get('equipment_type', [])):,}"
                     f"\n- Approved equipment: {len(b.get('approve', [])):,}")
    else:
        lines.append("- ยังไม่มีข้อมูลอ้างอิง" if th else "- No reference data loaded")
    names = {"base": "Base", "equipment": "Equipment Google Sheet", "area": "Area code file", "mapping": "Mapping file"}
    for key, label in names.items():
        src = _state["sources"].get(key)
        if src:
            mark = "OK" if src["status"] == "ok" else "ERROR"
            lines.append(f"{label}: {mark} {src['time']}\n  {src['detail']}")
        elif key != "base":
            lines.append(f"{label}: " + ("ยังไม่ได้ตั้งค่า" if th else "not configured"))
    return "\n".join(lines)
