[English](mcp.md) | [繁體中文](mcp.zh-TW.md)

# 安裝並連接 MCP（模型上下文協定）

這個可選套件讓各代理在存放任務狀態的同一台電腦上，啟動自己的 stdio（標準輸入／輸出）程序，共用同一份 SQLite（交易式資料庫）與既有任務協定。不需要帳號、API 金鑰、網路埠或本專案作者的私人 Host（主機）環境。

## 安裝

需要 Python 3.10 以上與 [uv 套件工具](https://docs.astral.sh/uv/getting-started/installation/)。macOS、Linux、Windows 使用相同指令：

```sh
uv tool install 'git+https://github.com/ChengLeH/shared-task-state@main'
```

找不到安裝後的指令時，執行 `uv tool update-shell` 後重開終端機。也可在持續保留的虛擬環境中，從專案根目錄執行 `python -m pip install .`。套件直接從 GitHub 安裝，這裡沒有宣稱已發布到 PyPI（Python 套件索引）。正式使用可將 `main` 改成檢查過的提交 SHA（提交識別碼）；更新 uv 安裝用 `uv tool upgrade shared-task-state-mcp`。

安裝包含公開版狀態核心、隱私防護與 MCP 入口。原本直接安裝技能資料夾的用法不需要 MCP SDK（開發套件）。安裝不會自動登記用戶端或啟動背景服務。

## 選擇既有狀態，或明確建立一次

已有任務狀態時，選用其他代理使用的同一份設定：

```sh
shared-task-state-mcp --config '/absolute/path/config.json' client-config
```

設定、資料庫、交換目錄的狀態 UUID（通用唯一識別碼）必須一致；缺少或格式不相容時會拒絕連接。伺服器不會自動初始化、遷移或替換資料庫。私人 Host 若採用不同格式，應保留相符的既有介接，不能直接套用公開版。

只有**全新使用者**才明確執行一次：

```sh
shared-task-state-mcp init
```

這會沿用原命令列的[平台預設位置](../README.zh-TW.md#初始化與加入)。自訂位置可用：

```sh
shared-task-state-mcp --config '/absolute/path/config.json' init --root '/absolute/path/data' --exchange '/absolute/path/exchange'
```

已有設定時，`init` 只驗證原狀態，不覆蓋或改指向其他資料庫。各代理都必須有共同位置的存取權限。容器或雲端代理不能靠 stdio 穿透到另一台 Mac；跨電腦使用需另有已授權通道或具備身分驗證的遠端 MCP 服務。本套件沒有 HTTP（網路傳輸）入口。

## Anti-Gravity（反重力）

```sh
shared-task-state-mcp client-config --client antigravity
```

自訂狀態位置時，把 `--config` 放在 `client-config` 前。輸出會包含已安裝 Python 的完整路徑、`-m shared_task_state_mcp`、設定檔完整路徑與狀態 UUID。只把 `mcpServers.shared-task-state` 合併到現有設定，保留其他伺服器。

[Anti-Gravity 官方文件](https://antigravity.google/docs/mcp) 說明整合開發環境可從 **MCP Servers → Manage MCP Servers → View raw config** 開啟設定，命令列介面使用 `/mcp`。目前全域檔案是 `~/.gemini/config/mcp_config.json`，專案範圍是 `.agents/mcp_config.json`；若安裝版本不同，以它的設定畫面為準。重新載入後，請代理呼叫 `shared_task_status`，確認 UUID 和原本狀態一致。

產生的設定含本機路徑，請勿提交到 Git。工具執行是否需確認，由用戶端管理。首次只想提供讀取，可產生完全沒有更新工具的設定：

```sh
shared-task-state-mcp --read-only client-config --client antigravity
```

## Hermes（赫密斯代理）

```sh
shared-task-state-mcp client-config --client hermes
```

把輸出的 `mcp_servers.shared-task-state` 合併到目前使用的設定檔之 `config.yaml`；預設位置為 `~/.hermes/config.yaml`。用 `hermes mcp test shared-task-state` 測試，再使用 `/reload-mcp` 或重新啟動相關代理。確認 `shared_task_status` 回傳相同 UUID。這是自訂 MCP，不要求先列入 Hermes 的審核目錄；參見 [Hermes 官方 MCP 文件](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/mcp.md)。

## 直接掛載與其他用戶端

接受 `command`／`args` 的用戶端可使用上述產生的設定。若已有共同狀態，也可以用 `uvx`（暫時執行套件）直接掛載，不必永久安裝工具：

```json
{
  "mcpServers": {
    "shared-task-state": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/ChengLeH/shared-task-state@main", "shared-task-state-mcp"]
    }
  }
}
```

此範例使用平台預設共用設定或 `SHARED_TASK_STATE_CONFIG`。要綁定指定狀態時，在參數最後加入 `--config` 與設定檔完整路徑，以及 `--state-id` 與預期 UUID。圖形介面找不到 `uvx` 時，改用其實際完整路徑。正式使用應固定 Git 提交版本。

`client-config` 產生的完整 Python 路徑，應來自持續保留的 uv 工具、pip 或虛擬環境安裝。不要把暫時 `uvx` 環境印出的 Python 快取位置當成永久設定；清掉快取後它可能消失。直接使用上面的 `uvx` 設定則可重新建立執行環境。

## 工具與重試

| 工具 | 功能 |
| --- | --- |
| `shared_task_status` | 唯讀檢查狀態 UUID、事件序號、任務數與掃描設定 |
| `shared_task_overview` | 以 `after_task_id` 分頁讀摘要；`limit` 為 1–100 |
| `shared_task_read` | 讀取確切任務、最新版本與租約 |
| `shared_task_history` | 以 `after_seq` 分頁讀事件；`limit` 為 1–100 |
| `shared_task_apply` | 提交既有建立、規劃、接單、更新、心跳、結束、釋放、重排與驗收協定 |

讀取採用唯讀交易，不會把逾時心跳判定為中斷或停止。讀取結果上限為 2 MiB（約 200 萬位元組），歷史過大可縮小頁面。更新沿用[資料契約](protocol.zh-TW.md)、版本、租約、資源占用、獨立驗收及隱私檢查；MCP 不接受 PID（程序識別碼）綁定。

更新回傳 `receipt`（保存回執）。`committed=true` 代表已保存，即使後續輸出投影失敗而出現 `ok=false, projection_updated=false`，也只能用**相同 `request_id` 與完全相同的原始內容**重試；不要換新識別碼重做。同一識別碼改內容會被拒絕。連線中斷或結果不明時，先查確切任務再判斷；取消 MCP 呼叫不能回滾已在線程內提交的請求。

套件沿用核心的憑證遮罩與已設定的離線 `privacy_scanner`（隱私掃描器），掃描失敗會拒絕。它不會安裝 Betterleaks（敏感內容掃描）、Headroom（輸出壓縮）或網路驗證層；需要時由管理者在外層接入。`actor`（協作名稱）不是身分驗證；同一作業系統帳號下的代理仍有相同帳號的檔案權限，不會自動開放遠端連線。

## 可直接交給本機代理的提示詞

> 請從 https://github.com/ChengLeH/shared-task-state 安裝 Shared Task State MCP，使用持續保留的 uv 工具環境並固定到已檢查的 Git 提交。先找我既有的共用任務設定，不另建資料庫。只有確認沒有既有狀態時，才詢問是否全新安裝，再執行 `shared-task-state-mcp init`。用 `client-config --client antigravity`（Hermes 則用 `hermes`）產生設定，只合併該伺服器項目到目前用戶端設定，保留其他項目。重新載入 MCP 後呼叫 `shared_task_status`，確認狀態 UUID 與原本一致，不用建立任務測試連線。不要把本機設定、資料庫、憑證或備份提交到 Git。

代理需有安裝及修改本機設定的權限；這不是宣稱用戶端內建任意 GitHub MCP 的自然語言安裝器，也不是商店上架。自動化測試驗證已安裝套件與實際 MCP 協定；Anti-Gravity／Hermes 應用程式畫面的完整連線仍需在使用者實際版本驗證。

## 移除與回復

停用或移除用戶端的這個伺服器項目，重新載入後，可用 `uv tool uninstall shared-task-state-mcp` 移除套件。任務設定、資料庫與交換目錄會保留。要回復程式版本，重新安裝相容於狀態格式的先前 Git 提交即可。初始化與產生設定不會改用戶端檔案，也沒有另外安裝的背景服務；stdio 程序由用戶端管理。
