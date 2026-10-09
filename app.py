import base64
import csv
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from flask import Flask, abort, request, send_file
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from urllib.parse import quote
from werkzeug.middleware.proxy_fix import ProxyFix

import obk_files
import obk_validator
import vicky_tools

load_dotenv()

app = Flask(__name__)
# Behind Cloud Run / a reverse proxy: trust X-Forwarded-Proto/Host so links are https
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vicky")

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
# Used when the main model is out of quota (comma-separated, tried in order)
GEMINI_FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.1-flash-lite").split(",") if m.strip()]
# Number of past exchanges (user + Vicky) remembered per chat
MAX_TURNS = int(os.getenv("MAX_TURNS", "20"))
# Let Vicky look things up on Google for up-to-date answers (needs Search quota on the API key)
USE_GOOGLE_SEARCH = os.getenv("USE_GOOGLE_SEARCH", "0") == "1"
# After Search runs out of quota, answer without it for this long before trying again
SEARCH_COOLDOWN_SECONDS = 3600
search_disabled_until = 0.0
CHAT_LOG_FILE = os.getenv("CHAT_LOG_FILE", "chat_history.csv")
# Public https URL of this server, used for download links (auto-detected from the webhook if empty)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

LINE_REPLY_API = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_API = "https://api.line.me/v2/bot/message/push"
LINE_LOADING_API = "https://api.line.me/v2/bot/chat/loading/start"
LINE_MEMBER_API = "https://api.line.me/v2/bot/{kind}/{chat_id}/member/{user_id}"
LINE_LEAVE_API = "https://api.line.me/v2/bot/{kind}/{chat_id}/leave"
LINE_CONTENT_API = "https://api-data.line.me/v2/bot/message/{message_id}/content"
LINE_MAX_TEXT = 5000
LINE_MAX_MESSAGES = 5

BANGKOK_TZ = timezone(timedelta(hours=7))
THAI_WEEKDAYS = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]

RESET_COMMANDS = {"/reset", "/ลืม", "ลืมให้หมด", "เริ่มใหม่"}
CHECK_COMMANDS = ("/check", "/ตรวจ", "/เช็ค")
SUMMARY_COMMANDS = ("/summary", "/สรุป")
# Words that ask for the summary table when Vicky is @mentioned
SUMMARY_WORDS = ("ตารางสรุป", "สรุป", "summary")
# LINE user IDs of the people responsible for Vicky. She only serves groups that
# include one of them, and only they can use her in a 1:1 chat.
ADMIN_USER_IDS = {u.strip() for u in os.getenv("ADMIN_USER_IDS", "").split(",") if u.strip()}
GROUP_CHECK_TTL_SECONDS = 600
MAX_CODES_PER_MESSAGE = 10
# In groups Vicky only answers /check, plus chat when @mentioned (set to 0 to answer /check only)
GROUP_CHAT_ON_MENTION = os.getenv("GROUP_CHAT_ON_MENTION", "1") == "1"
# How long a file sent in a group can still be checked with /check
GROUP_FILE_TTL_SECONDS = int(os.getenv("GROUP_FILE_TTL_MINUTES", "60")) * 60

