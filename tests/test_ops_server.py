"""Offline unit tests for FastMCP ops_server tools."""

import json
from pathlib import Path

import pytest
from src.mcp_server.ops_server import (
    TOOL_PERMISSIONS,
    check_llm_providers,
    check_neon_health,
    fetch_failed_traces,
    get_recent_agent_runs,
    get_trace_spans,
    save_eval_case,
)


def test_tool_permissions_classification():
    assert TOOL_PERMISSIONS["fetch_failed_traces"] == "READ"
    assert TOOL_PERMISSIONS["get_trace_spans"] == "READ"
    assert TOOL_PERMISSIONS["check_neon_health"] == "READ"
    assert TOOL_PERMISSIONS["get_recent_agent_runs"] == "READ"
    assert TOOL_PERMISSIONS["check_llm_providers"] == "READ"
    assert TOOL_PERMISSIONS["save_eval_case"] == "WRITE"


def test_langfuse_tools_handle_missing_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    traces = fetch_failed_traces()
    assert len(traces) == 1
    assert "error" in traces[0]

    spans = get_trace_spans("trace-123")
    assert "error" in spans


def test_check_neon_health_missing_database_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    res = check_neon_health()
    assert res["ok"] is False
    assert "DATABASE_URL" in res["error"]

    runs = get_recent_agent_runs()
    assert len(runs) == 1
    assert "DATABASE_URL" in runs[0]["error"]


def test_check_llm_providers_never_leaks_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-gemini-key")
    monkeypatch.setenv("GROQ_API_KEY", "secret-groq-key")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:19999")

    res = check_llm_providers()
    serialized = json.dumps(res)
    assert res["gemini"]["configured"] is True
    assert res["groq"]["configured"] is True
    assert "secret-gemini-key" not in serialized
    assert "secret-groq-key" not in serialized


def test_save_eval_case_writes_sanitized_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRIAGE_EVAL_CASES_DIR", str(tmp_path))
    res = save_eval_case(
        case_id="../bad/path case 01",
        title="Stage 2 empty places",
        root_cause="Google Places returned 0 results",
        trace_id="tr-999",
        reproduction_input={"city": "Tokyo", "budget": 1000},
        expected_behavior="Fallback gracefully",
    )
    assert res["saved"] is True
    out_file = Path(res["path"])
    assert out_file.parent == tmp_path
    assert out_file.exists()

    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["trace_id"] == "tr-999"
    assert data["root_cause"] == "Google Places returned 0 results"
