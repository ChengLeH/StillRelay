---
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-plan-bootstrap
execution: code
date: 2026-10-22
title: "feat: StillRelay portable silent delegation"
---
# StillRelay: quiet execution, shared progress

## Goal Capsule
Objective: Cooperating agents can delegate background work, inspect compact shared progress at intervals, and resume or review the exact run without replaying its verbose history.
Means: Existing task leases plus optional run binding, compact supervision, and an opt-in command runner.
Authority: User authorizes implementation, publication as a GitHub branch/PR, rebranding, and optional backward-compatible state-core fields. No merge authority. Preserve existing install names, Host database, privacy rules and local runtime capacity.
Execution profile: native implementation; subsequent subagents use gpt-6-luna, max effort. Controller owns verification and commits.
Stop conditions: evidence of incompatible persisted state, live Host migration, or unapproved runtime changes.

---
## Product Contract
### Summary
Brand the project StillRelay (formerly Shared Task State) and add portable silent delegation. Roles belong to tasks, not models. Quiet output and periodic evidence-based supervision coexist.
### Problem Frame
The local harness already runs quietly, but public users lack a portable lifecycle bridge and a consistent compact supervision view. Three agents must cooperate without a hard-coded three-agent ceiling or duplicated narrative state.
### Requirements
- R1. Preserve current CLI/MCP and state identity; no migration or production state changes. Keep existing reservation, revision, history and projection semantics.
- R2. Optional task delegation metadata links controller, executor and exact run. Existing tasks work unchanged. Controller/worker roles can reverse on separate tasks.
- R3. A readonly multi-task supervision query returns one consistent DB snapshot, compact task/run identity, heartbeat freshness, reported progress and review needs. Heartbeats are not evidence of progress or correctness. No automatic scheduling, model selection, hidden checks or acceptance.
- R4. An opt-in command wrapper claims/binds a task, starts explicit argv without a shell, keeps logs local, sends serialized heartbeats and records actual completion. It must not release reservations while a child might still be running. External agent tools and permissions remain the operator's responsibility.
- R5. Agent count is not fixed. Use existing configurable pools. Validate three, six and seven participants in parametrized cohorts with disposable command workers; never raise production model capacity for a test.
- R6. A portable Skill describes interval checks, role reversal, intervention, exact bindings, reattachment and independent acceptance. Mac-specific guides use the existing guarded path; do not publish personal paths or private Host implementation.
- R7. Improve English and zh-TW README with a clear hook, architecture/use case, quickstart and original cute flat-illustration hero image. Display name may change; package, commands, IDs and repository URL remain compatible.
### Scope Boundaries
No state-retention redesign, source-file multiwriter or projection overhaul, automatic merge, public network service, agent authentication, model calls, production Gate restart or local capacity change. No daemon or timer that pretends to wake inactive chat clients. Considered and not built: arbitrary command launching through MCP, because explicit local command execution is sufficient and preserves the current tool boundary.

---
## Planning Contract
- KTD1. Optional metadata and binding extend existing task transactions and receipts. No second progress database. Bind only for current owner/revision and preserve controller identity.
- KTD2. Read supervision from a single readonly transaction; external status/log paths are references, never automatically read or executed. A cursor marks observed events, not verified correctness.
- KTD3. Foreground wrapper is intended to run under an existing background executor. Heartbeat/update requests serialize with fresh revisions; identity is pinned. Unknown child outcome retains reservation.
- KTD4. No fixed participant cap. Batch size is a response limit, separate from execution capacity. Existing pool configuration remains authoritative.
- KTD5. Public core schema3 and private Host schema2 are distinct. This release is additive public packaging plus Mac integration instructions; no repointing private services.
### Run-binding interface
The existing request protocol gains `op=bind`, using normal `task_id`, `actor`, `expected_revision`, `lease_id`, and `execution={executor,run_id,status_ref?,report_ref?}`. The task stores optional `delegation={controller,worker,generation,executor,run_id,status_ref?,report_ref?}`; controller is derived from `created_by`, worker/generation from current ownership. Identifiers use existing safe-ID/privacy validation; metadata is bounded to 4 KiB. References are bounded strings, never read or executed by state tools. One binding per lease: identical content is stable, different content is rejected; a fresh claim clears current binding while history retains the previous generation. Existing tasks need no rewrite.

Supervision reads accept explicit task IDs (bounded response batch, not an agent limit) plus event cursor. CLI adds `supervise` and `run`. The portable runner module provides `run(config, task_id, actor, command, cwd=None, executor="command", heartbeat_seconds=None, run_id=None)`, returning a structured result. It does not spawn a detached daemon itself; users invoke it through their existing background executor. Abnormal interruptions retain reservations when descendant termination cannot be proven. Wrapped commands must remain foreground for their lifecycle; detached child work needs its own executor adapter.