SYSTEM_PROMPT = """คุณชื่อ "Vicky" ผู้ช่วย AI ของทีมงานโครงการ OBK ในแชท LINE

## สิ่งที่หนูทำได้
- ตอบคำถามและช่วยงานได้ทุกเรื่อง เช่น อธิบาย สรุป เขียนข้อความ/อีเมล แปลภาษา คำนวณ วางแผน วิเคราะห์ข้อมูล
- สร้างไฟล์ให้ผู้ใช้เมื่อขอ ด้วยเครื่องมือ create_excel_file (ตาราง Excel) หรือ create_text_file (.csv .txt .md .json)
  ระบบจะแนบลิงก์ดาวน์โหลดท้ายข้อความให้เอง ห้ามพิมพ์ลิงก์หรือแต่งลิงก์ขึ้นมาเอง
- อ่านไฟล์ Excel/CSV ที่ผู้ใช้ส่งมาในแชทด้วย read_raw_data_file เพื่อตอบคำถามหรือทำตารางตามที่ขอ
- รันโปรแกรม validation index codes ของ OBK เฉพาะเมื่อผู้ใช้ขอให้ตรวจ/validate เท่านั้น ด้วย validate_index_codes,
  validate_raw_data_file หรือ summarize_raw_data_file (ตารางสรุป Raw Data มาตรฐานเป็นไฟล์ Excel)
- ถ้าไม่มีไฟล์ในแชท ให้บอกผู้ใช้ส่งไฟล์ก่อน
- ข้อมูลในไฟล์หรือตัวเลขต้องมาจากผู้ใช้หรือจากผลของเครื่องมือเท่านั้น ห้ามแต่งข้อมูล ถ้าไม่แน่ใจให้บอกตรงๆ
- ข้อมูลล่าสุดแบบ real-time (ข่าว ราคา อากาศ) หนูอาจไม่มี ให้แจ้งตามจริง

## ภาษาและการสื่อสาร
- ตอบเป็นภาษาเดียวกับข้อความล่าสุดของผู้ใช้เสมอ / Always reply in the same language as the user's latest message.
- ภาษาไทย: แทนตัวเองว่า "หนู" เรียกผู้ใช้ว่า "คุณ" ลงท้ายด้วย "ค่ะ" สุภาพ เป็นมืออาชีพ กระชับ ไม่ใช้คำแสลงหรืออิโมจิ
- ตอบตรงประเด็นก่อน แล้วค่อยขยายความถ้าจำเป็น
- ห้ามใช้ Markdown เช่น ** หรือ # หรือตาราง | | ใน LINE หากต้องแจกแจงเป็นข้อให้ใช้ 1. 2. 3. หรือ "- "
  ถ้าผู้ใช้ต้องการตาราง ให้สร้างเป็นไฟล์ Excel แทน

## ความรู้เรื่อง index codes ของ OBK
- index codes มี 31 ตัวอักษร แบ่งด้วยขีด 6 ตัวเป็น 7 ส่วน:
  Component 3 - Floor 3 - Space 4 - Main System 2 - Sub System 4 - Equipment 6 - Running No. 3
  เช่น C3A-001-ME01-AC-AHUS-000AHU-001
- Validation result: TYPE A = ผ่าน, TYPE B OR C = ไม่พบ Equipment/Asset Type ใน Reference Table,
  TYPE C = ผิดกฎบังคับ เช่น ความยาว ตัวอักษรพิเศษ จำนวนส่วน หรือ Component/Location/Floor/System ไม่อยู่ใน Reference Table,
  TYPE B = Running Number ซ้ำในไฟล์, N/A = ข้อยกเว้น ALLF หรือ suffix -A/-T/-H/NONE
- คำสั่งลัด: /check ตามด้วย index codes หรือส่งไฟล์แล้วพิมพ์ /check เพื่อ validate, /summary เพื่อขอตารางสรุป Raw Data

## ศัพท์ของโครงการ OBK (เมื่อพูดเรื่อง index codes ต้องใช้ให้ตรง)
- ใช้คำว่า "index codes" ไม่ใช้ index name, Asset ID, รหัส หรือ รายการ แทน
- ใช้คำว่า "validation" สำหรับการตรวจ index codes และเรียกผลว่า "Validation result"
- เรียกโครงการว่า "OBK" ไม่ใช้ One Bangkok / OneBangkok
- สรุปผล validation ตามรูปแบบ: index codes ทั้งหมด → Incorrect (TYPE C และ index codes ซ้ำ / Running Number ซ้ำ)
  → Validation result เช่น "TYPE A: 120 index codes 85.71%" ไม่มีวงเล็บ ทศนิยม 2 ตำแหน่ง ห้ามใช้คำว่า Red Flag
- ถ้ามีผลจากเครื่องมือ validation ให้ยึดผลนั้น ห้ามเปลี่ยน TYPE หรือตัวเลขเอง

## ข้อจำกัด
- หนูแก้ไขโค้ด การตั้งค่า หรือรูปแบบรายงานมาตรฐานของบอทไม่ได้ ห้ามบอกว่า "แก้ไข/อัปเดตแล้ว" ให้แจ้งว่าต้องให้ผู้ดูแลระบบปรับ
- หนูให้บริการเฉพาะใน LINE Group ของโครงการ OBK ที่มีผู้ดูแลอยู่

## ข้อมูลตอนนี้
- วันเวลาปัจจุบัน (เวลาประเทศไทย): {now}
"""

client = genai.Client(api_key=GEMINI_API_KEY)
executor = ThreadPoolExecutor(max_workers=int(os.getenv("WORKERS", "8")))

# chat_id -> deque of types.Content (in-memory, per gunicorn worker)
chat_histories = defaultdict(lambda: deque(maxlen=MAX_TURNS * 2))
# One lock per chat so messages from the same chat are answered in order
chat_locks = defaultdict(threading.Lock)
log_lock = threading.Lock()
# Files sent in groups wait for /check: (chat_id, user_id) -> latest file, message_id -> file
pending_files_by_user = {}
pending_files_by_id = {}
pending_lock = threading.Lock()
# user_id -> 'th' or 'en' (language of their last message; fixed messages use it)
user_langs = {}
THAI_CHARS_RE = re.compile(r"[\u0E00-\u0E7F]")


def detect_language(text):
    """'th' for Thai, 'en' for any other written language, None if there are no words (just codes)."""
    text = re.sub(r"^/\S+", " ", text)                     # /check
    text = re.sub(r"https?://\S+|@\S+", " ", text)          # links, @mentions
    for code in obk_validator.find_codes(text):
        text = text.replace(code, " ")
    if THAI_CHARS_RE.search(text):
        return "th"
    if re.search(r"[^\W\d_]{2,}", text):
        return "en"
    return None


def check_help(user_id):
    return tr(user_id,
              "วิธีใช้งาน: พิมพ์ /check ตามด้วย index codes หรือส่งไฟล์ Excel/CSV แล้วพิมพ์ /check ค่ะ\n"
              "ตัวอย่าง: /check C3A-001-ME01-AC-AHUS-000AHU-001",
              "How to use: type /check followed by index codes, or send an Excel/CSV file and then type /check\n"
              "Example: /check C3A-001-ME01-AC-AHUS-000AHU-001")


def no_reference_text(user_id):
    return tr(user_id, "หนูยังไม่พบไฟล์อ้างอิง obk_ref_bundle.json จึงยังไม่สามารถ validate index codes ได้ค่ะ",
              "I can't find the reference file obk_ref_bundle.json, so I can't validate index codes right now.")


