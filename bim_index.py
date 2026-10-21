"""Index of every index code in the BIM exports, so "is this in BIM?" is answered without downloading files.

Each BIM export (one per component, e.g. CUP_25Digits_Combined_20261002.xlsx) has a sheet
Combined_25Digits with the index code in OBK.FM_25Digits_TAG. A background sync reads only the
files that changed since last time and stores the tags in a local SQLite database (bim_index.db).
Lookups query that database and take milliseconds.

How an update works (update_bim_db.py, /bimsync, or the weekly schedule):
1. Download: if BIM_DRIVE_FOLDER is set, new or changed Excel files are downloaded from that
   Google Drive folder into BIM_DOWNLOAD_DIR (default bim_files/). Access with
   GOOGLE_SERVICE_ACCOUNT_FILE (folder shared with the service account), BIM_DRIVE_API_KEY,
   or, with neither, gdown (folder shared as "Anyone with the link").
2. Build: every Excel file in the local folder (BIM_LOCAL_FOLDER, or the download folder) is read,
   sheet Combined_25Digits, column OBK.FM_25Digits_TAG, into the SQLite DB.
Only the newest file per component is used (by the date in the file name, then modified time).

The index is refreshed once a week (BIM_SYNC_DAY at BIM_SYNC_TIME, Bangkok time; default Monday 06:00).
If the bot was off at that time it catches up when it starts. Admins can force it with /bimsync.
"""
import fnmatch
import io
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from app_paths import APP_DIR

log = logging.getLogger("vicky")
BANGKOK = timezone(timedelta(hours=7))

DB_PATH = os.getenv("BIM_DB") or os.path.join(APP_DIR, "bim_index.db")
LOCAL_FOLDER = os.getenv("BIM_LOCAL_FOLDER", "")
DRIVE_FOLDER = os.getenv("BIM_DRIVE_FOLDER", "")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
DRIVE_API_KEY = os.getenv("BIM_DRIVE_API_KEY", "")
DOWNLOAD_DIR = os.getenv("BIM_DOWNLOAD_DIR") or os.path.join(APP_DIR, "bim_files")
# Only the tag column is needed to answer "is it in BIM"; set BIM_TAG_ONLY=0 to also keep discipline, family...
TAG_ONLY = os.getenv("BIM_TAG_ONLY", "1") == "1"
# Only the files directly in the folder; set BIM_RECURSIVE=1 to include sub-folders
RECURSIVE = os.getenv("BIM_RECURSIVE", "0") == "1"
# Files to read (';'-separated patterns)
FILE_PATTERNS = [p.strip() for p in os.getenv("BIM_FILE_PATTERN", "*_25Digits_Combined*.xlsx;CI_*.xlsx;CI_*.xlsm").split(";") if p.strip()]
SHEET = os.getenv("BIM_SHEET", "Combined_25Digits")
# Files whose name matches a pattern read these sheets instead of SHEET, combined into one list
# (format "pattern=sheet,sheet;pattern=sheet,..."); CI_ files have one sheet per area
SHEET_RULES = [(rule.split("=", 1)[0].strip(), [x.strip() for x in rule.split("=", 1)[1].split(",") if x.strip()])
               for rule in os.getenv("BIM_SHEET_RULES", "CI_*=P01,P02,P03,P04,P06").split(";") if "=" in rule]
TAG_COLUMN = os.getenv("BIM_TAG_COLUMN", "OBK.FM_25Digits_TAG")
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SYNC_WEEKDAY = WEEKDAYS.index(os.getenv("BIM_SYNC_DAY", "mon").strip().lower()[:3])
SYNC_HOUR, SYNC_MINUTE = (int(x) for x in os.getenv("BIM_SYNC_TIME", "06:00").split(":"))
RETRY_SECONDS = 600            # after a failed attempt, wait before trying again
SCHEDULER_CHECK_SECONDS = 1800

# Columns kept for each element (missing ones are stored empty)
DETAIL_COLUMNS = {
    "discipline": "Discipline", "category": "Category", "family": "Family", "type": "Type",
    "guid": "GUID", "element_id": "Element ID", "source_file": "Source File",
    "source_sheet": "Source Sheet", "status": "Status",
}

_lock = threading.Lock()
_state = {"syncing": False, "last_attempt": 0.0, "last_error": "", "scheduler": False}


