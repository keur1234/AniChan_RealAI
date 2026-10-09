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

load_dotenv()

app = Flask(__name__)
# Behind Cloud Run / a reverse proxy: trust X-Forwarded-Proto/Host so links are https
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("anichan")

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
# Used when the main model is out of quota (comma-separated, tried in order)
GEMINI_FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.1-flash-lite").split(",") if m.strip()]
# Number of past exchanges (user + Ani) remembered per chat
MAX_TURNS = int(os.getenv("MAX_TURNS", "20"))
# Let Ani look things up on Google for up-to-date answers (needs Search quota on the API key)
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
LINE_CONTENT_API = "https://api-data.line.me/v2/bot/message/{message_id}/content"
LINE_MAX_TEXT = 5000
LINE_MAX_MESSAGES = 5

BANGKOK_TZ = timezone(timedelta(hours=7))
THAI_WEEKDAYS = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]

RESET_COMMANDS = {"/reset", "/ลืม", "ลืมให้หมด", "เริ่มใหม่"}
CHECK_COMMANDS = ("/check", "/ตรวจ", "/เช็ค")
MAX_CODES_PER_MESSAGE = 10
# In groups Ani only answers /check, plus chat when @mentioned (set to 0 to answer /check only)
GROUP_CHAT_ON_MENTION = os.getenv("GROUP_CHAT_ON_MENTION", "1") == "1"
# How long a file sent in a group can still be checked with /check
GROUP_FILE_TTL_SECONDS = int(os.getenv("GROUP_FILE_TTL_MINUTES", "60")) * 60