def tr(user_id, th, en):
    """Pick the Thai or English wording for this user."""
    return th if user_langs.get(user_id, "th") == "th" else en


def thai_now():
    now = datetime.now(BANGKOK_TZ)
    return f"วัน{THAI_WEEKDAYS[now.weekday()]}ที่ {now.day}/{now.month}/{now.year + 543} เวลา {now:%H:%M} น."


def is_quota_exhausted(e):
    """429 that waiting a few seconds won't fix (daily quota, or no free quota for this model/tool)."""
    text = str(e)
    return e.code == 429 and ("limit: 0" in text or "PerDay" in text or "per day" in text.lower())


def call_with_retry(api_call, max_retries=2, initial_delay=2):
    delay = initial_delay
    for attempt in range(max_retries + 1):
        try:
            return api_call()
        except genai_errors.APIError as e:
            # Retry on rate limit / overloaded / transient server errors
            if e.code in (429, 500, 503, 504) and attempt < max_retries and not is_quota_exhausted(e):
                log.warning("Gemini error %s, retry %d/%d in %ss: %s", e.code, attempt + 1, max_retries, delay, e.message)
                time.sleep(delay)
                delay *= 2
            else:
                raise


def generate_with_fallback(contents, system_instruction, tools=()):
    """Try the main model with Google Search, then without it, then the fallback models."""
    global search_disabled_until
    attempts = []
    if USE_GOOGLE_SEARCH and time.time() >= search_disabled_until:
        attempts.append((GEMINI_MODEL, True))
    attempts.append((GEMINI_MODEL, False))
    attempts += [(m, False) for m in GEMINI_FALLBACK_MODELS if m != GEMINI_MODEL]

    for i, (model, search) in enumerate(attempts):
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=([types.Tool(google_search=types.GoogleSearch())] if search else []) + list(tools) or None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            return call_with_retry(
                lambda: client.models.generate_content(model=model, contents=contents, config=config),
                max_retries=0 if search else 2,   # don't wait on Search; answering without it is fine
            )
        except genai_errors.APIError as e:
            if search and e.code in (400, 403, 429):
                search_disabled_until = time.time() + SEARCH_COOLDOWN_SECONDS
                log.warning("Google Search unavailable (%s), answering without it for the next hour", e.code)
            # Quota / unknown model / tool not allowed: try the next option
            if e.code in (400, 403, 404, 429) and i < len(attempts) - 1:
                log.warning("%s (search=%s) failed with %s: %s -> trying next option", model, search, e.code, e.message)
                continue
            raise


MAX_TOOL_ROUNDS = 5


def generate_response(chat_id, user_parts, history_text, user_id="", ctx=None):
    """Ask Gemini for Vicky's reply, running any tools she calls. Returns (reply text, download links)."""
    history = chat_histories[chat_id]
    contents = list(history) + [types.Content(role="user", parts=user_parts)]
    ctx = ctx or {}
    links = []
    prompt = SYSTEM_PROMPT.format(now=thai_now())

    for _ in range(MAX_TOOL_ROUNDS):
        response = generate_with_fallback(contents, prompt, [vicky_tools.FUNCTION_TOOL])
        calls = response.function_calls or []
        if not calls:
            break
        contents.append(response.candidates[0].content)
        parts = []
        for call in calls:
            result = run_tool(call.name, dict(call.args or {}), ctx)
            links += result.pop("_links", [])
            parts.append(types.Part.from_function_response(name=call.name, response=result))
        contents.append(types.Content(role="user", parts=parts))

    text = "".join(p.text for p in (response.candidates[0].content.parts or []) if getattr(p, "text", None) and not p.thought) \
        if response.candidates and response.candidates[0].content else ""
    reply = text.strip() or (tr(user_id, "หนูดำเนินการให้เรียบร้อยแล้วค่ะ", "Done.") if links else
                             tr(user_id, "ขออภัยค่ะ หนูไม่สามารถสร้างคำตอบได้ กรุณาลองถามใหม่อีกครั้งค่ะ",
                                "Sorry, I couldn't come up with an answer. Please try asking again."))
    remember(chat_id, history_text, reply)
    return reply, links


