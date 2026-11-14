"""FastAPI Telegram Webhook server for running PickForMe Incident Triage Agent on GCP Cloud Run."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from src.harness import GeminiClient, TriageHarness
from src.mcp_client import MCPStdioClient
from src.skills_loader import SkillLoader

load_dotenv()
logger = logging.getLogger(__name__)

app = FastAPI(title="PickForMe Incident Triage Agent")

_MAX_HISTORY_MESSAGES = 12
_chat_histories: dict[int, list[dict[str, Any]]] = {}


def _is_allowed_user(user_id: int | None) -> bool:
    allowed_raw = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
    if not allowed_raw or user_id is None:
        return False
    allowed_ids = {int(x.strip()) for x in allowed_raw.split(",") if x.strip()}
    return user_id in allowed_ids


def _send_telegram_message(chat_id: int, text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN is not configured; cannot send reply.")
        return

    # Telegram limit is 4096 chars per message.
    chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)] or ["(empty response)"]
    for chunk in chunks:
        try:
            httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
                timeout=10.0,
            )
        except Exception as exc:
            logger.exception("Failed to send Telegram reply: %s", exc)


def _run_triage_and_reply(chat_id: int, text: str) -> None:
    if text.strip() == "/clear":
        _chat_histories.pop(chat_id, None)
        _send_telegram_message(chat_id, "已清除本次排查對話記憶。")
        return

    history = _chat_histories.setdefault(chat_id, [])
    try:
        llm = GeminiClient()
        with MCPStdioClient() as mcp_client:
            tools = mcp_client.build_tool_specs()
            harness = TriageHarness(
                llm_client=llm,
                skill_loader=SkillLoader("skills"),
                tools=tools,
                # Cloud Run is stateless and read-only: fail-closed on WRITE tools.
                confirm_fn=lambda _name, _args: False,
            )
            result = harness.run(text, history=history)
        if len(history) > _MAX_HISTORY_MESSAGES:
            _chat_histories[chat_id] = history[-_MAX_HISTORY_MESSAGES:]
        _send_telegram_message(chat_id, result.answer)
    except Exception as exc:
        logger.exception("Triage run failed: %s", exc)
        _send_telegram_message(chat_id, f"排查過程發生錯誤：{exc}")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, Any]:
    expected_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
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

    background_tasks.add_task(_run_triage_and_reply, int(chat_id), text)
    return {"ok": True, "queued": True}