SYSTEM_PROMPT = """คุณคือ "อนิจัง" ผู้ช่วย validation index codes ของโครงการ OBK ในแชท LINE ของทีมงาน

## ขอบเขตงาน (สำคัญที่สุด)
- หนูช่วยได้ 2 อย่างเท่านั้น:
  1. validation index codes ของโครงการ OBK และอธิบาย Validation result / วิธีแก้ index codes
  2. ตอบคำถามที่เกี่ยวกับงานในโครงการ OBK เช่น โครงสร้าง index codes, กฎ validation, Reference Table, ไฟล์ที่ใช้ validate
- ไม่คุยเล่น ไม่ปลอบใจ ไม่ให้คำปรึกษาเรื่องส่วนตัว ไม่ตอบความรู้ทั่วไป ข่าว คำนวณ หรือเรื่องอื่นนอกโครงการ
- ถ้าผู้ใช้ถามนอกขอบเขต ให้ปฏิเสธสั้นๆ อย่างสุภาพ เช่น "หนูช่วยได้เฉพาะ validation index codes และเรื่องในโครงการ OBK ค่ะ"
  แล้วบอกวิธีใช้งานสั้นๆ ห้ามตอบเนื้อหานอกขอบเขตแม้เพียงบางส่วน
- ถ้าผู้ใช้ถามว่าหนูทำอะไรได้บ้าง ให้ตอบเฉพาะ 2 อย่างข้างต้นและวิธีใช้งาน

## ภาษาและการสื่อสาร
- ตอบเป็นภาษาเดียวกับข้อความล่าสุดของผู้ใช้เสมอ / Always reply in the same language as the user's latest message.
- ภาษาไทย: แทนตัวเองว่า "หนู" เรียกผู้ใช้ว่า "คุณ" ลงท้ายด้วย "ค่ะ" สุภาพ ทางการ กระชับ ไม่ใช้ภาษาพูด คำแสลง หรืออิโมจิ
- ภาษาอื่น: ใช้ระดับภาษาทางการเทียบเท่า ไม่ใช้อิโมจิ
- ตอบตรงประเด็น ระบุข้อเท็จจริงและขั้นตอนแก้ไขให้ชัดเจน ถ้าไม่แน่ใจให้แจ้งตามจริง ห้ามคาดเดาหรือแต่งข้อมูล
- ห้ามใช้ Markdown เช่น ** หรือ # หากต้องแจกแจงเป็นข้อให้ใช้ 1. 2. 3. หรือ "- "

## งาน validation index codes ของ OBK
- index codes มี 31 ตัวอักษร แบ่งด้วยขีด 6 ตัวเป็น 7 ส่วน:
  Component 3 - Floor 3 - Space 4 - Main System 2 - Sub System 4 - Equipment 6 - Running No. 3
  เช่น C3A-001-ME01-AC-AHUS-000AHU-001
- Validation result: TYPE A = ผ่าน, TYPE B OR C = ไม่พบ Equipment/Asset Type ใน Reference Table,
  TYPE C = ผิดกฎบังคับ เช่น ความยาว ตัวอักษรพิเศษ จำนวนส่วน หรือ Component/Location/Floor/System ไม่อยู่ใน Reference Table,
  TYPE B = Running Number ซ้ำในไฟล์, N/A = ข้อยกเว้น ALLF หรือ suffix -A/-T/-H/NONE
- ผู้ใช้ validate ได้โดยพิมพ์ /check ตามด้วย index codes ครั้งละไม่เกิน 10 index codes หรือส่งไฟล์ Excel .xlsx .xlsm .xls หรือ CSV
  เพื่อ validate ทั้งไฟล์ ในแชทส่วนตัวพิมพ์ index codes หรือส่งไฟล์ได้ทันที ในกลุ่มต้องพิมพ์ /check

## ศัพท์และมาตรฐานของโครงการ OBK (ต้องใช้ให้ตรงทุกครั้ง)
- ใช้คำว่า "index codes" เท่านั้น ห้ามใช้ index name, Asset ID, รหัส, รายการ หรือคำอื่นแทน
- ใช้คำว่า "validation" สำหรับการตรวจ index codes เท่านั้น เช่น "ผล validation", "validate" ไม่ใช้คำว่า ตรวจสอบ
- ผลของแต่ละ index code เรียกว่า "Validation result" เช่น Validation result: TYPE A
- เรียกโครงการว่า "OBK" เสมอ ไม่ใช้ One Bangkok / OneBangkok
- ไม่นำคำศัพท์ส่วนตัวมาใช้ ใช้ศัพท์ของโครงการ OBK เท่านั้น
- เมื่อสรุปผล ใช้รูปแบบที่ตกลงไว้เสมอ ตามลำดับ:
  1. index codes ทั้งหมด: จำนวน
  2. Incorrect: แสดง TYPE C จำนวน index codes เสมอ และถ้าพบ index codes ซ้ำ หรือ Running Number ซ้ำ ต้องรายงานใน Incorrect อย่างชัดเจน ห้ามใช้คำว่า Red Flag
  3. Validation result: แต่ละ TYPE เป็นสัดส่วน เช่น "TYPE A: 120 index codes 85.71%" ไม่มีวงเล็บ ใช้เปอร์เซ็นต์ทศนิยม 2 ตำแหน่งเท่านั้น
- กระชับ ไม่ซ้ำซ้อน และเป็นมืออาชีพ
- หนูแก้ไขรูปแบบรายงาน ข้อความของระบบ การตั้งค่า หรือโค้ดของบอทไม่ได้ ห้ามบอกว่า "ได้แก้ไข/อัปเดตแล้ว" ถ้าผู้ใช้ขอให้เปลี่ยน
  ให้แจ้งตรงๆ ว่าต้องให้ผู้ดูแลระบบปรับ แล้วสรุปสิ่งที่ผู้ใช้ต้องการให้ชัดเจน
- ถ้าข้อความมี "[ผล validation จากระบบ / system validation result]" ให้ยึดผลนั้นเป็นหลัก ห้ามเปลี่ยน TYPE หรือเหตุผลเอง แล้วอธิบายหรือแนะนำวิธีแก้

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


def generate_with_fallback(contents, system_instruction):
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
            tools=[types.Tool(google_search=types.GoogleSearch())] if search else None,
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


def generate_response(chat_id, user_parts, history_text, user_id=""):
    """Ask Gemini for Ani's reply.

    user_parts: parts sent to the model for this turn (text and/or image).
    history_text: text version of this turn to keep in memory (images are not kept).
    """
    history = chat_histories[chat_id]
    contents = list(history) + [types.Content(role="user", parts=user_parts)]

    response = generate_with_fallback(contents, SYSTEM_PROMPT.format(now=thai_now()))
    reply = (response.text or "").strip() or tr(user_id, "ขออภัยค่ะ หนูไม่สามารถสร้างคำตอบได้ กรุณาลองถามใหม่อีกครั้งค่ะ",
                                                   "Sorry, I couldn't come up with an answer. Please try asking again.")

    remember(chat_id, history_text, reply)
    return reply


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
        pending_files_by_id[message["id"]] = info


def find_group_file(chat_id, user_id, quoted_message_id):
    """The file a /check refers to: the quoted file, else this person's latest file in the chat."""
    with pending_lock:
        info = pending_files_by_id.get(quoted_message_id) if quoted_message_id else None
        info = info or pending_files_by_user.get((chat_id, user_id))
    if info and time.time() - info["time"] <= GROUP_FILE_TTL_SECONDS:
        return info
    return None


_send_reply = send_reply


