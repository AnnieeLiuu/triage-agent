---
name: triage-langfuse-trace
description: 排查 PickForMe 最近請求經過、輸出異常、缺資料（如預算/評分）或 Langfuse 失敗 Trace 的標準 SOP。
---

# SOP: PickForMe 請求經過與故障排查

## 適用情境
- 使用者問「剛剛發生了什麼事情」、「為什麼輸出怪怪的 / 被截斷」、「為什麼某間店沒有預算/金額」或「Langfuse 出現失敗 / 低分 Trace」。

## 排查步驟
1. **調閱 Neon 執行步驟（看全貌）**：呼叫 `get_recent_agent_runs`（必要時帶 `query`、`chat_id` 或 `run_id` 過濾），比對各 step 的 `tool_input` 與 `tool_output`（例如前後步驟清單數量或欄位變化）與 `error_message`。
2. **按需補查細節（禁止對截斷資料腦補）**：
   - 若某步出現 `[truncated` 且需要看評論原文或完整輸出，呼叫 `get_agent_step_detail(run_id, step_number)`（可帶 `offset` 分段）。
   - 若要交叉比對 Langfuse，呼叫 `fetch_failed_traces` → `get_trace_spans(trace_id)`；若單一 span 出現 `[truncated`，呼叫 `get_observation_detail(observation_id)`。
3. **驗證最新 PickForMe 程式碼邏輯**：當前後步驟數量不一致或懷疑排序、預算、品類過濾條件時，呼叫 `list_pickforme_files` / `search_pickforme_code` 與 `read_pickforme_file` 直接讀取 `AnnieeLiuu/pickforme` 最新程式實作，以實際程式碼為準。
4. **補充健康檢查（視需要）**：懷疑 DB 異常載入 `triage-db-neon`；懷疑模型額度或熱切換異常呼叫 `check_llm_providers()`。
5. **輸出具體結論**：明確指出 `run_id` / `trace_id`、使用模型、實際數值／程式碼位置與修復建議（未查證的細節不可推測）。
6. **詢問是否轉存 Eval**：若已確認可重現根因，詢問使用者是否載入 `triage-eval-capture` 轉存案例（雲端環境將依權限閘門禁止寫入）。
