[English](protocol.md) | [繁體中文](protocol.zh-TW.md)

# 資料契約

SQLite（交易式資料庫）保存權威任務與版本歷史。交換目錄的總覽及 `tasks/<task_id>.json` 是唯讀投影，不直接修改。設定、資料庫與交換目錄 `state.json` 的 `state_id` 必須一致；`join` 驗證既有狀態，不建立新資料庫。

## 請求與回執

每次新動作使用新的 `request_id`。同 ID、同內容重送返回既有結果；同 ID 換內容拒絕。`actor` 為自由選擇的協作身分，不提供帳號認證。

```json
{"schema_version":1,"request_id":"example-create-001","actor":"controller","op":"create","execution_pool":"cloud","resources":[{"kind":"unscoped","mode":"write"}],"fields":{"title":"處理授權任務","goal":"確認問題並完成已授權修改","next_step":"工作者讀取現況"}}
```

任務更新以最新 `expected_revision` 防止過期覆寫。`claim` 產生本次 `lease_id` 與執行世代；工作者的 `update`、`heartbeat`、`finish`、`release` 使用同一身分與租約。成功回執中的任務版本是本次提交結果，下一次更新前仍須重讀現況。

新任務預設 `review_policy=required`。沒有 policy 欄位的舊任務保留 `legacy` 相容行為；明確執行 `review_start` 會將舊任務升級為必須獨立驗收，之後舊式 `accept` 不能繞過審查。

| 動作 | 必要內容與行為 |
| --- | --- |
| `create` | `fields.title`、`fields.goal`；執行前宣告執行池及目標 |
| `plan` | `task_id`、`expected_revision`；只在安全可規劃狀態修改內容或宣告 |
| `claim` | `task_id`、`expected_revision`、工作者 `actor`；一次取得全部宣告目標或全部失敗 |
| `update` | 任務 ID、版本、本次租約、`fields`；保存實際驗證結果 |
| `heartbeat` | 任務 ID、版本、本次租約；更新存活訊號，亦可能增加版本 |
| `finish` | 任務 ID、版本、本次租約、整數 `exit_code`；0 表示工作者完成、等待審查與驗收，非 0 表示失敗／阻礙 |
| `release` | 任務 ID、版本、本次租約、`stop_confirmed=true`、非空 `stop_evidence`；原程序仍存活則拒絕 |
| `requeue` | 任務 ID、版本、`stop_confirmed=true`、非空 `stop_evidence` 及 `resume_evidence`；查證副作用後才重新排隊 |
| `review_start` | 不同於工作者的審查者；快照本次完成世代、版本與成果指紋 |
| `review_bind` | 本次審查租約及 `execution={executor,run_id}`；執行 ID 須不同於工作者及先前審查執行 |
| `review_checkpoint` | 目前審查租約、已綁定的執行、已確認進度、下一步與有界證據參照 |
| `review_resume` | 目前審查租約、已確認停止的證據及續接證據；輪替租約、保留檢查點並綁定新執行 |
| `review_result` | 目前審查租約與執行；以 `pass`、`changes_requested` 或 `inconclusive` 記錄結果及 source/diff/tests/runtime 證據 |
| `accept` | 新任務須消費目前世代的 `passed` 審查結果；工作者可接受，但審查者不能驗收自己的結果。舊任務在 `review_start` 升級前，仍要求不同於工作者的 actor 與舊式證據欄位 |

StillRelay 只記錄審查者與執行 ID，不會啟動子代理，也不會認證執行或上下文隔離。Host 必須透過原生能力實際啟動全新的子代理審查；actor 與執行 ID 都是自我申報資料，不能證明程序獨立、上下文全新或身分已認證。用戶端無法啟動原生子代理時，任務留在 `worker_done`，不要記錄假審查或驗收。

成果指紋只涵蓋本工作者世代中，透過 `artifact_checks` 在本機核對過位元組的檔案，包含已核對路徑、SHA-256 與大小；核心會在開始／續接審查、提交審查結果及驗收時重新讀檔核對。報告與一般 artifact 參照不在範圍內；沒有已核對檔案時指紋為 `null`。重新核對能發現這些時間點的檔案變動，但不會鎖住檔案，也不能保證外部內容之後不再改變。成果變動時，審查或驗收會被拒絕，直到工作者產生並完成新世代。

凡是讀取或修改本機檔案、執行本機命令、呼叫或控制本機 App、服務或連接器的工作，都使用同一工作的單筆持久任務，不按工具呼叫拆單，即使工作短小或唯讀也一樣。尚未掌握確切任務時，只進行找到共用狀態與任務所需的最少設定／總覽查詢；建立或沿用任務後，立即把查詢與結果記為第一個檢查點，再做其他本機工作。首次設定時，只做讓共用狀態可用所需的本機步驟；狀態可用後，立即建立第一筆任務，並記錄實際執行的安裝／設定動作與結果，包括使用的 `init` 或 `bootstrap` 路徑，再做其他本機工作。必要的 join 或 doctor 檢查也記在該任務下。完全不接觸本機資源的工作，仍維持原門檻：非瑣碎且可能跨回合或需要交接時才建立任務。每個有意義的確認檢查點保存進度與下一步。中斷前保存任務 ID、世代、目前租約、執行身分、停止狀況、未知副作用與下一步。CLI `request --stdin` 與 MCP `shared_task_apply` 共用這套協定。Host 如何實際啟動全新審查見[靜默委派](delegation.zh-TW.md)。

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

停止證據應涵蓋本次代理及其啟動的子工作者，說明修改工作已停止的查核方式、結果，以及需人工解除的理由。`resume_evidence` 另說明外部動作是否已生效及安全續接點。成功 `finish` 不等於驗收通過；保存的審查結果也不取代主要 agent 對證據的判斷。

## 純檔案入口

預設停用；處理前的原始檔案有暴露風險。優先使用 `request --stdin`；限制、遷移與選配設定見 README。

讀總覽，更新前讀 `tasks/<task_id>.json`；寫完整 JSON 至 `inbox/<request_id>.json`，檔名需符合請求 ID。能重新命名時先寫暫存檔，再改為 `.json`。讀 `receipts/<request_id>.json`，只有 `ok=true` 才成功。沒有回執時先查原任務及消費者，不另派同一動作。失敗回執後重新讀取現況，以新 ID 提交修正請求。

有命令列能力的代理直接 `request --file <json>`，不需背景服務。純檔案代理的請求需要 `tick` 或 `watch` 消費者；`doctor` 用於查證狀態一致與相關能力，不能替代實際回執驗收。

## 平台與權限

第一版限同一主機、同一使用者合作。託管自動測試涵蓋 macOS、Linux 與 Windows；實際代理及權限接入仍需另外驗收。保留所在平台的存取權限差異，技能不安裝憑證、不升權、不自動重跑外部副作用。既有舊格式應明確遷移，不能靜默另建不同 `state_id`。
