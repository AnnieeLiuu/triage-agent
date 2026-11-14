"""Hand-crafted Agent Harness: ReAct loop, max_steps circuit breaker, READ/WRITE gate, and TrajectoryRecorder."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from src.skills_loader import SkillLoader


class ToolPermission(str, Enum):
    READ = "READ"
    WRITE = "WRITE"


@dataclass(frozen=True)
class ToolSpec:
    """Definition of a tool registered in the Agent Harness."""

    name: str
    description: str
    permission: ToolPermission
    fn: Callable[..., Any]
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass(frozen=True)
class ToolCallRequest:
    """A single tool invocation requested by the LLM."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    """Normalized response from any LLM backend (real Gemini or offline Mock)."""

    text: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)


class LLMClient(Protocol):
    """Minimal protocol for an LLM backend usable by TriageHarness."""

    def generate(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> LLMResponse: ...


@dataclass(frozen=True)
class TrajectoryEvent:
    """Single observable event recorded during a TriageHarness run."""

    step: int
    event_type: str  # "skill_loaded" | "tool_call" | "permission_decision" | "tool_result" | "final_answer" | "circuit_breaker"
    detail: dict[str, Any]


class TrajectoryRecorder:
    """Records agent execution events for CLI display and offline Trajectory Eval."""

    def __init__(self, on_event: Callable[[TrajectoryEvent], None] | None = None) -> None:
        self.events: list[TrajectoryEvent] = []
        self._on_event = on_event

    def record(self, step: int, event_type: str, detail: dict[str, Any]) -> TrajectoryEvent:
        event = TrajectoryEvent(step=step, event_type=event_type, detail=detail)
        self.events.append(event)
        if self._on_event is not None:
            self._on_event(event)
        return event

    @property
    def skills_loaded(self) -> list[str]:
        return [e.detail["skill"] for e in self.events if e.event_type == "skill_loaded"]

    @property
    def tools_called(self) -> list[str]:
        return [e.detail["tool"] for e in self.events if e.event_type == "tool_call"]

    @property
    def permission_decisions(self) -> list[dict[str, Any]]:
        return [e.detail for e in self.events if e.event_type == "permission_decision"]


@dataclass
class TriageResult:
    """Final output and full execution trajectory from TriageHarness."""

    answer: str
    stopped_reason: str  # "completed" | "max_steps"
    steps_used: int
    trajectory: TrajectoryRecorder
    messages: list[dict[str, Any]] = field(default_factory=list)


class GeminiClient:
    """Google GenAI adapter implementing LLMClient."""

    def __init__(self, api_key: str | None = None, model: str | None = None, timeout_ms: int = 30000) -> None:
        from google import genai
        from google.genai import types

        resolved_key = api_key or os.getenv("GEMINI_API_KEY", "").strip()
        if not resolved_key:
            raise ValueError("GEMINI_API_KEY is required to run GeminiClient.")
        self._client = genai.Client(api_key=resolved_key, http_options=types.HttpOptions(timeout=timeout_ms))
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")

    def generate(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        from google.genai import types

        declarations = [
            types.FunctionDeclaration(
                name=t.name,
                description=t.description,
                parameters_json_schema=t.parameters,
            )
            for t in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[types.Tool(function_declarations=declarations)],
        )
        contents = self._build_contents(messages)
        resp = self._client.models.generate_content(model=self.model, contents=contents, config=config)

        tool_calls: list[ToolCallRequest] = []
        if resp.function_calls:
            for fc in resp.function_calls:
                tool_calls.append(ToolCallRequest(name=fc.name, arguments=dict(fc.args or {})))

        text = None
        if not tool_calls:
            text = getattr(resp, "text", None) or ""
        return LLMResponse(text=text, tool_calls=tool_calls)

    @staticmethod
    def _build_contents(messages: list[dict[str, Any]]) -> list[Any]:
        """Convert normalized messages to Gemini Content, merging consecutive tool responses."""
        from google.genai import types

        contents: list[Any] = []
        idx = 0
        while idx < len(messages):
            msg = messages[idx]
            role = msg["role"]

            if role == "tool":
                tool_parts = []
                while idx < len(messages) and messages[idx]["role"] == "tool":
                    t_msg = messages[idx]
                    tool_parts.append(
                        types.Part.from_function_response(
                            name=t_msg["name"],
                            response={"result": t_msg["content"]},
                        )
                    )
                    idx += 1
                contents.append(types.Content(role="user", parts=tool_parts))
                continue

            if role == "assistant" and msg.get("tool_calls"):
                parts = [
                    types.Part.from_function_call(name=tc["name"], args=tc.get("arguments", {}))
                    for tc in msg["tool_calls"]
                ]
                contents.append(types.Content(role="model", parts=parts))
            else:
                contents.append(
                    types.Content(
                        role="model" if role == "assistant" else "user",
                        parts=[types.Part.from_text(text=str(msg.get("content", "")))],
                    )
                )
            idx += 1
        return contents


class TriageHarness:
    """Minimal ReAct Agent Harness with Progressive Skills, Permission Gate, and Circuit Breaker."""

    def __init__(
        self,
        llm_client: LLMClient,
        skill_loader: SkillLoader | None = None,
        tools: dict[str, ToolSpec] | None = None,
        max_steps: int = 8,
        confirm_fn: Callable[[str, dict[str, Any]], bool] | None = None,
        on_event: Callable[[TrajectoryEvent], None] | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.skill_loader = skill_loader or SkillLoader()
        self.max_steps = max_steps
        # Fail-closed by default: WRITE tools are denied unless a confirm_fn approves.
        self.confirm_fn = confirm_fn or (lambda _name, _args: False)
        self.on_event = on_event

        self.tools: dict[str, ToolSpec] = dict(tools or {})
        self.tools["load_skill"] = ToolSpec(
            name="load_skill",
            description="按需載入指定名稱的 troubleshooting skill SOP。",
            permission=ToolPermission.READ,
            fn=self.skill_loader.load_skill,
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Skill 名稱"}},
                "required": ["name"],
            },
        )

    def build_system_prompt(self) -> str:
        """Build system prompt exposing only the skill index (Progressive Disclosure)."""
        skill_index = self.skill_loader.format_index_for_prompt()
        return (
            "你是 PickForMe Incident Triage Agent，負責排查系統故障與彙整可重現的 Eval 案例。\n"
            "工作規範：\n"
            "1. 開始排查前，先根據問題類型呼叫 `load_skill(name)` 載入對應的 SOP。\n"
            "2. 僅能依據工具實際回傳的觀測結果下結論，不可猜測未驗證的資訊。\n"
            "3. `WRITE` 權限工具（如 `save_eval_case`）需要人類確認；若被拒絕，請尊重決定並停止寫入。\n\n"
            f"可用 Skills 清單：\n{skill_index}"
        )

    def run(self, user_query: str, history: list[dict[str, Any]] | None = None) -> TriageResult:
        """Execute the ReAct loop until completion or max_steps circuit breaker."""
        recorder = TrajectoryRecorder(on_event=self.on_event)
        system_prompt = self.build_system_prompt()
        messages: list[dict[str, Any]] = list(history) if history is not None else []
        messages.append({"role": "user", "content": user_query})
        tool_list = list(self.tools.values())

        for step in range(1, self.max_steps + 1):
            response = self.llm_client.generate(system_prompt, messages, tool_list)

            if not response.tool_calls:
                answer = response.text or ""
                messages.append({"role": "assistant", "content": answer})
                if history is not None:
                    history[:] = messages
                recorder.record(step, "final_answer", {"answer": answer})
                return TriageResult(
                    answer=answer,
                    stopped_reason="completed",
                    steps_used=step,
                    trajectory=recorder,
                    messages=messages,
                )

            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [{"name": tc.name, "arguments": tc.arguments} for tc in response.tool_calls],
                }
            )

            for tc in response.tool_calls:
                observation = self._execute_tool_call(step, tc, recorder)
                messages.append(
                    {
                        "role": "tool",
                        "name": tc.name,
                        "content": observation if isinstance(observation, str) else json.dumps(observation, ensure_ascii=False),
                    }
                )

        recorder.record(self.max_steps, "circuit_breaker", {"reason": "max_steps_reached"})
        answer = f"已達最大步數上限 ({self.max_steps})，觸發步數熔斷並中止排查。"
        messages.append({"role": "assistant", "content": answer})
        if history is not None:
            history[:] = messages
        return TriageResult(
            answer=answer,
            stopped_reason="max_steps",
            steps_used=self.max_steps,
            trajectory=recorder,
            messages=messages,
        )

    def _execute_tool_call(
        self,
        step: int,
        tc: ToolCallRequest,
        recorder: TrajectoryRecorder,
    ) -> Any:
        spec = self.tools.get(tc.name)
        if spec is None:
            recorder.record(step, "tool_call", {"tool": tc.name, "arguments": tc.arguments, "permission": "UNKNOWN"})
            result = {"error": f"Unknown tool '{tc.name}'"}
            recorder.record(step, "tool_result", {"tool": tc.name, "result": result})
            return result

        recorder.record(
            step,
            "tool_call",
            {"tool": tc.name, "arguments": tc.arguments, "permission": spec.permission.value},
        )

        if spec.permission == ToolPermission.WRITE:
            approved = bool(self.confirm_fn(tc.name, tc.arguments))
            recorder.record(
                step,
                "permission_decision",
                {"tool": tc.name, "approved": approved, "arguments": tc.arguments},
            )
            if not approved:
                denied_result = {"denied": True, "reason": "Human operator denied WRITE tool execution."}
                recorder.record(step, "tool_result", {"tool": tc.name, "result": denied_result})
                return denied_result

        try:
            output = spec.fn(**tc.arguments)
            if tc.name == "load_skill":
                recorder.record(step, "skill_loaded", {"skill": tc.arguments.get("name", "")})
            recorder.record(step, "tool_result", {"tool": tc.name, "result": output})
            return output
        except Exception as exc:
            error_result = {"error": str(exc)}
            recorder.record(step, "tool_result", {"tool": tc.name, "result": error_result})
            return error_result
