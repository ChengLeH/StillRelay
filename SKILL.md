---
name: shared-task-state
license: MIT
description: Track local work that changes state, and resume or supervise existing shared tasks across agents. Purely read-only work does not require a task.
---

[English](SKILL.md) | [繁體中文](SKILL.zh-TW.md)

# Shared Task State

Maintain one shared progress record so the next agent can continue from the last verified step. An update is complete only after a successful transaction or receipt. Worker completion is separate from controller acceptance.

## Verify the shared location

For tracked state-changing work or an explicitly resumed task, use `--config` or `SHARED_TASK_STATE_CONFIG` to join existing state. Only the primary agent bootstraps the first state; do not recreate or redirect existing configuration. When the exact task is not already known, make only the minimum configuration/overview lookup needed to identify the existing state and task. Immediately after creating or reusing the task, record that lookup and result as its first checkpoint before any other local work; run needed `join` or `doctor` checks under that task. For a first-time setup, do only the local steps required to make shared state available; immediately after it is available, create the first task and record the setup actions and results, including the installation/configuration and whichever `init` or `bootstrap` path was used, before any other local work. If the shared location is inaccessible, report the missing capability rather than initializing another database. See [README.md](README.md) for setup.

Prefer `scripts/state.py request --stdin` without saving shared raw input. File requests are disabled by default. File-only agents need an input-validating gateway; migrate existing inbox users before upgrading. Never use credentials as identity or resource metadata. Configured scanner failures stop persistence. See the README Credentials and safe submission section.

## Daily workflow

Purely read-only work does not require creating or claiming a StillRelay task, including local reads, inspections and queries. Do not create a task merely to record a read-only investigation or its review. A read can observe concurrent changes; it has no StillRelay resource reservation unless an existing task explicitly reserves it. If a read-only investigation later needs a write or other side effect, create or reuse one task and claim the declared targets before that action. For work that changes local files, settings, services or external state, use one task per work item, never one per tool call. When identifying an existing task, make only the minimum state lookup and record it as the first checkpoint after creating or reusing the task. For first-time setup, perform only the steps needed to make state available, then create the first task and record the setup and init or bootstrap path. Pure-cloud work that changes state follows the existing non-trivial multi-turn or handoff threshold. Save meaningful verified checkpoints and the next step. New tasks require a fresh independent review via the host, recorded through review_start and review_bind; if unavailable, leave worker_done. StillRelay records run metadata but cannot prove isolation. See [the review protocol](references/protocol.md).

The following steps apply only to tracked state-changing work or explicit resumption of an existing task; skip them for purely read-only work.

1. Read the overview first, then the exact task ID when details are needed. Check generation time, revision, and the last verified step; do not infer identity from similar titles.
2. Read the goal, constraints, verified facts, failed approaches, next action, and evidence locations. Recheck only drift that matters to the next step, not completed investigations.
3. Declare `execution_pool` and `resources` when creating or planning work. If precise targets are unknown, reserve `unscoped/write` conservatively. Use `read_only=true` and `resources=[]` only when there are genuinely no write side effects or specific reserved targets.
4. Claim before executing. Updates require the latest `expected_revision`, the current `lease_id`, and the same `actor`. Reread after conflicts; never force an overwrite. Send heartbeats more frequently than the configured timeout.
5. After each important verified step, save `last_confirmed_step`, a summary, evidence, and `next_step`. Before an external side effect, record the intended action and how its outcome will be checked. Store compact state, not full conversations, logs, or credentials.
6. Finish only after work has ended, with the actual `exit_code`. Keep the task pending until an independent review has recorded source, diff, tests, and runtime evidence and the result is accepted.

Do not add, remove, or change target declarations during execution. Stop safely and preserve evidence before replanning and claiming a new lease. `local` and `cloud` are unlimited by default; obey any configured capacity. Overlapping writes conflict across pools.

## Interruption and handoff

Lost contact does not mean termination. An `interrupted` task may still reserve its targets. Inspect `reservation_active`; do not automatically replace the writer or repeat unknown side effects.

Release or requeue requires `stop_confirmed=true` and actual `stop_evidence`; requeue also requires `resume_evidence`. A verified live original process causes rejection. If native checks cannot confirm termination, the operator must investigate and explicitly confirm stopping or clearing the reservation. Do not treat a timeout as stop evidence.

Reuse the same task ID in a handoff. State the last verified step, confirmed termination or continuing reservations, unknown side effects, the next action, and evidence locations. An old lease cannot update or release a new worker's reservations.

For an interrupted review, retain its checkpoint and verify that the old reviewer run stopped before `review_resume`; resumption rotates the review lease and requires binding a new run. A timeout or self-reported ID is not stop proof.

See the [protocol](references/protocol.md). This skill supports cooperation on one host within one user's trust boundary. `actor` is not account authentication, resource declarations are not an enforced sandbox, and platform permissions remain unchanged.

## Silent delegation

For background work with periodic evidence-based supervision, use the optional [silent-delegation Skill](skills/silent-delegation/SKILL.md) and [integration guide](references/delegation.md). Quiet execution keeps verbose logs local; it does not cancel supervision. The number of agents is independent of configured execution-pool capacity.
