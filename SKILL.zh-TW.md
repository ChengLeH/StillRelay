---
name: shared-task-state
license: MIT
description: Maintain one shared task record, reserve declared resources, and resume verified work across cooperating agents on one host.
---

[English](SKILL.md) | [繁體中文](SKILL.zh-TW.md)

# 共用任務續接

保存一份共同任務進度，讓下一位代理從最後確認步驟接續。完成更新的依據是成功提交或成功回執；工作者結束不等於控制者驗收。

## 先確認共用位置

依 `--config` 或 `SHARED_TASK_STATE_CONFIG` 加入既有狀態。首次才由管理者 `bootstrap`；已有設定不重新建立或改址。執行 `join` 與 `doctor` 驗證狀態身分及存取能力。無法讀取共同位置時，回報缺少的權限，不另建資料庫宣稱已加入。初始化說明見 [README.zh-TW.md](README.zh-TW.md)。

優先使用 `scripts/state.py request --stdin`，將要求直接傳入，不先存共享檔案。純檔案通道預設停用；只有管理者提供寫入前過濾入口才能安全接入。升級前確認既有 inbox 使用者已改直接提交。憑證不得作為識別碼或防撞目標。選配掃描器失敗時停止保存。詳見 README 的「憑證與安全提交」。

## 日常流程

1. 先讀總覽，需要詳細判斷才讀確切任務 ID。確認產生時間、版本與最後確認步驟；不要從近似標題猜身分。
2. 讀目標、限制、已驗證事實、失敗方法、下一步與證據位置。只重查影響下一步的漂移，不重做已完成調查。
3. 建立或規劃任務時宣告 `execution_pool` 與 `resources`。無法精確宣告時使用 `unscoped/write` 保守占用；確實沒有寫入副作用且不占用特定目標時可宣告 `read_only=true`、`resources=[]`。
4. 執行前 `claim`。後續更新使用最新 `expected_revision`、本次 `lease_id` 與相同 `actor`。衝突先重讀；不能強制覆蓋。心跳間隔須小於共用設定的逾時值。
5. 每個重要步驟驗證完成後，更新 `last_confirmed_step`、摘要、證據與 `next_step`。開始外部副作用前記錄動作及查證方式；只保存精簡狀態，不複製完整對話、日誌或憑證。
6. 工作確實結束才 `finish`，填實際 `exit_code`。由另一位控制者檢查來源、差異、測試及實際結果，再 `accept`。

執行中不能增減或改寫目標宣告。需要新增目標時，先安全停止並保留證據，再重新規劃及取得新租約。`local`、`cloud` 預設均不限數量；若管理者另設容量，遵循實際設定。同目標寫入互斥不受執行池影響。

## 中斷與交接

失聯不等於停止。`interrupted` 任務仍可能持有目標占用；檢查 `reservation_active`，不要自動換寫入者或重送未知副作用。

`release` 或 `requeue` 都要提供 `stop_confirmed=true` 與實際 `stop_evidence`，`requeue` 另需 `resume_evidence`。原程序仍存活時會拒絕。原生工具不能確認停止時，由操作者查證並明確確認停止或解除；不要把逾時當停止證據。

交接沿用同一任務 ID，說明最後確認步驟、已確認停止或仍占用的狀態、未知副作用、下一步與證據。舊租約不能更新或釋放新工作者的占用。

格式見 [資料契約](references/protocol.zh-TW.md)。本技能只適用於同一主機、同一使用者信任範圍內合作；`actor` 不是帳號認證，資源宣告不是強制沙箱，各平台權限維持原樣。

## 靜默委派

背景工作與間隔查證可搭配[選用的靜默委派 Skill](skills/silent-delegation/SKILL.md)與[接入指南](references/delegation.zh-TW.md)。詳細日誌留在執行端，監督仍依共同進度與必要證據持續進行。Agent 人數與執行池容量分開設定。
