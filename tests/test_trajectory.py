"""Offline Trajectory Eval tests for TriageHarness (skill routing, tool sequence, HITL gate, circuit breaker)."""

import json
from pathlib import Path
from typing import Any

from src.harness import LLMResponse, ToolCallRequest, ToolPermission, ToolSpec, TriageHarness
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
