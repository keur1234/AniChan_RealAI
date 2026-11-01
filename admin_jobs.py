"""Vicky-Admin: jobs an administrator can ask Vicky to run ("อัปเดต BIM", "สร้าง bundle ใหม่", "รัน build_ref").

Built-in jobs are below. Your own scripts go in admin_jobs.json next to the program (see admin_jobs.example.json):

    {
      "bim_update": {
        "description": "รัน update_bim_db.py ในโฟลเดอร์โปรเจกต์ แล้วก๊อป bim_index.db มาใช้",
        "command": ["D:\\\\AniChan_RealAI\\\\myenv\\\\Scripts\\\\python.exe", "update_bim_db.py"],
        "cwd": "D:\\\\AniChan_RealAI",
        "timeout_minutes": 120,
        "copy": [{"from": "D:\\\\AniChan_RealAI\\\\bim_index.db", "to": "bim_index.db"}]
      }
    }

"copy" runs after the command succeeds: each file is copied over "to" (relative = next to the program).
A job in admin_jobs.json with the same name as a built-in one (e.g. bim_update) replaces it.

Only jobs listed here can run: Vicky picks a job by name, she can't run any other command.
Jobs run in the background; when one finishes, notify(text) gets the result.
"""
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from app_paths import APP_DIR

log = logging.getLogger("vicky")

JOBS_FILE = os.getenv("ADMIN_JOBS_FILE") or os.path.join(APP_DIR, "admin_jobs.json")
OUTPUT_TAIL_CHARS = 1500
BANGKOK = timezone(timedelta(hours=7))

_lock = threading.Lock()
_running = {}          # job name -> start time
_last = {}             # job name -> {"ok", "started", "finished", "result"}


def _now():
    return datetime.now(BANGKOK).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- built-in jobs
def _bim_update(full):
    """Read the BIM files already in the folder (copied there by hand) into the DB: no download, fast."""
    import bim_index
    report = bim_index.sync(force=full, download=False)
    return f"อ่านไฟล์จาก {bim_index.source_folder()}\n{report}\n\n{bim_index.format_status('th')}"


def _bim_download(full):
    import bim_index
    if not bim_index.DRIVE_FOLDER:
        raise RuntimeError("ยังไม่ได้ตั้ง BIM_DRIVE_FOLDER ใน .env")
    report = bim_index.sync(force=full)
    return f"{report}\n\n{bim_index.format_status('th')}"


def _reference_refresh(full):
    import obk_reference
    obk_reference.refresh(force=True)
    return obk_reference.status_text("th")


def _bundle_rebuild(full):
    """New obk_ref_bundle.json from OBK_MAPPING_FILE + OBK_AREA_FILE (+ the equipment Google Sheet); the old one is kept."""
    import obk_reference
    import validate_index_code as vic
    mapping, area = obk_reference._resolve(obk_reference.MAPPING_FILE), obk_reference._resolve(obk_reference.AREA_FILE)
    missing = [n for n, p in (("OBK_MAPPING_FILE", mapping), ("OBK_AREA_FILE", area)) if not p or not os.path.isfile(p)]
    if missing:
        raise RuntimeError("ไม่พบไฟล์ต้นทาง: " + ", ".join(missing) + " (ตั้ง path เต็มใน .env)")
    current = obk_reference._state.get("bundle") or {}
    bundle = {**vic.mapping_part(mapping), "bim_id": vic.location_part(area), "approve": list(current.get("approve", []))}
    sheet = ""
    if obk_reference.EQUIPMENT_SHEET_URL:
        sheet = obk_reference._fetch_equipment(bundle)
    path = obk_reference.bundle_path()
    backup = ""
    if os.path.isfile(path):
        backup = f"{os.path.splitext(path)[0]}.backup-{datetime.now(BANGKOK):%Y%m%d-%H%M}.json"
        shutil.copy2(path, backup)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)
    obk_reference.refresh(force=True)
    lines = [f"สร้าง {os.path.basename(path)} ใหม่แล้ว",
             f"- Area codes: {len(bundle['bim_id']):,}",
             f"- Equipment types: {len(bundle['equipment_type']):,}",
             f"- Approved equipment: {len(bundle['approve']):,}" + (f" ({sheet})" if sheet else " (ของเดิม)")]
    if backup:
        lines.append(f"ไฟล์เดิมเก็บไว้ที่ {backup}")
    return "\n".join(lines)


BUILT_IN = {
    "bim_update": ("อัปเดตฐานข้อมูล BIM จากไฟล์ที่ก๊อปมาวางในโฟลเดอร์ BIM แล้ว (ไม่โหลดจาก Drive, เร็ว) "
                   "ใช้เมื่อสั่ง 'อัปเดต BIM' (full=true อ่านใหม่ทุกไฟล์)", _bim_update),
    "bim_download": ("โหลดไฟล์ BIM ใหม่จาก Google Drive แล้วอัปเดต DB (ช้า) ใช้เฉพาะเมื่อสั่งให้โหลดจาก Drive", _bim_download),
    "reference_refresh": ("โหลดตารางอ้างอิงใหม่ทันที (Mapping, Area Code, Equipment Google Sheet, bundle)", _reference_refresh),
    "bundle_rebuild": ("สร้าง obk_ref_bundle.json ใหม่จาก OBK_MAPPING_FILE + OBK_AREA_FILE + Equipment Google Sheet "
                       "(เก็บไฟล์เดิมเป็น backup)", _bundle_rebuild),
}


