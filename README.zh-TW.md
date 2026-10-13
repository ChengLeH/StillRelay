[English](README.md) | [繁體中文](README.zh-TW.md)

# 共用任務續接技能

這個技能讓同一台電腦上的多個代理共用任務身分、最後確認步驟與資源占用。代理可以直接維護本機狀態，不需要接單程式、Slack、MCP（模型上下文協定）或平台掛鉤。Python 3.10 以上即可使用，只依賴標準函式庫。把整個 `shared-task-state` 資料夾安裝到平台實際支援的技能目錄；只複製 `SKILL.md` 不會帶入讀寫工具。以下相對路徑以技能資料夾為基準，代理應使用它實際安裝位置的完整腳本路徑。

## 主代理先做的事

1. 確認所有參與者能存取同一份設定、資料庫與交換目錄，以及各自實際具有哪些讀寫或執行權限。
2. 首位管理者執行 `python3 scripts/state.py bootstrap`；後續代理執行 `python3 scripts/state.py join` 加入既有狀態。不要每位代理各建一份資料庫。
3. 執行 `python3 scripts/state.py doctor`，確認設定、資料庫與交換目錄的 `state_id` 一致。若有只能讀寫檔案的代理，另外確認請求消費者能處理其 inbox。
4. 讀 [SKILL.zh-TW.md](SKILL.zh-TW.md)，以確切任務 ID 接續工作，保存已驗證結果與下一步。

有命令列能力的代理使用 `request --file <json>`，直接提交交易並更新投影，不需要常駐服務。只能讀寫檔案的代理寫入 inbox 並讀取 receipts；需要一個有執行能力的代理呼叫 `tick`，或選配 `watch` 常駐處理。只安裝技能文字不會自動消費檔案請求。

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

45 個測試已在本機 macOS，以及 GitHub 提供的 macOS、Linux、Windows 執行環境通過，涵蓋 Python 3.10 與 3.14。這些驗證涵蓋狀態工具，不代表每個代理平台與使用者權限設定都已完成整合驗收。跨主機與網路檔案系統不在第一版驗證範圍。可查看 [自動測試結果](https://github.com/ChengLeH/shared-task-state/actions/workflows/tests.yml)。詳細請求格式見 [資料契約](references/protocol.zh-TW.md)。

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
