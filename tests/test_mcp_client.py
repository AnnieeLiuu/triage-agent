"""End-to-end tests verifying real MCP stdio transport between TriageHarness and FastMCP ops_server."""

import os
from pathlib import Path
from typing import Any

from src.harness import LLMResponse, ToolCallRequest, ToolPermission, ToolSpec, TriageHarness
from src.mcp_client import MCPStdioClient
from src.skills_loader import SkillLoader


class _ScriptedLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.messages_seen: list[list[dict[str, Any]]] = []

    def generate(self, system_prompt: str, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> LLMResponse:
        self.messages_seen.append(list(messages))
        return self._responses.pop(0)


def test_mcp_stdio_list_tools_and_permissions():
    with MCPStdioClient() as client:
        specs = client.build_tool_specs()

    assert set(specs.keys()) == {
        "fetch_failed_traces",
        "get_trace_spans",
        "check_neon_health",
        "get_recent_agent_runs",
        "check_llm_providers",
        "save_eval_case",
    }
    assert specs["fetch_failed_traces"].permission == ToolPermission.READ
    assert specs["get_trace_spans"].permission == ToolPermission.READ
    assert specs["check_neon_health"].permission == ToolPermission.READ
    assert specs["get_recent_agent_runs"].permission == ToolPermission.READ
    assert specs["check_llm_providers"].permission == ToolPermission.READ
    assert specs["save_eval_case"].permission == ToolPermission.WRITE
    assert "cases_dir" not in specs["save_eval_case"].parameters.get("properties", {})


def test_harness_runs_real_mcp_stdio_tools_with_hitl_gate(tmp_path: Path):
    mock_llm_denied = _ScriptedLLM(
        [
            LLMResponse(tool_calls=[ToolCallRequest("load_skill", {"name": "triage-eval-capture"})]),
            LLMResponse(
                tool_calls=[
                    ToolCallRequest(
                        "save_eval_case",
                        {
                            "case_id": "mcp-denied-case",
                            "title": "Denied over MCP",
                            "root_cause": "Test",
                        },
                    )
                ]
            ),
            LLMResponse(text="已取消寫入。"),
        ]
    )
    mock_llm_approved = _ScriptedLLM(
        [
            LLMResponse(tool_calls=[ToolCallRequest("load_skill", {"name": "triage-eval-capture"})]),
            LLMResponse(
                tool_calls=[
                    ToolCallRequest(
                        "save_eval_case",
                        {
                            "case_id": "mcp-approved-case",
                            "title": "Approved over MCP",
                            "root_cause": "Verified via real MCP stdio",
                        },
                    )
                ]
            ),
            LLMResponse(text="已透過 MCP 寫入案例。"),
        ]
    )

    with MCPStdioClient(env={**os.environ, "TRIAGE_EVAL_CASES_DIR": str(tmp_path)}) as client:
        mcp_tools = client.build_tool_specs()

        denied_harness = TriageHarness(
            llm_client=mock_llm_denied,
            skill_loader=SkillLoader("skills"),
            tools=mcp_tools,
            confirm_fn=lambda _name, _args: False,
        )
        denied_res = denied_harness.run("存檔")
        assert denied_res.stopped_reason == "completed"
        assert not (tmp_path / "mcp-denied-case.json").exists()
        assert "denied" in mock_llm_denied.messages_seen[-1][-1]["content"].lower()

        approved_harness = TriageHarness(
            llm_client=mock_llm_approved,
            skill_loader=SkillLoader("skills"),
            tools=mcp_tools,
            confirm_fn=lambda _name, _args: True,
        )
        approved_res = approved_harness.run("存檔")
        assert approved_res.stopped_reason == "completed"
        assert (tmp_path / "mcp-approved-case.json").exists()
