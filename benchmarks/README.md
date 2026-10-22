# Bounded supervisor payload token experiment

[English](README.md) | [繁體中文](README.zh-TW.md)

This experiment compares the observed input to a supervisor channel for the same deterministic local workload. It uses two real MCP stdio clients, ControllerA and WorkerB, and an instrumented local executor-log read in verbose mode. It makes no model inference or provider calls. It does not measure total agent tokens, model context accounting, provider usage, or billing.

Each trial creates a fresh disposable state. WorkerB runs three deterministic command phases once, emitting 5, 50, or 500 lines per phase. The command output stays in local stdout artifacts. Shared task state receives only bounded phase progress, line count, SHA-256, and artifact reference. ControllerA makes the same status, create, and actual shared_task_supervise calls in both modes; each exact MCP request and raw reply is preserved verbatim in both supervisor transcripts. In verbose mode, the harness also reads the phase log file and records the actual local executor-log call arguments and returned text. Quiet mode omits those local reads. WorkerB MCP traffic is captured separately and excluded from the comparison.

Tokens use tiktoken o200k_base, summed per serialized tool-call or tool-result payload, with UTF-8 payload bytes reported alongside. o200k_base is an encoder convention and is not asserted to match GPT-6 usage or billing. The measured percentage is limited to ControllerA's actual MCP payloads plus baseline-only local executor-log payloads. It is not an overall agent-token savings figure.

The output contains results.json, summary.md, paired verbose/quiet JSONL transcripts, the raw MCP wire transcript, and phase stdout artifacts. The paired transcripts contain the exact same ControllerA MCP calls and raw responses; no MCP response is edited or projected. The only additional verbose entries are local executor_log_read calls and their actual text results.

Run with the installed runtime that exposes shared_task_supervise:

    TIKTOKEN_CACHE_DIR=/path/to/writable-tokenizer-cache /path/to/python benchmarks/token_savings.py --output /path/to/new-results --trials 3

Use --runtime-root only to launch the MCP server from a source checkout rather than the installed package. The destination must be empty. The benchmark creates and removes fresh temporary state for each trial and makes no project dependency or production-state changes.
