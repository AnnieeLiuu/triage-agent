# PickForMe Incident Triage Agent

獨立、輕量級的 **Incident Triage Agent**，用來排查 `PickForMe` 系統的故障與異常案例（Langfuse 失敗/低分 Trace、Neon Postgres 連線與 `agent_runs`、LLM Provider 狀態），並在人類確認後將根因轉存為可重複驗證的 Eval 資料集（`evals/cases/`）。

## 架構組成

- **`src/harness.py`**：手刻極簡 Agent Harness（ReAct 迴圈、`max_steps` 步數熔斷、`READ` / `WRITE` 權限閘門、Human-in-the-loop 確認、`TrajectoryRecorder`、`GeminiClient` `v1beta` + `thought_signature` 保留 + 429 RPM 重試與 RPD 快速失敗、平行唯讀工具呼叫引導）。
- **`src/mcp_server/ops_server.py`**：標準 **FastMCP Server**（stdio transport），提供 `fetch_failed_traces`、`get_trace_spans`、`check_neon_health`、`get_recent_agent_runs`、`check_llm_providers`、`save_eval_case`。
- **`src/mcp_client.py`**：`MCPStdioClient`，負責以 stdio 啟動 `ops_server` 並**完整繼承父行程 `os.environ`**（確保 Cloud Run 無 `.env` 時仍可讀取連線變數）。
- **`src/telegram_server.py`**：GCP Cloud Run FastAPI Webhook 服務（`/health`、`/webhook`），支援 Secret Token 與白名單驗證、`/start` 與 `/clear` 多輪對話管理、即時進度編輯、Markdown 轉 Telegram HTML（含純文字降級保護），且強制 `WRITE` 工具 fail-closed。
- **`src/skills_loader.py` & `skills/*.md`**：Progressive Skill 載入器與排查 SOP（`triage-langfuse-trace`、`triage-db-neon`、`triage-eval-capture`）。
- **`cli.py`**：終端機互動介面，`WRITE` 工具觸發時會要求 `[y/N]` 確認。
- **`tests/`**：完全離線測試套件（`test_mcp_client.py`、`test_ops_server.py`、`test_skills_loader.py`、`test_telegram_server.py`、`test_trajectory.py`），涵蓋軌跡評測、權限閘門、429 處理、MCP 環境變數繼承與 Telegram Webhook 邊界測試。

## 快速開始

1. 設定環境變數（僅需唯讀觀測設定與 `GEMINI_API_KEY`）：
   ```powershell
   copy .env.example .env
   ```
2. 執行離線測試與程式碼檢查（見 [`AGENTS.md`](./AGENTS.md) 開發規範）：
   ```powershell
   python -m ruff check src tests cli.py
   python -m pytest -v
   ```
3. 啟動 CLI 排查：
   ```powershell
   python cli.py "請排查最近 Langfuse 失敗的 trace"
   ```
4. 作為 FastMCP Server（stdio）掛載：
   ```powershell
   python -m src.mcp_server.ops_server
   ```

## CI 與部署

- **Git Hook（`.git/hooks/pre-push`）**：推送前自動執行 `ruff`、`pytest`，並檢查當 `src/` 或 `cli.py` 有異動時是否已同步更新 `tests/` 與 `*.md` 文件。
- **GitHub Actions CI（`.github/workflows/ci.yml`）**：每次 push / PR 至 `main` 自動執行 `ruff` 與離線 `pytest -v`。
- **Cloud Run 自動部署**：推送至 `main` 後由 GCP Cloud Build Trigger 自動建置 `Dockerfile` 並部署至 `triage-agent-api`（`asia-northeast1`）。
