---
name: triage-langfuse-trace
description: 從 Langfuse 的失敗或低分 Trace 追查 PickForMe 4-Stage FSM 各階段根因的標準排查 SOP。
---

# SOP: PickForMe Langfuse Trace 故障排查

## 適用情境
- 使用者回報推薦結果異常、回覆空值、超時、或 Langfuse 出現高錯誤率 / 低評分 Trace。

## 排查步驟
1. **取得異常 Trace 清單**：呼叫 `fetch_failed_traces(limit=10)` 找出最近的失敗或低分 Trace。
2. **下鑽 Span 細節**：針對最相關的 `trace_id`，呼叫 `get_trace_spans(trace_id)` 檢查 `PickForMe` 4-Stage FSM 各階段狀態：
   - **Stage 1 (Intent / Query Parsing)**：是否解析失敗或擷取不到地點/預算條件？
   - **Stage 2 (Retrieval / Google Places / DB)**：是否外部 API 逾時、無結果，或 DB 查詢失敗？若懷疑 DB 問題，加載 `triage-db-neon` skill。
   - **Stage 3 (Ranking / Filtering)**：是否候選清單被過度過濾導致空結果？
   - **Stage 4 (LLM Generation)**：是否出現 Rate Limit (429)、Quota 耗盡或輸出格式錯誤？若懷疑模型額度或可用性，呼叫 `check_llm_providers()`。
3. **彙整根因**：明確指出故障發生在哪一個 Stage、錯誤訊息與可能修復方向。
4. **詢問是否轉存 Eval**：若已確認可重現的根因，詢問使用者是否要載入 `triage-eval-capture` 並轉存為 Eval 案例。
