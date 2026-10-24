[English](README.md) | [繁體中文](README.zh-TW.md)

# StillRelay

![StillRelay：三個小 agent 透過共同筆記本接力協作](assets/stillrelay-hero.png)

**小小 agent，一起把事情做好。**

代理共用確認過的進度並接力工作，不必搬動完整對話。工作結束後由全新審查者檢查成果，再由主要工作者接受審查結果。

StillRelay 讓合作的 agent 共用任務、進度和證據。工作者安靜執行，派工者隔一段時間看看進展、抽查成果，需要時再介入。換對話或換工作者，也沿用同一份任務。

> 前身為 **Shared Task State**。套件名稱、命令及 MCP 連線名稱維持不變。StillRelay 0.3 改變 `verified_facts` 寫入格式，原本送事實字串的用戶端必須更新。三個角色是範例，不是人數上限。

- **安靜做事，進度不失聯。** 詳細日誌留在執行端，共用狀態留下確認步驟、阻礙與下一步。
- **角色跟著任務走。** A 可以派工給 B，也能執行 B 派來的另一份工作。
- **人數由你決定。** 既有執行池可設定容量；互相衝突的修改仍會被擋下。
- **完成後等待審查。** 新任務必須由另一個審查執行留下結果，才可接受。

[開始使用 MCP](references/mcp.zh-TW.md) · [靜默委派指南](references/delegation.zh-TW.md) · [選用委派 Skill](skills/silent-delegation/SKILL.md)

**0.3 證據更新：**較短的更新不再洗掉累積事實、嘗試、產物參照或備份；撤回紀錄會保留原文與原因。新寫入的 `verified_facts` 必須包含 ID、主張、查核方式及來源。舊事實字串讀出時標為 `legacy_unverified`。選用的本機 SHA-256 檢查只會在管理者設定 `artifact_roots` 後執行；一般產物參照仍未驗證，檔案檢查紀錄也不等於主張成立或保存不可變副本。使用 0.3 前，先更新會寫入事實字串的用戶端。[證據契約](references/evidence.zh-TW.md) · [v0.3.0 發布說明與固定版本升級步驟](docs/releases/0.3.0.zh-TW.md)。

## 少讀一點日誌，實測差多少？

我們在 0.2 版用兩個 MCP 用戶端跑了九次本機實驗，每份任務執行三個命令階段。靜默模式保留相同的狀態檢查，省去每階段讀取工作者完整日誌；這組數據尚未針對 0.3 的證據格式重測。

| 每階段輸出 | 一般 → 靜默的工具內容 token，平均值 | 減少比例 |
| --- | ---: | ---: |
| 5 行 | 3,179 → 2,822 | 11.23% |
| 50 行 | 5,359 → 2,842 | 46.97% |
| 500 行 | 26,938 → 2,824 | 89.52% |

這裡算的是**派工者看見的工具呼叫與回傳內容**，以 `o200k_base` 計數，每種情境測三次。不是整體模型 token 或帳單節省：實驗沒有呼叫模型，也未把工作者流量算入比較；效果會隨日誌量與查詢頻率改變。[方法與重跑步驟](benchmarks/README.zh-TW.md) · [結果與原始紀錄](benchmarks/results/2026-10-23/summary.md)

## 透過 MCP（模型上下文協定）接入

