[English](protocol.md) | [繁體中文](protocol.zh-TW.md)

# Protocol

SQLite stores authoritative tasks and revision history. The exchange overview and `tasks/<task_id>.json` are read-only projections; do not edit them directly. Configuration, database, and exchange `state.json` must have the same `state_id`. Join validates existing state without creating a database.

## Requests and receipts

Use a new `request_id` for each new action. Replaying the same ID with the same content returns its existing result; different content with the same ID is rejected. `actor` is a freely chosen cooperation identity, not account authentication.

```json
{"schema_version":1,"request_id":"example-create-001","actor":"controller","op":"create","execution_pool":"cloud","resources":[{"kind":"unscoped","mode":"write"}],"fields":{"title":"Authorized task","goal":"Investigate the issue and complete the authorized change","next_step":"Worker inspects current state"}}
```

Updates use the latest `expected_revision` to reject stale writes. Claim creates the current `lease_id` and execution generation. Worker update, heartbeat, finish, and release use the same identity and lease. A successful receipt reflects that transaction's revision; reread current state before the next update.

New tasks default to `review_policy=required`. Existing task rows without a policy remain `legacy` for compatibility. An explicit `review_start` upgrades a legacy task to required review; old acceptance cannot bypass a review after that point.

| Operation | Required content and behavior |
| --- | --- |
| `create` | `fields.title` and `fields.goal`; declare the pool and targets before execution |
| `plan` | `task_id`, `expected_revision`; modify content or declarations only in a safe planning state |
| `claim` | `task_id`, `expected_revision`, worker `actor`; acquire all declared targets or none |
| `update` | Task ID, revision, current lease, `fields`; save actual verified results |
| `heartbeat` | Task ID, revision, current lease; refresh liveness information and potentially increase revision |
| `finish` | Task ID, revision, current lease, integer `exit_code`; 0 means worker completion awaiting review and acceptance, otherwise failure or blockage |
| `release` | Task ID, revision, current lease, `stop_confirmed=true`, nonempty `stop_evidence`; reject a verified live original process |
| `requeue` | Task ID, revision, `stop_confirmed=true`, nonempty `stop_evidence` and `resume_evidence`; check side effects before queueing again |
| `review_start` | A reviewer different from the worker; snapshot the completed generation, revision, and output fingerprint |
| `review_bind` | Current review lease plus `execution={executor,run_id}`; the run ID must differ from the worker and prior review runs |
| `review_checkpoint` | Current review lease, bound execution, a verified checkpoint, next step, and bounded evidence references |
| `review_resume` | Current review lease, confirmed stop evidence, and resume evidence; rotate the lease, preserve the checkpoint, and bind a new execution |
| `review_result` | Current review lease and bound execution; choose `pass`, `changes_requested`, or `inconclusive`, with source/diff/tests/runtime evidence |
| `accept` | For required tasks, consume a current `passed` review; the worker may accept it, and the reviewer may not accept their own result. Legacy tasks require the old different-actor and evidence checks until `review_start` upgrades them |

StillRelay records reviewer and execution IDs but does not launch sub-agents or authenticate a run. The host must actually start a fresh native sub-agent for review. IDs and actor labels are submitted metadata; they cannot prove a separate process, fresh context, or authenticated reviewer. If a client cannot start a native sub-agent, leave the task `worker_done` instead of recording a pretend review or accepting it.

The output fingerprint covers only files whose bytes were locally verified by `artifact_checks` on the current worker generation. It binds the verified path, SHA-256, and size, and the core rechecks those files at review start/resume/result and accept. Reports and artifact references alone are excluded; without checked files the fingerprint is `null`. Rechecking detects changes at those lifecycle points, but does not lock files or guarantee that external content will remain unchanged afterward. A changed artifact blocks review or acceptance until the worker produces and finishes a current generation.

For every work item that touches the local machine by reading or changing files, running commands, or invoking or controlling local apps, services, or connectors, use one durable task per work item, not per tool call, including for brief and read-only work. When the exact task is not already known, do only the minimum configuration/overview lookup needed to identify the existing state and task; immediately after creating or reusing the task, record that lookup and result as its first checkpoint before any other local work. For a first-time setup, perform only the local steps needed to make shared state available; immediately create the first task and record the setup actions and results, including the installation/configuration and whichever `init` or `bootstrap` path was used, before other local work. When shared state exists, run needed `join` or `doctor` checks under that task. For work with no local-machine interaction, retain the existing threshold: create a task before non-trivial work that may span turns or require handoff. Save confirmed progress and the next step after meaningful checkpoints. Before interruption, preserve the task ID, generation, current lease, execution identity, confirmed stop state, unknown effects, and next action. CLI `request --stdin` and MCP `shared_task_apply` use this same protocol. See [silent delegation](delegation.md) for the host-side fresh-review lifecycle.

