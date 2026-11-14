"""Offline tests for the FastAPI Telegram Webhook server."""

from fastapi.testclient import TestClient
from src import telegram_server


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