# ---------------------------------------------------------------- your own scripts
def custom_jobs():
    """Jobs from admin_jobs.json: {name: {description, command, cwd?, timeout_minutes?}}."""
    if not os.path.isfile(JOBS_FILE):
        return {}
    try:
        with open(JOBS_FILE, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log.warning("admin_jobs.json could not be read: %s", e)
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict) and (v.get("command") or v.get("copy"))}


def _run_command(spec):
    command = spec["command"]
    timeout = float(spec.get("timeout_minutes", 30)) * 60
    started = time.time()
    proc = subprocess.run(command, shell=isinstance(command, str), cwd=spec.get("cwd") or None,
                          capture_output=True, timeout=timeout)
    out = (proc.stdout or b"") + (b"\n" + proc.stderr if proc.stderr else b"")
    for enc in ("utf-8", "cp874", "cp1252"):
        try:
            text = out.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = out.decode("utf-8", errors="replace")
    text = text.strip()
    if len(text) > OUTPUT_TAIL_CHARS:
        text = "...\n" + text[-OUTPUT_TAIL_CHARS:]
    summary = f"exit code {proc.returncode}, {time.time() - started:.0f} วินาที"
    if proc.returncode != 0:
        raise RuntimeError(f"{summary}\n{text}")
    return f"{summary}\n{text}" if text else summary


def _copy_outputs(spec):
    """Copy the job's output files ("copy": [{"from", "to"}]) into place; "to" is relative to the program's folder."""
    lines = []
    for item in spec.get("copy") or []:
        src = item.get("from", "")
        dst = item.get("to") or os.path.basename(src)
        if not os.path.isabs(dst):
            dst = os.path.join(APP_DIR, dst)
        if not os.path.isfile(src):
            raise RuntimeError(f"ไม่พบไฟล์ที่จะก๊อป: {src}")
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        tmp = dst + ".copying"
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)              # swap in one step so lookups never see a half-copied file
        lines.append(f"ก๊อป {src} -> {dst} ({os.path.getsize(dst) / 1048576:.1f} MB)")
    return "\n".join(lines)


def _run_custom(spec):
    parts = []
    if spec.get("command"):
        parts.append(_run_command(spec))
    if spec.get("copy"):
        parts.append(_copy_outputs(spec))
    return "\n".join(parts)


# ---------------------------------------------------------------- running
def jobs():
    """[{name, description, kind}] of every job an admin can run."""
    custom = custom_jobs()
    out = [{"name": n, "description": d, "kind": "built-in"} for n, (d, _) in BUILT_IN.items() if n not in custom]
    out += [{"name": n, "description": s.get("description", ""), "kind": "script"} for n, s in custom.items()]
    return out


def run(name, notify, full=False):
    """Start a job in the background. Returns (started, message). notify(text) is called when it ends."""
    custom = custom_jobs()
    if name not in BUILT_IN and name not in custom:
        return False, f"ไม่มีงานชื่อ {name} (มี: {', '.join(j['name'] for j in jobs())})"
    with _lock:
        if name in _running:
            return False, f"งาน {name} กำลังรันอยู่ (เริ่มเมื่อ {_running[name]})"
        _running[name] = _now()

    def work():
        started = _running[name]
        try:
            result = _run_custom(custom[name]) if name in custom else BUILT_IN[name][1](full)
            ok = True
        except subprocess.TimeoutExpired:
            ok, result = False, "หมดเวลา (timeout)"
        except Exception as e:
            log.exception("Admin job %s failed", name)
            ok, result = False, f"{type(e).__name__}: {e}"
        with _lock:
            _running.pop(name, None)
            _last[name] = {"ok": ok, "started": started, "finished": _now(), "result": result}
        log.info("Admin job %s %s", name, "done" if ok else "failed")
        try:
            notify(("✅" if ok else "❌") + f" งาน {name} {'เสร็จแล้ว' if ok else 'ไม่สำเร็จ'}\n{result}")
        except Exception:
            log.exception("Could not send the result of admin job %s", name)

    threading.Thread(target=work, name=f"admin-{name}", daemon=True).start()
    return True, f"เริ่มงาน {name} แล้ว" + (" (แบบ full)" if full else "") + " เสร็จแล้วหนูจะแจ้งผลในแชทนี้"


def status_text():
    lines = ["Vicky-Admin: งานที่สั่งได้"]
    for j in jobs():
        name = j["name"]
        if name in _running:
            state = f"กำลังรัน (เริ่ม {_running[name]})"
        elif name in _last:
            last = _last[name]
            state = f"ล่าสุด {last['finished']} {'สำเร็จ' if last['ok'] else 'ไม่สำเร็จ'}"
        else:
            state = "ยังไม่เคยรัน"
        lines.append(f"- {name}: {j['description']}\n  [{state}]")
    if not custom_jobs():
        lines.append(f"(ยังไม่มีสคริปต์ของคุณเอง: เพิ่มได้ใน {JOBS_FILE})")
    lines.append("สั่งได้เลย เช่น \"อัปเดต BIM\" หรือพิมพ์ /run <ชื่องาน> (ใส่ full ต่อท้ายเพื่ออ่านใหม่ทั้งหมด)")
    return "\n".join(lines)


def last_result(name):
    return _last.get(name)
