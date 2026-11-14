# AGENTS.md — PickForMe Incident Triage Agent

本文件定義此專案的開發規範、架構約束與強制驗收門檻，供所有 AI Agent 與開發者遵循。

## 1. 核心強制規範（Bugfix / Feature / Docs）

1. **任何錯誤修正或新功能，必須補進離線測試（CI）**
   - 凡修改 `src/` 或 `cli.py`，必須在 `tests/` 補上對應的**完全離線單元／整合測試**（使用 mock / `TestClient` / `monkeypatch` / `ScriptedLLM`）。
   - **禁止**在 `tests/` 或 CI 中發送真實 Telegram 訊息、呼叫真實 Gemini API、或依賴外部網路／線下 `.env` 金鑰。
2. **程式碼異動必須同步更新相關文件（`*.md`）**
   - 修改架構、工具介面、環境變數、排錯流程或部署行為時，必須同步更新 `README.md`、`AGENTS.md` 或 `skills/*.md`。
3. **Git Hook（`.git/hooks/pre-push`）強制攔截**
   - 推送前會自動執行：
     1. `python -m ruff check src tests cli.py`
     2. `python -m pytest -q`
     3. **變更防呆檢查**：若 `origin/main..HEAD` 改動了 `src/` 或 `cli.py`，但未同步更新 `tests/` 下的測試檔案 **或** 未更新任何 `*.md` 文件，hook 會直接阻擋 `git push`（僅在純緊急狀況且經人類同意時才可使用 `--no-verify`）。

## 2. 關鍵架構與實作約束

- **MCP Stdio 環境變數繼承（`src/mcp_client.py`）**：
  - `mcp` Python SDK 的 `StdioServerParameters` 在 `env=None` 時會過濾掉敏感環境變數。`MCPStdioClient` 必須預設繼承 `dict(os.environ)`，否則在無 `.env` 的 Cloud Run 容器內 `ops_server` 會讀不到 `DATABASE_URL` 與 `LANGFUSE_*`。
- **Gemini 3 相容性與 429 限流處理（`src/harness.py`）**：
  - 需使用 `HttpOptions(api_version="v1beta")`，並在多輪 tool calling 中完整保留 `Part.thought_signature`，且將同回合多個 tool response 合併為單一 `user` content。
  - `GeminiClient._call_with_rate_limit_retry` 僅針對短時間 429 RPM（`retryDelay <= 60s`）自動退避重試；若遇到 `PerDay` / `RPD` 或 `retryDelay > 60s` 必須**快速失敗**，避免無意義等待。
  - System Prompt 引導模型在單回合平行呼叫多個唯讀工具，將排查回合壓低以減少撞限流機率。
- **Telegram Webhook 安全與排版（`src/telegram_server.py`）**：
  - Cloud Run 端為無狀態唯讀模式：`confirm_fn` 固定為 `False`（fail-closed），不可允許寫入 `evals/cases/`。
  - 需驗證 `X-Telegram-Bot-Api-Secret-Token` 與 `TELEGRAM_ALLOWED_USER_ID`。
  - 模型輸出透過 `_markdown_to_telegram_html()` 轉為 Telegram HTML（`parse_mode="HTML"`），若送出失敗或超長則自動降級為純文字送出，確保不漏訊。
  - `_run_triage_and_reply` 透過 `await asyncio.to_thread(...)` 執行，避免阻塞 FastAPI Event Loop（同時確保 Cloud Run CPU 在請求期間不被節流）。
- **資安**：嚴禁讀取、印出或提交 `.env` 內的任何真實金鑰。

## 3. 常用指令與自動部署

```powershell
# 程式碼檢查與全套離線測試（必須全數通過）
python -m ruff check src tests cli.py
python -m pytest -v
```

- **CI（`.github/workflows/ci.yml`）**：每次 push / PR 到 `main` 自動執行 `ruff` 與 `pytest -v`。
- **自動部署**：push 到 `main` 後，GCP Cloud Build Trigger（`rmgpgab-triage-agent-api-asia-northeast1-AnnieeLiuu-triage-ajai`）會自動建置並部署至 Cloud Run `triage-agent-api`（`asia-northeast1`）。
