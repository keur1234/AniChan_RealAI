#!/usr/bin/env python3
"""Chat with Vicky in your browser, without LINE.

    python app_local.py              # opens http://127.0.0.1:5050
    python app_local.py --port 6000 --no-browser

Same Vicky as app.py (validation, /check, /check FAS, /summary, /bim, documents, Gemini chat),
running as a private chat with you. Drag Excel/CSV files onto the page (or use the paperclip) to send them.
Needs GEMINI_API_KEY in .env for normal chat; LINE keys are not needed.
"""
import argparse
import getpass
import itertools
import os
import re
import subprocess
import sys
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
from flask import abort, request, send_file  # noqa: E402

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
            files = {url: local_path(url) for url in FILE_URL_RE.findall(m["text"])}
            replies.append({"text": m["text"], "files": {u: p for u, p in files.items() if p}})
    return replies


FILE_URL_RE = re.compile(r"https?://\S+/files/[^/\s]+/\S+")


def local_path(url):
    """Where a /files/<token>/<name> link lives on this computer: the permanent copy in OBK_RESULTS_DIR
    if there is one, else the working copy (deleted after OBK_DOWNLOAD_TTL_HOURS)."""
    m = re.search(r"/files/([^/]+)/([^/?#]+)$", url)
    if not m:
        return None
    from urllib.parse import unquote
    token, name = m.group(1), unquote(m.group(2))
    folder = vicky.obk_files._archive_dirs.get(token)
    if folder and os.path.isfile(os.path.join(folder, name)):
        return os.path.join(folder, name)
    return vicky.obk_files.get_download(token, name)


def reveal(path):
    """Open the file's folder in Explorer / Finder with the file selected."""
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


@server.route("/local/open", methods=["POST"])
def local_open():
    if not _local_request():
        abort(403)
    path = local_path(str((request.get_json(silent=True) or {}).get("url", "")))
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "ไม่พบไฟล์ (อาจหมดอายุแล้ว)"}
    try:
        reveal(path)
    except OSError as e:
        return {"ok": False, "error": str(e), "path": path}
    return {"ok": True, "path": path}


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