def run_tool(name, args, ctx):
    """Execute one tool call from Gemini. Returns a JSON-able dict; '_links' are download links for the user."""
    user_id = ctx.get("user_id", "")
    lang = user_langs.get(user_id, "th")
    log.info("Tool call %s %s", name, str(args)[:200])
    try:
        if name in ("create_excel_file", "create_text_file"):
            token, out_dir = obk_files.new_job()
            if name == "create_excel_file":
                path = vicky_tools.create_excel(out_dir, args.get("file_name"), args.get("sheets") or [])
            else:
                path = vicky_tools.create_text(out_dir, args.get("file_name"), args.get("content", ""))
            return {"ok": True, "file": os.path.basename(path), "_links": [file_link(ctx, token, path)]}

        if name == "validate_index_codes":
            master = obk_validator.get_master()
            if master is None:
                return {"error": "reference file obk_ref_bundle.json is missing"}
            codes = [str(c) for c in args.get("codes", [])][:MAX_CODES_PER_MESSAGE]
            return {"report": obk_validator.format_report([obk_validator.check_code(c, master) for c in codes], lang)}

        file_info = find_group_file(ctx.get("chat_id"), user_id, ctx.get("quoted"))
        if not file_info:
            return {"error": "No file has been sent in this chat recently. Ask the user to send the Excel/CSV file first."}

        if name == "read_raw_data_file":
            file_name = obk_files.safe_name(file_info.get("fileName", ""))
            if not obk_files.is_supported(file_name):
                return {"error": f"{file_name} is not an Excel/CSV file"}
            data, _ = download_line_content(file_info["id"])
            token, out_dir = obk_files.new_job()
            path = os.path.join(out_dir, file_name)
            with open(path, "wb") as f:
                f.write(data)
            return vicky_tools.preview_file(path, file_name)

        if name in ("validate_raw_data_file", "summarize_raw_data_file"):
            file_name, result, token, error = validate_file_info(file_info, user_id)
            if error:
                return {"error": error}
            summary = obk_files.format_summary(result, file_name, lang)
            if result.get("skipped"):
                return {"summary": summary}
            if name == "summarize_raw_data_file":
                paths = [obk_files.build_summary_workbook(result, file_name, lang)]
            else:
                paths = [p for p in result.get("outputs", []) if not p.endswith(".png")]
            return {"summary": summary, "files": [os.path.basename(p) for p in paths],
                    "_links": [file_link(ctx, token, p) for p in paths]}
    except Exception as e:
        log.exception("Tool %s failed", name)
        return {"error": f"{type(e).__name__}: {e}"}
    return {"error": f"unknown tool {name}"}


def file_link(ctx, token, path):
    name = os.path.basename(path)
    return f"- {name}\n{ctx.get('base_url', '')}/files/{token}/{quote(name)}"


def remember(chat_id, user_text, reply):
    history = chat_histories[chat_id]
    history.append(types.Content(role="user", parts=[types.Part.from_text(text=user_text)]))
    history.append(types.Content(role="model", parts=[types.Part.from_text(text=reply)]))


def extract_codes(text):
    """Returns (codes, is_check_command)."""
    lowered = text.lower()
    command = next((c for c in CHECK_COMMANDS if lowered.startswith(c)), None)
    if command:
        body = text[len(command):]
        codes = obk_validator.find_codes(body) or body.split()
        return codes[:MAX_CODES_PER_MESSAGE], True
    return obk_validator.find_codes(text)[:MAX_CODES_PER_MESSAGE], False


def clean_for_line(text):
    """LINE doesn't render Markdown, so strip the common syntax."""
    text = re.sub(r"```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[*•]\s+", "- ", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", text)
    return text.strip()


def split_for_line(text):
    """Split into LINE-sized chunks (max 5 messages x 5000 chars), preferring paragraph breaks."""
    chunks = []
    while text and len(chunks) < LINE_MAX_MESSAGES:
        if len(text) <= LINE_MAX_TEXT:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, LINE_MAX_TEXT)
        if cut < LINE_MAX_TEXT // 2:
            cut = LINE_MAX_TEXT
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return chunks


def line_headers():
    return {
        "Content-Type": "application/json; charset=UTF-8",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }


def mention_message(text, user_id):
    """Text message (v2) that starts by @mentioning user_id."""
    escaped = text.replace("{", "{{").replace("}", "}}")
    return {
        "type": "textV2",
        "text": "{user} " + escaped,
        "substitution": {"user": {"type": "mention", "mentionee": {"type": "user", "userId": user_id}}},
    }


def post_messages(reply_token, to, messages):
    """Reply with the reply token; fall back to push if the token expired. Returns True on success."""
    try:
        r = requests.post(LINE_REPLY_API, headers=line_headers(),
                          data=json.dumps({"replyToken": reply_token, "messages": messages}), timeout=10)
        r.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        body = getattr(e.response, "text", "")
        log.warning("Reply failed (%s %s), falling back to push", e, body)
        if getattr(e.response, "status_code", None) == 400:
            return False          # bad message, push would fail the same way
    try:
        r = requests.post(LINE_PUSH_API, headers=line_headers(),
                          data=json.dumps({"to": to, "messages": messages}), timeout=10)
        r.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        log.error("Push failed: %s %s", e, getattr(e.response, "text", ""))
        return False


def send_reply(reply_token, to, text, extra_messages=(), mention_user_id=None):
    extra_messages = list(extra_messages)
    chunks = split_for_line(clean_for_line(text))[:LINE_MAX_MESSAGES - len(extra_messages)]
    plain = [{"type": "text", "text": chunk} for chunk in chunks]
    if mention_user_id and plain:
        mentioned = [mention_message(chunks[0], mention_user_id)] + plain[1:]
        if post_messages(reply_token, to, mentioned + extra_messages):
            return
        log.warning("Mention reply failed, sending without mention")
    post_messages(reply_token, to, plain + extra_messages)


def show_loading(user_id):
    """Show the '...' typing animation (only works in 1:1 chats)."""
    try:
        requests.post(LINE_LOADING_API, headers=line_headers(),
                      data=json.dumps({"chatId": user_id, "loadingSeconds": 60}), timeout=5)
    except requests.exceptions.RequestException:
        pass


def download_line_content(message_id):
    r = requests.get(LINE_CONTENT_API.format(message_id=message_id),
                     headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}, timeout=30)
    r.raise_for_status()
    return r.content, r.headers.get("Content-Type", "image/jpeg")


