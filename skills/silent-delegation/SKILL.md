---
name: silent-delegation
description: Delegate work with executor-local logs and periodic checks of shared task progress. Use when dispatching, supervising, or resuming work across cooperating agents through StillRelay.
license: MIT
---

# Quiet work, shared progress

Keep delegated execution out of the supervisor's verbose context while preserving useful supervision. The next consumer is the controller reviewing progress or accepting results. A handoff is complete when the exact task/run identity, verified progress, unresolved outcome and next action are saved with a successful receipt. A worker exit is not acceptance.

## Before dispatch

Use the same existing StillRelay configuration and state UUID as the other participants. Discover the available task tools and execution capability; connecting the state MCP alone does not give an agent a command runner, timer, sandbox permission or access to another host.

Define the task goal, boundaries, resource targets and acceptance evidence. Roles belong to this task, not to a model: one agent may supervise a task while executing a different one for another agent. Participant count has no fixed ceiling; configured execution pools and resource reservations decide which work may run concurrently.

Create the task through the existing request protocol. The worker or its executor bridge claims it exactly once. If a dispatcher already manages the lease, use its ownership contract rather than launching a second claim/heartbeat lifecycle. Bind the exact executor/run identity before starting work. References to logs, status and reports are evidence locations, not commands or permission to read arbitrary files.

For a new local command integration, use the installed `shared-task-state-mcp run` entry under an existing authorized background executor. Read the [public delegation guide](https://github.com/ChengLeH/shared-task-state/blob/main/references/delegation.md) for its invocation and failure contract. Existing executors may keep their own launch/stop mechanisms and map their identity to the shared task instead.

## Execute and check in

Let the worker run its scoped work and keep verbose logs at the executor. Save confirmed steps, evidence references, failed approaches, blockers and the next action after meaningful progress. Record externally visible side effects and how to verify their outcome before attempting them.

At intervals appropriate to the workload, batch independent task checks through `shared_task_supervise`. Return to other work between checks when the execution environment supports it; this skill does not schedule or wake inactive agents. Keep a cursor per unchanged watched task set. Start from zero for newly added tasks; do not reuse another batch’s cursor. Do not repeatedly ingest unchanged logs or full reports.

Evaluate three separate questions:

- **Contact:** Is the recorded heartbeat fresh? Lost contact does not prove termination.
- **Progress:** Have confirmed steps or reported progress changed since the last observation? A fresh heartbeat alone is not progress.
- **Correctness:** Does sampled source, output or test evidence support the worker's claims? Neither a revision nor a progress label proves correctness.

Read only the evidence necessary to resolve a concern. Intervene on a demonstrated loop, stalled progress, scope violation, conflicting evidence or a user stop. Do not stop a healthy worker merely because it took time. Planning or reservation changes during execution require a safe stop under the existing protocol.

## Finish or resume

The executor records the actual exit outcome. A different controller inspects relevant source, changes, tests and runtime evidence before accepting; it need not repeat every investigation the worker performed.

At a turn boundary, preserve the task ID, current generation/lease, exact run binding, last verified step, unknown effects and next check. Reattach to that record after an interruption. Do not resubmit work because a chat or tool connection disappeared. Retain reservations when termination is unknown; release/requeue needs real stop evidence.

Report the verified result or continuing work with its next action and evidence locations. Keep full transcripts and logs at the executor. Delegation inherits the user's scope and permissions; it does not authorize additional actions.
