# Silent delegation with StillRelay

[English](delegation.md) | [繁體中文](delegation.zh-TW.md)

Silent delegation keeps the noisy part of execution with the worker. It does **not** mean “never check until done.” The delegator periodically reads shared progress, samples evidence, and intervenes when something actually needs attention.

## Who does what?

| Task | Delegator/controller | Worker |
|---|---|---|
| Investigate a bug | A | B |
| Check a separate change | B | A |
| Run another scoped check | A | C |

A, B and C are task participants, not fixed model ranks. Six, seven or more participants use the same protocol; pool capacity is a separate hardware policy. This remains a single-host cooperation tool within one user's trust boundary.

The state MCP records and queries work. It does not choose models, contact arbitrary agents, wake an inactive chat, or grant an execution tool. Use the executor your agent already has, or opt into the local command wrapper below.

## Three pieces, one progress record

1. The primary agent creates or reuses one task for the work item, with its goal, constraints, target reservations and acceptance criteria.
2. The worker or bridge claims it and binds an exact run. It executes locally, keeps detailed logs local, and saves meaningful verified progress and its next step.
3. After successful finish, the host starts a fresh native sub-agent for review. The workflow records its run, checkpoints and result, then a worker or non-reviewer controller accepts a pass. This needs no third agent distinct from the primary worker and reviewer.

StillRelay does not schedule the reviewer or verify context isolation: the host's actual launch must create the separate run, while actor and run IDs are self-reported metadata. If the host cannot launch a native sub-agent, keep the task `worker_done`. New tasks default to required review; older tasks keep legacy acceptance until `review_start` explicitly upgrades them. See the [review lifecycle](protocol.md#requests-and-receipts), including the exact artifact fingerprint scope.

The optional [silent-delegation Skill](../skills/silent-delegation/SKILL.md) teaches that behavior. Install the skill folder through your client's supported skill mechanism; it expects the installed CLI or the configured MCP connection. Do not assume every MCP client automatically loads repository skills.

## Opt-in command wrapper

Use an existing **public-format** configuration. Initialize once only for a new installation, then share the same config/state UUID. The private Mac Host's older state format is not an automatic migration target.

Create a task through `shared_task_apply` (or the existing CLI request protocol), for example:

```json
{"schema_version":1,"request_id":"create-check-001","actor":"agent-a","op":"create","task_id":"check-001","execution_pool":"local","resources":[{"kind":"path","path":"/absolute/project","mode":"write"}],"fields":{"title":"Check the project","goal":"Run the authorized test suite and preserve evidence"}}
```

Run the worker bridge through an authorized executor:

```sh
shared-task-state-mcp --config /absolute/shared/config.json run \
  --task-id check-001 --actor agent-b --executor local-command \
  --cwd /absolute/project -- python -m unittest discover -s tests
```

The wrapper is foreground: your existing job runner owns background launch, cancellation and reconnection. It claims/binds before starting the explicit argument vector, keeps stdout/stderr in a local run directory, sends heartbeats and records the command exit. It never passes the command through a shell or independently accepts a result. Commands must remain foreground; detached child work needs an executor adapter that can track its actual lifetime.

Logs stay under `<data-root>/runs/<task-id>/<run-id>/`; the task’s `delegation.report_ref` points to that directory, so a reconnecting controller can find them. The CLI returns the child’s exit code on a recorded completion (0–255), and a nonzero status for unresolved lifecycle errors. Raw logs are not privacy-filtered or uploaded to shared state. POSIX permissions are `0700` for run directories and `0600` for files; on Windows, keep the data directory’s ACL private.

A lease already owned by another dispatcher is not a reason to claim twice. Such dispatchers should use the existing lease and implement the bridge contract directly. If startup fails before a child launches, only a confirmed empty claim can be released. Ambiguous outcomes retain reservations for investigation.

## Periodic supervision

```sh
shared-task-state-mcp --config /absolute/shared/config.json supervise \
  --task-id check-001 --after-seq 0
```

The MCP equivalent is `shared_task_supervise` with task IDs and the prior event cursor. Batch size bounds a response; it is not an agent-count limit. Each call reads one database snapshot and does not update heartbeats, reconcile tasks, open the referenced logs, or accept work.

Keep these separate:

- **Fresh heartbeat:** recorded contact, not process-tree verification or useful progress.
- **Reported progress:** a worker update to inspect, not proof of correctness.
- **Evidence review:** the controller's check of source/output/tests before acceptance.

Choose check intervals for the workload and increase spacing while healthy. Only inspect verbose output when a failure, lack of progress or conflicting evidence warrants it. Keep a cursor per watched task set, starting at zero when adding tasks; do not advance another batch using this batch’s cursor. The latest reported progress includes its generation; a prior generation’s report is retained evidence, not new-run progress. Save the returned cursor for the next observation; observing a change does not mark it verified.

## Capacity and concurrent work

Participant count is not configured. The existing `pool_limits` controls simultaneously reserved work, for example `{"local":7,"cloud":null}` for an operator who explicitly chooses seven local slots. No setting is changed automatically. Existing local model hardware may need fewer slots than the number of participants.

Overlapping target writes remain exclusive. Use distinct target declarations for independent work; use isolated Git worktrees and a serialized integration step for parallel source edits that otherwise overlap. The task snapshot is not a source-code merge.

## Existing Mac MCP executors

Keep the original guarded Tunnel/Betterleaks/Headroom path and its approved executor. Link the shared task ID to the existing workstream, generation and run ID; let one bridge own the lease lifecycle. Retain native status/checkpoint files as executor identity and liveness evidence, without maintaining a second narrative of shared progress.

Query the executor's reconciled discovery/attach/result interface when available. A raw file saying `running` is not sufficient current health evidence. Do not replace production state, increase model capacity, or restart the control channel just to enable this Skill. This public package has no bundled private Host configuration and does not certify a deployed client integration merely because disposable-state tests pass.

## Give an agent this setup request

> Install StillRelay from ChengLeH/StillRelay using its MCP guide. Join the existing shared configuration rather than initializing another state. Load the optional silent-delegation Skill if this client supports skills. Use my already-authorized executor for background work, keep verbose logs local, and periodically inspect shared progress and selected evidence. Do not change model concurrency or permissions. Tell me which connection and execution capabilities were verified.

## Update and rollback

Existing package and command names remain `shared-task-state-mcp` / `shared-task-state`; StillRelay is the display name. Pin a Git commit for reproducibility. Update the installed tool to the tested release revision and reload the client connection. To roll back the package, reinstall the previously pinned revision. This release adds optional run-binding data; it does not migrate the existing private Host state or redesign retention. Preserve backups and history; do not restore an old database over later accepted work.

## Verification boundaries

Command workers and MCP clients can validate the transport, leases, role reversal and snapshots without model calls. They do not prove an inactive client can wake itself, that a particular model follows the Skill, or that every agent platform's UI integration works. Benchmark results must distinguish supervisor-visible payload tokens from total model consumption and billing.
