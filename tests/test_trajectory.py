"""Offline Trajectory Eval tests for TriageHarness (skill routing, tool sequence, HITL gate, circuit breaker)."""

import json
from pathlib import Path
from typing import Any

from src.harness import GeminiClient, LLMResponse, ToolCallRequest, ToolPermission, ToolSpec, TriageHarness
from src.skills_loader import SkillLoader


class ScriptedMockLLM:
    """Deterministic offline LLM returning pre-scripted responses per turn."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        self.calls.append({"system_prompt": system_prompt, "messages": list(messages), "tools": [t.name for t in tools]})
        if not self._responses:
            return LLMResponse(text="(no more scripted responses)")
        return self._responses.pop(0)


def _mock_read_tools(cases_dir: Path) -> dict[str, ToolSpec]:
    def _mock_save(
        case_id: str,
        title: str,
        root_cause: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        cases_dir.mkdir(parents=True, exist_ok=True)
        out = cases_dir / f"{case_id}.json"
        out.write_text(json.dumps({"case_id": case_id, "title": title, "root_cause": root_cause, "trace_id": trace_id}), encoding="utf-8")
        return {"saved": True, "path": str(out)}

    return {
        "fetch_failed_traces": ToolSpec(
            name="fetch_failed_traces",
            description="mock fetch_failed_traces",
            permission=ToolPermission.READ,
            fn=lambda limit=10, min_score=None: [
                {"trace_id": "tr-404", "level": "ERROR", "status_message": "Stage 2 timeout"}
            ],
        ),
        "get_trace_spans": ToolSpec(
            name="get_trace_spans",
            description="mock get_trace_spans",
            permission=ToolPermission.READ,
            fn=lambda trace_id: {"trace_id": trace_id, "spans": [{"name": "stage2_places", "level": "ERROR"}]},
        ),
        "check_neon_health": ToolSpec(
            name="check_neon_health",
            description="mock check_neon_health",
            permission=ToolPermission.READ,
            fn=lambda: {"ok": False, "ssl_configured": False, "error": "SSL required"},
        ),
        "save_eval_case": ToolSpec(
            name="save_eval_case",
            description="mock save_eval_case",
            permission=ToolPermission.WRITE,
            fn=_mock_save,
        ),
    }


def test_trajectory_langfuse_skill_routing_and_tool_order(tmp_path: Path):
    mock_llm = ScriptedMockLLM(
        [
            LLMResponse(tool_calls=[ToolCallRequest("load_skill", {"name": "triage-langfuse-trace"})]),
            LLMResponse(tool_calls=[ToolCallRequest("fetch_failed_traces", {"limit": 5})]),
            LLMResponse(tool_calls=[ToolCallRequest("get_trace_spans", {"trace_id": "tr-404"})]),
            LLMResponse(text="根因：tr-404 在 Stage 2 (stage2_places) 發生 timeout。"),
        ]
    )
    harness = TriageHarness(
        llm_client=mock_llm,
        skill_loader=SkillLoader("skills"),
        tools=_mock_read_tools(tmp_path),
    )

    result = harness.run("請排查最近 Langfuse 失敗的 trace")

    assert result.stopped_reason == "completed"
    assert result.trajectory.skills_loaded == ["triage-langfuse-trace"]
    assert result.trajectory.tools_called == ["load_skill", "fetch_failed_traces", "get_trace_spans"]
    assert "Stage 2" in result.answer


def test_trajectory_neon_db_triage(tmp_path: Path):
    mock_llm = ScriptedMockLLM(
        [
            LLMResponse(tool_calls=[ToolCallRequest("load_skill", {"name": "triage-db-neon"})]),
            LLMResponse(tool_calls=[ToolCallRequest("check_neon_health", {})]),
            LLMResponse(text="Neon DB 連線失敗，原因：缺少 sslmode=require。"),
        ]
    )
    harness = TriageHarness(
        llm_client=mock_llm,
        skill_loader=SkillLoader("skills"),
        tools=_mock_read_tools(tmp_path),
    )

    result = harness.run("PickForMe 一直報資料庫連線錯誤")

    assert result.stopped_reason == "completed"
    assert result.trajectory.skills_loaded == ["triage-db-neon"]
    assert result.trajectory.tools_called == ["load_skill", "check_neon_health"]


def test_trajectory_write_gate_approved_writes_eval_case(tmp_path: Path):
    mock_llm = ScriptedMockLLM(
        [
            LLMResponse(tool_calls=[ToolCallRequest("load_skill", {"name": "triage-eval-capture"})]),
            LLMResponse(
                tool_calls=[
                    ToolCallRequest(
                        "save_eval_case",
                        {
                            "case_id": "stage2-timeout-01",
                            "title": "Stage 2 Places timeout",
                            "root_cause": "Google Places API timeout",
                            "trace_id": "tr-404",
                        },
                    )
                ]
            ),
            LLMResponse(text="已將案例 stage2-timeout-01 寫入 evals/cases/。"),
        ]
    )
    harness = TriageHarness(
        llm_client=mock_llm,
        skill_loader=SkillLoader("skills"),
        tools=_mock_read_tools(tmp_path),
        confirm_fn=lambda _name, _args: True,
    )

    result = harness.run("把剛才的 tr-404 存成 eval case")

    assert result.stopped_reason == "completed"
    assert result.trajectory.permission_decisions == [
        {
            "tool": "save_eval_case",
            "approved": True,
            "arguments": {
                "case_id": "stage2-timeout-01",
                "title": "Stage 2 Places timeout",
                "root_cause": "Google Places API timeout",
                "trace_id": "tr-404",
            },
        }
    ]
    assert (tmp_path / "stage2-timeout-01.json").exists()


def test_trajectory_write_gate_denied_blocks_file_creation(tmp_path: Path):
    mock_llm = ScriptedMockLLM(
        [
            LLMResponse(tool_calls=[ToolCallRequest("load_skill", {"name": "triage-eval-capture"})]),
            LLMResponse(
                tool_calls=[
                    ToolCallRequest(
                        "save_eval_case",
                        {
                            "case_id": "should-not-exist",
                            "title": "Denied case",
                            "root_cause": "Test denial",
                        },
                    )
                ]
            ),
            LLMResponse(text="使用者已拒絕寫入，未建立任何檔案。"),
        ]
    )
    harness = TriageHarness(
        llm_client=mock_llm,
        skill_loader=SkillLoader("skills"),
        tools=_mock_read_tools(tmp_path),
        confirm_fn=lambda _name, _args: False,
    )

    result = harness.run("把這個存成 eval case")

    assert result.stopped_reason == "completed"
    assert result.trajectory.permission_decisions[0]["approved"] is False
    assert not (tmp_path / "should-not-exist.json").exists()
    # Verify the model received the denial observation
    last_tool_msg = mock_llm.calls[-1]["messages"][-1]
    assert last_tool_msg["role"] == "tool"
    assert "denied" in last_tool_msg["content"].lower()


def test_trajectory_max_steps_circuit_breaker(tmp_path: Path):
    infinite_tool_loop = [
        LLMResponse(tool_calls=[ToolCallRequest("check_neon_health", {})]) for _ in range(5)
    ]
    mock_llm = ScriptedMockLLM(infinite_tool_loop)
    harness = TriageHarness(
        llm_client=mock_llm,
        skill_loader=SkillLoader("skills"),
        tools=_mock_read_tools(tmp_path),
        max_steps=3,
    )

    result = harness.run("一直檢查 DB")

    assert result.stopped_reason == "max_steps"
    assert result.steps_used == 3
    assert result.trajectory.events[-1].event_type == "circuit_breaker"


def test_trajectory_multiturn_preserves_history(tmp_path: Path):
    mock_llm = ScriptedMockLLM(
        [
            LLMResponse(text="剛剛那筆燒肉 trace_id 是 tr-yakiniku，預算欄位是 null。"),
            LLMResponse(text="好的，已依據前文 tr-yakiniku 完成紀錄。"),
        ]
    )
    harness = TriageHarness(
        llm_client=mock_llm,
        skill_loader=SkillLoader("skills"),
        tools=_mock_read_tools(tmp_path),
    )

    history: list[dict[str, Any]] = []
    res1 = harness.run("剛剛的燒肉那則怎麼沒有金額？", history=history)
    assert "tr-yakiniku" in res1.answer

    harness.run("把剛剛那筆存成 eval case", history=history)
    turn2_messages = mock_llm.calls[1]["messages"]
    assert any("燒肉" in str(m.get("content", "")) for m in turn2_messages)
    assert any("tr-yakiniku" in str(m.get("content", "")) for m in turn2_messages)


def test_gemini_build_contents_merges_parallel_tool_responses():
    messages = [
        {"role": "user", "content": "查一下"},
        {
            "role": "assistant",
            "tool_calls": [
                {"name": "check_neon_health", "arguments": {}},
                {"name": "check_llm_providers", "arguments": {}},
            ],
        },
        {"role": "tool", "name": "check_neon_health", "content": '{"ok": true}'},
        {"role": "tool", "name": "check_llm_providers", "content": '{"gemini": true}'},
    ]
    contents = GeminiClient._build_contents(messages)
    assert len(contents) == 3
    assert len(contents[2].parts) == 2
    assert contents[2].parts[0].function_response.name == "check_neon_health"
    assert contents[2].parts[1].function_response.name == "check_llm_providers"


def test_gemini_rate_limit_retry_and_delay_parsing(monkeypatch):
    assert GeminiClient._extract_retry_delay("Please retry in 29.0096s.") == 30.5096
    assert GeminiClient._extract_retry_delay("no delay info", default=12.0) == 12.0
    assert GeminiClient._extract_retry_delay("Please retry in 50s.") == 40.0
    assert GeminiClient._extract_retry_delay("Please retry in 99s.") is None
    assert GeminiClient._extract_retry_delay("GenerateRequestsPerDayPerProjectPerModel") is None

    slept: list[float] = []
    monkeypatch.setattr("src.harness.time.sleep", lambda s: slept.append(s))

    attempts = {"n": 0}

    class _FakeModels:
        def generate_content(self, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("429 RESOURCE_EXHAUSTED. Please retry in 7.5s.")
            return "ok-resp"

    client = object.__new__(GeminiClient)
    client.model = "gemini-3-flash-preview"
    client._client = type("_FakeInner", (), {"models": _FakeModels()})()

    assert client._call_with_rate_limit_retry([], None) == "ok-resp"
    assert attempts["n"] == 2
    assert slept == [9.0]

    # Verify RPD (PerDay) switches immediately to fallback model without sleeping
    models_called: list[str] = []

    class _FakeFallbackModels:
        def generate_content(self, **kwargs):
            models_called.append(kwargs["model"])
            if kwargs["model"] == "gemini-3-flash-preview":
                raise RuntimeError("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel")
            if kwargs["model"] == "gemini-2.5-flash":
                raise RuntimeError("404 NOT_FOUND: gemini-2.5-flash is no longer available")
            if kwargs["model"] == "gemini-3.8-flash":
                raise RuntimeError("504 DEADLINE_EXCEEDED: Deadline expired")
            return "fallback-ok"

    statuses: list[str] = []
    slept.clear()
    fb_client = object.__new__(GeminiClient)
    fb_client.model = "gemini-3-flash-preview"
    fb_client.fallback_models = ["gemini-2.5-flash", "gemini-3.8-flash", "gemini-3.1-flash-lite"]
    fb_client.on_status = statuses.append
    fb_client._client = type("_FakeInner", (), {"models": _FakeFallbackModels()})()

    assert fb_client._call_with_rate_limit_retry([], None) == "fallback-ok"
    assert models_called == [
        "gemini-3-flash-preview",
        "gemini-2.5-flash",
        "gemini-3.8-flash",
        "gemini-3.1-flash-lite",
    ]
    assert fb_client.model == "gemini-3.1-flash-lite"
    assert slept == []
    assert len(statuses) == 3
    assert "gemini-2.5-flash" in statuses[0]
    assert "gemini-3.8-flash" in statuses[1]
    assert "gemini-3.1-flash-lite" in statuses[2]

    # Second call should NOT retry previously exhausted models
    models_called.clear()
    assert fb_client._call_with_rate_limit_retry([], None) == "fallback-ok"
    assert models_called == ["gemini-3.1-flash-lite"]


def test_gemini_cross_provider_groq_fallback(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fake-groq-key")
    monkeypatch.setenv("GROQ_FALLBACK_MODELS", "llama-3.3-70b-versatile")

    class _AlwaysExhaustedModels:
        def generate_content(self, **kwargs):
            raise RuntimeError("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel")

    posted: list[dict[str, Any]] = []

    class _FakeHttpResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "check_neon_health", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            }

    monkeypatch.setattr(
        "httpx.post",
        lambda url, headers, json, timeout: posted.append(json) or _FakeHttpResp(),
    )

    client = object.__new__(GeminiClient)
    client.model = "gemini-3-flash-preview"
    client.fallback_models = ["gemini-2.5-flash"]
    client._use_groq_fallback = False
    client._client = type("_FakeInner", (), {"models": _AlwaysExhaustedModels()})()

    tool = ToolSpec("check_neon_health", "check db", ToolPermission.READ, lambda: {})
    res = client.generate("sys", [{"role": "user", "content": "查 DB"}], [tool])
    assert client._use_groq_fallback is True
    assert posted[0]["model"] == "llama-3.3-70b-versatile"
    assert len(res.tool_calls) == 1
    assert res.tool_calls[0].name == "check_neon_health"

    # Turn 2: verify role="tool" message is mapped with matching tool_call_id
    posted.clear()
    monkeypatch.setattr(
        "httpx.post",
        lambda url, headers, json, timeout: posted.append(json)
        or type(
            "_TextResp",
            (),
            {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"choices": [{"message": {"content": "DB 正常"}}]},
            },
        )(),
    )
    res2 = client.generate(
        "sys",
        [
            {"role": "user", "content": "查 DB"},
            {"role": "assistant", "tool_calls": [{"name": "check_neon_health", "arguments": {}}]},
            {"role": "tool", "name": "check_neon_health", "content": '{"ok": true}'},
        ],
        [tool],
    )
    assert res2.text == "DB 正常"
    assert posted[0]["messages"][-1]["role"] == "tool"
    assert posted[0]["messages"][-1]["tool_call_id"] == "call_1"


def test_system_prompt_concise_operator_guidance(tmp_path: Path):
    harness = TriageHarness(
        llm_client=ScriptedMockLLM([]),
        skill_loader=SkillLoader("skills"),
        tools=_mock_read_tools(tmp_path),
    )
    prompt = harness.build_system_prompt()
    assert "150–250 字" in prompt
    assert "工程師" in prompt
