"""Validate Excel/CSV files sent in LINE and keep the outputs downloadable for a while."""
import os
import re
import secrets
import shutil
import tempfile
import threading
import time

import obk_validator

ALLOWED_EXTENSIONS = ('.xlsx', '.xlsm', '.xls', '.csv')
MAX_FILE_BYTES = int(os.getenv("OBK_MAX_FILE_MB", "20")) * 1024 * 1024
DOWNLOAD_TTL_SECONDS = int(os.getenv("OBK_DOWNLOAD_TTL_HOURS", "24")) * 3600
WORK_ROOT = os.path.join(tempfile.gettempdir(), "anichan_obk")

# Thai fonts to fall back to for Thai file/sheet names in the pie chart (Windows, Linux, macOS)
THAI_FONTS = ['Leelawadee UI', 'Leelawadee', 'Tahoma', 'Loma', 'Garuda', 'Noto Sans Thai', 'Thonburi']

try:
    import matplotlib
    from matplotlib import font_manager
    installed = {f.name for f in font_manager.fontManager.ttflist}
    matplotlib.rcParams['font.family'] = ['DejaVu Sans'] + [f for f in THAI_FONTS if f in installed]
except ImportError:
    pass

# token -> (job_dir, {filename: path}, expires_at)
_downloads = {}
_lock = threading.Lock()


def is_supported(file_name):
    return file_name.lower().endswith(ALLOWED_EXTENSIONS)


def safe_name(file_name):
    name = os.path.basename(file_name or "input.xlsx")
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name) or "input.xlsx"


def _cleanup_expired():
    now = time.time()
    with _lock:
        expired = [t for t, (_, _, exp) in _downloads.items() if exp < now]
        for t in expired:
            job_dir, _, _ = _downloads.pop(t)
            shutil.rmtree(job_dir, ignore_errors=True)


def get_download(token, file_name):
    with _lock:
        entry = _downloads.get(token)
    if not entry or entry[2] < time.time():
        return None
    return entry[1].get(file_name)


def validate_file(file_name, data):
    """Run the original file pipeline. Returns (result dict, token for downloads)."""
    # Imported lazily so the chat bot still starts if pandas isn't installed
    import validate_index_code as vic

    _cleanup_expired()
    token = secrets.token_urlsafe(16)
    job_dir = os.path.join(WORK_ROOT, token)
    os.makedirs(job_dir, exist_ok=True)
    path = os.path.join(job_dir, safe_name(file_name))
    with open(path, 'wb') as f:
        f.write(data)

    result = vic.process_file(path, obk_validator.get_master(), os.path.join(job_dir, "out"), room=True, dup_mode='sheet')
    files = {os.path.basename(p): p for p in result.get('outputs', [])}
    with _lock:
        _downloads[token] = (job_dir, files, time.time() + DOWNLOAD_TTL_SECONDS)
    return result, token


def format_summary(result, file_name):
    """Plain-text summary for LINE (no Markdown)."""
    if result.get('skipped'):
        msg = [f"ไฟล์ {file_name} ตรวจไม่ได้ค่ะพี่: {result['skipped']}"]
        if result.get('columns'):
            msg.append("คอลัมน์ที่หนูเจอ: " + ", ".join(result['columns'][:15]))
            msg.append("หนูหาคอลัมน์ชื่อ Asset ID, IndexCode, Index, Equipment ID, Name ฯลฯ ลองเปลี่ยนชื่อหัวคอลัมน์แล้วส่งใหม่นะคะ")
        return "\n".join(msg)

    lines = [
        f"ผลตรวจไฟล์ {file_name}",
        f"คอลัมน์ที่ใช้: {result['column_used']}",
        f"จำนวนแถว: {result['records']:,} (Asset ID ไม่ซ้ำ {result['unique_assets']:,})",
        "",
        f"QLT (TYPE A): {result['QLT_type_a']['count']:,} ({result['QLT_type_a']['pct']}%)",
        f"BFV (รูปแบบ BIM ถูก): {result['BFV_correct_format']['count']:,} ({result['BFV_correct_format']['pct']}%)",
        f"Asset Type อยู่ใน Approve list: {result['approved_asset_type']['count']:,} ({result['approved_asset_type']['pct']}%)",
        "",
        "แยกตาม TYPE:",
    ]
    for t, v in result['types'].items():
        if v['count']:
            lines.append(f"- {obk_validator.TYPE_ICON.get(t, '')} {t}: {v['count']:,} ({v['pct']}%)")
    lines.append(f"Asset ID ซ้ำข้ามชีท: {result['duplicates']:,}")

    if result['top_rules_unique_assets']:
        lines += ["", "กฎที่ผิดบ่อยที่สุด (นับ Asset ID ไม่ซ้ำ):"]
        lines += [f"- กฎ {rule}: {n:,}" for rule, n in result['top_rules_unique_assets'][:5]]
    if len(result['worst_sheets']) > 1:
        lines += ["", "ชีทที่ TYPE A น้อยสุด:"]
        lines += [f"- {w['Source Sheet']}: {w['TYPE A %']}% จาก {w['TOTAL']:,}" for w in result['worst_sheets'][:3]]
    if result['sample_failures']:
        lines += ["", "ตัวอย่างที่ไม่ผ่าน:"]
        for s in result['sample_failures'][:5]:
            lines.append(f"- {s['Asset ID']} ({s['TYPE']}): {s['validation_result_thai'].split('; ')[0]}")
    return "\n".join(lines)
