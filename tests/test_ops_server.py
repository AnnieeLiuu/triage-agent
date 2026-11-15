"""Offline unit tests for FastMCP ops_server tools."""

import json
from pathlib import Path
from typing import Any

import pytest
from src.mcp_server.ops_server import (
    TOOL_PERMISSIONS,
    _compact_io,
    check_llm_providers,
    check_neon_health,
    fetch_failed_traces,
    get_agent_step_detail,
    get_observation_detail,
    get_recent_agent_runs,
    get_trace_spans,
    list_pickforme_files,
    read_pickforme_file,
    save_eval_case,
    search_pickforme_code,
)


def test_tool_permissions_classification():
    assert TOOL_PERMISSIONS["fetch_failed_traces"] == "READ"
    assert TOOL_PERMISSIONS["get_trace_spans"] == "READ"
    assert TOOL_PERMISSIONS["get_observation_detail"] == "READ"
    assert TOOL_PERMISSIONS["check_neon_health"] == "READ"
    assert TOOL_PERMISSIONS["get_recent_agent_runs"] == "READ"
    assert TOOL_PERMISSIONS["get_agent_step_detail"] == "READ"
    assert TOOL_PERMISSIONS["check_llm_providers"] == "READ"
    assert TOOL_PERMISSIONS["list_pickforme_files"] == "READ"
    assert TOOL_PERMISSIONS["search_pickforme_code"] == "READ"
    assert TOOL_PERMISSIONS["read_pickforme_file"] == "READ"
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


def test_compact_io_preserves_all_stores_while_shrinking_reviews():
    long_review = "とても美味しいコーヒーでした。" * 40
    payload = json.dumps(
        {
            "aligned_stores": [
                {
                    "name_ja": f"Store-{idx}",
                    "tabelog_rating": 3.5,
                    "price_dinner": "￥1,000～￥1,999",
                    "sample_reviews": [{"text": long_review}],
                }
                for idx in range(6)
            ]
        },
        ensure_ascii=False,
    )
    compacted = _compact_io(payload)
    assert isinstance(compacted, dict)
    stores = compacted["aligned_stores"]
    assert len(stores) == 6
    assert [s["name_ja"] for s in stores] == [f"Store-{i}" for i in range(6)]
    assert all(s["price_dinner"] == "￥1,000～￥1,999" for s in stores)
    assert "use detail tool" in stores[0]["sample_reviews"][0]["text"]


def test_get_recent_agent_runs_query_and_chat_id_filters(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db?sslmode=require")
    recorded_queries: list[tuple[str, Any]] = []

    class FakeCursor:
        def __init__(self) -> None:
            self._call = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql: str, params: Any = None) -> None:
            self._call += 1
            recorded_queries.append((sql, params))

        def fetchall(self):
            if self._call == 1:
                return [("run-1", 12345, "藏前 咖啡", "gemini", 100, 500, "completed", None)]
            return [("run-1", 1, "state_1", "intent", {"q": "咖啡"}, '{"ok": true}', 50, None, None)]

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return FakeCursor()

    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: FakeConn())
    runs = get_recent_agent_runs(limit=3, query="咖啡", chat_id=12345)
    assert len(runs) == 1
    assert runs[0]["run_id"] == "run-1"
    assert runs[0]["steps"][0]["tool_output"] == {"ok": True}
    first_sql, first_params = recorded_queries[0]
    assert "user_query ILIKE %s" in first_sql
    assert "chat_id = %s" in first_sql
    assert first_params == ("%咖啡%", 12345, 3)


def test_get_agent_step_detail_paginates(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db?sslmode=require")

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql: str, params: Any = None) -> None:
            pass

        def fetchone(self):
            return ("state_3", "tabelog", {"k": "v"}, "A" * 500, 120, None, None)

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return FakeCursor()

    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: FakeConn())
    res = get_agent_step_detail("run-1", 3, offset=100, max_chars=250)
    assert res["tool_output"]["offset"] == 100
    assert res["tool_output"]["returned_chars"] == 250
    assert res["tool_output"]["total_chars"] == 500
    assert res["tool_output"]["has_more"] is True


def test_get_observation_detail_paginates(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")

    class FakeResp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {
                "id": "obs-1",
                "traceId": "tr-1",
                "name": "synthesis",
                "type": "GENERATION",
                "level": "DEFAULT",
                "statusMessage": "",
                "input": "prompt",
                "output": "B" * 400,
            }

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: FakeResp())
    res = get_observation_detail("obs-1", offset=0, max_chars=200)
    assert res["id"] == "obs-1"
    assert res["output"]["returned_chars"] == 200
    assert res["output"]["has_more"] is True


def test_search_pickforme_code_zipball_without_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pickforme-main/scripts/analyze.py", "aligned_stores = []\n")
        zf.writestr("pickforme-main/src/pipeline.py", "def run():\n    aligned_stores = stores[:3]\n    return aligned_stores\n")
        zf.writestr("pickforme-main/README.md", "# PickForMe\n")

    class FakeResp:
        status_code = 200
        content = buf.getvalue()

        def raise_for_status(self) -> None:
            pass

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: FakeResp())
    matches = search_pickforme_code("aligned_stores", max_results=1)
    assert len(matches) == 1
    assert matches[0]["path"] == "src/pipeline.py"
    assert matches[0]["line"] == 2
    assert matches[0]["source"] == "zip_content_search"


def test_list_pickforme_files_filters_prefix(monkeypatch: pytest.MonkeyPatch):
    class FakeResp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {
                "tree": [
                    {"type": "blob", "path": "src/agent/pipeline.py"},
                    {"type": "blob", "path": "src/agent/stages/synthesis.py"},
                    {"type": "blob", "path": "scripts/analyze.py"},
                ]
            }

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: FakeResp())
    res = list_pickforme_files(prefix="src/")
    assert res["total_matched"] == 2
    assert res["files"] == ["src/agent/pipeline.py", "src/agent/stages/synthesis.py"]


def test_read_pickforme_file_and_invalid_path(monkeypatch: pytest.MonkeyPatch):
    assert "error" in read_pickforme_file("../secrets.env")

    class FakeResp:
        text = "\n".join(f"line {i}" for i in range(1, 11))

        def raise_for_status(self) -> None:
            pass

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: FakeResp())
    res = read_pickforme_file("src/app.py", start_line=2, end_line=4)
    assert res["start_line"] == 2
    assert res["end_line"] == 4
    assert res["total_lines"] == 10
    assert "2: line 2" in res["content"]