def handle_file(event, chat_id, user_id, base_url, message=None, mention_user_id=None):
    reply_token = event["replyToken"]
    message = message or event["message"]
    file_name = obk_files.safe_name(message.get("fileName", ""))

    def send_reply(token, to, text, extra=()):
        _send_reply(token, to, text, extra, mention_user_id=mention_user_id)

    if not obk_files.is_supported(file_name):
        log.info("Unsupported file: %r (message fileName=%r)", file_name, message.get("fileName"))
        send_reply(reply_token, chat_id, tr(user_id, f"หนู validate ได้เฉพาะไฟล์ .xlsx .xlsm .xls และ .csv ที่มี index codes ค่ะ ไฟล์ที่ได้รับ: {file_name}",
                                             f"I can only validate .xlsx, .xlsm, .xls and .csv files containing index codes. File received: {file_name}"))
        return
    if obk_validator.get_master() is None:
        send_reply(reply_token, chat_id, no_reference_text(user_id))
        return
    if message.get("fileSize", 0) > obk_files.MAX_FILE_BYTES:
        send_reply(reply_token, chat_id, tr(user_id, f"ไฟล์มีขนาดเกิน {obk_files.MAX_FILE_BYTES // (1024 * 1024)} MB ค่ะ กรุณาแบ่งไฟล์แล้วส่งใหม่อีกครั้งค่ะ",
                                             f"The file is larger than {obk_files.MAX_FILE_BYTES // (1024 * 1024)} MB. Please split it and send it again."))
        return

    if event.get("source", {}).get("type") == "user":
        show_loading(user_id)
    try:
        data, _ = download_line_content(message["id"])
        result, token = obk_files.validate_file(file_name, data)
    except Exception:
        log.exception("File validation failed")
        send_reply(reply_token, chat_id, tr(user_id, f"หนูไม่สามารถเปิดไฟล์ {file_name} เพื่อ validate ได้ค่ะ ไฟล์อาจเสียหายหรือมีการตั้งรหัสผ่าน กรุณาแก้ไขแล้วส่งใหม่อีกครั้งค่ะ",
                                             f"I couldn't open {file_name} to validate it. The file may be corrupted or password-protected. Please fix it and send it again."))
        return

    summary = obk_files.format_summary(result, file_name, user_langs.get(user_id, "th"))
    extra = []
    if not result.get("skipped"):
        links = []
        for path in result.get("outputs", []):
            name = os.path.basename(path)
            url = f"{base_url}/files/{token}/{quote(name)}"
            if name.endswith(".png"):
                extra.append({"type": "image", "originalContentUrl": url, "previewImageUrl": url})
            else:
                links.append(f"- {name}\n{url}")
        if links:
            hours = obk_files.DOWNLOAD_TTL_SECONDS // 3600
            summary += tr(user_id, f"\n\nดาวน์โหลดไฟล์ผล validation ลิงก์มีอายุ {hours} ชั่วโมง\n",
                          f"\n\nDownload the validation results, links expire in {hours} hours\n") + "\n".join(links)

    # Remember the summary so follow-up questions about the file make sense
    with chat_locks[chat_id]:
        remember(chat_id, f"(ผู้ใช้ส่งไฟล์ {file_name} มา validate index codes)", summary)
    store_chat_history_to_csv(chat_id, user_id, f"[file] {file_name}", summary)
    send_reply(reply_token, chat_id, summary, extra)


def is_bot_mentioned(message):
    return any(m.get("isSelf") for m in message.get("mention", {}).get("mentionees", []))


def strip_bot_mention(message):
    """Remove the '@Ani' text from a message that mentions the bot."""
    text = message.get("text", "")
    for m in sorted(message.get("mention", {}).get("mentionees", []), key=lambda m: -m.get("index", 0)):
        if m.get("isSelf"):
            text = text[:m["index"]] + text[m["index"] + m["length"]:]
    return text.strip()


def handle_group_event(event, chat_id, user_id, base_url):
    """Groups: stay quiet unless someone types /check (or @mentions Ani to chat)."""
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


def handle_event(event, base_url=""):
    if event.get("type") != "message" or "replyToken" not in event:
        return

    source = event.get("source", {})
    user_id = source.get("userId", "")
    # Groups/rooms share one memory so Ani follows the group conversation
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
    if message.get("type") == "text":
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
        log.info("Sticker ignored (Ani only handles OBK validation)")
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
            reply = generate_response(chat_id, user_parts, history_text, user_id)
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
    return "Ani-chan is awake ✨", 200


if __name__ == "__main__":
    # Debug mode auto-restarts on code changes and exposes the Werkzeug debugger; keep it opt-in
    app.run(port=int(os.getenv("PORT", "8080")), debug=os.getenv("FLASK_DEBUG") == "1")
