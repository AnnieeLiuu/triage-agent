# Langfuse & Token Observability Implementation Plan (triage-agent)

**Goal:** Provide end-to-end token and execution observability for `triage-agent` via Langfuse and Trajectory tracking, enabling full traceability of multi-turn ReAct steps, tool invocations, and cross-model fallback transitions (e.g. Gemini 429 quota exhaustion switching to fallback models or Groq).

---

## 1. Architecture & Design

### A. Fail-Open Telemetry Facade (`src/telemetry.py`)
- Provide a `TelemetryManager` wrapping `langfuse.Langfuse` with a robust Null-Object (`NoOpSpan`) pattern.
- Zero network requests and zero latency impact during testing (`ENVIRONMENT == 'test'` or unconfigured `LANGFUSE_*` keys).
- Fail-open resilience: Telemetry failures never affect core triage execution or Telegram replies.
- Support `start_trace`, `end_trace`, `start_span`, `end_span`, and `record_generation` (tracking `model`, `prompt_tokens`, `completion_tokens`, `total_tokens`).

### B. LLM Token & Model Metadata Extraction (`src/harness.py`)
- Extend `LLMResponse`:
  ```python
  @dataclass
  class LLMResponse:
      text: str | None = None
      tool_calls: list[ToolCallRequest] = field(default_factory=list)
      model: str | None = None
      usage: dict[str, int] = field(default_factory=dict)  # {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
  ```
- **Gemini**: In `GeminiClient.generate()`, extract tokens from `resp.usage_metadata` (`prompt_token_count`, `candidates_token_count`, `total_token_count`).
- **Groq Fallback**: In `_call_groq_fallback()`, extract tokens from OpenAI-compatible `data["usage"]` (`prompt_tokens`, `completion_tokens`, `total_tokens`).
- **Fallback Recording**: When `_call_with_rate_limit_retry` or Groq fallback triggers, record a `model_fallback` event with reason (e.g., 429 quota exhaustion).

### C. Step-by-Step Trajectory Token Accounting (`src/harness.py`)
- Extend `TrajectoryRecorder`:
  - Record `"llm_turn"` event per ReAct step containing `step`, `model`, and `usage`.
  - Add `@property total_tokens -> int` summing all token counts across turns.
  - Add `@property model_usage_summary -> dict[str, dict[str, int]]` providing breakdown:
    ```python
    {
        "gemini-3-flash-preview": {"turns": 2, "prompt_tokens": 2000, "completion_tokens": 400, "total_tokens": 2400},
        "gemini-3.1-flash-lite": {"turns": 1, "prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500}
    }
    ```
- In `TriageHarness.run()`:
  - Open a Langfuse Trace (`incident-triage`).
  - Trace each ReAct step as a Span + Generation.
  - Trace tool executions as sub-spans.
  - Close the Trace gracefully upon `final_answer` or circuit breaker.

### D. User Interface & Telegram Display (`src/telegram_server.py`)
- In Telegram reply header/footer, add clean token summary:
  `✅ 排查完成（共 {result.steps_used} 步｜{model_name}｜🪙 {result.trajectory.total_tokens} tokens）`
- Full breakdown is observable via Langfuse web dashboard and detailed server logs.

---

## 2. Tasks & Implementation Steps

### Task 1: Create `src/telemetry.py`
- Implement `NoOpSpan` and `TelemetryManager`.
- Expose singleton `telemetry`.

### Task 2: Update `LLMResponse`, `GeminiClient` and `TrajectoryRecorder` in `src/harness.py`
- Add `model` and `usage` to `LLMResponse`.
- Update `GeminiClient.generate()` to extract `resp.usage_metadata`.
- Update `_call_groq_fallback()` to extract `data["usage"]`.
- Update `TrajectoryRecorder` with token aggregation properties (`total_tokens`, `model_usage_summary`).
- Wrap `TriageHarness.run()` with telemetry tracing and per-step token recording.

### Task 3: Update `src/telegram_server.py`
- Include token metrics in the summary header / status logs.

### Task 4: Offline Tests & Documentation
- Add `tests/test_telemetry.py` to test NoOp, trace lifecycle, and token counting offline (0ms, no network).
- Update `tests/test_harness.py` to verify token accumulation and `model_usage_summary` across mock turns.
- Update `README.md` and `CHANGELOG.md` in `triage-agent`.
- Run `ruff check src tests cli.py` and `pytest -v`.
