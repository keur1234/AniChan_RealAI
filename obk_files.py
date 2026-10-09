"""Validate Excel/CSV files sent in LINE and keep the outputs downloadable for a while."""
import os
import re
import secrets
import shutil
import tempfile
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

# Each job lives in WORK_ROOT/<random token>/ on disk, so download links keep
# working after the bot restarts. Jobs older than the TTL are deleted.
TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{16,64}$')


def is_supported(file_name):
    return file_name.lower().endswith(ALLOWED_EXTENSIONS)


def safe_name(file_name):
    name = os.path.basename(file_name or "input.xlsx")
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name) or "input.xlsx"


def _cleanup_expired():
    if not os.path.isdir(WORK_ROOT):
        return
    cutoff = time.time() - DOWNLOAD_TTL_SECONDS
    for token in os.listdir(WORK_ROOT):
        job_dir = os.path.join(WORK_ROOT, token)
        try:
            if os.path.getmtime(job_dir) < cutoff:
                shutil.rmtree(job_dir, ignore_errors=True)
        except OSError:
            pass


def get_download(token, file_name):
    """Path of an output file of a job, or None if it doesn't exist or has expired."""
    if not TOKEN_RE.match(token) or os.path.basename(file_name) != file_name:
        return None
    out_dir = os.path.join(WORK_ROOT, token, "out")
    if not os.path.isdir(out_dir) or os.path.getmtime(os.path.join(WORK_ROOT, token)) < time.time() - DOWNLOAD_TTL_SECONDS:
        return None
    for root, _, files in os.walk(out_dir):
        if file_name in files:
            return os.path.join(root, file_name)
    return None


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
    return result, token


SKIP_REASONS_EN = {
    'ไฟล์ว่าง': 'the file is empty',
    'ไม่พบคอลัมน์ Asset ID': 'no index code column was found',
    'ไม่มี Asset ID ที่ใช้ได้': 'no usable index codes were found',
}


def format_summary(result, file_name, lang='th'):
    """File summary in the agreed OBK format: total, Incorrect, Validation result."""
    t = lambda th, en: obk_validator._t(lang, th, en)
    if result.get('skipped'):
        reason = (result['skipped'].replace('ไม่พบคอลัมน์ Asset ID', 'ไม่พบคอลัมน์ index codes')
                  .replace('ไม่มี Asset ID ที่ใช้ได้', 'ไม่มี index codes ที่ใช้ได้')
                  if lang == 'th' else SKIP_REASONS_EN.get(result['skipped'], result['skipped']))
        msg = [t(f"หนูไม่สามารถ validate ไฟล์ {file_name} ได้ค่ะ: {reason}", f"I couldn't validate {file_name}: {reason}")]
        if result.get('columns'):
            msg.append(t("คอลัมน์ที่พบในไฟล์: ", "Columns found: ") + ", ".join(result['columns'][:15]))
            msg.append(t("หนูอ่าน index codes จากคอลัมน์ชื่อ Asset ID, IndexCode, Index, Equipment ID หรือ Name "
                         "กรุณาแก้ชื่อหัวคอลัมน์แล้วส่งไฟล์อีกครั้งค่ะ",
                         "I read index codes from columns named Asset ID, IndexCode, Index, Equipment ID or Name. "
                         "Please fix the header row and send the file again."))
        return "\n".join(msg)

    type_counts = {typ: v['count'] for typ, v in result['types'].items()}
    lines = [t("ผล validation index codes", "Index codes validation result"), t("ไฟล์: ", "File: ") + file_name]
    dup_lines = [t(f"  {code} ซ้ำ {n} ครั้ง", f"  {code} appears {n} times") for code, n in result.get('duplicate_codes', [])]
    lines += obk_validator.summary_lines(result['records'], result.get('duplicate_codes_total', 0), dup_lines,
                                         type_counts.get('TYPE B', 0), type_counts, result.get('type_c_codes', []), lang)
    return "\n".join(lines)
