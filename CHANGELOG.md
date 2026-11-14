# Changelog

本檔案記錄 `PickForMe Incident Triage Agent` 的重要功能演進與線上故障修復紀錄。

## [Unreleased]

### Added
- **Groq 跨 Provider 備援（`src/harness.py`）**：當所有 Gemini 模型額度耗盡（429）且已設定 `GROQ_API_KEY` 時，自動切換至 Groq（預設 `llama-3.3-70b-versatile`、`openai/gpt-oss-120b`）。
- **Gemini 備用模型鏈（`src/harness.py`）**：設定 `GEMINI_FALLBACK_MODELS`（預設 `gemini-2.5-flash,gemini-2.5-flash-lite`），遇到每日額度（`PerDay` / `RPD`）或長延遲（`>60s`）時不等待直接切換。
- **Telegram HTML 排版與純文字降級（`src/telegram_server.py`）**：將模型 Markdown 輸出轉為 Telegram HTML（`parse_mode="HTML"`），若送出失敗或超長則自動降級為純文字。
- **開發規範與 Git Hook 防呆（`AGENTS.md`、`.git/hooks/pre-push`）**：強制要求改動 `src/` 或 `cli.py` 必須同步更新 `tests/` 離線測試與 `*.md` 文件。
- **Cloud Run Telegram Webhook 服務（`src/telegram_server.py`、`Dockerfile`、`cloudbuild.yaml`）**：支援 Secret Token、白名單、`await asyncio.to_thread` 非阻塞執行、即時進度編輯與 `/start`、`/clear` 指令。
- **觀測工具擴充（`src/mcp_server/ops_server.py`）**：新增 Neon `get_recent_agent_runs`、Langfuse span io/usage 與 `input_preview`。

### Fixed
- **Gemini 備用模型 `404` / `503` 跳過與模型清單更新（`src/harness.py`）**：當備用模型回傳 `404 NOT_FOUND`（如 `gemini-2.5-flash` / `gemini-2.5-flash-lite` 對新用戶停用）或 `503 UNAVAILABLE`（高負載）時直接跳過至下一個備用模型（預設改為 `gemini-3.8-flash,gemini-3.1-flash-lite`）或 Groq，不中斷排查。
- **Cloud Run MCP 子行程讀不到環境變數（`src/mcp_client.py`）**：`MCPStdioClient` 預設完整繼承 `os.environ`，修正 `mcp` SDK `env=None` 時過濾 `DATABASE_URL` 與 `LANGFUSE_*` 的問題。
- **Gemini 429 限流與回合數優化（`src/harness.py`）**：短時間 RPM 限流自動依 `retryDelay` 退避重試，每日 RPD 快速切換備用模型，並在 System Prompt 引導單回合平行呼叫唯讀工具。
- **Gemini 3 工具呼叫相容性（`src/harness.py`）**：固定 `HttpOptions(api_version="v1beta")`，保留多輪 `Part.thought_signature`，並將同回合多個 tool response 合併為單一 `user` content。
- **CI 與安全加固（`pyproject.toml`、`ops_server.py`、`.github/workflows/ci.yml`）**：鎖定 `mcp>=1.2.0,<2.0.0`、鎖定 `save_eval_case` 寫入路徑、補齊 29 項完全離線測試。
