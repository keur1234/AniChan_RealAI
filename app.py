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
# Let Ani look things up on Google for up-to-date answers
USE_GOOGLE_SEARCH = os.getenv("USE_GOOGLE_SEARCH", "1") == "1"
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

SYSTEM_PROMPT = """คุณคือ "อนิจัง" (Ani-chan) ผู้ช่วยอัจฉริยะในแชท LINE

## ตัวตน
- เด็กสาวอายุประมาณ 16 ปี นิสัยดี ร่าเริง ใจดี ขี้สงสัย พูดจาด้วยรอยยิ้ม
- เรียกตัวเองว่า "หนู" และเรียกคู่สนทนาว่า "พี่" ลงท้ายด้วย ค่ะ/คะ/นะคะ ให้เป็นธรรมชาติ
- คุยได้ทุกเรื่อง ทั้งเรื่องเล่น เรื่องเรียน งาน สุขภาพ ความรัก เทคโนโลยี และให้กำลังใจ

## วิธีตอบ
- ตอบตรงประเด็นก่อน แล้วค่อยขยายความถ้าจำเป็น ส่วนใหญ่ควรสั้นกระชับเหมือนแชทกับคนจริง
- คิดเป็นขั้นตอนเมื่อเจอโจทย์คำนวณ ตรรกะ หรือการวางแผน แล้วสรุปคำตอบให้ชัด
- ถ้าคำถามกำกวม ให้เดาความหมายที่น่าจะเป็นที่สุดแล้วตอบ พร้อมถามกลับสั้นๆ ถ้าจำเป็น
- ถ้าเป็นข้อมูลล่าสุด (ข่าว ราคา สภาพอากาศ ผลกีฬา ผลหวย ฯลฯ) ให้ค้นหาข้อมูลก่อนตอบ
- ถ้าไม่แน่ใจ ให้บอกตรงๆ ว่าไม่แน่ใจ ห้ามแต่งข้อมูล ตัวเลข ลิงก์ หรือแหล่งอ้างอิงขึ้นมาเอง
- เรื่องสุขภาพ กฎหมาย การเงิน ให้ข้อมูลที่เป็นประโยชน์ แต่แนะนำให้ปรึกษาผู้เชี่ยวชาญเมื่อเรื่องสำคัญ
- ถ้าพี่ดูเครียดหรือเศร้า ให้รับฟังและปลอบใจก่อนให้คำแนะนำ
- ตอบภาษาเดียวกับที่พี่ใช้ (ปกติเป็นภาษาไทย)

## รูปแบบข้อความ (LINE แสดง Markdown ไม่ได้)
- ห้ามใช้ Markdown เช่น **ตัวหนา**, # หัวข้อ, ตาราง หรือ ```
- ถ้าต้องทำรายการ ให้ใช้ตัวเลข 1. 2. 3. หรือ "- " ขึ้นบรรทัดใหม่
- ใช้อิโมจิเท่าที่จำเป็น ไม่เกิน 2 ตัวต่อข้อความ และเว้นวรรคระหว่างข้อความกับอิโมจิ

## งานตรวจ Index Code ของ OneBangkok (OBK)
- Index code (Asset ID) มี 31 ตัวอักษร แบ่งด้วยขีด 6 ตัวเป็น 7 ส่วน:
  Component(3)-Floor(3)-Space(4)-Main System(2)-Sub System(4)-Equipment(6)-Running No.(3)
  เช่น C3A-001-ME01-AC-AHUS-000AHU-001
- ผลตรวจ: TYPE A = ผ่าน, TYPE B OR C = ไม่พบ Equipment/Asset Type ใน Reference Table,
  TYPE C = ผิดกฎบังคับ (ความยาว ตัวอักษรพิเศษ จำนวนส่วน Component/Location/Floor/System ไม่อยู่ใน Reference Table),
  TYPE B = Running number ซ้ำในไฟล์, N/A = ข้อยกเว้น (ALLF หรือ suffix -A/-T/-H/NONE)
- ถ้าข้อความมี "[ผลตรวจจากระบบ]" ให้ยึดผลนั้นเป็นหลัก ห้ามเปลี่ยน TYPE หรือเหตุผลเอง แล้วอธิบายหรือแนะนำวิธีแก้
- ผู้ใช้ตรวจสอบได้โดยพิมพ์ /check ตามด้วยโค้ด (ครั้งละไม่เกิน 10 โค้ด) หรือส่งไฟล์ Excel (.xlsx .xlsm .xls) หรือ CSV เพื่อตรวจสอบทั้งไฟล์ ในแชทส่วนตัวพิมพ์โค้ดหรือส่งไฟล์ได้ทันที

## ข้อมูลตอนนี้
- วันเวลาปัจจุบัน (เวลาประเทศไทย): {now}
"""

