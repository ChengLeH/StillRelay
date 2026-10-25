---
name: shared-task-state
license: MIT
description: Use to track any task that accesses the user's local machine, including brief or read-only work, in one shared StillRelay task. Also use to resume or supervise shared tasks across agents.
---

[English](SKILL.md) | [繁體中文](SKILL.zh-TW.md)

# 共用任務續接

保存一份共同任務進度，讓下一位代理從最後確認步驟接續。完成更新的依據是成功提交或成功回執；工作者結束不等於控制者驗收。

## 先確認共用位置

依 `--config` 或 `SHARED_TASK_STATE_CONFIG` 加入既有狀態。只有主要代理可建立第一份狀態；已有設定時不得重建或改址。尚未掌握確切任務時，只進行找到既有狀態與任務所需的最少設定／總覽查詢。建立或沿用任務後，立即把查詢與結果記為第一個檢查點，再做其他本機工作；必要的 join 或 doctor 檢查也記在該任務下。首次設定時，只做讓共用狀態可用所需的本機步驟；狀態可用後，立刻建立第一筆任務，並記錄實際執行的安裝／設定動作及結果，包括使用的 `init` 或 `bootstrap` 路徑，再做其他本機工作。無法讀取共同位置時，回報缺少的權限，不另建資料庫宣稱已加入。初始化說明見 [README.zh-TW.md](README.zh-TW.md)。

優先使用 `scripts/state.py request --stdin`，將要求直接傳入，不先存共享檔案。純檔案通道預設停用；只有管理者提供寫入前過濾入口才能安全接入。升級前確認既有 inbox 使用者已改直接提交。憑證不得作為識別碼或防撞目標。選配掃描器失敗時停止保存。詳見 README 的「憑證與安全提交」。

## 日常流程

任何涉及本機資源的工作（讀取或修改本機檔案、執行本機命令、呼叫或控制本機 App、服務或連接器），即使唯讀、短小或單回合，也使用同一工作的單筆任務，不按工具呼叫拆單。尚未掌握確切 `task_id` 時，只進行找到共用狀態與任務所需的最少設定／總覽查詢；建立或沿用任務後，立即把查詢與結果記為第一個檢查點，再做其他本機工作。首次設定時，只做讓共用狀態可用所需的本機步驟；狀態可用後，立刻建立第一筆任務並記錄實際執行的安裝／設定動作與結果，包括使用的 `init` 或 `bootstrap` 路徑，再進行其他本機工作。完全不接觸本機資源的純雲端工作，仍沿用原則：非瑣碎且可能跨回合或需要交接時才建立任務。每個有意義的確認檢查點保存證據與下一步。新任務必須獨立審查。工作結束後，透過 Host 原生能力實際啟動全新子代理，再以 `review_start`、`review_bind` 記錄其執行，並保存檢查點與結果。工作者可以接受通過的審查。用戶端無法啟動原生子代理時，讓結果保持 `worker_done`。StillRelay 只記錄執行 ID，無法證明上下文全新或身分已認證；只有 Host 實際啟動才能建立獨立執行。詳見[審查協定](references/protocol.zh-TW.md)。

1. 先讀總覽，需要詳細判斷才讀確切任務 ID。確認產生時間、版本與最後確認步驟；不要從近似標題猜身分。
2. 讀目標、限制、已驗證事實、失敗方法、下一步與證據位置。只重查影響下一步的漂移，不重做已完成調查。
3. 建立或規劃任務時宣告 `execution_pool` 與 `resources`。無法精確宣告時使用 `unscoped/write` 保守占用；確實沒有寫入副作用且不占用特定目標時可宣告 `read_only=true`、`resources=[]`。
4. 執行前 `claim`。後續更新使用最新 `expected_revision`、本次 `lease_id` 與相同 `actor`。衝突先重讀；不能強制覆蓋。心跳間隔須小於共用設定的逾時值。
5. 每個重要步驟驗證完成後，更新 `last_confirmed_step`、摘要、證據與 `next_step`。開始外部副作用前記錄動作及查證方式；只保存精簡狀態，不複製完整對話、日誌或憑證。
6. 工作確實結束才 `finish`，填實際 `exit_code`。來源、差異、測試與實際結果都由獨立審查記錄後，任務才可接受。

執行中不能增減或改寫目標宣告。需要新增目標時，先安全停止並保留證據，再重新規劃及取得新租約。`local`、`cloud` 預設均不限數量；若管理者另設容量，遵循實際設定。同目標寫入互斥不受執行池影響。

## 中斷與交接

失聯不等於停止。`interrupted` 任務仍可能持有目標占用；檢查 `reservation_active`，不要自動換寫入者或重送未知副作用。

`release` 或 `requeue` 都要提供 `stop_confirmed=true` 與實際 `stop_evidence`，`requeue` 另需 `resume_evidence`。原程序仍存活時會拒絕。原生工具不能確認停止時，由操作者查證並明確確認停止或解除；不要把逾時當停止證據。

交接沿用同一任務 ID，說明最後確認步驟、已確認停止或仍占用的狀態、未知副作用、下一步與證據。舊租約不能更新或釋放新工作者的占用。

審查中斷時保留檢查點，先確認舊審查執行已停止，再呼叫 `review_resume`；它會輪替審查租約，並要求綁定新執行。逾時或自我申報 ID 都不是停止證據。

格式見 [資料契約](references/protocol.zh-TW.md)。本技能只適用於同一主機、同一使用者信任範圍內合作；`actor` 不是帳號認證，資源宣告不是強制沙箱，各平台權限維持原樣。

## 靜默委派

背景工作與間隔查證可搭配[選用的靜默委派 Skill](skills/silent-delegation/SKILL.md)與[接入指南](references/delegation.zh-TW.md)。詳細日誌留在執行端，監督仍依共同進度與必要證據持續進行。Agent 人數與執行池容量分開設定。
