#!/usr/bin/env python3
"""Chat with Vicky in your browser, without LINE.

    python app_local.py              # opens http://127.0.0.1:5050
    python app_local.py --port 6000 --no-browser

Same Vicky as app.py (validation, /check, /check FAS, /summary, /bim, documents, Gemini chat),
running as a private chat with you. Send Excel/CSV files with the paperclip button.
Needs GEMINI_API_KEY in .env for normal chat; LINE keys are not needed.
"""
import argparse
import getpass
import itertools
import os
import threading
import webbrowser

from dotenv import load_dotenv

load_dotenv()
# app.py reads these at import; nothing is sent to LINE from here
os.environ.setdefault("LINE_CHANNEL_SECRET", "local")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "local")
if not os.getenv("GEMINI_API_KEY"):
    print("GEMINI_API_KEY is not set in .env: /check, /summary and /bim work, normal chat won't")
    os.environ["GEMINI_API_KEY"] = "missing"

import app as vicky  # noqa: E402
from flask import abort, request  # noqa: E402

LOCAL_USER_ID = "local-user"
LOCAL_USER_NAME = os.getenv("LOCAL_USER_NAME") or getpass.getuser()
MAX_UPLOAD_BYTES = vicky.obk_files.MAX_FILE_BYTES

_uploads = {}                      # message id -> (bytes, content type)
_ids = itertools.count(1)
_lock = threading.Lock()           # one message at a time, like a single chat

# Run app.py's handlers as a private chat with LOCAL_USER_ID; files come from uploads instead of LINE
vicky.PRIVATE_CHAT_USERS[LOCAL_USER_ID] = LOCAL_USER_NAME
vicky.download_line_content = lambda message_id: _uploads[message_id]
vicky.show_loading = lambda user_id: None
vicky.display_name = lambda user_id: LOCAL_USER_NAME
vicky.sender_name = lambda file_info: LOCAL_USER_NAME
server = vicky.app


def run_message(message, base_url):
    """Handle one message as Vicky would and return what she replies (text and image URLs)."""
    event = {"type": "message", "replyToken": "local", "source": {"type": "user", "userId": LOCAL_USER_ID},
             "message": message}
    with _lock:
        vicky._capture.messages = []
        try:
            vicky.handle_event(event, base_url)
            captured = vicky._capture.messages
        finally:
            vicky._capture.messages = None
    replies = []
    for m in captured:
        if m.get("type") == "image":
            replies.append({"image": m.get("originalContentUrl", "")})
        elif m.get("text"):
            replies.append({"text": m["text"]})
    return replies


@server.route("/local/send", methods=["POST"])
def local_send():
    if not _local_request():
        abort(403)
    text = str((request.get_json(silent=True) or {}).get("text", "")).strip()
    if not text:
        return {"replies": []}
    message = {"type": "text", "id": f"local-{next(_ids)}", "text": text}
    return {"replies": run_message(message, request.url_root.rstrip("/"))}