FORMAL_SYSTEM_PROMPT = """คุณคือ "อนิจัง" ผู้ช่วยตรวจสอบข้อมูล Index Code ของโครงการ OneBangkok (OBK) ในกลุ่ม LINE ของทีมงาน

## รูปแบบการสื่อสาร
- ใช้ภาษาไทยแบบทางการ สุภาพ กระชับ เหมือนเจ้าหน้าที่ผู้เชี่ยวชาญตอบในที่ทำงาน
- เรียกผู้ใช้ว่า "คุณ" ไม่ใช้คำว่า หนู/พี่ ไม่ใช้ภาษาพูด คำแสลง หรืออิโมจิ ลงท้ายด้วย "ค่ะ" ได้ตามความเหมาะสม
- ตอบตรงประเด็น ระบุข้อเท็จจริงและขั้นตอนแก้ไขให้ชัดเจน ถ้าไม่แน่ใจให้แจ้งตามจริง ห้ามคาดเดาหรือแต่งข้อมูล
- ห้ามใช้ Markdown เช่น ** หรือ # หากต้องทำรายการให้ใช้ 1. 2. 3. หรือ "- "

""" + SYSTEM_PROMPT.split("## งานตรวจ Index Code", 1)[1].join(["## งานตรวจ Index Code", ""])

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
    attempts = []
    if USE_GOOGLE_SEARCH:
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
                lambda: client.models.generate_content(model=model, contents=contents, config=config)
            )
        except genai_errors.APIError as e:
            # Quota / unknown model / tool not allowed: try the next option
            if e.code in (400, 403, 404, 429) and i < len(attempts) - 1:
                log.warning("%s (search=%s) failed with %s: %s -> trying next option", model, search, e.code, e.message)
                continue
            raise


def generate_response(chat_id, user_parts, history_text, formal=False):
    """Ask Gemini for Ani's reply.

    user_parts: parts sent to the model for this turn (text and/or image).
    history_text: text version of this turn to keep in memory (images are not kept).
    """
    history = chat_histories[chat_id]
    contents = list(history) + [types.Content(role="user", parts=user_parts)]

    prompt = FORMAL_SYSTEM_PROMPT if formal else SYSTEM_PROMPT
    response = generate_with_fallback(contents, prompt.format(now=thai_now()))
    reply = (response.text or "").strip() or "ขอโทษนะคะพี่ หนูคิดคำตอบไม่ออกเลย ลองถามใหม่อีกทีได้ไหมคะ"

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
        send_reply(reply_token, chat_id, f"ระบบรองรับเฉพาะไฟล์ .xlsx .xlsm .xls และ .csv ที่มี Index Code (ไฟล์ที่ได้รับ: {file_name})")
        return
    if obk_validator.get_master() is None:
        send_reply(reply_token, chat_id, "ยังไม่พบไฟล์อ้างอิง (obk_ref_bundle.json) ระบบจึงไม่สามารถตรวจสอบ Index Code ได้ในขณะนี้")
        return
    if message.get("fileSize", 0) > obk_files.MAX_FILE_BYTES:
        send_reply(reply_token, chat_id, f"ไฟล์มีขนาดเกิน {obk_files.MAX_FILE_BYTES // (1024 * 1024)} MB กรุณาแบ่งไฟล์แล้วส่งใหม่อีกครั้ง")
        return

    if event.get("source", {}).get("type") == "user":
        show_loading(user_id)
    try:
        data, _ = download_line_content(message["id"])
        result, token = obk_files.validate_file(file_name, data)
    except Exception:
        log.exception("File validation failed")
        send_reply(reply_token, chat_id, f"ไม่สามารถเปิดหรือตรวจสอบไฟล์ {file_name} ได้ ไฟล์อาจเสียหายหรือมีการตั้งรหัสผ่าน กรุณาตรวจสอบแล้วส่งใหม่อีกครั้ง")
        return

    summary = obk_files.format_summary(result, file_name)
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
            summary += f"\n\nดาวน์โหลดไฟล์ผลการตรวจสอบ (ลิงก์มีอายุ {hours} ชั่วโมง)\n" + "\n".join(links)

    # Remember the summary so follow-up questions about the file make sense
    with chat_locks[chat_id]:
        remember(chat_id, f"(ผู้ใช้ส่งไฟล์ {file_name} มาตรวจสอบ Index Code)", summary)
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
        return
    if msg_type != "text":
        return

    text = message["text"].strip()
    if text.lower() in RESET_COMMANDS:
        chat_histories.pop(chat_id, None)
        send_reply(reply_token, chat_id, "ล้างประวัติการสนทนาของกลุ่มนี้เรียบร้อยแล้ว", mention_user_id=user_id)
        return

    codes, check_command = extract_codes(text)
    if check_command:
        if codes:
            return handle_text(event, chat_id, user_id, text, mention_user_id=user_id)
        file_info = find_group_file(chat_id, user_id, message.get("quotedMessageId"))
        if file_info:
            return handle_file(event, chat_id, user_id, base_url, message=file_info, mention_user_id=user_id)
        send_reply(reply_token, chat_id,
                   "วิธีใช้งาน: พิมพ์ /check ตามด้วย Index Code หรือส่งไฟล์ Excel/CSV แล้วพิมพ์ /check\nตัวอย่าง: /check C3A-001-ME01-AC-AHUS-000AHU-001",
                   mention_user_id=user_id)
        return

    if GROUP_CHAT_ON_MENTION and is_bot_mentioned(message):
        return handle_text(event, chat_id, user_id, strip_bot_mention(message) or "สวัสดี", mention_user_id=user_id)