Antigravity、Hermes 等支援 MCP 的用戶端，可用 [uv 套件工具](https://docs.astral.sh/uv/guides/tools/) 從本專案安裝可選的本機伺服器：

```sh
uv tool install 'git+https://github.com/ChengLeH/StillRelay@main'
shared-task-state-mcp init
shared-task-state-mcp client-config --client antigravity
```

只有**全新安裝**才執行一次 `init`。已經有任務狀態時，先以 `--config /absolute/path/config.json`（放在子命令前）選擇既有設定，不另建資料庫。把產生的伺服器項目合併到用戶端設定，再重新載入 MCP；Hermes 改用 `--client hermes`。產生的設定含既有狀態 UUID（通用唯一識別碼）與已安裝執行環境的完整路徑，不必手填 Python 位置。

入口使用 stdio（標準輸入／輸出），提供六個結構化工具，不開網路埠；各用戶端共用指定的資料庫。加入 `--read-only` 可完全移除更新工具。安裝 MCP 版本會增加官方 Python MCP SDK（開發套件）依賴，下方獨立技能／命令列用法仍只需要標準函式庫。完整步驟、設定範例與可直接交給代理的提示詞見 [MCP 安裝說明](references/mcp.zh-TW.md)。

## 主代理先做的事

1. 確認所有參與者能存取同一份設定、資料庫與交換目錄，以及各自實際具有哪些讀寫或執行權限。
2. 首位管理者執行 `python3 scripts/state.py bootstrap`；後續代理執行 `python3 scripts/state.py join` 加入既有狀態。不要每位代理各建一份資料庫。
3. 執行 `python3 scripts/state.py doctor`，確認設定、資料庫與交換目錄的 `state_id` 一致。只能讀寫檔案的代理需另有寫入前驗證入口。
4. 讀 [SKILL.zh-TW.md](SKILL.zh-TW.md)，以確切任務 ID 接續工作，保存已驗證結果與下一步。

有命令列能力的代理使用 `request --stdin`，直接提交並更新投影，不需要常駐服務。純檔案通道預設停用；必要條件與既有安裝遷移見下方「憑證與安全提交」。

## 初始化與加入

共用設定由 `--config <path>`（放在子命令前）或 `SHARED_TASK_STATE_CONFIG` 指定。未指定時使用所在平台的預設位置：

| 平台 | 設定位置 | 預設資料根目錄 |
| --- | --- | --- |
| macOS | `~/Library/Application Support/shared-task-state/config.json` | 設定所在目錄下的 `data/` |
| Linux | `$XDG_CONFIG_HOME/shared-task-state/config.json`；未設定則 `~/.config/shared-task-state/config.json` | `$XDG_DATA_HOME/shared-task-state`；未設定則 `~/.local/share/shared-task-state` |
| Windows | `%LOCALAPPDATA%/shared-task-state/config.json`；未設定則 `~/AppData/Local/shared-task-state/config.json` | 設定所在目錄下的 `data/` |

首次 `bootstrap` 可用 `--root` 與 `--exchange` 明確指定位置。設定一旦存在，不能藉再次初始化重新指向別處；後續加入必須驗證既有位置。設定、資料庫與交換目錄的 `state.json` 共用同一個 UUID（唯一狀態識別碼）。初始化以檔案鎖及 pending manifest（待完成清單）處理競爭與中斷；`join` 只驗證，不建立資料庫。遇到舊格式需明確遷移，不自動建立另一套狀態掩蓋問題。

```text
python3 scripts/state.py bootstrap
python3 scripts/state.py join
python3 scripts/state.py doctor
python3 scripts/state.py overview
python3 scripts/state.py read <task_id>
python3 scripts/state.py request --file <request.json>
python3 scripts/state.py history <task_id>
```

Windows 若使用 Python 啟動器，把上述 `python3` 換成 `py -3`，例如：

```text
py -3 scripts/state.py bootstrap
py -3 scripts/state.py join
```

指定共用設定時，所有代理沿用同一條路徑；例如：

```text
python3 scripts/state.py --config "<共用設定檔完整路徑>" join --state-id "<主代理提供的識別碼>"
```

容器或雲端代理若看不到同一個家目錄，需由管理者提供共用位置或既有工具入口；找不到時回報能力不足，不能自行建立另一套。初始化不會安裝背景服務、不會修改其他平台設定，也不會提升任何代理權限。

`local` 與 `cloud` 執行池預設都沒有數量上限。需要限制本地負載時，可在初始化使用 `bootstrap --local-capacity 2`；兩筆不是通用版的固定限制。資源衝突仍獨立檢查：不同目標可並行，同一目標只允許一位寫入者，唯讀可共享。未能精確宣告目標時，可使用 `unscoped` 的 `write` 保守占用；它會與其他目標讀寫衝突。

## 失聯與安全續接

心跳逾期表示失聯，不能證明工作者已停止。任務轉為 `interrupted` 時仍保留 `reservation_active` 與目標占用，避免另一位寫入者接手後衝突。

釋放或重新排隊都需要 `stop_confirmed=true` 與非空 `stop_evidence`；重新排隊另外需要 `resume_evidence`。若原始程序仍存活，請求會被拒絕。無法原生確認停止時，操作者必須查證並明確確認停止或解除占用，不能只因逾時就填入確認。外部副作用結果未知時，先查證，再決定如何續接，不自動重送。

## 使用界線

這是同一主機、同一使用者信任範圍內的協作契約。`actor` 是協作識別，租約是擁有者檢查；它們不是帳號認證，也不是強制沙箱。技能不放寬任何平台原有權限，也不能阻止繞過共用入口的程式修改資源。

回歸測試已在本機 macOS，以及 GitHub 提供的 macOS、Linux、Windows 執行環境通過，涵蓋 Python 3.10 與 3.14。這些驗證涵蓋狀態工具，不代表每個代理平台與使用者權限設定都已完成整合驗收。跨主機與網路檔案系統不在第一版驗證範圍。可查看 [自動測試結果](https://github.com/ChengLeH/StillRelay/actions/workflows/tests.yml)。詳細請求格式見 [資料契約](references/protocol.zh-TW.md)。

## 測試

在技能資料夾或儲存庫根目錄執行：

```text
python3 -m unittest discover -s tests -p "test_*.py"
```

Windows 可使用 `py -3 -m unittest discover -s tests -p "test_*.py"`。

## 安全分享

只分享來源檔案。設定、任務、資料庫、交換目錄、憑證與備份不要納入儲存庫。選配平台整合也應沿用同一狀態身分及權限界線。

## 授權

採用 [MIT 授權](LICENSE)，著作權人為公開帳號 ChengLeH。

## 憑證與安全提交

優先使用 `python3 scripts/state.py request --stdin`，將要求 JSON（資料格式）直接提供到標準輸入。交易與投影同步完成，不保留原始要求檔案。`request --file` 仍可用於已脫敏、位於私人位置的輸入檔案；該檔案由提交者負責清理。不得把憑證、環境變數清單、完整日誌或對話複製到狀態，只記錄用途及已核准的取得方式。

所有寫入入口會遞迴遮罩已知供應商憑證、憑證指定值、敏感 JSON 欄位及完整或截斷的私鑰區塊。識別碼、來源鍵、來源與防撞目標含憑證時直接拒絕，避免遮罩造成身分或鎖定目標碰撞。歷史讀取、任務快照、報告、結果及投影也經過相同防護。冪等性（同一請求重送不重做）的摘要以原始要求計算，避免不同憑證內容被當成同一個請求。

可在設定的 `privacy_scanner` 指定可信任離線掃描器的參數陣列，以標準輸入接收遮罩後 JSON。結束碼 0 才通過；有發現、逾時、程式不存在或其他故障都拒絕。可攜版本不強制安裝或下載掃描器；掃描器的網路隔離由管理者另外設定。隱私掃描啟動器不繼承環境中的憑證。未知或編碼後的憑證仍可能無法辨識，這不是憑證保管庫或權限隔離系統。

**預設停用共享檔案提交**，未設定 `allow_file_requests` 也會停用。既有使用 inbox（收件匣）的安裝，升級前應改為直接提交；總覽與任務讀取不變。只有檔案能力的代理，需要工具或閘道在寫入共享檔案前驗證內容，光載入技能無法建立此能力。管理者可明確設 `allow_file_requests: true` 啟用可信任檔案通道，但原始要求在處理前仍可被讀取，當機或未寫完時也可能殘留。成功處理的輸入只在投影及回執發布後移除；這無法消除暴露窗口。同一帳號的代理也無法只靠作業系統檔案權限隔離。

若 macOS／Linux 的呼叫者已在沙盒裡，無法再啟動掃描沙盒，可選配由可信任主機程序執行 `scripts/privacy_scan_service.py --socket <本機連線位置> --serve-config <主機設定.json>`。主機設定內容為 `{"scanner": ["<離線掃描器命令>", "<參數>"]}`，狀態設定的 `privacy_scanner` 改為 `["<Python絕對路徑>", "<privacy_scan_service.py絕對路徑>", "--socket", "<本機連線位置>"]`。程序只使用 Unix socket（本機通訊端），不保存或回傳輸入原文，限制輸入大小；程序不可用時拒絕保存。啟動及重啟由主機管理，不自動安裝服務。此選配工具測試 macOS／Linux；Windows 使用直接掃描命令。它不提供跨帳號驗證，也不授予檔案權限。

## 共用筆記本，不是秘密保險箱

**沒裝 Betterleaks，也有內建的保存前憑證保護。** StillRelay 會遮罩可辨識的常見 API token、密碼與敏感欄位、Bearer／Basic 驗證資訊、私鑰區塊，以及網址裡的帳密。巢狀資料、任務歷史和查詢回傳沿用同一層保護。任務身分或資源目標含憑證時會直接拒絕，避免遮罩後把不同對象混在一起。

但它不是所有個資都認得的過濾器。**Email、電話、姓名、住址及自由文字中的私密內容，目前沒有全面遮罩。** 共用狀態應放已確認進度和證據位置，不要放客戶資料、原始提示詞或完整日誌。靜默命令接合層把原始輸出留在本機檔案，不送進共用 exchange；這些檔案仍可能含敏感內容。POSIX 系統使用目錄 `0700`、檔案 `0600` 權限；Windows 則沿用資料目錄的 ACL（存取權限）。請保持目錄私密，並自行設定保存政策。

想再加一道檢查，可以設定離線 `privacy_scanner`，接 Betterleaks 或其他掃描器。這是選配，不會自動下載或悄悄啟用；一旦設定，掃到問題、程式失敗、逾時或找不到掃描器，都會阻擋操作。接法見本頁的憑證與安全提交說明，以及[靜默委派指南](references/delegation.zh-TW.md)。