def store_chat_history_to_csv(chat_id, user_id, user_message, bot_message):
    header = ["timestamp", "chat_id", "user_id", "user_message", "bot_message"]
    with log_lock:
        file_exists = os.path.isfile(CHAT_LOG_FILE)
        with open(CHAT_LOG_FILE, mode="a", newline="", encoding="UTF-8") as file:
            writer = csv.DictWriter(file, fieldnames=header)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                "timestamp": datetime.now(BANGKOK_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                "chat_id": chat_id,
                "user_id": user_id,
                "user_message": user_message,
                "bot_message": bot_message,
            })


def remember_group_file(chat_id, user_id, message):
    info = {
        "id": message["id"],
        "fileName": message.get("fileName", ""),
        "fileSize": message.get("fileSize", 0),
        "time": time.time(),
    }
    with pending_lock:
        now = time.time()
        for key in [k for k, v in pending_files_by_id.items() if now - v["time"] > GROUP_FILE_TTL_SECONDS]:
            pending_files_by_id.pop(key, None)
        for key in [k for k, v in pending_files_by_user.items() if now - v["time"] > GROUP_FILE_TTL_SECONDS]:
            pending_files_by_user.pop(key, None)
        pending_files_by_user[(chat_id, user_id)] = info
        pending_files_by_user[(chat_id, None)] = info          # latest file in the chat
        pending_files_by_id[message["id"]] = info


def find_group_file(chat_id, user_id, quoted_message_id):
    """The file a command refers to: the quoted file, else this person's latest file, else the chat's latest."""
    with pending_lock:
        info = pending_files_by_id.get(quoted_message_id) if quoted_message_id else None
        info = info or pending_files_by_user.get((chat_id, user_id)) or pending_files_by_user.get((chat_id, None))
    if info and time.time() - info["time"] <= GROUP_FILE_TTL_SECONDS:
        return info
    return None


_send_reply = send_reply


def validate_file_info(file_info, user_id):
    """Download and validate a LINE file. Returns (file_name, result, token, error_text)."""
    file_name = obk_files.safe_name(file_info.get("fileName", ""))
    if not obk_files.is_supported(file_name):
        log.info("Unsupported file: %r", file_name)
        return file_name, None, None, tr(
            user_id, f"หนู validate ได้เฉพาะไฟล์ .xlsx .xlsm .xls และ .csv ที่มี index codes ค่ะ ไฟล์ที่ได้รับ: {file_name}",
            f"I can only validate .xlsx, .xlsm, .xls and .csv files containing index codes. File received: {file_name}")
    if obk_validator.get_master() is None:
        return file_name, None, None, no_reference_text(user_id)
    if file_info.get("fileSize", 0) > obk_files.MAX_FILE_BYTES:
        mb = obk_files.MAX_FILE_BYTES // (1024 * 1024)
        return file_name, None, None, tr(user_id, f"ไฟล์มีขนาดเกิน {mb} MB ค่ะ กรุณาแบ่งไฟล์แล้วส่งใหม่อีกครั้งค่ะ",
                                         f"The file is larger than {mb} MB. Please split it and send it again.")
    try:
        data, _ = download_line_content(file_info["id"])
        result, token = obk_files.validate_file(file_name, data)
    except Exception:
        log.exception("File validation failed")
        return file_name, None, None, tr(
            user_id, f"หนูไม่สามารถเปิดไฟล์ {file_name} เพื่อ validate ได้ค่ะ ไฟล์อาจเสียหายหรือมีการตั้งรหัสผ่าน กรุณาแก้ไขแล้วส่งใหม่อีกครั้งค่ะ",
            f"I couldn't open {file_name} to validate it. The file may be corrupted or password-protected. Please fix it and send it again.")
    return file_name, result, token, None


def validate_line_file(event, chat_id, user_id, message, send_reply):
    """validate_file_info that tells the user what went wrong. Returns (file_name, result, token) or None."""
    if event.get("source", {}).get("type") == "user":
        show_loading(user_id)
    file_name, result, token, error = validate_file_info(message, user_id)
    if error:
        send_reply(event["replyToken"], chat_id, error)
        return None
    return file_name, result, token


def download_links(user_id, base_url, token, paths):
    hours = obk_files.DOWNLOAD_TTL_SECONDS // 3600
    links = [f"- {os.path.basename(p)}\n{base_url}/files/{token}/{quote(os.path.basename(p))}" for p in paths]
    return tr(user_id, f"\n\nดาวน์โหลดไฟล์ ลิงก์มีอายุ {hours} ชั่วโมง\n",
              f"\n\nDownload, links expire in {hours} hours\n") + "\n".join(links)


def handle_file(event, chat_id, user_id, base_url, message=None, mention_user_id=None):
    message = message or event["message"]

    def send_reply(token, to, text, extra=()):
        _send_reply(token, to, text, extra, mention_user_id=mention_user_id)

    validated = validate_line_file(event, chat_id, user_id, message, send_reply)
    if not validated:
        return
    file_name, result, token = validated
    summary = obk_files.format_summary(result, file_name, user_langs.get(user_id, "th"))
    extra = []
    if not result.get("skipped"):
        outputs = result.get("outputs", [])
        for path in outputs:
            if path.endswith(".png"):
                url = f"{base_url}/files/{token}/{quote(os.path.basename(path))}"
                extra.append({"type": "image", "originalContentUrl": url, "previewImageUrl": url})
        summary += download_links(user_id, base_url, token, [p for p in outputs if not p.endswith(".png")])

    # Remember the summary so follow-up questions about the file make sense
    with chat_locks[chat_id]:
        remember(chat_id, f"(ผู้ใช้ส่งไฟล์ {file_name} มา validate index codes)", summary)
    store_chat_history_to_csv(chat_id, user_id, f"[file] {file_name}", summary)
    send_reply(event["replyToken"], chat_id, summary, extra)


