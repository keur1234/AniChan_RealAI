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
    """Formal plain-text summary for LINE (no Markdown)."""
    t = lambda th, en: obk_validator._t(lang, th, en)
    cnt = lambda n: f"{n:,} รายการ" if lang == 'th' else f"{n:,}"
    if result.get('skipped'):
        reason = (result['skipped'].replace('ไม่พบคอลัมน์ Asset ID', 'ไม่พบคอลัมน์ index code')
                  .replace('ไม่มี Asset ID ที่ใช้ได้', 'ไม่มี index code ที่ใช้ได้')
                  if lang == 'th' else SKIP_REASONS_EN.get(result['skipped'], result['skipped']))
        msg = [t(f"ไม่สามารถตรวจสอบไฟล์ {file_name} ได้: {reason}", f"Unable to validate {file_name}: {reason}")]
        if result.get('columns'):
            msg.append(t("คอลัมน์ที่พบในไฟล์: ", "Columns found: ") + ", ".join(result['columns'][:15]))
            msg.append(t("ระบบอ่าน index code จากคอลัมน์ชื่อ Asset ID, IndexCode, Index, Equipment ID, Name เป็นต้น "
                         "กรุณาตรวจสอบชื่อหัวคอลัมน์แล้วส่งไฟล์อีกครั้ง",
                         "Index codes are read from columns named Asset ID, IndexCode, Index, Equipment ID or Name. "
                         "Please check the header row and send the file again."))
        return "\n".join(msg)

    pct = lambda k: f"{cnt(result[k]['count'])} ({result[k]['pct']}%)"
    lines = [
        t("รายงานผลการตรวจสอบ index code", "Index code validation report"),
        t("ไฟล์", "File") + f": {file_name}",
        t("คอลัมน์ที่ใช้ตรวจสอบ", "Column checked") + f": {result['column_used']}",
        t(f"จำนวนรายการ: {result['records']:,} รายการ (index code ไม่ซ้ำ {result['unique_assets']:,} รายการ)",
          f"Records: {result['records']:,} ({result['unique_assets']:,} unique index codes)"),
        "",
        t("สรุปตัวชี้วัด", "Key metrics"),
        f"- QLT (TYPE A): {pct('QLT_type_a')}",
        t("- BFV (รูปแบบ BIM ถูกต้อง): ", "- BFV (correct BIM format): ") + pct('BFV_correct_format'),
        t("- Asset Type ในรายการที่อนุมัติ: ", "- Asset Type in approved list: ") + pct('approved_asset_type'),
        "",
        t("จำแนกตามผลการตรวจสอบ", "Breakdown by result"),
    ]
    for typ, v in result['types'].items():
        if v['count']:
            lines.append(f"- {obk_validator.type_label(typ, lang)}: {cnt(v['count'])} ({v['pct']}%)")
    lines.append(t("- index code ซ้ำข้ามชีท: ", "- Index codes duplicated across sheets: ") + cnt(result['duplicates']))

    if result['top_rules_unique_assets']:
        lines += ["", t("กฎที่พบข้อผิดพลาดมากที่สุด (นับตาม index code ไม่ซ้ำ)", "Most frequent rule failures (unique index codes)")]
        lines += [t(f"- กฎข้อ {rule}", f"- Rule {rule}") + f": {cnt(n)}" for rule, n in result['top_rules_unique_assets'][:5]]
    if len(result['worst_sheets']) > 1:
        lines += ["", t("ชีทที่มีสัดส่วน TYPE A ต่ำที่สุด", "Sheets with the lowest TYPE A rate")]
        lines += [f"- {w['Source Sheet']}: {w['TYPE A %']}% " + t("จาก", "of") + f" {cnt(w['TOTAL'])}"
                  for w in result['worst_sheets'][:3]]
    if result['sample_failures']:
        lines += ["", t("ตัวอย่าง index code ที่ไม่ผ่านเกณฑ์", "Examples of failed index codes")]
        for s in result['sample_failures'][:5]:
            reason = s['validation_result_thai'] if lang == 'th' else s.get('validation_result', s['validation_result_thai'])
            lines.append(f"- {s['Asset ID']} ({s['TYPE']}): {obk_validator.reason_text(reason.split('; ')[0])}")
    return "\n".join(lines)
