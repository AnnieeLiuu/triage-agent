"""Offline tests for the FastAPI Telegram Webhook server (including adversarial & edge cases)."""

import os
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from src import telegram_server
from src.harness import LLMResponse, ToolCallRequest, ToolSpec


class _ScriptedLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)

    def generate(self, system_prompt: str, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> LLMResponse:
        return self._responses.pop(0)


def test_health_endpoint():
    client = TestClient(telegram_server.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_webhook_rejects_invalid_secret(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "top-secret")
    client = TestClient(telegram_server.app)
    resp = client.post(
        "/webhook",
        json={"message": {"from": {"id": 123}, "chat": {"id": 123}, "text": "hi"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert resp.status_code == 403


def test_webhook_ignores_unauthorized_user(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "top-secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "999")
    called = []
    monkeypatch.setattr(telegram_server, "_run_triage_and_reply", lambda c, t: called.append((c, t)))

    client = TestClient(telegram_server.app)
    resp = client.post(
        "/webhook",
        json={"message": {"from": {"id": 123}, "chat": {"id": 123}, "text": "查一下"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "top-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["ignored"] == "unauthorized_user"
    assert called == []


def test_webhook_ignores_empty_and_non_text_payloads(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "top-secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "999")
    client = TestClient(telegram_server.app)

    r1 = client.post("/webhook", json={"update_id": 1}, headers={"X-Telegram-Bot-Api-Secret-Token": "top-secret"})
    assert r1.status_code == 200 and r1.json()["ignored"] == "no_message"

    r2 = client.post(
        "/webhook",
        json={"message": {"from": {"id": 999}, "chat": {"id": 999}, "text": "   "}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "top-secret"},
    )
    assert r2.status_code == 200 and r2.json()["ignored"] == "empty_text"


def test_webhook_queues_authorized_user(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "top-secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "999")
    called = []
    monkeypatch.setattr(telegram_server, "_run_triage_and_reply", lambda c, t: called.append((c, t)))

    client = TestClient(telegram_server.app)
    resp = client.post(
        "/webhook",
        json={"message": {"from": {"id": 999}, "chat": {"id": 999}, "text": "剛剛的燒肉怎麼沒金額"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "top-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["handled"] is True
    assert called == [(999, "剛剛的燒肉怎麼沒金額")]


def test_start_and_clear_commands(monkeypatch):
    sent: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(telegram_server, "_telegram_api", lambda m, p: sent.append((m, p)) or {"result": {"message_id": 1}})
    telegram_server._chat_histories[999] = [{"role": "user", "content": "old"}]

    telegram_server._run_triage_and_reply(999, "/start")
    assert "PickForMe Ops Agent" in sent[-1][1]["text"]

    telegram_server._run_triage_and_reply(999, "/clear")
    assert 999 not in telegram_server._chat_histories
    assert "已清除" in sent[-1][1]["text"]


def test_cloud_run_blocks_write_tool_even_if_llm_attempts_it(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TRIAGE_EVAL_CASES_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "999")
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    telegram_server._chat_histories.pop(999, None)

    api_calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_tg(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        api_calls.append((method, payload))
        return {"result": {"message_id": 42}}

    monkeypatch.setattr(telegram_server, "_telegram_api", _fake_tg)
    monkeypatch.setattr(
        telegram_server,
        "GeminiClient",
        lambda: _ScriptedLLM(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCallRequest(
                            "save_eval_case",
                            {"case_id": "forced-write", "title": "Injection", "root_cause": "Test"},
                        )
                    ]
                ),
                LLMResponse(text="雲端唯讀模式已拒絕寫入。"),
            ]
        ),
    )

    client = TestClient(telegram_server.app)
    resp = client.post("/webhook", json={"message": {"from": {"id": 999}, "chat": {"id": 999}, "text": "強制存檔"}})
    assert resp.status_code == 200
    assert not (tmp_path / "forced-write.json").exists()
    assert any(m == "sendMessage" and "拒絕寫入" in p["text"] for m, p in api_calls)


def test_long_reply_is_chunked_and_errors_are_reported(monkeypatch):
    api_calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        telegram_server,
        "_telegram_api",
        lambda m, p: api_calls.append((m, p)) or {"result": {"message_id": 77}},
    )

    # 1. Verify >4000 char reply splits into multiple sendMessage calls
    telegram_server._send_telegram_message(999, "A" * 4500)
    send_calls = [p["text"] for m, p in api_calls if m == "sendMessage"]
    assert len(send_calls) == 2
    assert len(send_calls[0]) == 4000
    assert len(send_calls[1]) == 500

    # 2. Verify exception surfaces to editMessageText instead of silent failure
    api_calls.clear()
    monkeypatch.setattr(telegram_server, "GeminiClient", lambda: (_ for _ in ()).throw(RuntimeError("boom-504")))
    telegram_server._run_triage_and_reply(999, "測試錯誤回報")
    assert any(m == "editMessageText" and "boom-504" in p["text"] for m, p in api_calls)


# Keep os imported for env-based MCP tests if needed
_ = os