# ---------------------------------------------------------------- database
def _connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("""CREATE TABLE IF NOT EXISTS files (
        key TEXT PRIMARY KEY, component TEXT, name TEXT, version TEXT, source_id TEXT,
        modified TEXT, link TEXT, rows INTEGER, synced_at TEXT)""")
    con.execute(f"""CREATE TABLE IF NOT EXISTS elements (
        tag TEXT, file_key TEXT, {', '.join(f'{c} TEXT' for c in DETAIL_COLUMNS)})""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tag ON elements(tag)")
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return con


def _now():
    return datetime.now(BANGKOK).strftime("%Y-%m-%d %H:%M")


def normalize(code):
    return str(code).strip().upper()


# ---------------------------------------------------------------- choosing files
def _matches(name):
    return any(fnmatch.fnmatch(name.lower(), p.lower()) for p in FILE_PATTERNS) and not name.startswith("~$")


def sheets_for(name):
    """Sheets to read from a file: the first matching BIM_SHEET_RULES entry, else SHEET."""
    for pattern, sheets in SHEET_RULES:
        if fnmatch.fnmatch(os.path.basename(name).lower(), pattern.lower()):
            return sheets
    return [SHEET]


def _component_and_version(name):
    """CUP_25Digits_Combined_20261002.xlsx -> ('CUP', '20261002'); CI_Z3_20261001.xlsx -> ('CI_Z3', '20261001')."""
    stem = os.path.splitext(os.path.basename(name))[0]
    if re.search(r"_25Digits", stem, flags=re.IGNORECASE):
        component = re.split(r"_25Digits", stem, flags=re.IGNORECASE)[0] or stem
    else:                                                  # drop a trailing date / revision, keep the name
        component = re.sub(r"[_\- ]*\d{8}.*$", "", stem) or stem
    dates = re.findall(r"(\d{8})", stem)
    return component.upper(), (dates[-1] if dates else "")


def _latest_per_component(files):
    """Keep the newest file for each component."""
    best = {}
    for f in files:
        component, version = _component_and_version(f["name"])
        f["component"], f["version"] = component, version
        current = best.get(component)
        if current is None or (version, f["modified"]) > (current["version"], current["modified"]):
            best[component] = f
    return list(best.values())


# ---------------------------------------------------------------- sources
def _local_files():
    files = []
    for root, dirs, names in os.walk(LOCAL_FOLDER or DOWNLOAD_DIR):
        if not RECURSIVE:
            dirs.clear()                                   # don't go into sub-folders
        for name in names:
            if _matches(name):
                path = os.path.join(root, name)
                files.append({"name": name, "source_id": path, "link": path,
                              "modified": datetime.fromtimestamp(os.path.getmtime(path), BANGKOK).isoformat()})
    return files


def _local_read(f):
    with open(f["source_id"], "rb") as fh:
        return fh.read()


def _folder_id(value):
    m = re.search(r"/folders/([A-Za-z0-9_-]{10,})", value) or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", value)
    return m.group(1) if m else value.strip()


def _drive_auth():
    """(headers, params) for Drive API calls."""
    if SERVICE_ACCOUNT_FILE:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        key_file = SERVICE_ACCOUNT_FILE
        if not os.path.isabs(key_file) and not os.path.isfile(key_file):
            key_file = os.path.join(APP_DIR, key_file)          # relative to the project folder
        if not os.path.isfile(key_file):
            raise RuntimeError(f"service account key file not found: {key_file}")
        creds = service_account.Credentials.from_service_account_file(
            key_file, scopes=["https://www.googleapis.com/auth/drive.readonly"])
        creds.refresh(Request())
        return {"Authorization": f"Bearer {creds.token}"}, {}
    if DRIVE_API_KEY:
        return {}, {"key": DRIVE_API_KEY}
    raise RuntimeError("set GOOGLE_SERVICE_ACCOUNT_FILE or BIM_DRIVE_API_KEY to read the Drive folder")


def _drive_files():
    import requests
    headers, params = _drive_auth()
    files, folders = [], [_folder_id(DRIVE_FOLDER)]
    while folders:
        folder = folders.pop()
        page = None
        while True:
            q = {"q": f"'{folder}' in parents and trashed=false", "pageSize": 1000,
                 "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink)",
                 "supportsAllDrives": "true", "includeItemsFromAllDrives": "true", **params}
            if page:
                q["pageToken"] = page
            r = requests.get("https://www.googleapis.com/drive/v3/files", headers=headers, params=q, timeout=60)
            r.raise_for_status()
            data = r.json()
            for item in data.get("files", []):
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    if RECURSIVE:
                        folders.append(item["id"])
                    continue
                name = item["name"]
                is_sheet = item["mimeType"] == "application/vnd.google-apps.spreadsheet"
                if is_sheet:
                    name += ".xlsx"
                if _matches(name):
                    files.append({"name": name, "source_id": item["id"], "modified": item["modifiedTime"],
                                  "link": item.get("webViewLink", ""), "google_sheet": is_sheet})
            page = data.get("nextPageToken")
            if not page:
                break
    return files


def _drive_read(f):
    import requests
    headers, params = _drive_auth()
    base = f"https://www.googleapis.com/drive/v3/files/{f['source_id']}"
    if f.get("google_sheet"):
        url, extra = base + "/export", {"mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    else:
        url, extra = base, {"alt": "media", "supportsAllDrives": "true"}
    r = requests.get(url, headers=headers, params={**extra, **params}, timeout=300)
    r.raise_for_status()
    return r.content


def configured():
    """A source to update from is set (Drive folder or local folder)."""
    return bool(LOCAL_FOLDER or DRIVE_FOLDER)


def ready():
    """The DB has been built (by the bot or by update_bim_db.py) and can answer lookups."""
    return bool(last_sync())


def _manifest_path():
    return os.path.join(DOWNLOAD_DIR, ".drive_manifest.json")


def download_from_drive(force=False, progress=None):
    """Download new or changed BIM Excel files from BIM_DRIVE_FOLDER into DOWNLOAD_DIR. Returns a report."""
    import json
    say = progress or (lambda msg: log.info("BIM download: %s", msg))
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    if not (SERVICE_ACCOUNT_FILE or DRIVE_API_KEY):
        return _download_with_gdown(say)
    try:
        with open(_manifest_path(), encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        manifest = {}
    files = _drive_files()
    say(f"{len(files)} Excel file(s) in the Drive folder")
    fetched = 0
    for f in files:
        path = os.path.join(DOWNLOAD_DIR, re.sub(r'[\\/:*?"<>|]', "_", f["name"]))
        if not force and manifest.get(f["source_id"]) == f["modified"] and os.path.isfile(path):
            continue
        say(f"downloading {f['name']}")
        data = _drive_read(f)
        with open(path + ".part", "wb") as out:
            out.write(data)
        os.replace(path + ".part", path)
        manifest[f["source_id"]] = f["modified"]
        fetched += 1
    with open(_manifest_path(), "w", encoding="utf-8") as out:
        json.dump(manifest, out, indent=1)
    return f"downloaded {fetched}, unchanged {len(files) - fetched}"


def _download_with_gdown(say):
    """No API credentials: let gdown fetch the shared folder (it must be 'Anyone with the link')."""
    try:
        import gdown
    except ImportError:
        raise RuntimeError("set GOOGLE_SERVICE_ACCOUNT_FILE or BIM_DRIVE_API_KEY, or pip install gdown")
    url = DRIVE_FOLDER if DRIVE_FOLDER.startswith("http") else f"https://drive.google.com/drive/folders/{DRIVE_FOLDER}"
    say("listing the folder with gdown (no API credentials set)")
    try:
        listed = gdown.download_folder(url=url, output=DOWNLOAD_DIR, quiet=True, remaining_ok=True, skip_download=True) or []
    except TypeError:                                       # old gdown without skip_download
        paths = gdown.download_folder(url=url, output=DOWNLOAD_DIR, quiet=False, remaining_ok=True) or []
        return f"downloaded {len(paths)} file(s) with gdown"
    wanted = [f for f in listed
              if (RECURSIVE or len(os.path.normpath(f.path).split(os.sep)) == 1)
              and _matches(os.path.basename(f.path))]
    say(f"{len(wanted)} Excel file(s) in the folder")
    for f in wanted:
        target = os.path.join(DOWNLOAD_DIR, os.path.basename(f.path))
        say(f"downloading {os.path.basename(f.path)}")
        gdown.download(id=f.id, output=target, quiet=True)
    if len(listed) >= 50:
        say("gdown lists at most 50 files per folder; use an API key or service account for more")
    return f"downloaded {len(wanted)} file(s) with gdown"


# ---------------------------------------------------------------- sync
def _read_sheet(book, sheet):
    """One sheet as a DataFrame whose header row is the one holding TAG_COLUMN (searched in the first 10 rows)."""
    import pandas as pd
    raw = pd.read_excel(book, sheet_name=sheet, dtype=str, header=None)
    for i in range(min(10, len(raw))):
        row = [str(v).strip() for v in raw.iloc[i].tolist()]
        if TAG_COLUMN in row:
            df = raw.iloc[i + 1:].copy()
            df.columns = [str(v).strip() if str(v) != "nan" else f"col{j}" for j, v in enumerate(raw.iloc[i].tolist())]
            df = df.loc[:, ~df.columns.duplicated()]
            return df[[TAG_COLUMN]] if TAG_ONLY else df
    raise RuntimeError(f"column {TAG_COLUMN} not found in sheet {sheet}")


def _read_tags(data, name=""):
    """Tags (and details) from a BIM file: its sheet(s) per sheets_for(name), combined."""
    import pandas as pd
    book = pd.ExcelFile(io.BytesIO(data))
    wanted = sheets_for(name)
    present = [sh for sh in wanted if sh in book.sheet_names]
    if not present:
        raise RuntimeError(f"none of the sheets {', '.join(wanted)} found (sheets: {', '.join(book.sheet_names[:10])})")
    missing = [sh for sh in wanted if sh not in present]
    if missing:
        log.warning("BIM: %s has no sheet %s", name, ", ".join(missing))
    rows = []
    for sheet in present:
        df = _read_sheet(book, sheet)
        df = df[df[TAG_COLUMN].notna()]
        for rec in df.to_dict("records"):
            tag = normalize(rec[TAG_COLUMN])
            if tag and tag != "NAN":
                details = [("" if pd.isna(rec.get(col)) else str(rec.get(col))) for col in DETAIL_COLUMNS.values()]
                if len(present) > 1 and not details[list(DETAIL_COLUMNS).index("source_sheet")]:
                    details[list(DETAIL_COLUMNS).index("source_sheet")] = sheet      # remember which sheet it came from
                rows.append((tag, *details))
    return rows


def sync(force=False, progress=None):
    """Download (if a Drive folder is set) and index new or changed BIM files. Returns a short report."""
    if not configured():
        return "BIM source not configured (set BIM_LOCAL_FOLDER or BIM_DRIVE_FOLDER)"
    with _lock:
        _state["last_attempt"] = time.time()
        download_report = ""
        if DRIVE_FOLDER:
            try:
                download_report = download_from_drive(force, progress)
            except Exception as e:                     # keep going with whatever is already downloaded
                download_report = f"download failed: {e}"
                log.warning("BIM sync: %s", download_report)
        try:
            listed = _local_files()
        except Exception as e:
            _state["last_error"] = f"listing failed: {e}"
            log.warning("BIM sync: %s", _state["last_error"])
            return _state["last_error"]
        wanted = _latest_per_component(listed)
        con = _connect()
        try:
            known = {r[0]: r for r in con.execute("SELECT key, source_id, modified FROM files")}
            updated, errors = [], []
            for f in wanted:
                key = f["component"]
                if not force and key in known and known[key][1] == f["source_id"] and known[key][2] == f["modified"]:
                    continue
                try:
                    if progress:
                        progress(f"reading {f['name']}")
                    rows = _read_tags(_local_read(f), f["name"])
                except Exception as e:
                    errors.append(f"{f['name']}: {e}")
                    log.warning("BIM sync: %s failed: %s", f["name"], e)
                    continue
                with con:                                  # replace this component atomically
                    con.execute("DELETE FROM elements WHERE file_key=?", (key,))
                    con.executemany(f"INSERT INTO elements VALUES ({', '.join('?' * (len(DETAIL_COLUMNS) + 2))})",
                                    [(r[0], key, *r[1:]) for r in rows])
                    con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                                (key, f["component"], f["name"], f["version"], f["source_id"], f["modified"],
                                 f["link"], len(rows), _now()))
                updated.append(f"{f['name']} {len(rows):,}")
                log.info("BIM sync: indexed %s (%d tags)", f["name"], len(rows))
            # components that disappeared from the source
            gone = set(known) - {f["component"] for f in wanted}
            with con:
                for key in gone:
                    con.execute("DELETE FROM elements WHERE file_key=?", (key,))
                    con.execute("DELETE FROM files WHERE key=?", (key,))
                con.execute("INSERT OR REPLACE INTO meta VALUES ('last_sync', ?)", (_now(),))
        finally:
            con.close()
        _state["last_error"] = "; ".join(errors)
        parts = ([download_report] if download_report else []) + [f"indexed {len(updated)} file(s)" + (": " + ", ".join(updated) if updated else ""),
                 f"unchanged {len(wanted) - len(updated) - len(errors)}"]
        if gone:
            parts.append(f"removed {', '.join(sorted(gone))}")
        if errors:
            parts.append("errors: " + "; ".join(errors))
        return "; ".join(parts)


def sync_in_background(force=False):
    if _state["syncing"]:
        return False

    def run():
        try:
            sync(force)
        finally:
            _state["syncing"] = False
    _state["syncing"] = True
    threading.Thread(target=run, daemon=True).start()
    return True


def last_scheduled(now=None):
    """The most recent weekly sync time that has already passed."""
    now = now or datetime.now(BANGKOK)
    slot = now.replace(hour=SYNC_HOUR, minute=SYNC_MINUTE, second=0, microsecond=0) \
        - timedelta(days=(now.weekday() - SYNC_WEEKDAY) % 7)
    return slot if slot <= now else slot - timedelta(days=7)


def next_scheduled(now=None):
    return last_scheduled(now) + timedelta(days=7)


def is_due(now=None):
    """True when the index has never been built or is older than the last weekly sync time."""
    done = last_sync()
    if not done:
        return True
    done_at = datetime.strptime(done, "%Y-%m-%d %H:%M").replace(tzinfo=BANGKOK)
    return done_at < last_scheduled(now)


def maybe_sync():
    """Start a background sync if the weekly one is due (and we didn't just fail)."""
    if configured() and not _state["syncing"] and time.time() - _state["last_attempt"] >= RETRY_SECONDS and is_due():
        sync_in_background()


def start_scheduler():
    """Check every 30 minutes whether the weekly sync is due, so it runs even when nobody is chatting."""
    if _state["scheduler"] or not configured():
        return
    _state["scheduler"] = True

    def loop():
        while True:
            try:
                maybe_sync()
            except Exception:
                log.exception("BIM scheduler")
            time.sleep(SCHEDULER_CHECK_SECONDS)
    threading.Thread(target=loop, daemon=True).start()


# ---------------------------------------------------------------- queries
def last_sync():
    if not os.path.exists(DB_PATH):
        return None
    con = _connect()
    try:
        row = con.execute("SELECT v FROM meta WHERE k='last_sync'").fetchone()
        return row[0] if row else None
    finally:
        con.close()


def lookup(codes, per_code=20):
    """{code: [element dicts]} for each code (empty list = not in BIM)."""
    codes = [normalize(c) for c in codes if str(c).strip()]
    out = {c: [] for c in codes}
    if not codes or not os.path.exists(DB_PATH):
        return out
    con = _connect()
    con.row_factory = sqlite3.Row
    try:
        cols = ", ".join(f"e.{c}" for c in DETAIL_COLUMNS)
        for i in range(0, len(codes), 500):
            chunk = list(dict.fromkeys(codes[i:i + 500]))
            q = (f"SELECT e.tag, e.file_key, f.name AS bim_file, f.link, {cols} FROM elements e "
                 f"JOIN files f ON f.key = e.file_key WHERE e.tag IN ({','.join('?' * len(chunk))})")
            for row in con.execute(q, chunk):
                if len(out[row["tag"]]) < per_code:
                    out[row["tag"]].append(dict(row))
        return out
    finally:
        con.close()


def count_by_tag(codes):
    """{code: number of BIM elements} for many codes at once."""
    codes = list(dict.fromkeys(normalize(c) for c in codes if str(c).strip()))
    out = dict.fromkeys(codes, 0)
    if not codes or not os.path.exists(DB_PATH):
        return out
    con = _connect()
    try:
        for i in range(0, len(codes), 500):
            chunk = codes[i:i + 500]
            for tag, n in con.execute(f"SELECT tag, COUNT(*) FROM elements WHERE tag IN ({','.join('?' * len(chunk))}) "
                                      "GROUP BY tag", chunk):
                out[tag] = n
        return out
    finally:
        con.close()


def add_found_column(xlsx_path, sheets=("Sheet1", "Duplicates"), code_header="Asset ID", skip=()):
    """Append a "Found in BIM" (Yes/No) column to the given sheets of a validation output workbook.
    Codes in `skip` (FAS special cases) get "N/A กรณีพิเศษ" and are not looked up."""
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    wb = load_workbook(xlsx_path)
    targets = []
    for name in sheets:
        if name not in wb.sheetnames:
            continue
        ws = wb[name]
        headers = [c.value for c in ws[1]]
        if code_header not in headers or "Found in BIM" in headers:
            continue
        targets.append((ws, headers.index(code_header) + 1))
    if not targets:
        return False
    skip = {normalize(c) for c in skip}
    codes = {normalize(ws.cell(r, col).value) for ws, col in targets for r in range(2, ws.max_row + 1)
             if ws.cell(r, col).value is not None} - skip
    counts = count_by_tag(codes)
    for ws, col in targets:
        new = ws.max_column + 1
        head = ws.cell(1, new, "Found in BIM")
        ref = ws.cell(1, col)
        if ref.has_style:
            head._style = ref._style
        for r in range(2, ws.max_row + 1):
            value = ws.cell(r, col).value
            if value is not None:
                code = normalize(value)
                ws.cell(r, new, "N/A กรณีพิเศษ" if code in skip else "Yes" if counts.get(code) else "No")
        ws.column_dimensions[get_column_letter(new)].width = 14
        if ws.auto_filter.ref:
            ws.auto_filter.ref = ws.dimensions
    wb.save(xlsx_path)
    return True


def status():
    """Files in the index, for /bimstatus."""
    info = {"configured": configured(),
            "source": "Google Drive -> " + DOWNLOAD_DIR if DRIVE_FOLDER else (LOCAL_FOLDER or "update_bim_db.py"),
            "last_sync": last_sync(), "syncing": _state["syncing"], "error": _state["last_error"], "files": []}
    if os.path.exists(DB_PATH):
        con = _connect()
        try:
            info["files"] = [{"component": c, "name": n, "rows": r} for c, n, r in
                             con.execute("SELECT component, name, rows FROM files ORDER BY component")]
        finally:
            con.close()
    return info


# ---------------------------------------------------------------- reports
def _t(lang, th, en):
    return th if lang == "th" else en


def _pct(n, total):
    return f"{(n / total * 100 if total else 0):.2f}%"


def _as_of(lang):
    when = last_sync()
    return _t(lang, f"ข้อมูล BIM ณ {when}" if when else "ยังไม่มีข้อมูล BIM",
              f"BIM data as of {when}" if when else "No BIM data yet")


def _element_line(e):
    sheet = f"sheet {e['source_sheet']}" if e.get("source_sheet") else ""
    parts = [e["bim_file"], sheet, e["discipline"], e["category"], e["family"]]
    return " | ".join(p for p in parts if p) + (f" | Status {e['status']}" if e.get("status") else "")


def format_lookup(results, lang="th", details_per_code=3):
    total = len(results)
    found = [c for c, els in results.items() if els]
    lines = [_t(lang, "ผลค้น index codes ใน BIM", "Index codes BIM lookup"), _as_of(lang),
             _t(lang, f"index codes ทั้งหมด: {total:,}", f"Total index codes: {total:,}"),
             _t(lang, f"พบใน BIM: {len(found):,} index codes {_pct(len(found), total)}",
                f"Found in BIM: {len(found):,} index codes {_pct(len(found), total)}"),
             _t(lang, f"ไม่พบใน BIM: {total - len(found):,} index codes {_pct(total - len(found), total)}",
                f"Not in BIM: {total - len(found):,} index codes {_pct(total - len(found), total)}"), ""]
    for i, (code, els) in enumerate(results.items(), 1):
        if els:
            lines.append(f"{i}. {code}: " + _t(lang, f"พบ {len(els)} elements", f"{len(els)} elements"))
            lines += [f"   - {_element_line(e)}" for e in els[:details_per_code]]
            if len(els) > details_per_code:
                lines.append(_t(lang, f"   และอีก {len(els) - details_per_code} elements", f"   and {len(els) - details_per_code} more"))
        else:
            lines.append(f"{i}. {code}: " + _t(lang, "ไม่พบใน BIM", "not in BIM"))
    return "\n".join(lines).strip()


def _codes_from_file(path):
    """(DataFrame of 'index codes' + 'Source Sheet', column used) using the validator's own file reader."""
    import validate_index_code as vic
    log_lines = []
    df = vic.read_input(path, log_lines)
    if df is None:
        return None, None
    df = df.loc[:, ~df.columns.duplicated(keep="first")]
    col = vic.find_check_column(df)
    if col is None:
        return None, None
    df = df.dropna(subset=[col])
    raw = df[col].astype(str).map(lambda x: x if "_" not in x else "_".join(x.split("_")[:-1]))
    out = df.assign(**{"index codes": raw.map(normalize), "raw": raw})[["index codes", "raw", "Source Sheet"]]
    return out[(out["index codes"] != "") & (out["index codes"] != "NAN")], str(col)


def _tags_with_components(components):
    if not os.path.exists(DB_PATH) or not components:
        return set()
    con = _connect()
    try:
        tags = set()
        for comp in components:
            tags |= {r[0] for r in con.execute("SELECT DISTINCT tag FROM elements WHERE tag LIKE ?", (f"{comp}-%",))}
        return tags
    finally:
        con.close()


def _tag_counts_with_components(components):
    """{tag: number of BIM elements} for every tag of these components."""
    if not os.path.exists(DB_PATH) or not components:
        return {}
    con = _connect()
    try:
        out = {}
        for comp in components:
            out.update(con.execute("SELECT tag, COUNT(*) FROM elements WHERE tag LIKE ? GROUP BY tag", (f"{comp}-%",)))
        return out
    finally:
        con.close()


def _validate_codes(codes):
    """{code: (TYPE, validation_result)} with the same validation as /check (rule 9 counted over these codes)."""
    import obk_validator
    import validate_index_code as vic
    md = obk_validator.get_master()
    uniq = list(dict.fromkeys(codes))
    if md is None:
        return {c: ("", "") for c in uniq}
    rc = vic.count_running_keys(uniq, room=True)
    out = {}
    for c in uniq:
        en, _, typ, _ = vic.validate_index_code(c, md, rc, True)
        out[c] = (vic.apply_override(c, typ), en)
    return out


def _report_frame(codes, validated, fas):
    """Rows for compare_asset_id.py: Asset ID, TYPE, validation_result (+ NEW TYPE = N/A for FAS special cases)."""
    import pandas as pd
    import validate_index_code as vic
    import fas_rules
    rows = []
    for raw, code in codes:
        typ, result = validated[raw]
        row = {"Asset ID": code, "TYPE": typ, "validation_result": result}
        if fas:
            row["NEW TYPE"] = "N/A" if fas_rules.is_special(raw, vic.get_equipment(raw)) else ""
        rows.append(row)
    columns = ["Asset ID", "TYPE"] + (["NEW TYPE"] if fas else []) + ["validation_result"]
    return pd.DataFrame(rows, columns=columns)


def _write_report(sub, bim, sub_name, bim_name, path_out):
    """The Asset ID Comparison Report (compare_asset_id.py, unchanged) for these two lists."""
    import contextlib
    import shutil
    import tempfile
    import compare_asset_id
    tmp = tempfile.mkdtemp(prefix="bim_report_")
    try:
        sub_path, bim_path = os.path.join(tmp, sub_name), os.path.join(tmp, bim_name)
        sub.to_excel(sub_path, index=False)
        bim.to_excel(bim_path, index=False)
        with contextlib.redirect_stdout(io.StringIO()):     # it prints a console summary
            compare_asset_id.compare_asset_id(sub_path, bim_path, path_out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def compare_file(path, file_name, out_dir, lang="th", with_stats=False, fas=False):
    """Check every index code of a file against BIM. Returns (reply text, xlsx path or None[, stats]).
    fas: FAS check, only CM-FIRE codes; FAS special cases (N/A กรณีพิเศษ) are not looked up in BIM."""
    import fas_rules

    codes_df, col = _codes_from_file(path)
    if codes_df is not None and fas:
        codes_df = codes_df[codes_df["index codes"].map(fas_rules.keep_row)]
    if codes_df is None or codes_df.empty:
        msg = _t(lang, f"หนูไม่พบคอลัมน์ index codes ในไฟล์ {file_name} ค่ะ",
                 f"I couldn't find an index codes column in {file_name}.")
        return (msg, None, None) if with_stats else (msg, None)

    sub = _report_frame(zip(codes_df["raw"], codes_df["index codes"]), _validate_codes(codes_df["raw"]), fas)
    special = set(sub.loc[sub.get("NEW TYPE", "") == "N/A", "Asset ID"]) if fas else set()
    unique = [c for c in dict.fromkeys(codes_df["index codes"]) if c not in special]
    counts = count_by_tag(unique)
    total = len(unique)
    found = sum(1 for c in unique if counts.get(c))
    missing = [c for c in unique if not counts.get(c)]
    components = {c.split("-")[0] for c in unique if "-" in c}
    bim_tags = _tag_counts_with_components(components)
    if fas:
        bim_tags = {t: n for t, n in bim_tags.items() if fas_rules.keep_row(t)}
    bim_rows = [(t, t) for t, n in sorted(bim_tags.items()) for _ in range(n)]     # one row per BIM element
    bim = _report_frame(bim_rows, _validate_codes(bim_tags), fas)
    only_in_bim = sorted(set(bim_tags) - set(unique) - special)

    stem = os.path.splitext(os.path.basename(file_name))[0]
    path_out = os.path.join(out_dir, f"{stem}_BIM_check.xlsx")
    as_of = (last_sync() or "").split(" ")[0].replace("-", "")
    _write_report(sub, bim, os.path.basename(file_name) if file_name.lower().endswith(".xlsx") else f"{stem}.xlsx",
                  f"BIM_DB_{as_of or 'latest'}.xlsx", path_out)

    lines = [_t(lang, "ผลเทียบ index codes กับ BIM", "Index codes vs BIM"), _t(lang, "ไฟล์: ", "File: ") + file_name,
             _as_of(lang),
             _t(lang, f"index codes ทั้งหมด: {total:,}", f"Total index codes: {total:,}"),
             _t(lang, f"พบใน BIM: {found:,} index codes {_pct(found, total)}",
                f"Found in BIM: {found:,} index codes {_pct(found, total)}"),
             _t(lang, f"ไม่พบใน BIM: {len(missing):,} index codes {_pct(len(missing), total)}",
                f"Not in BIM: {len(missing):,} index codes {_pct(len(missing), total)}")]
    lines += [f"  {c}" for c in missing[:10]]
    if len(missing) > 10:
        lines.append(_t(lang, f"  และอีก {len(missing) - 10:,} index codes ดูในไฟล์", f"  and {len(missing) - 10:,} more in the file"))
    lines.append(_t(lang, f"มีใน BIM แต่ไม่มีในไฟล์: {len(only_in_bim):,} index codes Component {', '.join(sorted(components))}",
                    f"In BIM but not in the file: {len(only_in_bim):,} index codes, components {', '.join(sorted(components))}"))
    if with_stats:
        stats = {"total": total, "found": found, "missing": len(missing), "only_in_bim": len(only_in_bim)}
        return "\n".join(lines), path_out, stats
    return "\n".join(lines), path_out


def format_status(lang="th"):
    s = status()
    if not s["configured"] and not ready():
        return _t(lang, "ยังไม่ได้ตั้งค่าแหล่งไฟล์ BIM (BIM_LOCAL_FOLDER หรือ BIM_DRIVE_FOLDER)",
                  "BIM source not configured (BIM_LOCAL_FOLDER or BIM_DRIVE_FOLDER)")
    nxt = next_scheduled()
    lines = [_t(lang, "สถานะข้อมูล BIM", "BIM index status"), f"Source: {s['source']}", _as_of(lang),
             _t(lang, f"อัปเดตอัตโนมัติทุกสัปดาห์ ครั้งถัดไป {nxt:%Y-%m-%d %H:%M}",
                f"Updated weekly, next update {nxt:%Y-%m-%d %H:%M}")]
    if s["syncing"]:
        lines.append(_t(lang, "กำลังซิงก์อยู่", "Sync in progress"))
    total = sum(f["rows"] for f in s["files"])
    lines.append(_t(lang, f"ไฟล์: {len(s['files'])} Component, {total:,} elements", f"Files: {len(s['files'])} components, {total:,} elements"))
    lines += [f"- {f['component']}: {f['name']} {f['rows']:,}" for f in s["files"]]
    if s["error"]:
        lines.append("Error: " + s["error"])
    return "\n".join(lines)


def search_partial(fragment, limit=20):
    """Index codes in BIM that start with (or else contain) a fragment. Returns ([(tag, elements)], total matches)."""
    frag = normalize(fragment)
    if not frag or not os.path.exists(DB_PATH):
        return [], 0
    con = _connect()
    try:
        for pattern in (f"{frag}%", f"%{frag}%"):               # prefix uses the index; contains is the fallback
            total = con.execute("SELECT COUNT(DISTINCT tag) FROM elements WHERE tag LIKE ?", (pattern,)).fetchone()[0]
            if total:
                rows = con.execute("SELECT tag, COUNT(*) FROM elements WHERE tag LIKE ? GROUP BY tag ORDER BY tag LIMIT ?",
                                   (pattern, limit)).fetchall()
                return rows, total
        return [], 0
    finally:
        con.close()


def format_partial(fragment, rows, total, lang="th"):
    frag = normalize(fragment)
    if not total:
        return _t(lang, f"ไม่พบ index codes ที่มี {frag} ใน BIM", f"No index codes containing {frag} in BIM")
    lines = [_t(lang, f"index codes ใน BIM ที่มี {frag}: {total:,} index codes", f"Index codes in BIM containing {frag}: {total:,}")]
    lines += [f"- {tag}" + (_t(lang, f" {n} elements", f" {n} elements") if n > 1 else "") for tag, n in rows]
    if total > len(rows):
        lines.append(_t(lang, f"และอีก {total - len(rows):,} index codes พิมพ์ให้ยาวขึ้นเพื่อแคบผลลัพธ์",
                        f"and {total - len(rows):,} more; type a longer part to narrow it down"))
    return "\n".join(lines)


# Part of an index code: has a letter and at least one dash, e.g. CUP-002-0027 or AC-AHUS (not dates like 2026-10-15)
PARTIAL_RE = re.compile(r"(?<![A-Za-z0-9-])(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9]+(?:-[A-Za-z0-9]+){1,5}(?![A-Za-z0-9-])")


def find_partials(text):
    return [m.group(0) for m in PARTIAL_RE.finditer(text) if len(m.group(0)) >= 5]


def answer_lookup(tokens, lang="th"):
    """Full index codes -> exact lookup; parts of codes -> partial search. One reply text."""
    full = [t for t in tokens if t.count("-") >= 4]
    parts = [t for t in tokens if t.count("-") < 4]
    blocks = []
    if full:
        blocks.append(format_lookup(lookup(full), lang))
    for p in parts[:3]:
        rows, total = search_partial(p)
        blocks.append(format_partial(p, rows, total, lang))
    if parts and not full:
        blocks.insert(0, _as_of(lang))
    return "\n\n".join(blocks)