AVATAR_FILE = os.getenv("LOCAL_AVATAR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "vicky_avatar.png")


@server.route("/local/avatar.png", methods=["GET"])
def local_avatar():
    if not os.path.isfile(AVATAR_FILE):
        abort(404)
    return send_file(AVATAR_FILE, max_age=3600)


@server.route("/chat", methods=["GET"])
def chat_page():
    if not _local_request():
        abort(403)
    return CHAT_PAGE.replace("{{NAME}}", LOCAL_USER_NAME)


CHAT_PAGE = r"""<!doctype html>
<html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vicky Local Chat</title>
<link rel="icon" href="/local/avatar.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono&family=IBM+Plex+Sans+Thai:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#f4f6fa; --surface:#ffffff; --surface-2:#eef1f6; --text:#101828; --muted:#667085; --line:#e4e7ec;
    --accent:#4f46e5; --accent-2:#7c3aed; --accent-soft:#eef2ff; --me-text:#ffffff; --ok:#12b76a;
    --shadow:0 1px 2px rgba(16,24,40,.06), 0 4px 12px rgba(16,24,40,.06);
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0d1117; --surface:#161b22; --surface-2:#1f2630; --text:#e6edf3; --muted:#8b949e; --line:#2a313c;
            --accent:#6366f1; --accent-2:#8b5cf6; --accent-soft:#1e1b4b; --shadow:0 1px 2px rgba(0,0,0,.4); }
  }
  * { box-sizing:border-box; }
  html, body { height:100%; }
  body { margin:0; display:flex; flex-direction:column; background:var(--bg); color:var(--text);
         font:15px/1.6 "IBM Plex Sans Thai","Noto Sans Thai","Segoe UI",Tahoma,sans-serif; }
  .wrap { width:100%; max-width:880px; margin:0 auto; padding:0 16px; }

  header { background:color-mix(in srgb, var(--surface) 85%, transparent); backdrop-filter:blur(10px);
           border-bottom:1px solid var(--line); position:sticky; top:0; z-index:5; }
  header .wrap { display:flex; align-items:center; gap:12px; height:64px; }
  img.avatar { object-fit:cover; border-radius:50% !important; background:var(--surface-2); box-shadow:0 0 0 2px var(--surface), 0 0 0 3px var(--accent); }
  .avatar { width:38px; height:38px; border-radius:12px; flex:none; display:grid; place-items:center;
            background:linear-gradient(135deg,var(--accent),var(--accent-2)); color:#fff; font-weight:600; }
  .title b { display:block; font-weight:600; line-height:1.2; }
  .title span { font-size:12.5px; color:var(--muted); display:flex; align-items:center; gap:6px; }
  .dot { width:7px; height:7px; border-radius:50%; background:var(--ok); }
  .who { margin-left:auto; font-size:12.5px; color:var(--muted); background:var(--surface-2);
         padding:4px 10px; border-radius:999px; }

  main { flex:1; overflow-y:auto; }
  #log { display:flex; flex-direction:column; gap:14px; padding:24px 16px 12px; }
  .row { display:flex; gap:10px; align-items:flex-end; animation:in .18s ease-out; }
  .row.me { justify-content:flex-end; }
  .row .avatar { width:30px; height:30px; border-radius:10px; font-size:13px; }
  @keyframes in { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:none; } }
  .bubble { max-width:min(680px, 85%); padding:11px 15px; border-radius:16px; white-space:pre-wrap;
            word-break:break-word; box-shadow:var(--shadow); }
  .bot .bubble { background:var(--surface); border:1px solid var(--line); border-bottom-left-radius:6px; }
  .me .bubble { background:linear-gradient(135deg,var(--accent),var(--accent-2)); color:var(--me-text);
                border-bottom-right-radius:6px; }
  .bubble a { color:var(--accent); } .me .bubble a { color:#fff; }
  .bubble img { width:440px; max-width:100%; height:auto; border-radius:10px; display:block; }
  .bubble.mono { font-family:"IBM Plex Mono",Consolas,"IBM Plex Sans Thai","Noto Sans Thai",monospace; font-size:13.5px; }
  .time { font-size:11px; color:var(--muted); margin:0 4px 2px; white-space:nowrap; }

  .file { display:flex; align-items:center; gap:10px; margin:6px 0 2px; padding:9px 12px; border-radius:12px;
          background:var(--surface-2); border:1px solid var(--line); text-decoration:none; color:var(--text);
          white-space:normal; transition:border-color .15s, transform .15s; }
  .file:hover { border-color:var(--accent); transform:translateY(-1px); }
  .file .ic { width:32px; height:32px; border-radius:8px; display:grid; place-items:center; flex:none;
              background:#107c41; color:#fff; font-size:11px; font-weight:600; }
  .file .ic.png { background:#f79009; } .file .ic.csv, .file .ic.txt { background:#475467; }
  .file .info { display:flex; flex-direction:column; min-width:0; }
  .file .nm { font-weight:500; font-size:14px; }
  .file .path { font-size:11.5px; color:var(--muted); overflow-wrap:anywhere; line-height:1.35; }
  .file .dl { margin-left:auto; color:var(--muted); font-size:12px; white-space:nowrap; padding-left:8px; }
  .me .file { background:rgba(255,255,255,.16); border-color:rgba(255,255,255,.25); color:#fff; }
  .me .file .dl { color:rgba(255,255,255,.8); }

  .typing { display:inline-flex; gap:4px; padding:4px 2px; }
  .typing i { width:7px; height:7px; border-radius:50%; background:var(--muted); animation:b 1.2s infinite; }
  .typing i:nth-child(2) { animation-delay:.15s; } .typing i:nth-child(3) { animation-delay:.3s; }
  @keyframes b { 0%,60%,100% { opacity:.3; transform:none; } 30% { opacity:1; transform:translateY(-3px); } }

  .welcome { text-align:center; color:var(--muted); padding:32px 16px 8px; }
  .welcome .avatar { width:56px; height:56px; border-radius:18px; font-size:22px; margin:0 auto 12px; }
  .welcome h2 { color:var(--text); font-size:20px; font-weight:600; margin:0 0 4px; }

  footer { padding:10px 0 18px; background:linear-gradient(transparent, var(--bg) 30%); }
  .chips { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; }
  .chip { border:1px solid var(--line); background:var(--surface); color:var(--text); border-radius:999px;
          padding:5px 12px; font:inherit; font-size:13px; cursor:pointer; transition:all .15s; }
  .chip:hover { border-color:var(--accent); color:var(--accent); }
  .composer { display:flex; align-items:flex-end; gap:8px; padding:8px; background:var(--surface);
              border:1px solid var(--line); border-radius:18px; box-shadow:var(--shadow); transition:border-color .15s; }
  .composer:focus-within { border-color:var(--accent); }
  textarea { flex:1; resize:none; border:0; outline:0; background:transparent; color:var(--text); font:inherit;
             padding:8px 6px; max-height:180px; min-height:24px; line-height:1.5; }
  .icon-btn { width:40px; height:40px; border-radius:12px; border:0; flex:none; cursor:pointer; display:grid;
              place-items:center; background:transparent; color:var(--muted); transition:all .15s; }
  .icon-btn:hover { background:var(--surface-2); color:var(--text); }
  .send { background:linear-gradient(135deg,var(--accent),var(--accent-2)); color:#fff; }
  .send:hover { filter:brightness(1.08); background:linear-gradient(135deg,var(--accent),var(--accent-2)); color:#fff; }
  .icon-btn:disabled { opacity:.45; cursor:wait; }
  .hint { font-size:12px; color:var(--muted); text-align:center; margin-top:8px; }

  #drop { position:fixed; inset:0; z-index:20; display:none; place-items:center; padding:24px;
          background:color-mix(in srgb, var(--bg) 70%, transparent); backdrop-filter:blur(4px); }
  #drop.on { display:grid; }
  #drop div { border:2px dashed var(--accent); border-radius:24px; padding:48px 64px; text-align:center;
              background:var(--surface); box-shadow:var(--shadow); pointer-events:none; }
  #drop b { display:block; font-size:18px; margin:8px 0 2px; } #drop span { color:var(--muted); font-size:13px; }
</style></head>
<body>
<header><div class="wrap">
  <img class="avatar" src="/local/avatar.png" alt="Vicky">
  <div class="title"><b>Vicky</b><span><i class="dot"></i>น้องสาวของ วิคเตอร์ ผู้ช่วย AI ของ NXY · Local</span></div>
  <div class="who">{{NAME}}</div>
</div></header>

<main id="scroll"><div class="wrap" style="padding:0"><div id="log">
  <div class="welcome"><img class="avatar" src="/local/avatar.png" alt="Vicky"><h2>สวัสดีค่ะ หนู Vicky</h2>
    ลากไฟล์ Excel/CSV มาวางตรงไหนก็ได้ หรือกด 📎 แล้วพิมพ์ /check</div>
</div></div></main>

<footer><div class="wrap">
  <div class="chips">
    <button class="chip" data-send="/check">/check</button>
    <button class="chip" data-send="/check FAS">/check FAS</button>
    <button class="chip" data-send="/summary">/summary</button>
    <button class="chip" data-send="/bim">/bim</button>
    <button class="chip" data-send="/docs">/docs</button>
    <button class="chip" data-send="/reset">/reset</button>
  </div>
  <form class="composer" id="f">
    <button type="button" class="icon-btn" id="attach" title="แนบไฟล์">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
    </button>
    <input id="file" type="file" hidden multiple accept=".xlsx,.xlsm,.xls,.csv,image/*">
    <textarea id="t" rows="1" placeholder="พิมพ์ข้อความ หรือ index codes..."></textarea>
    <button class="icon-btn send" id="b" title="ส่ง">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
           stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>
    </button>
  </form>
  <div class="hint">Enter ส่ง · Shift+Enter ขึ้นบรรทัดใหม่ · ลากไฟล์มาวางเพื่อแนบ</div>
</div></footer>

<div id="drop"><div><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="color:var(--accent)">
  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg>
  <b>วางไฟล์ที่นี่</b><span>.xlsx .xlsm .xls .csv หรือรูปภาพ</span></div></div>

<script>
const log = document.getElementById('log'), scroller = document.getElementById('scroll'),
      t = document.getElementById('t'), b = document.getElementById('b'), file = document.getElementById('file'),
      drop = document.getElementById('drop');
let busy = false;
const queue = [];

const esc = s => s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const now = () => new Date().toLocaleTimeString('th-TH', {hour:'2-digit', minute:'2-digit'});
const ext = n => (n.split('.').pop() || '').toLowerCase().slice(0, 4);
const size = n => n < 1024 * 1024 ? (n / 1024).toFixed(0) + ' KB' : (n / 1024 / 1024).toFixed(1) + ' MB';
const fileCard = (name, url, note, path) =>
  `<a class="file" ${path ? `href="#" data-open="${esc(url)}" title="เปิดโฟลเดอร์"` : url ? `href="${url}" target="_blank"` : ''}>` +
  `<span class="ic ${ext(name)}">${esc(ext(name).toUpperCase())}</span>` +
  `<span class="info"><span class="nm">${esc(name)}</span>${path ? `<span class="path">${esc(path)}</span>` : ''}</span>` +
  `<span class="dl">${note}</span></a>`;

function render(text, files) {
  // "- name\nhttp://.../files/..." lines become file cards: open the folder on this computer, or download
  const cards = [];
  text = text.replace(/^- ([^\n]+)\n(https?:\/\/\S+\/files\/\S+)$/gm, (m, name, url) => {
    const path = files && files[url];
    cards.push(fileCard(name, url, path ? '📂 เปิดโฟลเดอร์' : 'ดาวน์โหลด ↓', path));
    return `\u0000${cards.length - 1}\u0000`;
  });
  if (files && Object.keys(files).length)
    text = text.replace(/ดาวน์โหลดไฟล์ ลิงก์มีอายุ \d+ ชั่วโมง/, 'ไฟล์ผล (กดเพื่อเปิดโฟลเดอร์)')
               .replace(/Download, links expire in \d+ hours/, 'Result files (click to open the folder)');
  let html = esc(text).replace(/https?:\/\/[^\s<]+/g, u => `<a href="${u}" target="_blank">${u}</a>`);
  return html.replace(/\u0000(\d+)\u0000\n?/g, (m, i) => cards[+i]);
}
const toBottom = () => { scroller.scrollTop = scroller.scrollHeight; };
function add(who, html, mono) {
  log.querySelector('.welcome')?.remove();
  const row = document.createElement('div'); row.className = 'row ' + who;
  const cls = 'bubble' + (mono ? ' mono' : '');
  row.innerHTML = who === 'bot'
    ? `<img class="avatar" src="/local/avatar.png" alt="Vicky"><div class="${cls}">${html}</div><div class="time">${now()}</div>`
    : `<div class="time">${now()}</div><div class="${cls}">${html}</div>`;
  log.appendChild(row);
  row.querySelectorAll('img').forEach(img => img.addEventListener('load', toBottom));
  toBottom(); return row;
}
function setBusy(on) { busy = on; b.disabled = on; }

async function call(url, opts) {
  setBusy(true);
  const wait = add('bot', '<span class="typing"><i></i><i></i><i></i></span>');
  try {
    const r = await fetch(url, opts);
    const data = await r.json();
    wait.remove();
    if (!data.replies.length) add('bot', '<i>(ไม่มีคำตอบ)</i>');
    for (const m of data.replies)
      // console-style reports (/check) keep their column alignment in a monospace font
      add('bot', m.image ? `<a href="${m.image}" target="_blank"><img src="${m.image}" alt="chart"></a>` : render(m.text, m.files),
          !m.image && /^-{4} /m.test(m.text));
  } catch (e) { wait.querySelector('.bubble').textContent = 'เกิดข้อผิดพลาด: ' + e; }
  setBusy(false);
  next();
}
function next() {
  if (busy || !queue.length) return;
  const job = queue.shift();
  if (job.text !== undefined) {
    add('me', render(job.text));
    call('/local/send', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text:job.text})});
  } else {
    add('me', fileCard(job.file.name, '', size(job.file.size)));
    const fd = new FormData(); fd.append('file', job.file);
    call('/local/upload', {method:'POST', body:fd});
  }
}
const send = text => { if (text.trim()) { queue.push({text:text.trim()}); next(); } };
const sendFiles = files => { for (const f of files) queue.push({file:f}); next(); };

function grow() { t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 180) + 'px'; }
document.getElementById('f').onsubmit = e => { e.preventDefault(); send(t.value); t.value = ''; grow(); t.focus(); };
t.addEventListener('input', grow);
t.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); document.getElementById('f').requestSubmit(); }
});
document.getElementById('attach').onclick = () => file.click();
file.onchange = () => { sendFiles([...file.files]); file.value = ''; };
document.querySelectorAll('.chip').forEach(c => c.onclick = () => send(c.dataset.send));
// File cards: open the folder in Explorer (the local server does it; browsers can't open folders)
log.addEventListener('click', async e => {
  const a = e.target.closest('a[data-open]'); if (!a) return;
  e.preventDefault();
  const dl = a.querySelector('.dl'), old = dl.textContent;
  const r = await fetch('/local/open', {method:'POST', headers:{'Content-Type':'application/json'},
                                        body:JSON.stringify({url:a.dataset.open})}).then(r => r.json());
  dl.textContent = r.ok ? '✓ เปิดแล้ว' : (r.error || 'เปิดไม่ได้');
  setTimeout(() => dl.textContent = old, 2000);
});

// Drag and drop anywhere on the page
let depth = 0;
const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
addEventListener('dragenter', e => { if (hasFiles(e)) { e.preventDefault(); depth++; drop.classList.add('on'); } });
addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
addEventListener('dragleave', e => { if (hasFiles(e) && --depth <= 0) { depth = 0; drop.classList.remove('on'); } });
addEventListener('drop', e => {
  if (!hasFiles(e)) return;
  e.preventDefault(); depth = 0; drop.classList.remove('on');
  sendFiles([...e.dataTransfer.files]);
});
// Paste a file (e.g. a screenshot) into the message box
t.addEventListener('paste', e => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); sendFiles(files); }
});
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
