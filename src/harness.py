"""Hand-crafted Agent Harness: ReAct loop, max_steps circuit breaker, READ/WRITE gate, and TrajectoryRecorder."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from src.skills_loader import SkillLoader

logger = logging.getLogger(__name__)


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
    thought_signature: bytes | None = None


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

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout_ms: int = 30000,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        from google import genai
        from google.genai import types

        resolved_key = api_key or os.getenv("GEMINI_API_KEY", "").strip()
        if not resolved_key:
            raise ValueError("GEMINI_API_KEY is required to run GeminiClient.")
        self._client = genai.Client(
            api_key=resolved_key,
            http_options=types.HttpOptions(timeout=timeout_ms, api_version="v1beta"),
        )
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
        raw_fallbacks = os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.8-flash,gemini-3.1-flash-lite")
        self.fallback_models = [m.strip() for m in raw_fallbacks.split(",") if m.strip() and m.strip() != self.model]
        self._use_groq_fallback = False
        self._exhausted_models: set[str] = set()
        self.on_status = on_status

    def _notify_status(self, message: str) -> None:
        cb = getattr(self, "on_status", None)
        if cb is not None:
            cb(message)

    @staticmethod
    def _short_error_reason(err_str: str) -> str:
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            return "額度已滿 (429)"
        if "404" in err_str or "NOT_FOUND" in err_str:
            return "不可用 (404)"
        if "503" in err_str or "UNAVAILABLE" in err_str:
            return "高負載 (503)"
        if "504" in err_str or "DEADLINE_EXCEEDED" in err_str:
            return "逾時 (504)"
        return "異常"

    def generate(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        if getattr(self, "_use_groq_fallback", False):
            return self._call_groq_fallback(system_prompt, messages, tools)

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
        try:
            resp = self._call_with_rate_limit_retry(contents, config)
        except Exception as exc:
            err_str = str(exc)
            is_fallback_err = any(
                k in err_str
                for k in ("429", "RESOURCE_EXHAUSTED", "404", "NOT_FOUND", "503", "UNAVAILABLE", "504", "DEADLINE_EXCEEDED")
            )
            if is_fallback_err and os.getenv("GROQ_API_KEY", "").strip():
                logger.warning("All Gemini models failed (%s); falling back to Groq.", err_str[:120])
                self._notify_status(
                    f"🔄 Gemini 備援皆{self._short_error_reason(err_str)}，切換至 Groq"
                )
                self._use_groq_fallback = True
                return self._call_groq_fallback(system_prompt, messages, tools)
            raise

        tool_calls: list[ToolCallRequest] = []
        candidates = getattr(resp, "candidates", None) or []
        if candidates and getattr(candidates[0], "content", None):
            for part in getattr(candidates[0].content, "parts", None) or []:
                fc = getattr(part, "function_call", None)
                if fc is not None:
                    tool_calls.append(
                        ToolCallRequest(
                            name=fc.name,
                            arguments=dict(fc.args or {}),
                            thought_signature=getattr(part, "thought_signature", None),
                        )
                    )
        elif resp.function_calls:
            for fc in resp.function_calls:
                tool_calls.append(ToolCallRequest(name=fc.name, arguments=dict(fc.args or {})))

        text = None
        if not tool_calls:
            text = getattr(resp, "text", None) or ""
        return LLMResponse(text=text, tool_calls=tool_calls)

    def _call_with_rate_limit_retry(self, contents: list[Any], config: Any, max_retries: int = 2) -> Any:
        """Retry short-window 429 RPM; switch immediately to fallback models on RPD/404/503/504."""
        exhausted = getattr(self, "_exhausted_models", set())
        raw_candidates = [self.model, *getattr(self, "fallback_models", [])]
        candidates = [m for m in dict.fromkeys(raw_candidates) if m not in exhausted] or [self.model]
        last_exc: Exception | None = None
        for idx, candidate_model in enumerate(candidates):
            has_fallback = idx < len(candidates) - 1
            for attempt in range(max_retries + 1):
                try:
                    resp = self._client.models.generate_content(model=candidate_model, contents=contents, config=config)
                    if candidate_model != self.model:
                        logger.warning("Switched Gemini model from %s to fallback %s.", self.model, candidate_model)
                        self.model = candidate_model
                    return resp
                except Exception as exc:
                    last_exc = exc
                    err_str = str(exc)
                    is_unavailable = any(
                        k in err_str for k in ("404", "NOT_FOUND", "503", "UNAVAILABLE", "504", "DEADLINE_EXCEEDED")
                    )
                    if is_unavailable:
                        logger.warning("Gemini model %s unavailable (%s); trying next fallback.", candidate_model, err_str[:80])
                        exhausted.add(candidate_model)
                        if has_fallback:
                            next_model = candidates[idx + 1]
                            self._notify_status(
                                f"🔄 {candidate_model} {self._short_error_reason(err_str)}，切換至 {next_model}"
                            )
                        break
                    is_rate_limit = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str
                    if not is_rate_limit:
                        raise
                    delay = self._extract_retry_delay(err_str, default=20.0 * (attempt + 1))
                    if delay is None:
                        logger.warning("Gemini daily quota (RPD) hit on %s; switching fallback.", candidate_model)
                        exhausted.add(candidate_model)
                        if has_fallback:
                            next_model = candidates[idx + 1]
                            self._notify_status(f"🔄 {candidate_model} 額度已滿 (429)，切換至 {next_model}")
                        break
                    if attempt >= max_retries:
                        if has_fallback:
                            logger.warning("Retries exhausted on %s; trying fallback.", candidate_model)
                            next_model = candidates[idx + 1]
                            self._notify_status(f"🔄 {candidate_model} 重試上限，切換至 {next_model}")
                        break
                    logger.warning(
                        "Gemini 429 RPM hit on %s (attempt %d/%d); sleeping %.1fs.",
                        candidate_model,
                        attempt + 1,
                        max_retries,
                        delay,
                    )
                    self._notify_status(f"⏳ {candidate_model} 限流 (429)，等待 {delay:.0f}s 重試")
                    time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _build_openai_payload(
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> dict[str, Any]:
        """Convert normalized harness messages/tools to OpenAI-compatible Chat Completions format."""
        oai_messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        pending_call_ids: list[tuple[str, str]] = []
        call_seq = 0

        for msg in messages:
            role = msg["role"]
            if role == "assistant" and msg.get("tool_calls"):
                oai_calls = []
                pending_call_ids = []
                for tc in msg["tool_calls"]:
                    call_seq += 1
                    call_id = f"call_{call_seq}"
                    pending_call_ids.append((tc["name"], call_id))
                    oai_calls.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc.get("arguments", {}), ensure_ascii=False),
                            },
                        }
                    )
                oai_messages.append({"role": "assistant", "content": None, "tool_calls": oai_calls})
            elif role == "tool":
                matched_id = f"call_{call_seq}"
                for idx, (t_name, cid) in enumerate(pending_call_ids):
                    if t_name == msg["name"]:
                        matched_id = cid
                        pending_call_ids.pop(idx)
                        break
                oai_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": matched_id,
                        "name": msg["name"],
                        "content": str(msg.get("content", "")),
                    }
                )
            else:
                oai_messages.append({"role": role, "content": str(msg.get("content", ""))})

        oai_tools = [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
            }
            for t in tools
        ]
        return {"messages": oai_messages, "tools": oai_tools}

    def _call_groq_fallback(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        """Cross-provider fallback to Groq OpenAI-compatible API when Gemini quota is exhausted."""
        import httpx

        api_key = os.getenv("GROQ_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not configured for fallback.")

        raw_models = os.getenv("GROQ_FALLBACK_MODELS", "llama-3.3-70b-versatile,openai/gpt-oss-120b")
        groq_models = [m.strip() for m in raw_models.split(",") if m.strip()]
        if not groq_models:
            raise RuntimeError("GROQ_FALLBACK_MODELS is empty; no Groq fallback models configured.")
        payload_base = self._build_openai_payload(system_prompt, messages, tools)

        last_error: Exception | None = None
        for idx, groq_model in enumerate(groq_models):
            try:
                if idx > 0:
                    self._notify_status(f"🔄 切換 Groq 模型至 {groq_model}")
                resp = httpx.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": groq_model, **payload_base},
                    timeout=30.0,
                )
                resp.raise_for_status()
                self.model = f"groq/{groq_model}"
                data = resp.json()
                msg = data["choices"][0]["message"]
                raw_calls = msg.get("tool_calls") or []
                tool_calls: list[ToolCallRequest] = []
                for tc in raw_calls:
                    fn = tc.get("function") or {}
                    raw_args = fn.get("arguments") or "{}"
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    tool_calls.append(ToolCallRequest(name=fn["name"], arguments=args))
                text = None if tool_calls else (msg.get("content") or "")
                return LLMResponse(text=text, tool_calls=tool_calls)
            except Exception as exc:
                last_error = exc
                logger.warning("Groq fallback model %s failed: %s", groq_model, exc)
        assert last_error is not None
        raise last_error

    @staticmethod
    def _extract_retry_delay(err_str: str, default: float = 20.0) -> float | None:
        if "PerDay" in err_str or "RPD" in err_str:
            return None
        match = re.search(r"retry in (\d+(?:\.\d+)?)s", err_str, re.IGNORECASE)
        if not match:
            match = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", err_str)
        if match:
            raw_seconds = float(match.group(1))
            if raw_seconds > 60.0:
                return None
            return min(max(raw_seconds + 1.5, 5.0), 40.0)
        return min(max(default, 5.0), 40.0)

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
                parts = []
                for tc in msg["tool_calls"]:
                    sig = tc.get("thought_signature")
                    fc = types.FunctionCall(name=tc["name"], args=tc.get("arguments", {}))
                    parts.append(types.Part(function_call=fc, thought_signature=sig) if sig else types.Part(function_call=fc))
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
            "3. `WRITE` 權限工具（如 `save_eval_case`）需要人類確認；若被拒絕，請尊重決定並停止寫入。\n"
            "4. 盡量在同一個回合平行呼叫多個互相獨立的唯讀工具（例如同時載入 SOP 並查詢 traces / DB / LLM 狀態），以壓低 LLM 回合數與延遲。\n"
            "5. 回覆對象是工程師，請保持精簡直接：用短條列給出「根因」、「關鍵證據（run_id / trace_id / 欄位）」與「建議處置」，控制在 150–250 字內，避免客套話、重複敘述或假裝代表產品團隊承諾排程；若值得存成 Eval 案例，最後用一句話簡短詢問即可。\n\n"
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
                    "tool_calls": [
                        {
                            "name": tc.name,
                            "arguments": tc.arguments,
                            "thought_signature": tc.thought_signature,
                        }
                        for tc in response.tool_calls
                    ],
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
