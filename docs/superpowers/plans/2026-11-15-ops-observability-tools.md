# Ops Observability & Code-Lookup Tools Implementation Plan

**Goal:** Eliminate Ops Agent blind spots caused by blind 2000-char truncation, lack of step/observation drill-down, lack of query filtering, and lack of visibility into PickForMe's latest source code.

**Architecture:**
1. Replace blind character slicing in `get_recent_agent_runs` and `get_trace_spans` with a schema-agnostic `_compact_io()` helper that preserves all short fields (store names, prices, ratings, URLs) while collapsing only oversized text blobs/nested reviews.
2. Add on-demand drill-down READ tools (`get_agent_step_detail`, `get_observation_detail`) with `offset`/`max_chars` pagination for full un-truncated inspection.
3. Add read-only GitHub tools (`search_pickforme_code`, `read_pickforme_file`) targeting `AnnieeLiuu/pickforme` (`main` branch by default, optional `GITHUB_TOKEN`) so the stateless Cloud Run container can verify live PickForMe logic without bundling stale code.
4. Update `TriageHarness` system prompt and `skills/triage-langfuse-trace.md` with generic anti-hallucination rules (no hardcoded PickForMe stage assumptions).

---

### Task 1: Schema-Agnostic `_compact_io` & Filtered `get_recent_agent_runs` / `get_trace_spans`
**Files:** `src/mcp_server/ops_server.py`
- Add `_compact_io(value: Any, max_str_chars: int = 240, max_list_items: int = 20) -> Any`:
  - If `value` is a JSON-encoded `str` starting with `{` or `[`, parse it first so nested structures are compacted rather than sliced mid-JSON.
  - Recursively compact `dict` values (preserving all keys and short scalar fields intact).
  - For `str` longer than `max_str_chars`, keep prefix + `"... [truncated {N} chars; use detail tool for full text]"`.
  - For `list`: keep up to `max_list_items` items (preserving all store entries for typical 3–10 store lists) and compact each item.
- Update `get_recent_agent_runs(limit: int = 5, run_id: str | None = None, query: str | None = None, chat_id: int | None = None)`:
  - Support optional `query` (`user_query ILIKE %query%`) and `chat_id` filters in SQL.
  - Apply `_compact_io()` to `tool_input` and `tool_output`.
- Update `get_trace_spans(trace_id: str, days: int = 7)`:
  - Apply `_compact_io()` to span `input` and `output`, keeping `id` for drill-down.

### Task 2: On-Demand Drill-Down Tools (`get_agent_step_detail` & `get_observation_detail`)
**Files:** `src/mcp_server/ops_server.py`
- Add `get_agent_step_detail(run_id: str, step_number: int, offset: int = 0, max_chars: int = 8000) -> dict[str, Any]`:
  - Permission: `READ`. Queries Neon `agent_steps` for exact `(run_id, step_number)` and returns paginated full `tool_input` / `tool_output` + `total_chars` / `has_more`.
- Add `get_observation_detail(observation_id: str, offset: int = 0, max_chars: int = 8000) -> dict[str, Any]`:
  - Permission: `READ`. Calls Langfuse `GET /api/public/observations/{observation_id}` and returns paginated full `input` / `output` + `total_chars` / `has_more`.

### Task 3: Read-Only GitHub Code Lookup for `pickforme`
**Files:** `src/mcp_server/ops_server.py`
- Env vars (all optional): `PICKFORME_GITHUB_REPO` (default `"AnnieeLiuu/pickforme"`), `PICKFORME_GITHUB_BRANCH` (default `"main"`), `GITHUB_TOKEN` (optional Bearer token for higher rate limits or private repos).
- Add `search_pickforme_code(query: str, max_results: int = 10) -> list[dict[str, Any]]`:
  - Permission: `READ`. Uses GitHub Code Search API (`GET /search/code?q={query}+repo:{repo}`), with fallback to GitHub Git Trees API filename/path matching if unauthenticated search is rate-limited.
- Add `read_pickforme_file(path: str, start_line: int = 1, end_line: int = 150) -> dict[str, Any]`:
  - Permission: `READ`. Fetches raw file from `raw.githubusercontent.com/{repo}/{branch}/{path}` (or Contents API when `GITHUB_TOKEN` is set), clamps line window to max 200 lines per call, and returns numbered lines + `total_lines`.

### Task 4: Anti-Hallucination Prompt & Generic SOP Update
**Files:** `src/harness.py`, `skills/triage-langfuse-trace.md`
- `src/harness.py` (`build_system_prompt`): Add explicit rule that whenever tool output shows `[truncated` or missing detail, the agent MUST call `get_agent_step_detail` / `get_observation_detail` or GitHub code-lookup tools before concluding, and must never guess hidden/truncated data or unverified code behavior.
- `skills/triage-langfuse-trace.md`: Keep SOP generic (do not hardcode PickForMe stage internals); instruct agent to compare step inputs/outputs, drill down on truncated payloads, and verify actual filtering/ranking logic via `search_pickforme_code` / `read_pickforme_file`.

### Task 5: Offline Tests, Docs & Verification
**Files:** `tests/test_ops_server.py`, `tests/test_trajectory.py`, `README.md`, `CHANGELOG.md`
- Add offline `monkeypatch` unit tests for `_compact_io`, `get_recent_agent_runs(query=..., chat_id=...)`, `get_agent_step_detail`, `get_observation_detail`, `search_pickforme_code`, `read_pickforme_file`, and updated `TOOL_PERMISSIONS`.
- Update `README.md` (tool list + optional GitHub env vars) and `CHANGELOG.md`.
- Run `python -m ruff check src tests cli.py` and `python -m pytest -v`.