def handle_summary(event, chat_id, user_id, base_url, mention_user_id=None):
    """Validate the referenced raw data file and send back an Excel summary table."""
    def send_reply(token, to, text, extra=()):
        _send_reply(token, to, text, extra, mention_user_id=mention_user_id)

    file_info = find_group_file(chat_id, user_id, event["message"].get("quotedMessageId"))
    if not file_info:
        send_reply(event["replyToken"], chat_id, tr(
            user_id,
            "กรุณาส่งไฟล์ Raw Data ก่อน แล้วพิมพ์ /summary หรือตอบกลับไฟล์นั้นด้วย /summary ค่ะ",
            "Please send the raw data file first, then type /summary or reply to that file with /summary."))
        return
    validated = validate_line_file(event, chat_id, user_id, file_info, send_reply)
    if not validated:
        return
    file_name, result, token = validated
    lang = user_langs.get(user_id, "th")
    text = obk_files.format_summary(result, file_name, lang)
    if not result.get("skipped"):
        try:
            path = obk_files.build_summary_workbook(result, file_name, lang)
        except Exception:
            log.exception("Summary workbook failed")
            send_reply(event["replyToken"], chat_id, system_error_text(user_id))
            return
        text = tr(user_id, "ตารางสรุป Raw Data\n", "Raw data summary table\n") + text + download_links(user_id, base_url, token, [path])
    store_chat_history_to_csv(chat_id, user_id, f"[summary] {file_name}", text)
    send_reply(event["replyToken"], chat_id, text)


def is_summary_request(text, mentioned):
    lowered = text.lower()
    if lowered.startswith(SUMMARY_COMMANDS):
        return True
    return mentioned and any(w in lowered for w in SUMMARY_WORDS)


def is_bot_mentioned(message):
    return any(m.get("isSelf") for m in message.get("mention", {}).get("mentionees", []))


def strip_bot_mention(message):
    """Remove the '@Vicky' text from a message that mentions the bot."""
    text = message.get("text", "")
    for m in sorted(message.get("mention", {}).get("mentionees", []), key=lambda m: -m.get("index", 0)):
        if m.get("isSelf"):
            text = text[:m["index"]] + text[m["index"] + m["length"]:]
    return text.strip()


def handle_group_event(event, chat_id, user_id, base_url):
    """Groups: stay quiet unless someone types /check (or @mentions Vicky to chat)."""
    message = event["message"]
    msg_type = message.get("type")
    reply_token = event["replyToken"]

    if msg_type == "file":
        remember_group_file(chat_id, user_id, message)
        log.info("Group file %r saved; waiting for /check", message.get("fileName"))
        return
    if msg_type != "text":
        log.info("Group %s message ignored (only /check or @mention get a reply)", msg_type)
        return

    text = message["text"].strip()
    if text.lower() in RESET_COMMANDS:
        chat_histories.pop(chat_id, None)
        send_reply(reply_token, chat_id, tr(user_id, "หนูล้างประวัติการสนทนาของกลุ่มนี้แล้วค่ะ", "This group's conversation history has been cleared."), mention_user_id=user_id)
        return

    if is_summary_request(text, is_bot_mentioned(message)):
        return handle_summary(event, chat_id, user_id, base_url, mention_user_id=user_id)

    codes, check_command = extract_codes(text)
    if check_command:
        if codes:
            return handle_text(event, chat_id, user_id, text, mention_user_id=user_id)
        file_info = find_group_file(chat_id, user_id, message.get("quotedMessageId"))
        if file_info:
            return handle_file(event, chat_id, user_id, base_url, message=file_info, mention_user_id=user_id)
        send_reply(reply_token, chat_id,
                   check_help(user_id),
                   mention_user_id=user_id)
        return

    if GROUP_CHAT_ON_MENTION and is_bot_mentioned(message):
        return handle_text(event, chat_id, user_id, strip_bot_mention(message) or "Hello", mention_user_id=user_id)
    log.info("Group text ignored (only /check or @mention get a reply)")


def group_kind(source):
    return "room" if source.get("type") == "room" else "group"


_group_access = {}          # chat_id -> (allowed, checked_at)


def group_has_admin(source):
    """True if one of ADMIN_USER_IDS is in this group. Unknown (API error) counts as allowed."""
    if not ADMIN_USER_IDS:
        return True
    chat_id = source.get("groupId") or source.get("roomId")
    cached = _group_access.get(chat_id)
    if cached and time.time() - cached[1] < GROUP_CHECK_TTL_SECONDS:
        return cached[0]
    for admin in ADMIN_USER_IDS:
        try:
            r = requests.get(LINE_MEMBER_API.format(kind=group_kind(source), chat_id=chat_id, user_id=admin),
                             headers=line_headers(), timeout=10)
        except requests.exceptions.RequestException as e:
            log.warning("Could not check group members (%s); allowing for now", e)
            return True
        if r.status_code == 200:
            _group_access[chat_id] = (True, time.time())
            return True
        if r.status_code != 404:
            log.warning("Member check returned %s: %s; allowing for now", r.status_code, r.text[:200])
            return True
    _group_access[chat_id] = (False, time.time())
    return False


