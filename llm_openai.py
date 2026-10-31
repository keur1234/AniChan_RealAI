"""Chat through an OpenAI-compatible API (default: tokenme, DeepSeek), with the same tools Vicky has on Gemini.

.env:
    TOKENME_API_KEY=...                                   # turns this on
    TOKENME_MODEL=deepseek-ai/deepseek-v4-flash-0731      # optional: text chat
    TOKENME_VISION_MODEL=google/gemma-4-31b-it            # optional: messages with images ("" = images go to Gemini)
    TOKENME_BASE_URL=https://api.tokenme.limited/api/v1   # optional
"""
import base64
import json
import logging
import os
import re

log = logging.getLogger("vicky")

API_KEY = os.getenv("TOKENME_API_KEY", "").strip()
BASE_URL = os.getenv("TOKENME_BASE_URL", "https://api.tokenme.limited/api/v1").strip()
MODEL = os.getenv("TOKENME_MODEL", "deepseek-ai/deepseek-v4-flash-0731").strip()
VISION_MODEL = os.getenv("TOKENME_VISION_MODEL", "google/gemma-4-31b-it").strip()
# The API's firewall rejects the OpenAI SDK's default User-Agent, so name the app
USER_AGENT = os.getenv("TOKENME_USER_AGENT", "vicky-obk/1.0")
TIMEOUT_SECONDS = float(os.getenv("TOKENME_TIMEOUT", "120"))

_client = None
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def ready():
    return bool(API_KEY)


def client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(base_url=BASE_URL, api_key=API_KEY, default_headers={"User-Agent": USER_AGENT},
                         timeout=TIMEOUT_SECONDS, max_retries=2)
    return _client


def tools_from(declarations):
    """Gemini FunctionDeclarations -> OpenAI tools (the JSON schemas are the same)."""
    return [{"type": "function", "function": {"name": d.name, "description": d.description,
                                              "parameters": d.parameters_json_schema or {"type": "object", "properties": {}}}}
            for d in declarations]


def _text(content):
    return "".join(p.text for p in (content.parts or []) if getattr(p, "text", None)).strip()


def history_messages(history):
    """Vicky's stored conversation (Gemini Contents with text) -> chat messages."""
    out = []
    for content in history:
        text = _text(content)
        if text:
            out.append({"role": "assistant" if content.role == "model" else "user", "content": text})
    return out


def can_handle(images):
    return ready() and (not images or bool(VISION_MODEL))


def _create(model, messages, tools):
    """One completion; if the model refuses tools (some vision models do), ask again without them."""
    from openai import BadRequestError
    try:
        return client().chat.completions.create(model=model, messages=messages, tools=tools) if tools else \
            client().chat.completions.create(model=model, messages=messages)
    except BadRequestError as e:
        if not tools:
            raise
        log.warning("%s rejected the request with tools (%s); asking without tools", model, str(e)[:150])
        return client().chat.completions.create(model=model, messages=messages)


def chat(system_prompt, history, user_text, declarations, run_tool, max_rounds=5, images=()):
    """Ask the model, running the tools it calls (run_tool(name, args) -> dict). Returns the reply text.
    images: [(bytes, mime type)] -> sent to VISION_MODEL."""
    content = user_text
    if images:
        content = [{"type": "text", "text": user_text}] + [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}
            for data, mime in images]
    model = VISION_MODEL if images else MODEL
    messages = [{"role": "system", "content": system_prompt}] + history_messages(history) + \
               [{"role": "user", "content": content}]
    tools = tools_from(declarations)
    message = None
    for _ in range(max_rounds):
        response = _create(model, messages, tools)
        message = response.choices[0].message
        calls = message.tool_calls or []
        if not calls:
            break
        messages.append({"role": "assistant", "content": message.content or "",
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                                        for c in calls]})
        for call in calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = run_tool(call.function.name, args if isinstance(args, dict) else {})
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(result, ensure_ascii=False, default=str)})
    return THINK_RE.sub("", (message.content if message else "") or "").strip()