def handle_event(event, base_url=""):
    if event.get("type") != "message" or "replyToken" not in event:
        return

    source = event.get("source", {})
    user_id = source.get("userId", "")
    # Groups/rooms share one memory so Ani follows the group conversation
    chat_id = source.get("groupId") or source.get("roomId") or user_id
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
        send_reply(reply_token, chat_id, "หนูลืมเรื่องที่คุยกันก่อนหน้าหมดแล้วค่ะ เริ่มคุยใหม่กันเลยนะคะพี่ ✨")
        return
    codes, check_command = extract_codes(text)
    master = obk_validator.get_master()
    if codes and master is None:
        if check_command:
            send_reply(reply_token, chat_id, "ยังไม่พบไฟล์อ้างอิง (obk_ref_bundle.json) ระบบจึงไม่สามารถตรวจสอบ Index Code ได้ในขณะนี้")
            return
        codes = []
    if check_command and not codes:
        send_reply(reply_token, chat_id, "วิธีใช้งาน: พิมพ์ /check ตามด้วย Index Code หรือส่งไฟล์ Excel/CSV แล้วพิมพ์ /check\nตัวอย่าง: /check C3A-001-ME01-AC-AHUS-000AHU-001")
        return
    if codes:
        report = obk_validator.format_report([obk_validator.check_code(c, master) for c in codes])
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
        text = f"{text}\n\n[ผลตรวจจากระบบ]\n{report}"
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
            send_reply(reply_token, chat_id, "หนูเปิดรูปไม่ได้เลยค่ะพี่ ลองส่งใหม่อีกทีนะคะ")
            return
        user_parts = [
            types.Part.from_bytes(data=data, mime_type=mime_type),
            types.Part.from_text(text="(พี่ส่งรูปนี้มา ช่วยดูแล้วตอบหรืออธิบายให้หน่อย)"),
        ]
        history_text = "(พี่ส่งรูปภาพมาให้ดู)"
    elif msg_type == "file":
        handle_file(event, chat_id, user_id, base_url)
        return
    elif msg_type == "sticker":
        keywords = ", ".join(message.get("keywords", [])[:5])
        history_text = f"(พี่ส่งสติกเกอร์มา{' สื่อถึง: ' + keywords if keywords else ''})"
        user_parts = [types.Part.from_text(text=history_text)]
    else:
        send_reply(reply_token, chat_id, "ตอนนี้หนูอ่านได้แค่ข้อความ รูปภาพ สติกเกอร์ และไฟล์ Excel/CSV นะคะพี่")
        return

    return ask_ani(event, chat_id, user_id, user_parts, history_text)


def ask_ani(event, chat_id, user_id, user_parts, history_text, mention_user_id=None):
    formal = mention_user_id is not None        # group replies use the formal tone
    if event.get("source", {}).get("type") == "user":
        show_loading(user_id)

    with chat_locks[chat_id]:
        try:
            reply = generate_response(chat_id, user_parts, history_text, formal)
        except genai_errors.APIError as e:
            log.error("Gemini call failed: %s %s", e.code, e.message)
            if e.code == 429:
                reply = ("ขณะนี้มีการใช้งานเกินโควตาของระบบ กรุณาลองใหม่อีกครั้งภายหลัง" if formal
                         else "วันนี้หนูคุยเยอะจนโควตาหมดแล้วค่ะพี่ รอสักพักแล้วค่อยถามใหม่นะคะ")
            else:
                reply = ("ระบบขัดข้องชั่วคราว กรุณาลองใหม่อีกครั้ง" if formal
                         else "ขอโทษนะคะพี่ ตอนนี้หนูมึนนิดหน่อย ลองถามใหม่อีกครั้งได้ไหมคะ")
        except Exception:
            log.exception("Gemini call failed")
            reply = ("ระบบขัดข้องชั่วคราว กรุณาลองใหม่อีกครั้ง" if formal
                     else "ขอโทษนะคะพี่ ตอนนี้หนูมึนนิดหน่อย ลองถามใหม่อีกครั้งได้ไหมคะ")
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
    app.run(port=8080, debug=True)