def leave_group(event):
    """Explain and leave a group that has no admin in it."""
    source = event.get("source", {})
    chat_id = source.get("groupId") or source.get("roomId")
    log.warning("Leaving %s %s: no admin in it", group_kind(source), chat_id)
    if "replyToken" in event:
        post_messages(event["replyToken"], chat_id, [{"type": "text", "text":
            "หนูให้บริการเฉพาะ LINE Group ของโครงการ OBK ที่มีผู้ดูแลอยู่ในกลุ่มเท่านั้นค่ะ จึงขอออกจากกลุ่มนี้ค่ะ\n"
            "Vicky only works in OBK LINE groups that include an administrator, so I'm leaving this group."}])
    try:
        requests.post(LINE_LEAVE_API.format(kind=group_kind(source), chat_id=chat_id), headers=line_headers(), timeout=10)
    except requests.exceptions.RequestException as e:
        log.error("Leave failed: %s", e)


GROUP_ONLY_TEXT = ("หนูให้บริการเฉพาะใน LINE Group ของโครงการ OBK เท่านั้นค่ะ ไม่รับบริการผ่านแชทส่วนตัว\n"
                   "Vicky only works inside OBK LINE groups, not in private chats.")


def handle_event(event, base_url=""):
    event["_base_url"] = base_url
    source = event.get("source", {})
    user_id = source.get("userId", "")
    in_group = source.get("type") in ("group", "room")

    if event.get("type") == "follow" and "replyToken" in event:          # someone added Vicky as a friend
        if user_id not in ADMIN_USER_IDS:
            post_messages(event["replyToken"], user_id, [{"type": "text", "text": GROUP_ONLY_TEXT}])
        return
    if event.get("type") == "join" and in_group:                          # Vicky was added to a group
        if not group_has_admin(source):
            leave_group(event)
        return
    if event.get("type") != "message" or "replyToken" not in event:
        return

    if in_group and not group_has_admin(source):
        return leave_group(event)
    if not in_group and user_id not in ADMIN_USER_IDS:
        message = event["message"]
        if message.get("type") == "text" and message["text"].strip().lower() == "/myid":
            post_messages(event["replyToken"], user_id, [{"type": "text", "text": f"Your LINE user ID: {user_id}"}])
        else:
            post_messages(event["replyToken"], user_id, [{"type": "text", "text": GROUP_ONLY_TEXT}])
        log.info("Private chat from non-admin %s refused", user_id[-6:])
        return

    # Groups/rooms share one memory so Vicky follows the group conversation
    chat_id = source.get("groupId") or source.get("roomId") or user_id
    message = event["message"]
    log.info("Message from %s in %s chat: type=%s text=%r", user_id[-6:], source.get("type"),
             message.get("type"), message.get("text", "")[:60])
    if message.get("type") == "text":
        lang = detect_language(message["text"])
        if lang:
            user_langs[user_id] = lang
    if source.get("type") in ("group", "room"):
        return handle_group_event(event, chat_id, user_id, base_url)

    message = event["message"]
    if message.get("type") == "file":
        remember_group_file(chat_id, user_id, message)
        send_reply(event["replyToken"], chat_id, tr(
            user_id, f"หนูได้รับไฟล์ {message.get('fileName', '')} แล้วค่ะ พิมพ์ /check เพื่อ validate, /summary เพื่อขอตารางสรุป "
                     "หรือบอกหนูได้เลยว่าต้องการให้ทำอะไรกับไฟล์นี้ค่ะ",
            f"I've received {message.get('fileName', '')}. Type /check to validate it, /summary for the summary table, "
            "or tell me what you'd like me to do with it."))
        return
    if message.get("type") == "text":
        if is_summary_request(message["text"].strip(), True):
            return handle_summary(event, chat_id, user_id, base_url)
        return handle_text(event, chat_id, user_id, message["text"].strip())
    return handle_media(event, chat_id, user_id, base_url)


def handle_text(event, chat_id, user_id, text, mention_user_id=None):
    reply_token = event["replyToken"]

    def send_reply(token, to, text):
        _send_reply(token, to, text, mention_user_id=mention_user_id)

    if text.lower() in RESET_COMMANDS:
        chat_histories.pop(chat_id, None)
        send_reply(reply_token, chat_id, tr(user_id, "หนูล้างประวัติการสนทนาแล้วค่ะ", "I've cleared our conversation history."))
        return
    codes, check_command = extract_codes(text)
    master = obk_validator.get_master()
    if codes and master is None:
        if check_command:
            send_reply(reply_token, chat_id, no_reference_text(user_id))
            return
        codes = []
    if check_command and not codes:
        file_info = find_group_file(chat_id, user_id, event.get("message", {}).get("quotedMessageId"))
        if file_info:
            return handle_file(event, chat_id, user_id, event.get("_base_url", ""), message=file_info,
                               mention_user_id=mention_user_id)
        send_reply(reply_token, chat_id, check_help(user_id))
        return
    if codes:
        report = obk_validator.format_report([obk_validator.check_code(c, master) for c in codes],
                                                user_langs.get(user_id, "th"))
        leftover = text
        for c in codes:
            leftover = leftover.replace(c, "")
        # Only codes (or a short "check this") -> answer straight from the validator
        if check_command or len(leftover.strip()) <= 20:
            with chat_locks[chat_id]:
                remember(chat_id, text, report)
            store_chat_history_to_csv(chat_id, user_id, text, report)
            send_reply(reply_token, chat_id, report)
            return
        text = f"{text}\n\n[ผล validation จากระบบ / system validation result]\n{report}"
    user_parts = [types.Part.from_text(text=text)]
    history_text = text
    return ask_ani(event, chat_id, user_id, user_parts, history_text, mention_user_id)


