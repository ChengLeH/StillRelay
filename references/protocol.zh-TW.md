[English](protocol.md) | [繁體中文](protocol.zh-TW.md)

# 資料契約

SQLite（交易式資料庫）保存權威任務與版本歷史。交換目錄的總覽及 `tasks/<task_id>.json` 是唯讀投影，不直接修改。設定、資料庫與交換目錄 `state.json` 的 `state_id` 必須一致；`join` 驗證既有狀態，不建立新資料庫。

## 請求與回執

每次新動作使用新的 `request_id`。同 ID、同內容重送返回既有結果；同 ID 換內容拒絕。`actor` 為自由選擇的協作身分，不提供帳號認證。

```json
{"schema_version":1,"request_id":"example-create-001","actor":"controller","op":"create","execution_pool":"cloud","resources":[{"kind":"unscoped","mode":"write"}],"fields":{"title":"處理授權任務","goal":"確認問題並完成已授權修改","next_step":"工作者讀取現況"}}
```

任務更新以最新 `expected_revision` 防止過期覆寫。`claim` 產生本次 `lease_id` 與執行世代；工作者的 `update`、`heartbeat`、`finish`、`release` 使用同一身分與租約。成功回執中的任務版本是本次提交結果，下一次更新前仍須重讀現況。

| 動作 | 必要內容與行為 |
| --- | --- |
| `create` | `fields.title`、`fields.goal`；執行前宣告執行池及目標 |
| `plan` | `task_id`、`expected_revision`；只在安全可規劃狀態修改內容或宣告 |
| `claim` | `task_id`、`expected_revision`、工作者 `actor`；一次取得全部宣告目標或全部失敗 |
| `update` | 任務 ID、版本、本次租約、`fields`；保存實際驗證結果 |
| `heartbeat` | 任務 ID、版本、本次租約；更新存活訊號，亦可能增加版本 |
| `finish` | 任務 ID、版本、本次租約、整數 `exit_code`；0 表示工作者結束待驗收，非 0 表示失敗／阻礙 |
| `release` | 任務 ID、版本、本次租約、`stop_confirmed=true`、非空 `stop_evidence`；原程序仍存活則拒絕 |
| `requeue` | 任務 ID、版本、`stop_confirmed=true`、非空 `stop_evidence` 及 `resume_evidence`；查證副作用後才重新排隊 |
| `accept` | 不同於工作者的控制者；`checks` 的 `source`、`diff`、`tests`、`runtime` 四鍵均須實際證據文字 |

常用進度欄位：`title`、`goal`、`phase`、`summary`、`last_confirmed_step`、`next_step` 為文字；`constraints`、`failed_attempts`、`artifacts`、`backups`、`blockers` 為文字陣列。新的 `verified_facts` 使用含 id／statement／method／sources 的來源物件。事實、失敗方法、產物參照與備份在 update／finish／plan 累積合併；限制與阻礙仍可整體更新。撤回累積項目需明確留下 retractions。證據記位置，不複製機密內容。檔案檢查、舊事實分類、容量限制與退出碼信任範圍見 [0.3 證據格式與升級說明](evidence.zh-TW.md)。

## 最小領取與更新範例

先讀 `tasks/<task_id>.json`。以下 ID、版本與租約都換成實際值，每次更新前重讀最新版本。

```json
{"schema_version":1,"request_id":"claim-001","actor":"worker-a","op":"claim","task_id":"actual-task-id","expected_revision":1}
```

成功結果會包含本次 `lease_id` 與新版本，再更新進度：

```json
{"schema_version":1,"request_id":"update-001","actor":"worker-a","op":"update","task_id":"actual-task-id","expected_revision":2,"lease_id":"actual-lease-id","fields":{"last_confirmed_step":"已驗證第一部分","next_step":"處理第二部分"}}
```

上述 JSON 可放檔案，使用 README 的 `request --file` 命令提交；僅在明確啟用時，可信任純檔案代理將它寫進設定中 `exchange` 所指的 `inbox/claim-001.json` 或 `inbox/update-001.json`，並讀取同一交換目錄的 `receipts/` 回執。不能把範例中的固定版本與租約直接當成正式值。

## 執行池與資源

`execution_pool` 使用 `local` 或 `cloud`；預設容量均為 `null`，表示不限數量。初始化可用 `--local-capacity 2` 選擇限制本地容量。資源互斥跨執行池生效，不按代理名稱推定後端。

```json
[{"kind":"path","path":"/absolute/project/file.txt","mode":"write"},{"kind":"service","name":"logical-service-id","mode":"write"}]
```

`path` 為絕對檔案或目錄路徑；目錄占用涵蓋其子目標。`service` 使用穩定邏輯識別碼。`read/read` 可共享；含 `write` 的重疊目標排他。`unscoped/write` 是無法精確申報時的保守占用，與任意目標衝突。`read_only=true` 且 `resources=[]` 表示確實沒有寫入副作用、沒有宣告特定目標，不得用它繞過修改目標鎖。

取得目標時重新驗證路徑；執行中不能新增或移除宣告。宣告不是作業系統沙箱，只保障遵循共同入口的合作。

## 中斷與停止證據

心跳過期不自動釋放目標。`interrupted` 仍保留 `reservation_active`；未知副作用以 `outcome_unknown` 記錄。再次接手前確認停止並查證副作用。無法原生判斷時，要求操作者明確確認，不把缺少 PID 或工具無法讀取程序資訊視為已停止。

停止證據應涵蓋本次代理及其啟動的子工作者，說明修改工作已停止的查核方式、結果，以及需人工解除的理由。`resume_evidence` 另說明外部動作是否已生效及安全續接點。成功 `finish` 不等於驗收通過；程式檢查驗收欄位結構，不替代控制者實際檢查。

## 純檔案入口

預設停用；處理前的原始檔案有暴露風險。優先使用 `request --stdin`；限制、遷移與選配設定見 README。

讀總覽，更新前讀 `tasks/<task_id>.json`；寫完整 JSON 至 `inbox/<request_id>.json`，檔名需符合請求 ID。能重新命名時先寫暫存檔，再改為 `.json`。讀 `receipts/<request_id>.json`，只有 `ok=true` 才成功。沒有回執時先查原任務及消費者，不另派同一動作。失敗回執後重新讀取現況，以新 ID 提交修正請求。

有命令列能力的代理直接 `request --file <json>`，不需背景服務。純檔案代理的請求需要 `tick` 或 `watch` 消費者；`doctor` 用於查證狀態一致與相關能力，不能替代實際回執驗收。

## 平台與權限

第一版限同一主機、同一使用者合作。託管自動測試涵蓋 macOS、Linux 與 Windows；實際代理及權限接入仍需另外驗收。保留所在平台的存取權限差異，技能不安裝憑證、不升權、不自動重跑外部副作用。既有舊格式應明確遷移，不能靜默另建不同 `state_id`。
