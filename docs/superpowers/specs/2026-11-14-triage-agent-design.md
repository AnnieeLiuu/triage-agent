# PickForMe Incident Triage Agent — Design Spec

**Date**: 2026-11-14  
**Target Directory**: `c:\Users\User\OneDrive\文件\2026\triage-agent`

## 1. Intent & Why
建立一個獨立、輕量級的 **Incident Triage Agent**（問題排查代理人），用來主動排查 `PickForMe` 系統的故障與異常案例（例如 Langfuse 上的失敗/低分 Trace、Neon Postgres 連線問題、LLM 額度異常），並將確認根因的失敗案例轉存為可重複驗證的 Eval 資料集。

同時，本專案刻意與 `PickForMe` 本體（確定性 4-Stage FSM）解耦，作為學習與展示 **Agent Harness、MCP (Model Context Protocol)、Progressive Skills 與 Trajectory Eval** 的獨立作品。

## 2. Goals, Non-Goals & Anti-Goals
* **Goals**
  * 實作清晰可讀的手刻 **Agent Harness**（ReAct 迴圈、步數熔斷、READ/WRITE 權限分級、Human-in-the-loop 二次確認、軌跡紀錄）。
  * 實作標準 **FastMCP Server**（stdio transport），對外提供 Langfuse / Neon DB / LLM 狀態查詢與 Eval 寫入工具，並可同時掛載至 IDE Agent 使用。
  * 實作 **Progressive `load_skill`**：依故障類型按需載入 `skills/*.md` 排查 SOP。
  * 實作 **Trajectory Eval**（`pytest` + Mock）：驗證 Agent 是否載入正確 Skill、呼叫正確工具順序，以及寫入前是否確實觸發人類確認閘門。
* **Non-Goals**
  * 不取代 `PickForMe` 的主推薦流程（`PickForMe` 維持既有的確定性 FSM 與 `SkillLoader`）。
  * 第一版不做 Web UI 或 Telegram Bot 介面，專注於終端機 CLI 與 MCP Server。
  * 不做自動修改 `PickForMe` 線上程式碼或自動重啟服務等高風險寫入動作。
* **Anti-Goals**
  * 為了湊齊關鍵字而把重型框架強行疊加進來，導致無法清楚解釋 Harness 底層運作。
  * 在未經人類確認的情況下寫入任何本地檔案或測試集。

## 3. Constraints
* 語言與環境：Python 3.11+、獨立專案目錄與獨立依賴（`google-genai`、`mcp`、`langfuse`、`psycopg`/`httpx`、`pytest`）。
* 連線隔離：僅透過 `.env` 讀取 `PickForMe` 所需的唯讀觀測設定（如 `LANGFUSE_*`、`DATABASE_URL`、`GEMINI_API_KEY`）。
* 離線測試：所有 `pytest` 軌跡評測必須可在無網路、無真實 API Key 下快速執行。

## 4. Approaches Considered
* **方案 A（選用）：手刻極簡 Agent Harness + `FastMCP` + `pytest` 軌跡評測**
  * *Trade-off*：需自行撰寫迴圈、權限閘門與步數熔斷，無現成視覺化執行圖；但程式碼完全透明（約數百行），最適合深入理解 Harness 原理與面試講解。
* **方案 B：使用 `LangGraph` 當底座 + `FastMCP`**
  * *Trade-off*：內建 Checkpointer 與 `interrupt()`，履歷關鍵字強；但初期容易把精力花在對齊框架 API 與狀態型別，偏離手刻學習目標。
* **方案 C：重度 MCP-First + 極簡 CLI 測試腳本**
  * *Trade-off*：MCP Server 最完整；但 Harness 控制層太薄，難以展現迴圈控制、權限隔離與軌跡評測設計。

## 5. Architecture & Components
1. **CLI (`cli.py`)**：終端機互動介面，顯示每步 Skill 載入與工具呼叫，並在 `WRITE` 權限工具觸發時詢問 `[y/N]`。
2. **Agent Harness (`src/harness.py`)**：
   * `max_steps` 步數熔斷。
   * Tool Permission Gate：`READ` 直接放行、`WRITE` 暫停等待人類確認。
   * Local `load_skill(name)` 工具：讀取 `skills/*.md`。
   * `TrajectoryRecorder`：記錄每步事件（Thought / Skill / Tool / Decision）供檢視與 Eval 斷言。
3. **MCP Server (`src/mcp_server/ops_server.py`)**：使用 `FastMCP` 提供：
   * `fetch_failed_traces` / `get_trace_spans`（READ，Langfuse）
   * `check_neon_health`（READ，Neon Postgres）
   * `check_llm_providers`（READ，LLM API 健康檢查）
   * `save_eval_case`（WRITE，儲存失敗案例至 `evals/cases/`）
4. **Initial Triage Skills (`skills/*.md`)**：
   * `triage-langfuse-trace.md`：從異常 Trace 追查 `PickForMe` 各 FSM Stage 根因。
   * `triage-db-neon.md`：排查 Neon Serverless Postgres 連線與環境設定。
   * `triage-eval-capture.md`：整理已確認根因的 Trace 並引導轉存為 Eval 案例。
5. **Trajectory Eval (`tests/test_trajectory.py`)**：離線 Mock 測試，驗證 Skill 路由、工具呼叫序列與 `WRITE` 權限攔截。
