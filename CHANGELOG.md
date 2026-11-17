# Changelog

本檔案記錄 `PickForMe Incident Triage Agent` 的重要功能演進與線上故障修復紀錄。

## [Unreleased]

### Added
- **Langfuse 追蹤與 ReAct 回合級 Token 統計（`src/telemetry.py`、`src/harness.py`、`src/telegram_server.py`、`cli.py`）**：
  - 新增 `src/telemetry.py`（提供零網路請求的 `NoOpSpan` 防護與 `TelemetryManager`），在執行排查時記錄完整 Langfuse Trace、Step Span、Tool 子 Span 與 LLM Generation。
  - `LLMResponse` 支援 `model` 與 `usage`（自動由 Gemini `resp.usage_metadata` 或 Groq `usage` 擷取 Prompt / Completion / Total tokens）。
  - `TrajectoryRecorder` 新增 `total_tokens` 與 `model_usage_summary`（支援跨模型降級時分別統計個別模型的回合數與 Token 消耗）。
  - `telegram_server.py` 與 `cli.py` 在排查完成標題顯示總消耗 Token（如 `｜🪙 1,450 tokens`），並於結構化日誌記錄 `model_usage` 完整拆解。
- **結構化精簡、按需細節查詢與 PickForMe 唯讀查碼工具（`src/mcp_server/ops_server.py`、`src/harness.py`、`skills/triage-langfuse-trace.md`）**：
  - `get_recent_agent_runs` 與 `get_trace_spans` 改用通用 `_compact_io()` 遞迴精簡長字串/長陣列並完整保留短欄位（如所有店名與價格）；`get_recent_agent_runs` 新增 `query` 與 `chat_id` 篩選。
  - 新增 `get_agent_step_detail` 與 `get_observation_detail`（支援 `offset` / `max_chars` 分段讀取完整 `input` / `output`）。
  - 新增唯讀 GitHub 工具 `search_pickforme_code` 與 `read_pickforme_file`（預設讀取 `AnnieeLiuu/pickforme` `main`，支援選填 `GITHUB_TOKEN`）。
  - 在 `AGENTS.md` 新增中大型異動先寫 Plan 之規範，並強化 System Prompt 與 SOP 的防腦補紀律。
- **結構化 `triage_reply` Log（`src/telegram_server.py`）**：確保 Cloud Run 容器啟動時初始化 root `StreamHandler`（`logging.basicConfig(level=logging.INFO)`），每次 Telegram 排查完成後以 `INFO` 記錄 `chat_id`、`query`、`model`、`steps`、`progress` 與 `answer`。
- **精簡工程師導向回覆格式與唯讀模式開關（`src/harness.py`、`src/telegram_server.py`）**：對工程師輸出精簡條列（根因、證據、建議處置，控制在 150–250 字內），並在 Telegram 唯讀模式（`read_only=True`）自動省略「是否存入 Eval」的無效詢問（僅保留於本機 CLI）。
- **Telegram 模型切換即時進度提示（`src/harness.py`、`src/telegram_server.py`）**：切換備用模型（或切至 Groq / 等待 429）時，透過 `on_status` **原地更新同一行**切換狀態（不洗版、也不會在過程中顯示 `❌`），並在完成標題顯示最終使用模型。
- **Groq 跨 Provider 備援（`src/harness.py`）**：當所有 Gemini 模型額度耗盡（429）且已設定 `GROQ_API_KEY` 時，自動切換至 Groq（預設 `llama-3.3-70b-versatile`、`openai/gpt-oss-120b`）。
- **Gemini 備用模型鏈（`src/harness.py`）**：設定 `GEMINI_FALLBACK_MODELS`（預設 `gemini-3.8-flash,gemini-3.1-flash-lite`），遇到每日額度（`PerDay` / `RPD`）或長延遲（`>60s`）時不等待直接切換。
- **Telegram HTML 排版與純文字降級（`src/telegram_server.py`）**：將模型 Markdown 輸出轉為 Telegram HTML（`parse_mode="HTML"`），若送出失敗或超長則自動降級為純文字。
- **開發規範與 Git Hook 防呆（`AGENTS.md`、`.git/hooks/pre-push`）**：強制要求改動 `src/` 或 `cli.py` 必須同步更新 `tests/` 離線測試與 `*.md` 文件。
- **Cloud Run Telegram Webhook 服務（`src/telegram_server.py`、`Dockerfile`、`cloudbuild.yaml`）**：支援 Secret Token、白名單、`await asyncio.to_thread` 非阻塞執行、即時進度編輯與 `/start`、`/clear` 指令。
- **觀測工具擴充（`src/mcp_server/ops_server.py`）**：新增 Neon `get_recent_agent_runs`、Langfuse span io/usage 與 `input_preview`。

### Fixed
- **Telegram Webhook `update_id` 去重與移除 `504` 卡頓備用模型（`src/telegram_server.py`、`src/harness.py`）**：
  - `/webhook` 新增 `_claim_update_id()`（上限 500 筆的執行緒安全 LRU），防止 Cloud Run 回應較久時被 Telegram 重試機制重複觸發多輪回覆。
  - `GEMINI_FALLBACK_MODELS` 預設值由 `gemini-3.8-flash,gemini-3.1-flash-lite` 改為 `gemini-2.5-flash,gemini-3.1-flash-lite`，避免每次 429 後白等 `3.8-flash` 30 秒才 `504`。
- **Gemini 備用模型 `404` / `503` / `504` 跳過與模型清單更新（`src/harness.py`）**：當備用模型回傳 `404 NOT_FOUND`（對新用戶停用）、`503 UNAVAILABLE`（高負載）或 `504 DEADLINE_EXCEEDED`（逾時）時直接跳過至下一個備用模型（預設 `gemini-3.8-flash,gemini-3.1-flash-lite`）或 Groq，不中斷排查。
- **Cloud Run MCP 子行程讀不到環境變數（`src/mcp_client.py`）**：`MCPStdioClient` 預設完整繼承 `os.environ`，修正 `mcp` SDK `env=None` 時過濾 `DATABASE_URL` 與 `LANGFUSE_*` 的問題。
- **Gemini 429 限流與回合數優化（`src/harness.py`）**：短時間 RPM 限流自動依 `retryDelay` 退避重試，每日 RPD 快速切換備用模型，並在 System Prompt 引導單回合平行呼叫唯讀工具。
- **Gemini 3 工具呼叫相容性（`src/harness.py`）**：固定 `HttpOptions(api_version="v1beta")`，保留多輪 `Part.thought_signature`，並將同回合多個 tool response 合併為單一 `user` content。
- **全專案 Code Review 安全與穩定性加固（`src/telegram_server.py`、`src/mcp_server/ops_server.py`、`src/harness.py`）**：
  - `TELEGRAM_WEBHOOK_SECRET` 改為 fail-closed（未設定一律回 403）並使用 `hmac.compare_digest`。
  - `_chat_histories` 加上執行緒鎖並對齊 `user` 回合邊界裁剪，避免截斷產生孤立 `tool` 訊息。
  - `get_recent_agent_runs` 的 `tool_input` / `tool_output` 套用 `_truncate_io(..., max_chars=2000)` 防止爆 token。
  - `GeminiClient` 加上 `_exhausted_models` 追蹤與 `GROQ_FALLBACK_MODELS` 空值防護。