Common progress fields `title`, `goal`, `phase`, `summary`, `last_confirmed_step`, and `next_step` are strings. `constraints`, `failed_attempts`, `artifacts`, `backups`, and `blockers` are arrays of strings. New `verified_facts` are source-backed objects with id/statement/method/sources. Facts, failed attempts, artifact references and backups accumulate across update/finish/plan; constraints and blockers remain replaceable. Use explicit retractions to withdraw accumulated entries. Record evidence locations rather than secret contents. See the [0.3 evidence contract and upgrade notes](evidence.md) for local artifact checks, legacy fact classification, limits and exit-code trust.

## Minimal claim and update examples

First read `tasks/<task_id>.json`. Replace the example IDs, revisions, and lease with actual values, and reread the current revision before each update.

```json
{"schema_version":1,"request_id":"claim-001","actor":"worker-a","op":"claim","task_id":"actual-task-id","expected_revision":1}
```

A successful result includes the new revision and current `lease_id`. Then update progress:

```json
{"schema_version":1,"request_id":"update-001","actor":"worker-a","op":"update","task_id":"actual-task-id","expected_revision":2,"lease_id":"actual-lease-id","fields":{"last_confirmed_step":"First part verified","next_step":"Handle the second part"}}
```

Save JSON in a file and submit it using the README's `request --file` command. Only when explicitly enabled, trusted file-only agents write it to `inbox/claim-001.json` or `inbox/update-001.json` under the configured exchange directory, then read `receipts/` there. Do not use fixed example revisions or leases as real values.

## Execution pools and resources

Use `local` or `cloud` for `execution_pool`. Both default to `null` capacity, meaning unlimited. Bootstrap can set `--local-capacity 2`. Resource exclusion applies across pools; the backend is not inferred from the actor's name.

```json
[{"kind":"path","path":"/absolute/project/file.txt","mode":"write"},{"kind":"service","name":"logical-service-id","mode":"write"}]
```

Paths are absolute file or directory paths; a directory reservation covers descendants. Services use stable logical identifiers. Readers may share; overlapping targets involving a writer are exclusive. `unscoped/write` conservatively conflicts with any target when scope is unknown. `read_only=true` with `resources=[]` declares no write side effects and no specific reserved targets; it must not be used to bypass locks for modifications.

Paths are revalidated at admission. Do not add or remove targets during execution. Declarations are a cooperation contract, not an operating-system sandbox.

## Interruption and stop evidence

Heartbeat expiry does not automatically release targets. Interrupted tasks retain `reservation_active`; unknown side effects are marked by `outcome_unknown`. Confirm stopping and inspect side effects before handing work over. If native checks cannot establish termination, require explicit operator confirmation. Missing PIDs or unavailable process inspection do not prove stopping.

Stop evidence must cover the current agent and child workers it started, stating how cessation of modifying work was checked, the result, and any reason for manual clearing. Resume evidence additionally explains whether external actions took effect and where work can safely continue. Successful finish is not acceptance; a recorded review result does not replace the primary agent's judgment about the evidence.

## File-only channel

Disabled by default. Raw input is exposed before processing. Prefer `request --stdin`; see README for migration, limitations and optional settings.

Read the overview, then `tasks/<task_id>.json` before updating. Write complete JSON to `inbox/<request_id>.json`, matching the filename to the request ID. If renaming is available, write a temporary file first and rename it to `.json`. Read `receipts/<request_id>.json`; only `ok=true` means success. If no receipt exists, inspect the original task and consumer instead of dispatching the same action again. After a failure receipt, reread current state and submit corrected content with a new ID.

Execution-capable agents use `request --file <json>` directly without a background service. File-only requests need a `tick` or `watch` consumer. Doctor checks identity and capabilities; it does not replace actual receipt verification.

## Platforms and permissions

The first release supports cooperation on one host under one user. Hosted CI covers macOS, Linux and Windows; real agent and permission integrations still need separate acceptance. Preserve platform access differences: the skill does not provision credentials, elevate permissions, or replay external side effects automatically. Older formats need explicit migration, not silently created replacement state IDs.

## Optional run binding (StillRelay 0.2)

A current worker may send `op=bind` with `task_id`, `expected_revision`, `actor`, `lease_id` and `execution={executor,run_id,status_ref?,report_ref?}`. The task's optional `delegation` includes its original creator as controller and the current worker/generation. Binding does not launch work. One lease binds one exact run; a different binding under that lease is rejected. A new claim clears the active binding; prior snapshots retain it. References are inert evidence locations, not commands or permission to read them. Existing tasks require no rewrite.

The readonly `shared_task_supervise` view is advisory: heartbeat freshness is recorded contact, progress is a worker report, and correctness still requires independent evidence. Task batches are bounded for output size, independent of participant count. See [silent delegation](delegation.md).
