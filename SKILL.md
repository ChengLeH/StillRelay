---
name: shared-task-state
license: MIT
description: Maintain one shared task record, reserve declared resources, and resume verified work across cooperating agents on one host.
---

[English](SKILL.md) | [繁體中文](SKILL.zh-TW.md)

# Shared Task State

Maintain one shared progress record so the next agent can continue from the last verified step. An update is complete only after a successful transaction or receipt. Worker completion is separate from controller acceptance.

## Verify the shared location

Use `--config` or `SHARED_TASK_STATE_CONFIG` to join existing state. Only the primary agent bootstraps the first state; do not recreate or redirect existing configuration. Run `join` and `doctor` to check identity and access. If the shared location is inaccessible, report the missing capability rather than initializing another database. See [README.md](README.md) for setup.

Prefer `scripts/state.py request --stdin` without saving shared raw input. File requests are disabled by default. File-only agents need an input-validating gateway; migrate existing inbox users before upgrading. Never use credentials as identity or resource metadata. Configured scanner failures stop persistence. See the README Credentials and safe submission section.

## Daily workflow

1. Read the overview first, then the exact task ID when details are needed. Check generation time, revision, and the last verified step; do not infer identity from similar titles.
2. Read the goal, constraints, verified facts, failed approaches, next action, and evidence locations. Recheck only drift that matters to the next step, not completed investigations.
3. Declare `execution_pool` and `resources` when creating or planning work. If precise targets are unknown, reserve `unscoped/write` conservatively. Use `read_only=true` and `resources=[]` only when there are genuinely no write side effects or specific reserved targets.
4. Claim before executing. Updates require the latest `expected_revision`, the current `lease_id`, and the same `actor`. Reread after conflicts; never force an overwrite. Send heartbeats more frequently than the configured timeout.
5. After each important verified step, save `last_confirmed_step`, a summary, evidence, and `next_step`. Before an external side effect, record the intended action and how its outcome will be checked. Store compact state, not full conversations, logs, or credentials.
6. Finish only after work has ended, with the actual `exit_code`. A different controller checks source, changes, tests, and observed behavior before accepting.

Do not add, remove, or change target declarations during execution. Stop safely and preserve evidence before replanning and claiming a new lease. `local` and `cloud` are unlimited by default; obey any configured capacity. Overlapping writes conflict across pools.

## Interruption and handoff

Lost contact does not mean termination. An `interrupted` task may still reserve its targets. Inspect `reservation_active`; do not automatically replace the writer or repeat unknown side effects.

Release or requeue requires `stop_confirmed=true` and actual `stop_evidence`; requeue also requires `resume_evidence`. A verified live original process causes rejection. If native checks cannot confirm termination, the operator must investigate and explicitly confirm stopping or clearing the reservation. Do not treat a timeout as stop evidence.

Reuse the same task ID in a handoff. State the last verified step, confirmed termination or continuing reservations, unknown side effects, the next action, and evidence locations. An old lease cannot update or release a new worker's reservations.

See the [protocol](references/protocol.md). This skill supports cooperation on one host within one user's trust boundary. `actor` is not account authentication, resource declarations are not an enforced sandbox, and platform permissions remain unchanged.

## Silent delegation

For background work with periodic evidence-based supervision, use the optional [silent-delegation Skill](skills/silent-delegation/SKILL.md) and [integration guide](references/delegation.md). Quiet execution keeps verbose logs local; it does not cancel supervision. The number of agents is independent of configured execution-pool capacity.
