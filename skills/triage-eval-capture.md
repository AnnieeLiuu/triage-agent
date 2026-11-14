---
name: triage-eval-capture
description: 將已確認根因的失敗案例整理成結構化 JSON，並透過人類確認閘門寫入 evals/cases/ 的 SOP。
---

# SOP: 失敗案例轉存為 Eval 資料集

## 適用情境
- 已經透過 Langfuse / Neon / LLM 排查確認故障根因，需要將案例保存至 `evals/cases/` 供日後回歸測試。

## 執行規範
1. **確認必要資訊齊全**：
   - `case_id`：簡短且具描述性的 slug（例如 `fsm-stage2-empty-places-tokyo`）。
   - `title`：一句話描述問題。
   - `root_cause`：已驗證的根因摘要（不可猜測未驗證的細節）。
   - `trace_id`：相關聯的 Langfuse Trace ID（若有）。
   - `reproduction_input`：重現該問題的輸入結構（例如使用者查詢與條件）。
   - `expected_behavior`：修復後應該展現的正確行為。
2. **呼叫寫入工具**：呼叫 `save_eval_case(...)`。
3. **尊重 Human-in-the-loop 閘門**：
   - `save_eval_case` 屬於 `WRITE` 權限工具，Harness 會暫停並要求人類確認 `[y/N]`。
   - 若人類核准，回報寫入的檔案路徑。
   - 若人類拒絕，**不可重試或繞過**，直接告知使用者已取消寫入並結束該步驟。
