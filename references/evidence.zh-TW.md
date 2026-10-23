# 看得出查過什麼的證據

StillRelay 0.3 分開記錄參照位置、程式實際讀取的檔案，以及附來源的主張。成功結束仍是 `worker_done`，另一位控制者查核後才是驗收。

## 檔案參照與實際檢查

`fields.artifacts` 保留字串參照格式，本身不代表讀取授權。只放在這個陣列中的不存在路徑或遠端參照，**不會被檢查，也不會阻止 finish**。請一起讀 `unverified_artifacts`；有對應 `artifact_evidence` 才代表留下本機檢查紀錄。伺服器寫出的報告代表已保存工作者的陳述，不代表報告中的內容都是真的。

需要強制檢查本機檔案時，管理者先在既有設定加入選用的 `artifact_roots`，不能由任務要求自行放寬：

```json
{"artifact_roots": ["/absolute/project/output"]}
```

不設定就不開放這項讀檔功能；不需要換資料庫或設定版本。請選狹窄的產出目錄，不要開整個家目錄或憑證目錄。這不增加作業系統權限，也不是隔離其他同使用者程序的沙箱。檢查不會新增 HTTP 端點、託管服務或遠端讀檔路徑；MCP 伺服器仍使用本機 stdio。

透過 MCP 依賴這項保證前，先呼叫 `shared_task_status`，確認 `evidence_contract_version=1` 且 `artifact_checks_enabled=true`。沒有這些能力資訊的舊版或私人轉接器，不能假定會執行驗證。混用版本前先更新會寫入事實的用戶端；本次不自動部署到正式 Host。

在 `finish` 的最外層加入：

```json
{"artifact_checks": [{"path": "/absolute/project/output/result.txt", "sha256": "<64 lowercase hex characters>"}]}
```

核心只開啟允許目錄內的一般檔案，拒絕符號連結路徑；每次最多 100 項、合計最多讀取 64 MiB。程式重算 SHA-256 並比對宣稱的摘要。不存在、無法讀取、檢查中改變、超出範圍或摘要不符，都拒絕整筆 finish，保留原租約。其他操作不能帶著這個欄位卻悄悄略過驗證。

請使用解析後的實際路徑，例如 Python 的 `str(Path(path).resolve())`。macOS 的 `/tmp` 本身也是指向 `/private/tmp` 的符號連結，因此直接傳 `/tmp/...` 也會被拒絕。

成功後，伺服器在不可由用戶端直接修改的 `artifact_evidence` 記錄位置、摘要、大小、時間、actor、執行世代與選用的 run ID，並加入產物參照。同一筆已提交要求的原樣重送會回傳原回執，不重新讀檔；新要求則重新檢查。檔案屬性比對可偵測一般並行變動，不保證抵抗惡意檔案系統替換。檢查後檔案仍可改變；日後必須重現的成果需另存不可變副本。

## 累積紀錄與更正

`verified_facts`、`failed_attempts`、`artifacts`、`backups` 在 create／update／finish／plan 共用累積合併，送 `[]` 不會清空。字串按內容去重，事實按 ID 去重；同 ID 改內容會被拒絕。`blockers` 與 `constraints` 仍可整體更新，反映當前情況。合併後的可編輯欄位與證據合計不得超過 32 KiB，每個清單最多 100 項；不會靜默截斷舊紀錄。

更正時在 update／finish／plan 最外層加撤回紀錄：

```json
{"retractions": [{"field": "failed_attempts", "value": "old incorrect record", "reason": "This referred to a different task"}]}
```

字串欄位的 `value` 填原內容；`verified_facts` 填事實 ID。伺服器確認目標存在，再記錄誰在何時、因何原因撤回。原文保留供稽核；接手者必須把已撤回項目視為失效，即使原項目仍在清單或檔案驗證紀錄中。更正事實請使用新 ID，重送舊 ID 不會取消撤回。

## 附來源的事實

新版不接受直接把自由字串寫進 `verified_facts`，改用：

```json
{"verified_facts": [{"id": "check-1", "statement": "The fixture returned the expected output", "method": "Compare the recorded output with the fixture's expected value", "sources": [{"kind": "event", "seq": 12}]}]}
```

事件來源必須已存在於**同一個任務**。檔案來源使用 `{"kind":"artifact","path":"/absolute/project/output/result.txt","sha256":"<64 lowercase hex characters>"}`，必須對上程式實際留下的檔案檢查紀錄；也能引用同一筆 finish 剛完成的檢查。引用過去的紀錄不會重新驗證現在的檔案，請注意原檢查時間與執行世代。

伺服器加入 `status=reported_with_source`、`reported_by`、`reported_at`、`generation`、`run_id`。新事實不能自行填這些欄位。程式確認來源存在且關聯有效，**不會自動證明主張為真，也不會替工作者執行宣稱的查核方式**。引用自己的前一筆事件，只證明那件事曾被記下。控制者仍需讀證據再驗收。

## 舊資料怎麼辦？

0.3 改變的是**事實寫入格式**：原本送字串的用戶端必須更新。未查核的觀察可放 `summary`，或改送附來源物件。套件名稱、命令、MCP 工具名稱及用戶端連線設定不變，但名稱相同不代表舊事實格式仍可使用。CLI 與 MCP 走同一套規則。詳見 [v0.3.0 發布說明與固定版本升級步驟](../docs/releases/0.3.0.zh-TW.md)。

舊字串讀出時會包成 `legacy_unverified` 物件，產生穩定 ID，來源與方法留空；讀取不改資料庫。後續任務更新保留這個分類；歷史讀取也會標示，但不重寫事件原文。舊的已提交要求仍重播原回執，請讀當前任務／歷史取得標示後的內容。已讀到的事實物件可原樣送回，但不能藉此偽造新事實的伺服器標記。不會自動驗收、換資料庫或遷移正式 Host。

## 退出碼與靜默委派的取捨

任務的 `exit_code` 是工作者回報，`exit_code_trust` 明確標示這點，舊任務也一樣。隨附前景 runner 透過 `subprocess.wait` 取得直接子程序的實際退出碼，並在**本機執行結果**標示 `exit_code_source=subprocess_wait`；這不是遠端身分認證、整棵程序樹的停止證明，也不是成果正確的證明。`accept` 會檢查不同 actor 與非空的 source／diff／tests／runtime 說明，但不自行執行那些查核。actor 是同一使用者信任範圍內的合作名稱。

這次沒有加入 AST 簽名檢查、額外心跳執行緒或自動 worktree。runner 已負責序列化心跳；心跳代表接觸，不代表進度。`acquire_more` 先延後，避免各持有部分資源後互等。現階段擴大範圍時，先安全停止、附證據釋放，用 plan 重報完整資源，再 requeue／claim；一次取得全部目標，否則不取得。
