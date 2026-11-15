"""FastAPI Telegram Webhook server for running PickForMe Incident Triage Agent on GCP Cloud Run."""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import logging
import os
import re
import threading
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

from src.harness import GeminiClient, TrajectoryEvent, TriageHarness
from src.mcp_client import MCPStdioClient
from src.skills_loader import SkillLoader

load_dotenv()
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

app = FastAPI(title="PickForMe Incident Triage Agent")

_MAX_HISTORY_MESSAGES = 12
_chat_histories: dict[int, list[dict[str, Any]]] = {}
_history_lock = threading.Lock()


def _trim_history(history: list[dict[str, Any]], max_messages: int = _MAX_HISTORY_MESSAGES) -> list[dict[str, Any]]:
    """Keep recent turns within max_messages while always starting on a 'user' boundary."""
    trimmed = history[-max_messages:] if len(history) > max_messages else list(history)
    while trimmed and trimmed[0].get("role") != "user":
        trimmed.pop(0)
    return trimmed


def _is_allowed_user(user_id: int | None) -> bool:
    allowed_raw = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
    if not allowed_raw or user_id is None:
        return False
    allowed_ids = {int(x.strip()) for x in allowed_raw.split(",") if x.strip()}
    return user_id in allowed_ids


def _telegram_api(method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN is not configured; cannot call %s.", method)
        return None
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload,
            timeout=10.0,
        )
        return resp.json() if resp.status_code == 200 else None
    except Exception as exc:
        logger.exception("Telegram API %s failed: %s", method, exc)
        return None


def _markdown_to_telegram_html(text: str) -> str:
    """Convert common LLM Markdown into safe Telegram-compatible HTML."""
    code_blocks: list[str] = []
    inline_codes: list[str] = []

    def _stash_code_block(match: re.Match[str]) -> str:
        code_blocks.append(f"<pre>{html.escape(match.group(1).strip())}</pre>")
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    def _stash_inline_code(match: re.Match[str]) -> str:
        inline_codes.append(f"<code>{html.escape(match.group(1))}</code>")
        return f"\x00INLINECODE{len(inline_codes) - 1}\x00"

    # Extract code spans before escaping or formatting the rest of the text.
    out = re.sub(r"```(?:[a-zA-Z0-9_+-]+)?\n([\s\S]*?)```", _stash_code_block, text)
    out = re.sub(r"`([^`\n]+)`", _stash_inline_code, out)
    out = html.escape(out)

    # Headings, horizontal rules, and bullet lists.
    out = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$", r"<b>\1</b>", out)
    out = re.sub(r"(?m)^\s*([-*_])\1{2,}\s*$", "──────────", out)
    out = re.sub(r"(?m)^(\s*)[*-]\s+", r"\1• ", out)

    # Links, bold, and asterisk-italics (avoid underscores so snake_case stays intact).
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"__(.+?)__", r"<b>\1</b>", out)
    out = re.sub(r"(?<!\w)\*([^*\n]+?)\*(?!\w)", r"<i>\1</i>", out)

    for idx, block in enumerate(code_blocks):
        out = out.replace(f"\x00CODEBLOCK{idx}\x00", block)
    for idx, code in enumerate(inline_codes):
        out = out.replace(f"\x00INLINECODE{idx}\x00", code)
    return out


