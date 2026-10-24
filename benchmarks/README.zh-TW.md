# 派工者工具內容 token 實驗

[English](README.md) | [繁體中文](README.zh-TW.md)

這個實驗比較同一份本機工作在一般與靜默模式下，派工者實際讀取的工具內容。兩個真正的 MCP stdio 用戶端分別作為 ControllerA 與 WorkerB，每份工作執行三個固定命令階段，每階段輸出 5、50 或 500 行，各測三次。

兩種模式保留完全相同的狀態檢查、MCP 呼叫與原始回覆。ControllerA 會建立任務、監督進度並走完審查協定，最後由 WorkerB 消費通過結果。此 benchmark 的 reviewer 綁定只是協定測試資料，不會實際啟動或認證原生子代理。一般模式額外讀取每階段的完整本機日誌，紀錄實際讀檔參數與內容；靜默模式省去這些讀取。共用狀態只保存階段、行數、雜湊與證據位置，不存原始日誌。工作者 MCP 流量另外計算，未列入比較。

使用 `tiktoken 0.14.0` 的 `o200k_base`，逐筆計算序列化工具呼叫與回傳內容，並列出 UTF-8 位元組數。**沒有模型推論或供應商呼叫；這不是整體 agent token、上下文費用或帳單節省。** 效果會隨日誌量與檢查頻率改變。

[九次實測結果與原始紀錄](results/2026-10-23/summary.md)

重跑需使用已安裝本套件與 `tiktoken` 的 Python 環境，指定一個新的空目錄：

```sh
TIKTOKEN_CACHE_DIR=/path/to/writable-tokenizer-cache /path/to/python benchmarks/token_savings.py --output /path/to/new-results --trials 3
```

Windows 可用自己的環境變數設定方式。只有要從原始碼啟動 MCP 時才加 `--runtime-root`。每次試驗建立新的暫存狀態，結束後移除，不會操作正式資料。輸出包含 `results.json`、`summary.md`、成對的監督紀錄、原始 MCP 呼叫／回覆與命令輸出。