### Evidence
Existing patterns: scripts/state.py Store.apply/save/revision/owner; shared_task_state_mcp/backend.py store(readonly); existing installed stdio tests. Native Mac harness is background/nohup plus local verbose.log and broker reconciliation. SQLite WAL keeps readers on snapshots while serializing writers (https://www.sqlite.org/wal.html). Git worktrees isolate source edits (https://git-scm.com/docs/git-worktree.html).
### Assumptions
Three-agent support means tested coordination and command execution, not three simultaneous model inferences. StillRelay is a product display name; existing public identifiers remain unchanged.

---
## Implementation Units
### U1. Task/run binding and compact supervision
Goal: satisfy R1-R3 and R5 with backward-compatible APIs.
Dependencies: none.
Files: scripts/state.py, shared_task_state_mcp/backend.py, shared_task_state_mcp/server.py, shared_task_state_mcp/__main__.py, tests/test_delegation.py, mcp_tests/test_supervision.py.
Approach: validate optional delegation metadata; add owner-checked idempotent bind operation using existing request protocol. Add bounded multi-task snapshot query and CLI supervision read. Do not change retention, existing locks or capacity defaults.
Patterns: existing transaction, owner/revision/privacy checks, read-only backend and bounded MCP tools.
Test scenarios: legacy tasks unchanged; stale/old-owner binding rejected; controller identity preserved; identical replay stable; no writes on supervision; heartbeat freshness distinct from last reported progress; three controllers/workers with reversed roles; six and seven participants with configured capacity; consistent cursor.
Execution note: observe failing behavior tests before production edits.
Verification: core and MCP regression suites plus actual stdio three-client roundtrip.

### U2. Portable quiet command lifecycle
Goal: satisfy R4-R5 without prescribing a model or provider.
Dependencies: U1 API contract. May author isolated module against frozen packet before U1 integration.
Files: shared_task_state_mcp/runner.py, mcp_tests/test_runner.py. CLI dispatch is owned by controller after integration.
Approach: explicit argv/cwd/config/task/actor, generated stable run ID and local log paths; claim then bind before child starts; periodic heartbeat with fresh revisions; finish with actual exit code; if signalled terminate/reap child before finish, otherwise retain reservation. Never include full logs in response or accept task automatically. No shell string.
Test scenarios: successful command output remains local; nonzero exit; no child launched when claim/bind fails; known prelaunch bind failure releases only its own claim with explicit no-child stop evidence; ambiguous outcomes retain the reservation; sustained heartbeat; revision conflict does not blindly replay stale fields; interruption and child-stop evidence; configurable six- and seven-participant cohorts.
Verification: subprocess tests with Python fixtures and no model requests.

### U3. Portable Skill, brand and release documentation
Goal: satisfy R6-R7.
Dependencies: final U1-U2 interfaces.
Files: SKILL.md, SKILL.zh-TW.md, references/delegation.md, references/delegation.zh-TW.md, skills/silent-delegation/SKILL.md, README.md, README.zh-TW.md, assets/stillrelay-hero.png, pyproject.toml, shared_task_state_mcp/__init__.py, .github/workflows/tests.yml.
Approach: optional skill and natural-language setup guidance, generic capability contract and Mac adapter guide, supported-versus-unverified matrix, version0.2.0, original hero and factual marketing. Document rollback by prior pinned revision, no DB downgrade needed for optional fields. Exclude AppleDouble files.
Verification: links, installed command examples, package assets, original image visual inspection. No behavior-claim marketing without test evidence. Model-based prose evals require explicit invocation; if not run record that limit.

---
## Verification Contract
Run core unittest discovery and MCP unittest discovery against an installed wheel from outside the source checkout. Add real stdio clients A/B/C sharing one temporary config; create A→B, B→A, A→C, bind runs, execute fixture workers, read supervision/history, finish and independently accept. Parameterize agent cohorts at six and seven participants with separate resources/pools. Test conflicts and unknown outcomes; existing state snapshots remain unchanged. Use local fixtures only. CI should cover existing OS/Python matrix. Inspect diff and independent review before normal branch push/PR.

---
## Definition of Done
The approved flat illustration replaces the initial realistic concept. A supplementary controlled two-participant experiment measures actual observed supervisor/tool transcripts in verbose versus quiet mode on identical work. Report tokenizer, raw payloads, checkpoint cadence and workload size; do not claim whole-agent or billing savings without provider usage evidence. Use no paid model requests by default because the user has limited quota.

Implemented units have concrete verification, installed CLI and MCP work, old commands remain, no writes to existing private production state during implementation, no unowned files, original banner is in repo, README makes accurate claims, changes pushed in open PR with CI decided and rollback documented. Remove abandoned probes from the shipped diff. No automatic merge.
