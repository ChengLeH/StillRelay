# Bounded supervisor payload token experiment

This is an observed supervisory-channel input comparison for two MCP participants, ControllerA and WorkerB, plus instrumented local executor-log reads. It uses real shared_task_supervise calls and real local file reads; no model inference or provider calls were made. It does not measure total agent tokens, model context accounting, provider usage, or billing.

For each trial, WorkerB runs three deterministic command phases once and stores only bounded phase/count/hash/artifact evidence in shared state. ControllerA makes the same status, create, and shared_task_supervise calls in both modes; the exact supervision requests and replies are preserved verbatim in both transcript files. In verbose mode the harness additionally reads each local phase log and records the actual path argument and returned text as an executor_log_read operation. Quiet mode omits these local reads. The raw MCP call/result stream is captured once in mcp-wire.jsonl; WorkerB traffic is reported separately.

Counts use tiktoken o200k_base on each serialized tool-call or tool-result payload separately. This is an encoder convention, not GPT-6 token accounting. The percentages refer only to measured ControllerA MCP payloads plus baseline local executor-log payloads; they are not an overall agent-token savings estimate.

| Case | Lines / phase | Trials | Mean verbose tokens | Mean quiet tokens | Mean saved | Numerator / denominator | Saved / verbose | Mean verbose / quiet bytes | WorkerB MCP tokens / bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| low | 5 | 3 | 3178.7 | 2821.7 | 357.0 | 1071 / 9536 | 11.23% | 9637.3 / 8488.3 | 4147.3 / 12294.0 |
| medium | 50 | 3 | 5359.0 | 2842.0 | 2517.0 | 7551 / 16077 | 46.97% | 16616.7 / 8573.7 | 4165.0 / 12431.0 |
| high | 500 | 3 | 26938.0 | 2824.0 | 24114.0 | 72342 / 80814 | 89.52% | 85425.3 / 8538.3 | 4151.0 / 12369.7 |

## Artifacts

- low trial 1: verbose [./artifacts/low/trial-01/supervisor-verbose.jsonl](./artifacts/low/trial-01/supervisor-verbose.jsonl), quiet [./artifacts/low/trial-01/supervisor-quiet.jsonl](./artifacts/low/trial-01/supervisor-quiet.jsonl), wire [./artifacts/low/trial-01/mcp-wire.jsonl](./artifacts/low/trial-01/mcp-wire.jsonl), stdout [./artifacts/low/trial-01/worker-output.
-](./artifacts/low/trial-01/worker-output.
-) low trial 2: verbose [./artifacts/low/trial-02/supervisor-verbose.jsonl](./artifacts/low/trial-02/supervisor-verbose.jsonl), quiet [./artifacts/low/trial-02/supervisor-quiet.jsonl](./artifacts/low/trial-02/supervisor-quiet.jsonl), wire [./artifacts/low/trial-02/mcp-wire.jsonl](./artifacts/low/trial-02/mcp-wire.jsonl), stdout [./artifacts/low/trial-02/worker-output.
-](./artifacts/low/trial-02/worker-output.
-) low trial 3: verbose [./artifacts/low/trial-03/supervisor-verbose.jsonl](./artifacts/low/trial-03/supervisor-verbose.jsonl), quiet [./artifacts/low/trial-03/supervisor-quiet.jsonl](./artifacts/low/trial-03/supervisor-quiet.jsonl), wire [./artifacts/low/trial-03/mcp-wire.jsonl](./artifacts/low/trial-03/mcp-wire.jsonl), stdout [./artifacts/low/trial-03/worker-output.
-](./artifacts/low/trial-03/worker-output.
-) medium trial 1: verbose [./artifacts/medium/trial-01/supervisor-verbose.jsonl](./artifacts/medium/trial-01/supervisor-verbose.jsonl), quiet [./artifacts/medium/trial-01/supervisor-quiet.jsonl](./artifacts/medium/trial-01/supervisor-quiet.jsonl), wire [./artifacts/medium/trial-01/mcp-wire.jsonl](./artifacts/medium/trial-01/mcp-wire.jsonl), stdout [./artifacts/medium/trial-01/worker-output.
-](./artifacts/medium/trial-01/worker-output.
-) medium trial 2: verbose [./artifacts/medium/trial-02/supervisor-verbose.jsonl](./artifacts/medium/trial-02/supervisor-verbose.jsonl), quiet [./artifacts/medium/trial-02/supervisor-quiet.jsonl](./artifacts/medium/trial-02/supervisor-quiet.jsonl), wire [./artifacts/medium/trial-02/mcp-wire.jsonl](./artifacts/medium/trial-02/mcp-wire.jsonl), stdout [./artifacts/medium/trial-02/worker-output.
-](./artifacts/medium/trial-02/worker-output.
-) medium trial 3: verbose [./artifacts/medium/trial-03/supervisor-verbose.jsonl](./artifacts/medium/trial-03/supervisor-verbose.jsonl), quiet [./artifacts/medium/trial-03/supervisor-quiet.jsonl](./artifacts/medium/trial-03/supervisor-quiet.jsonl), wire [./artifacts/medium/trial-03/mcp-wire.jsonl](./artifacts/medium/trial-03/mcp-wire.jsonl), stdout [./artifacts/medium/trial-03/worker-output.
-](./artifacts/medium/trial-03/worker-output.
-) high trial 1: verbose [./artifacts/high/trial-01/supervisor-verbose.jsonl](./artifacts/high/trial-01/supervisor-verbose.jsonl), quiet [./artifacts/high/trial-01/supervisor-quiet.jsonl](./artifacts/high/trial-01/supervisor-quiet.jsonl), wire [./artifacts/high/trial-01/mcp-wire.jsonl](./artifacts/high/trial-01/mcp-wire.jsonl), stdout [./artifacts/high/trial-01/worker-output.
-](./artifacts/high/trial-01/worker-output.
-) high trial 2: verbose [./artifacts/high/trial-02/supervisor-verbose.jsonl](./artifacts/high/trial-02/supervisor-verbose.jsonl), quiet [./artifacts/high/trial-02/supervisor-quiet.jsonl](./artifacts/high/trial-02/supervisor-quiet.jsonl), wire [./artifacts/high/trial-02/mcp-wire.jsonl](./artifacts/high/trial-02/mcp-wire.jsonl), stdout [./artifacts/high/trial-02/worker-output.
-](./artifacts/high/trial-02/worker-output.
-) high trial 3: verbose [./artifacts/high/trial-03/supervisor-verbose.jsonl](./artifacts/high/trial-03/supervisor-verbose.jsonl), quiet [./artifacts/high/trial-03/supervisor-quiet.jsonl](./artifacts/high/trial-03/supervisor-quiet.jsonl), wire [./artifacts/high/trial-03/mcp-wire.jsonl](./artifacts/high/trial-03/mcp-wire.jsonl), stdout [./artifacts/high/trial-03/worker-output.

Example:](./artifacts/high/trial-03/worker-output.

Example:) python benchmarks/token_savings.py --output /path/to/new-results --trials 3

Use --runtime-root only when serving from a source checkout instead of the installed package. The selected output directory must be empty. The harness creates fresh temporary state per trial.
