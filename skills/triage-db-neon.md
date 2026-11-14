---
name: triage-db-neon
description: 排查 Neon Serverless Postgres 連線異常、SSL 設定、冷啟動逾時與環境變數的標準 SOP。
---

# SOP: Neon Serverless Postgres 連線排查

## 適用情境
- `PickForMe` 出現 `OperationalError`、連線逾時、SSL 憑證錯誤、或懷疑資料庫無法連線。

## 排查步驟
1. **執行資料庫健康檢查**：呼叫 `check_neon_health()` 確認 `DATABASE_URL` 是否設定、能否建立連線並執行 `SELECT 1`。
2. **判讀常見異常**：
   - **`DATABASE_URL` 未設定**：提示檢查 `.env` 是否包含唯讀或目標連線字串。
   - **SSL / `sslmode=require` 錯誤**：Neon Serverless Postgres 強制要求 TLS，確認連線字串包含 `?sslmode=require`。
   - **連線逾時 / 冷啟動 (Cold Start)**：Neon 免費/休眠端點首次喚醒可能需要 1–3 秒，確認連線池與 `connect_timeout` 設定是否過短。
   - **Pool Exhausted**：檢查是否有未關閉的連線或需要透過 Neon `-pooler` 端點連線。
3. **交叉比對 Trace**：若使用者同時提到特定請求失敗，可呼叫 `fetch_failed_traces()` 確認錯誤發生時間點是否與 DB 異常吻合。
4. **輸出診斷報告**：給出具體檢查結果與建議修正項目（不自動修改線上設定）。