def _chunk_text(text: str, max_len: int = 4000) -> list[str]:
    if not text:
        return ["(empty response)"]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        split_at = remaining.rfind("\n", max_len // 2, max_len)
        if split_at == -1:
            split_at = max_len
        else:
            split_at += 1
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _send_telegram_message(chat_id: int, text: str) -> int | None:
    last_message_id: int | None = None
    for chunk in _chunk_text(text, max_len=4000):
        html_chunk = _markdown_to_telegram_html(chunk)
        data: dict[str, Any] | None = None
        if len(html_chunk) <= 4096:
            data = _telegram_api(
                "sendMessage",
                {"chat_id": chat_id, "text": html_chunk, "parse_mode": "HTML"},
            )
        if data is None:
            # Fallback to plain text if HTML formatting is rejected or exceeds Telegram's 4096 limit.
            data = _telegram_api("sendMessage", {"chat_id": chat_id, "text": chunk})
        if isinstance(data, dict) and isinstance(data.get("result"), dict):
            last_message_id = data["result"].get("message_id")
    return last_message_id


def _edit_telegram_message(chat_id: int, message_id: int, text: str) -> None:
    _telegram_api("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text[:4000]})


def _send_typing(chat_id: int) -> None:
    _telegram_api("sendChatAction", {"chat_id": chat_id, "action": "typing"})


def _run_triage_and_reply(chat_id: int, text: str) -> None:
    stripped = text.strip()
    if stripped == "/start":
        _send_telegram_message(
            chat_id,
            "你好！我是 PickForMe Ops Agent。\n請直接傳訊息問我排查問題（例如：「剛剛的燒肉怎麼沒金額？」），或輸入 /clear 清除對話記憶。",
        )
        return
    if stripped == "/clear":
        _chat_histories.pop(chat_id, None)
        _send_telegram_message(chat_id, "已清除本次排查對話記憶。")
        return

    status_msg_id = _send_telegram_message(chat_id, "🔍 收到！正在啟動排查...")
    _send_typing(chat_id)
    progress_lines: list[str] = []
    model_status: list[str] = []  # Single mutable slot so model switches update in-place without clutter.

    def _render_progress() -> None:
        if status_msg_id is None:
            return
        body = list(progress_lines)
        if model_status:
            body.append(f"• {model_status[0]}")
        _edit_telegram_message(chat_id, status_msg_id, "🔍 思考與排查中：\n" + "\n".join(body))
        _send_typing(chat_id)

    def _on_event(event: TrajectoryEvent) -> None:
        line: str | None = None
        if event.event_type == "skill_loaded":
            line = f"• [Step {event.step}] 載入 SOP：{event.detail['skill']}"
        elif event.event_type == "tool_call" and event.detail.get("tool") != "load_skill":
            line = f"• [Step {event.step}] 查詢工具：{event.detail['tool']}"
        if line is not None:
            progress_lines.append(line)
            _render_progress()

    def _on_status(status_msg: str) -> None:
        model_status[:] = [status_msg]
        _render_progress()

    with _history_lock:
        history = list(_chat_histories.get(chat_id, []))
    try:
        llm = GeminiClient()
        if hasattr(llm, "on_status"):
            llm.on_status = _on_status
        with MCPStdioClient() as mcp_client:
            tools = mcp_client.build_tool_specs()
            harness = TriageHarness(
                llm_client=llm,
                skill_loader=SkillLoader("skills"),
                tools=tools,
                # Cloud Run is stateless and read-only: fail-closed on WRITE tools.
                confirm_fn=lambda _name, _args: False,
                on_event=_on_event,
                read_only=True,
            )
            result = harness.run(stripped, history=history)
        with _history_lock:
            _chat_histories[chat_id] = _trim_history(history)
        model_name = getattr(llm, "model", None)
        logger.info(
            "triage_reply %s",
            json.dumps(
                {
                    "chat_id": chat_id,
                    "query": stripped[:200],
                    "model": model_name,
                    "steps": result.steps_used,
                    "progress": progress_lines,
                    "answer": result.answer,
                },
                ensure_ascii=False,
            ),
        )
        if status_msg_id is not None:
            model_tag = f"｜{model_name}" if model_name else ""
            summary_header = f"✅ 排查完成（共 {result.steps_used} 步{model_tag}）"
            if progress_lines:
                summary_header += "：\n" + "\n".join(progress_lines)
            _edit_telegram_message(chat_id, status_msg_id, summary_header)
        _send_telegram_message(chat_id, result.answer)
    except Exception as exc:
        logger.exception("Triage run failed: %s", exc)
        err_text = f"❌ 排查過程發生錯誤：{exc}"
        if progress_lines:
            err_text += "\n\n已完成步驟：\n" + "\n".join(progress_lines)
        if status_msg_id is not None:
            _edit_telegram_message(chat_id, status_msg_id, err_text)
        else:
            _send_telegram_message(chat_id, err_text)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, Any]:
    expected_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not expected_secret or not hmac.compare_digest(x_telegram_bot_api_secret_token or "", expected_secret):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    payload = await request.json()
    message = payload.get("message") or payload.get("edited_message")
    if not isinstance(message, dict):
        return {"ok": True, "ignored": "no_message"}

    user_id = (message.get("from") or {}).get("id")
    if not _is_allowed_user(user_id):
        return {"ok": True, "ignored": "unauthorized_user"}

    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return {"ok": True, "ignored": "empty_text"}

    # Run synchronously before returning 200 OK so Cloud Run request-based CPU is not throttled.
    await asyncio.to_thread(_run_triage_and_reply, int(chat_id), text)
    return {"ok": True, "handled": True}
