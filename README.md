# PickForMe Incident Triage Agent

獨立、輕量級的 **Incident Triage Agent**，用來排查 `PickForMe` 系統的故障與異常案例（Langfuse 失敗/低分 Trace、Neon Postgres 連線、LLM Provider 狀態），並在人類確認後將根因轉存為可重複驗證的 Eval 資料集（`evals/cases/`）。

## 架構組成

- **`src/harness.py`**：手刻極簡 Agent Harness（ReAct 迴圈、`max_steps` 步數熔斷、`READ` / `WRITE` 權限閘門、Human-in-the-loop 確認、`TrajectoryRecorder`）。
- **`src/mcp_server/ops_server.py`**：標準 **FastMCP Server**（stdio transport），提供 `fetch_failed_traces`、`get_trace_spans`、`check_neon_health`、`check_llm_providers`、`save_eval_case`。
- **`src/skills_loader.py` & `skills/*.md`**：Progressive Skill 載入器與排查 SOP（`triage-langfuse-trace`、`triage-db-neon`、`triage-eval-capture`）。
- **`cli.py`**：終端機互動介面，`WRITE` 工具觸發時會要求 `[y/N]` 確認。
- **`tests/test_trajectory.py`**：離線 Mock 軌跡評測（Skill 路由、工具呼叫順序、HITL 寫入攔截、步數熔斷）。

## 快速開始

1. 設定環境變數（僅需唯讀觀測設定與 `GEMINI_API_KEY`）：
   ```powershell
   copy .env.example .env
   ```
2. 執行離線測試與程式碼檢查：
   ```powershell
   python -m pytest -v
   python -m ruff check src tests cli.py
   ```
3. 啟動 CLI 排查：
   ```powershell
   python cli.py "請排查最近 Langfuse 失敗的 trace"
   ```
4. 作為 FastMCP Server（stdio）掛載：
   ```powershell
   python -m src.mcp_server.ops_server
   ```
