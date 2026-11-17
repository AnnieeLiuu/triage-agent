"""Offline tests for TelemetryManager, NoOpSpan, token accounting, and multi-model fallbacks."""

from __future__ import annotations

from typing import Any

import pytest
from src.harness import LLMResponse, ToolCallRequest, ToolPermission, ToolSpec, TrajectoryRecorder, TriageHarness
from src.skills_loader import SkillLoader
from src.telemetry import NoOpSpan, TelemetryManager


def test_noop_span_lifecycle():
    span = NoOpSpan("test-noop")
    assert span.end() is span
    assert span.update(output="abc") is span
    assert span.span(name="child") is span
    assert span.generation(name="gen", model="gemini-3-flash") is span
    assert span.score(value=1.0) is span


def test_telemetry_manager_disabled_in_test_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")

    tm = TelemetryManager()
    assert tm.is_enabled() is False

    trace = tm.start_trace("incident-triage", input_data={"q": "test"})
    assert isinstance(trace, NoOpSpan)

    span = tm.start_span(trace, "step-1")
    assert isinstance(span, NoOpSpan)

    gen = tm.record_generation(span, "gen", model="gemini-3-flash", usage={"total_tokens": 100})
    assert isinstance(gen, NoOpSpan)

    tm.end_span(span)
    tm.end_trace(trace)
    tm.flush()


def test_trajectory_recorder_token_breakdown_and_fallback():
    rec = TrajectoryRecorder()

    # Step 1: gemini-3-flash
    rec.record(
        1,
        "llm_turn",
        {"model": "gemini-3-flash", "prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
    )
    # Step 2: gemini-3-flash
    rec.record(
        2,
        "llm_turn",
        {"model": "gemini-3-flash", "prompt_tokens": 1500, "completion_tokens": 300, "total_tokens": 1800},
    )
    # Fallback event: 429
    rec.record(
        3,
        "model_fallback",
        {"from_model": "gemini-3-flash", "to_model": "gemini-3.1-flash-lite", "reason": "429 RESOURCE_EXHAUSTED"},
    )
    # Step 3: gemini-3.1-flash-lite
    rec.record(
        3,
        "llm_turn",
        {"model": "gemini-3.1-flash-lite", "prompt_tokens": 1800, "completion_tokens": 400, "total_tokens": 2200},
    )

    assert rec.total_tokens == 1200 + 1800 + 2200
    summary = rec.model_usage_summary
    assert len(summary) == 2

    assert summary["gemini-3-flash"]["turns"] == 2
    assert summary["gemini-3-flash"]["prompt_tokens"] == 2500
    assert summary["gemini-3-flash"]["completion_tokens"] == 500
    assert summary["gemini-3-flash"]["total_tokens"] == 3000

    assert summary["gemini-3.1-flash-lite"]["turns"] == 1
    assert summary["gemini-3.1-flash-lite"]["prompt_tokens"] == 1800
    assert summary["gemini-3.1-flash-lite"]["completion_tokens"] == 400
    assert summary["gemini-3.1-flash-lite"]["total_tokens"] == 2200


class ScriptedTokenMockLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)

    def generate(self, system_prompt: str, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> LLMResponse:
        if not self._responses:
            return LLMResponse(text="done", model="mock", usage={"total_tokens": 10})
        return self._responses.pop(0)


def test_triage_harness_records_tokens_and_telemetry(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "index.json").write_text("{}", encoding="utf-8")

    responses = [
        LLMResponse(
            tool_calls=[ToolCallRequest(name="check_health", arguments={})],
            model="gemini-3-flash",
            usage={"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600},
        ),
        LLMResponse(
            text="Diagnosis complete: all services healthy.",
            model="gemini-3.1-flash-lite",
            usage={"prompt_tokens": 700, "completion_tokens": 150, "total_tokens": 850},
        ),
    ]

    tools = {
        "check_health": ToolSpec(
            name="check_health",
            description="mock health tool",
            permission=ToolPermission.READ,
            fn=lambda: {"status": "ok"},
        )
    }

    harness = TriageHarness(
        llm_client=ScriptedTokenMockLLM(responses),
        skill_loader=SkillLoader(str(skills_dir)),
        tools=tools,
        max_steps=5,
    )

    result = harness.run("檢查系統狀態", chat_id=123456)
    assert result.stopped_reason == "completed"
    assert result.steps_used == 2
    assert result.trajectory.total_tokens == 600 + 850

    summary = result.trajectory.model_usage_summary
    assert "gemini-3-flash" in summary
    assert summary["gemini-3-flash"]["total_tokens"] == 600
    assert "gemini-3.1-flash-lite" in summary
    assert summary["gemini-3.1-flash-lite"]["total_tokens"] == 850
