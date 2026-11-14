---
name: triage-langfuse-trace
description: 排查 PickForMe 最近請求經過、輸出異常、缺資料（如預算/評分）或 Langfuse 失敗 Trace 的標準 SOP。
---

# SOP: PickForMe 請求經過與故障排查

## 適用情境
- 使用者問「剛剛發生了什麼事情」、「為什麼輸出怪怪的 / 被截斷」、「為什麼某間店沒有預算/金額」或「Langfuse 出現失敗 / 低分 Trace」。

## 排查步驟
1. **優先調閱 Neon 完整執行步驟**：呼叫 `get_recent_agent_runs(limit=3)`，從 `agent_runs` 與 `agent_steps` 取得最近請求的 `user_query`、`model_used`、`total_tokens`、`latency_ms`、`status` 與 Stage 1–4 每一步的 `tool_input` / `tool_output` / `error_message`：
   - **Stage 1 (`state_1_intent`)**：檢查意圖、地點、料理類型是否解析正確。
   - **Stage 2 (`state_2_places`)**：檢查 Google Places 搜尋結果數量與座標。
   - **Stage 3 (`state_3_tabelog`)**：檢查 `aligned_stores` 各欄位；若 `price_lunch` / `price_dinner` 為 `null` 且 `sample_reviews` 只有搜尋摘要，通常是 Tabelog 詳情頁被 403 WAF 阻擋後走摘要備援所致。
   - **Stage 4 (`state_4_synthesis`)**：檢查 `card_content` 與 `length`；若輸出異常過短（例如 `< 100` 字或句子斷在一半），檢查 `model_used`（如 `openai/gpt-oss-120b` 容易把 `max_tokens` 耗在 reasoning tokens 導致截斷）。
2. **交叉追查 Langfuse**：呼叫 `fetch_failed_traces(limit=10)`（或帶 `only_errors=True` 查錯誤），再針對相關 `trace_id` 呼叫 `get_trace_spans(trace_id)` 檢視 `input`、`output`、`usage` 與 `status_message`。
3. **補充健康檢查（視需要）**：懷疑 DB 異常載入 `triage-db-neon`；懷疑模型額度或熱切換異常呼叫 `check_llm_providers()`。
4. **輸出具體結論**：明確指出 `run_id` / `trace_id`、使用模型、卡在哪一個 Stage、實際數值與修復建議（不可猜測未觀測到的欄位）。
5. **詢問是否轉存 Eval**：若已確認可重現根因，詢問使用者是否載入 `triage-eval-capture` 轉存案例（雲端環境將依權限閘門禁止寫入）。
