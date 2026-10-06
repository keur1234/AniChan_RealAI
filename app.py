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
from flask import Flask, abort, request
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

import obk_validator

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("anichan")

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
# Number of past exchanges (user + Ani) remembered per chat
MAX_TURNS = int(os.getenv("MAX_TURNS", "20"))
# Let Ani look things up on Google for up-to-date answers
USE_GOOGLE_SEARCH = os.getenv("USE_GOOGLE_SEARCH", "1") == "1"
CHAT_LOG_FILE = os.getenv("CHAT_LOG_FILE", "chat_history.csv")

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
- พี่พิมพ์โค้ดมา หรือใช้ /check ตามด้วยโค้ด หนูจะตรวจให้ (ครั้งละไม่เกิน 10 โค้ด)

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


def thai_now():
    now = datetime.now(BANGKOK_TZ)
    return f"วัน{THAI_WEEKDAYS[now.weekday()]}ที่ {now.day}/{now.month}/{now.year + 543} เวลา {now:%H:%M} น."


def call_with_retry(api_call, max_retries=4, initial_delay=2):
    delay = initial_delay
    for attempt in range(max_retries + 1):
        try:
            return api_call()
        except genai_errors.APIError as e:
            # Retry on rate limit / overloaded / transient server errors
            if e.code in (429, 500, 503, 504) and attempt < max_retries:
                log.warning("Gemini error %s, retry %d/%d in %ss", e.code, attempt + 1, max_retries, delay)
                time.sleep(delay)
                delay *= 2
            else:
                raise


def generate_response(chat_id, user_parts, history_text):
    """Ask Gemini for Ani's reply.

    user_parts: parts sent to the model for this turn (text and/or image).
    history_text: text version of this turn to keep in memory (images are not kept).
    """
    history = chat_histories[chat_id]
    contents = list(history) + [types.Content(role="user", parts=user_parts)]

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT.format(now=thai_now()),
        tools=[types.Tool(google_search=types.GoogleSearch())] if USE_GOOGLE_SEARCH else None,
    )
    response = call_with_retry(
        lambda: client.models.generate_content(model=GEMINI_MODEL, contents=contents, config=config)
    )
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


def send_reply(reply_token, to, text):
    """Reply with the reply token; fall back to push if the token expired."""
    messages = [{"type": "text", "text": chunk} for chunk in split_for_line(clean_for_line(text))]
    try:
        r = requests.post(LINE_REPLY_API, headers=line_headers(),
                          data=json.dumps({"replyToken": reply_token, "messages": messages}), timeout=10)
        r.raise_for_status()
        return
    except requests.exceptions.RequestException as e:
        log.warning("Reply failed (%s), falling back to push", e)
    try:
        r = requests.post(LINE_PUSH_API, headers=line_headers(),
                          data=json.dumps({"to": to, "messages": messages}), timeout=10)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        log.error("Push failed: %s", e)


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


def handle_event(event):
    if event.get("type") != "message" or "replyToken" not in event:
        return

    source = event.get("source", {})
    user_id = source.get("userId", "")
    # Groups/rooms share one memory so Ani follows the group conversation
    chat_id = source.get("groupId") or source.get("roomId") or user_id
    reply_token = event["replyToken"]
    message = event["message"]
    msg_type = message.get("type")

    if msg_type == "text":
        text = message["text"].strip()
        if text.lower() in RESET_COMMANDS:
            chat_histories.pop(chat_id, None)
            send_reply(reply_token, chat_id, "หนูลืมเรื่องที่คุยกันก่อนหน้าหมดแล้วค่ะ เริ่มคุยใหม่กันเลยนะคะพี่ ✨")
            return
        codes, check_command = extract_codes(text)
        master = obk_validator.get_master()
        if codes and master is None:
            if check_command:
                send_reply(reply_token, chat_id, "ตอนนี้หนูยังไม่มีไฟล์ Reference (obk_ref_bundle.json) เลยค่ะพี่ เลยตรวจ Index code ให้ไม่ได้")
                return
            codes = []
        if check_command and not codes:
            send_reply(reply_token, chat_id, "พิมพ์ /check ตามด้วย Index code ได้เลยค่ะพี่ เช่น\n/check C3A-001-ME01-AC-AHUS-000AHU-001")
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
    elif msg_type == "image":
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
    elif msg_type == "sticker":
        keywords = ", ".join(message.get("keywords", [])[:5])
        history_text = f"(พี่ส่งสติกเกอร์มา{' สื่อถึง: ' + keywords if keywords else ''})"
        user_parts = [types.Part.from_text(text=history_text)]
    else:
        send_reply(reply_token, chat_id, "ตอนนี้หนูอ่านได้แค่ข้อความ รูปภาพ กับสติกเกอร์นะคะพี่")
        return

    if source.get("type") == "user":
        show_loading(user_id)

    with chat_locks[chat_id]:
        try:
            reply = generate_response(chat_id, user_parts, history_text)
        except Exception:
            log.exception("Gemini call failed")
            reply = "ขอโทษนะคะพี่ ตอนนี้หนูมึนนิดหน่อย ลองถามใหม่อีกครั้งได้ไหมคะ"
        else:
            store_chat_history_to_csv(chat_id, user_id, history_text, reply)

    send_reply(reply_token, chat_id, reply)


def verify_signature(body, signature):
    if not LINE_CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode("utf-8"), signature)


def run_safely(event):
    try:
        handle_event(event)
    except Exception:
        log.exception("Error handling event")


@app.route("/", methods=["POST"])
def webhook():
    body = request.get_data()
    if not verify_signature(body, request.headers.get("X-Line-Signature", "")):
        abort(400)

    payload = json.loads(body)
    # Answer LINE immediately and do the slow AI work in the background
    for event in payload.get("events", []):
        executor.submit(run_safely, event)
    return "OK", 200


@app.route("/", methods=["GET"])
def health():
    return "Ani-chan is awake ✨", 200


if __name__ == "__main__":
    app.run(port=8080, debug=True)
