---
name: silent-delegation
description: Delegate work with executor-local logs and periodic checks of shared task progress. Use when dispatching, supervising, or resuming work across cooperating agents through StillRelay.
license: MIT
---

# Quiet work, shared progress

Keep delegated execution out of the supervisor's verbose context while preserving useful supervision. The next consumer is the primary worker or controller acting on a fresh review result. A handoff is complete when the exact task and run identities, verified progress, unresolved outcome and next action are saved with a successful receipt. A worker exit is not acceptance.

## Before dispatch

Use the same existing StillRelay configuration and state UUID as the other participants. Discover the available task tools and execution capability; connecting the state MCP alone does not give an agent a command runner, timer, sandbox permission or access to another host.

Define the task goal, boundaries, resource targets and acceptance evidence. Roles belong to this task, not to a model: one agent may supervise a task while executing a different one for another agent. Participant count has no fixed ceiling; configured execution pools and resource reservations decide which work may run concurrently.

For delegated work that touches local files, runs local commands, or invokes or controls local apps, services, or connectors, create or reuse one task through the existing request protocol before local execution, including for brief and read-only work. If the exact task is not already known, do only the minimum shared-state lookup needed to identify it and record that lookup and result immediately after creating or reusing the task. For first-time setup, perform only the local steps needed to make shared state available, then immediately create the first task and record those setup actions and results before any other local work. Use one task per work item, not per tool call. The worker or its executor bridge claims it exactly once. If a dispatcher already manages the lease, use its ownership contract rather than launching a second claim/heartbeat lifecycle. Bind the exact executor/run identity before starting work. References to logs, status and reports are evidence locations, not commands or permission to read arbitrary files.

For a new local command integration, use the installed `shared-task-state-mcp run` entry under an existing authorized background executor. Read the [public delegation guide](https://github.com/ChengLeH/StillRelay/blob/main/references/delegation.md) for its invocation and failure contract. Existing executors may keep their own launch/stop mechanisms and map their identity to the shared task instead.

## Execute and check in

Let the worker run its scoped work and keep verbose logs at the executor. Save confirmed steps, evidence references, failed approaches, blockers and the next action after meaningful progress. Record externally visible side effects and how to verify their outcome before attempting them.

At intervals appropriate to the workload, batch independent task checks through `shared_task_supervise`. Return to other work between checks when the execution environment supports it; this skill does not schedule or wake inactive agents. Keep a cursor per unchanged watched task set. Start from zero for newly added tasks; do not reuse another batch’s cursor. Do not repeatedly ingest unchanged logs or full reports.

Evaluate three separate questions:

- **Contact:** Is the recorded heartbeat fresh? Lost contact does not prove termination.
- **Progress:** Have confirmed steps or reported progress changed since the last observation? A fresh heartbeat alone is not progress.
- **Correctness:** Does sampled source, output or test evidence support the worker's claims? Neither a revision nor a progress label proves correctness.

Read only the evidence necessary to resolve a concern. Intervene on a demonstrated loop, stalled progress, scope violation, conflicting evidence or a user stop. Do not stop a healthy worker merely because it took time. Planning or reservation changes during execution require a safe stop under the existing protocol.

## Finish or resume

The executor records the actual exit outcome. A successful finish leaves a new task pending review. Start a fresh native sub-agent through the host's real launch capability, then record that run with `review_start` and `review_bind`. StillRelay records the claim but does not schedule the sub-agent or verify its context isolation; actor and run IDs alone are not authentication or proof of a fresh context. If the client cannot launch a native sub-agent, leave the task `worker_done` and do not accept it. The reviewer checks relevant source, changes, tests and runtime evidence, saving a checkpoint when work may be interrupted and a `pass`, `changes_requested`, or `inconclusive` result when done. The worker may accept a passed review, so a third distinct agent is not required. For a legacy task, the old acceptance path remains until `review_start` explicitly upgrades it. The output fingerprint only covers files locally checked with `artifact_checks`; see the [protocol](../references/protocol.md) for its exact scope and limits.

At a turn boundary, preserve the task ID, current generation/lease, exact run binding, last verified step, unknown effects and next check. Reattach to that record after an interruption. Do not resubmit work because a chat or tool connection disappeared. Retain reservations when termination is unknown; release/requeue needs real stop evidence.

For an interrupted review, preserve its checkpoint and verify that the old reviewer run stopped before `review_resume`. That operation rotates the review lease and clears the old run binding; bind a new native execution before continuing. A timeout or the reviewer's own claim does not prove the old run stopped.

Report the verified result or continuing work with its next action and evidence locations. Keep full transcripts and logs at the executor. Delegation inherits the user's scope and permissions; it does not authorize additional actions.