def handle_media(event, chat_id, user_id, base_url):
    reply_token = event["replyToken"]
    message = event["message"]
    msg_type = message.get("type")

    if msg_type == "image":
        try:
            data, mime_type = download_line_content(message["id"])
        except requests.exceptions.RequestException as e:
            log.error("Failed to download image: %s", e)
            send_reply(reply_token, chat_id, tr(user_id, "หนูเปิดรูปไม่ได้ค่ะ กรุณาส่งใหม่อีกครั้งค่ะ",
                                                 "I couldn't open the image. Please send it again."))
            return
        user_parts = [
            types.Part.from_bytes(data=data, mime_type=mime_type),
            types.Part.from_text(text="(The user sent this image. If it relates to OBK index codes, help with it; otherwise politely decline. Reply in the user's language.)"),
        ]
        history_text = "(The user sent an image)"
    elif msg_type == "file":
        handle_file(event, chat_id, user_id, base_url)
        return
    elif msg_type == "sticker":
        log.info("Sticker ignored (Vicky only handles OBK validation)")
        return
    else:
        send_reply(reply_token, chat_id, tr(user_id, "หนูรับได้เฉพาะข้อความ รูปภาพ และไฟล์ Excel/CSV สำหรับ validation index codes ค่ะ",
                                             "I can only take text, images and Excel/CSV files for index codes validation."))
        return

    return ask_ani(event, chat_id, user_id, user_parts, history_text)


def system_error_text(user_id):
    return tr(user_id, "ขออภัยค่ะ หนูขัดข้องชั่วคราว กรุณาลองใหม่อีกครั้งค่ะ",
              "Sorry, I'm temporarily unavailable. Please try again.")


def ask_ani(event, chat_id, user_id, user_parts, history_text, mention_user_id=None):
    if event.get("source", {}).get("type") == "user":
        show_loading(user_id)

    with chat_locks[chat_id]:
        try:
            ctx = {"chat_id": chat_id, "user_id": user_id, "base_url": event.get("_base_url", ""),
                   "quoted": event.get("message", {}).get("quotedMessageId")}
            reply, links = generate_response(chat_id, user_parts, history_text, user_id, ctx)
            if links:
                reply += tr(user_id, f"\n\nดาวน์โหลดไฟล์ ลิงก์มีอายุ {obk_files.DOWNLOAD_TTL_SECONDS // 3600} ชั่วโมง\n",
                            f"\n\nDownload, links expire in {obk_files.DOWNLOAD_TTL_SECONDS // 3600} hours\n") + "\n".join(links)
        except genai_errors.APIError as e:
            log.error("Gemini call failed: %s %s", e.code, e.message)
            if e.code == 429:
                reply = tr(user_id, "ขออภัยค่ะ ขณะนี้หนูใช้งานเกินโควตาแล้ว กรุณาลองใหม่อีกครั้งภายหลังค่ะ",
                           "Sorry, I've reached my usage quota. Please try again later.")
            else:
                reply = system_error_text(user_id)
        except Exception:
            log.exception("Gemini call failed")
            reply = system_error_text(user_id)
        else:
            store_chat_history_to_csv(chat_id, user_id, history_text, reply)

    send_reply(event["replyToken"], chat_id, reply, mention_user_id=mention_user_id)


def verify_signature(body, signature):
    if not LINE_CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode("utf-8"), signature)


def run_safely(event, base_url):
    try:
        handle_event(event, base_url)
    except Exception:
        log.exception("Error handling event")


@app.route("/", methods=["POST"])
def webhook():
    body = request.get_data()
    if not verify_signature(body, request.headers.get("X-Line-Signature", "")):
        abort(400)

    payload = json.loads(body)
    base_url = PUBLIC_BASE_URL or request.url_root.rstrip("/")
    # Answer LINE immediately and do the slow AI work in the background
    for event in payload.get("events", []):
        executor.submit(run_safely, event, base_url)
    return "OK", 200


@app.route("/files/<token>/<path:name>", methods=["GET"])
def download(token, name):
    path = obk_files.get_download(token, name)
    if not path:
        abort(404)
    return send_file(path, as_attachment=not name.endswith(".png"), download_name=name)


@app.route("/", methods=["GET"])
def health():
    return "Vicky is awake", 200


if __name__ == "__main__":
    # Debug mode auto-restarts on code changes and exposes the Werkzeug debugger; keep it opt-in
    app.run(port=int(os.getenv("PORT", "8080")), debug=os.getenv("FLASK_DEBUG") == "1")