@server.route("/local/upload", methods=["POST"])
def local_upload():
    if not _local_request():
        abort(403)
    f = request.files.get("file")
    if not f:
        return {"replies": [{"text": "No file received."}]}
    data = f.read()
    if len(data) > MAX_UPLOAD_BYTES:
        return {"replies": [{"text": f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."}]}
    message_id = f"local-{next(_ids)}"
    _uploads[message_id] = (data, f.mimetype or "application/octet-stream")
    if (f.mimetype or "").startswith("image/"):
        message = {"type": "image", "id": message_id}
    else:
        message = {"type": "file", "id": message_id, "fileName": f.filename or "file", "fileSize": len(data)}
    return {"replies": run_message(message, request.url_root.rstrip("/"))}


def _local_request():
    """Only this computer can use the local chat."""
    return request.remote_addr in ("127.0.0.1", "::1")


@server.route("/chat", methods=["GET"])
def chat_page():
    if not _local_request():
        abort(403)
    return CHAT_PAGE.replace("{{NAME}}", LOCAL_USER_NAME)


CHAT_PAGE = r"""<!doctype html>
<html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vicky Local Chat</title>
<style>
  :root { --bg:#eef1f5; --panel:#ffffff; --me:#06c755; --me-text:#ffffff; --bot:#ffffff; --text:#1d2939;
          --muted:#667085; --line:#d0d5dd; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#111418; --panel:#1b1f24; --me:#1f8f4e; --me-text:#ffffff; --bot:#242a31; --text:#e6e9ee;
            --muted:#9aa4b2; --line:#343b44; }
  }
  * { box-sizing:border-box; }
  body { margin:0; height:100vh; display:flex; flex-direction:column; background:var(--bg); color:var(--text);
         font:15px/1.5 "Sarabun","Noto Sans Thai","Segoe UI",Tahoma,sans-serif; }
  header { padding:12px 16px; background:var(--panel); border-bottom:1px solid var(--line); display:flex;
           align-items:center; gap:10px; }
  header b { font-size:16px; } header span { color:var(--muted); font-size:13px; }
  #log { flex:1; overflow-y:auto; padding:16px; display:flex; flex-direction:column; gap:10px; }
  .msg { max-width:min(760px, 88%); padding:10px 14px; border-radius:14px; white-space:pre-wrap;
         word-break:break-word; box-shadow:0 1px 2px rgba(0,0,0,.08); }
  .me { align-self:flex-end; background:var(--me); color:var(--me-text); border-bottom-right-radius:4px; }
  .bot { align-self:flex-start; background:var(--bot); border-bottom-left-radius:4px;
         font-family:"Sarabun","Noto Sans Thai",Consolas,monospace; }
  .bot a { color:#2e90fa; } .me a { color:#fff; }
  .bot img { max-width:100%; border-radius:8px; display:block; }
  .note { align-self:center; color:var(--muted); font-size:13px; }
  form { display:flex; gap:8px; padding:12px 16px; background:var(--panel); border-top:1px solid var(--line); }
  textarea { flex:1; resize:none; height:44px; max-height:160px; padding:10px 12px; border-radius:10px;
             border:1px solid var(--line); background:var(--bg); color:var(--text); font:inherit; }
  button, label.btn { border:0; border-radius:10px; padding:0 16px; height:44px; font:inherit; cursor:pointer;
                      background:var(--me); color:#fff; display:flex; align-items:center; }
  label.btn { background:var(--bot); color:var(--text); border:1px solid var(--line); padding:0 12px; }
  button:disabled { opacity:.5; cursor:wait; }
</style></head>
<body>
<header><b>Vicky</b><span>Local chat as {{NAME}} &middot; /check, /check FAS, /summary, /bim, /docs, /reset</span></header>
<div id="log"><div class="note">ส่งไฟล์ด้วยปุ่ม 📎 แล้วพิมพ์ /check หรือพิมพ์คุยได้เลย</div></div>
<form id="f">
  <label class="btn" title="Send a file">📎<input id="file" type="file" hidden
         accept=".xlsx,.xlsm,.xls,.csv,image/*"></label>
  <textarea id="t" placeholder="พิมพ์ข้อความ... (Enter = ส่ง, Shift+Enter = ขึ้นบรรทัดใหม่)"></textarea>
  <button id="b">ส่ง</button>
</form>
<script>
const log = document.getElementById('log'), t = document.getElementById('t'),
      b = document.getElementById('b'), file = document.getElementById('file');
function esc(s) { return s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function linkify(s) { return esc(s).replace(/https?:\/\/[^\s<]+/g, u => `<a href="${u}" target="_blank">${u}</a>`); }
function add(cls, html) {
  const d = document.createElement('div'); d.className = 'msg ' + cls; d.innerHTML = html;
  log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
}
function busy(on) { b.disabled = on; file.disabled = on; }
async function call(url, opts) {
  busy(true);
  const wait = add('bot', '…');
  try {
    const r = await fetch(url, opts);
    const data = await r.json();
    wait.remove();
    if (!data.replies.length) add('bot', '<i>(ไม่มีคำตอบ)</i>');
    for (const m of data.replies) {
      if (m.image) add('bot', `<a href="${m.image}" target="_blank"><img src="${m.image}"></a>`);
      else add('bot', linkify(m.text));
    }
  } catch (e) { wait.innerHTML = 'เกิดข้อผิดพลาด: ' + esc(String(e)); }
  busy(false); t.focus();
}
document.getElementById('f').onsubmit = e => {
  e.preventDefault();
  const text = t.value.trim(); if (!text || b.disabled) return;
  add('me', linkify(text)); t.value = '';
  call('/local/send', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text})});
};
t.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); document.getElementById('f').requestSubmit(); }
});
file.onchange = () => {
  const f = file.files[0]; if (!f) return;
  add('me', '📎 ' + esc(f.name));
  const fd = new FormData(); fd.append('file', f); file.value = '';
  call('/local/upload', {method:'POST', body:fd});
};
t.focus();
</script>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser(description="Chat with Vicky in the browser, without LINE")
    ap.add_argument("--port", type=int, default=int(os.getenv("LOCAL_PORT", "5050")))
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    args = ap.parse_args()
    url = f"http://127.0.0.1:{args.port}/chat"
    print(f"Vicky local chat: {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    server.run(host="127.0.0.1", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
