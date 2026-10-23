# Evidence that tells you what was checked

StillRelay 0.3 keeps a reference, a file observation and a source-backed claim distinct. A worker finishing successfully still means `worker_done`, not independent acceptance.

## File references and local checks

`fields.artifacts` remains an array of reference strings. It grants no permission to open them. A missing local path or remote reference in this array alone is **not checked** and does not prevent finishing. Read `unverified_artifacts` alongside it; only entries with matching `artifact_evidence` have a recorded local observation. A server-written report is a stored report of the worker's words, not verification of those words.

To require a local file check, an administrator first adds `artifact_roots` to the existing configuration (not a task request):

```json
{"artifact_roots": ["/absolute/project/output"]}
```

This is an optional configuration key, not a new configuration schema or database. Omit it to disable local artifact reads. Choose a narrow output directory, never a home directory or credential store. It does not change OS permissions or provide a sandbox against other programs running as the same user.

Before relying on checks over MCP, call `shared_task_status`: require `evidence_contract_version=1` and `artifact_checks_enabled=true`. An older or private adapter without these capabilities must not be assumed to enforce this contract. Update all clients that write facts before mixing versions; no automatic Host rollout is included.

On `finish`, submit top-level `artifact_checks`, for example:

```json
{"artifact_checks": [{"path": "/absolute/project/output/result.txt", "sha256": "<64 lowercase hex characters>"}]}
```

The core opens regular files only, within the configured roots, rejects symlink paths, and reads at most 64 MiB total per request (at most 100 checks). It calculates SHA-256 and compares it with the supplied digest. Missing, unreadable, changed-during-check, out-of-scope or mismatched files reject the entire finish without consuming its lease. The request does not grant additional read access. Checks cannot be sent through update/create/plan and silently ignored.

Use canonical paths (for example, Python's `str(Path(path).resolve())`): on macOS `/tmp` itself is a symlink to `/private/tmp`, so the raw `/tmp/...` spelling is rejected too.

A successful observation records the path, hash, byte count, time, actor, generation and optional run ID in server-owned `artifact_evidence`, and adds its reference to `artifacts`. A replay of the identical committed request returns its original receipt without rereading files. A new request rechecks them. File metadata comparisons detect ordinary concurrent changes; they are not a hostile-filesystem security boundary. Files remain mutable afterwards. Save immutable copies separately when later reproduction is required.

## Accumulation and corrections

`verified_facts`, `failed_attempts`, `artifacts` and `backups` accumulate on **create/update/finish/plan**. Sending `[]` does not erase them. Strings merge by exact value and facts by ID; same-ID changes are rejected. `blockers` and `constraints` remain replaceable current descriptions. The final merged editable fields and evidence must fit 32 KiB, with at most 100 entries per list; entries are never silently truncated to fit.

Correct an accumulated entry with a top-level retraction on update, finish or plan:

```json
{"retractions": [{"field": "failed_attempts", "value": "old incorrect record", "reason": "This referred to a different task"}]}
```

Use the original string as `value`, or the fact ID for `verified_facts`. The server checks that the target exists and records who withdrew it, why and when. Originals remain for audit. Consumers must treat retracted entries as withdrawn, even if they remain in an array or a file-verification record. Submit a replacement fact under a new ID; reusing an old ID does not undo a withdrawal.

## Source-backed facts

New writes to `verified_facts` require objects, not unsourced strings:

```json
{"verified_facts": [{"id": "check-1", "statement": "The fixture returned the expected output", "method": "Compare the recorded output with the fixture's expected value", "sources": [{"kind": "event", "seq": 12}]}]}
```

An event source must already exist in **the same task**. An artifact source uses `{"kind":"artifact","path":"/absolute/project/output/result.txt","sha256":"<64 lowercase hex characters>"}` and must match recorded local artifact evidence, including checks performed earlier in that finish. Past file observations retain their timestamp and generation; citing one does not recheck the current bytes.

The server adds `status=reported_with_source`, `reported_by`, `reported_at`, `generation` and `run_id`. Clients cannot set these stamps on a new fact. The source's existence and linkage are checked; the truth of the statement and the claimed checking method are **not** automatically established. Referencing your own earlier event proves only that the event was recorded. A controller must inspect the actual evidence before accepting work.

## Upgrade without rewriting old evidence

0.3 changes the **fact write contract**: update clients that submit string facts. They can move an unverified observation into `summary`, or submit a sourced object. MCP command names and client connection settings stay the same. CLI and MCP use the same rules.

Old fact strings are returned as `legacy_unverified` objects with deterministic IDs and empty sources/method. Reads do not rewrite database rows. Later task updates preserve that classification; history reads annotate legacy snapshots without rewriting stored events. Old committed receipts keep their original replay payload. Read current task/history for the annotated view. A previously read fact object may be resubmitted unchanged; it cannot be used to forge new server stamps. No database replacement, automatic acceptance or production Host migration occurs.

## Exit codes and existing delegation choices

Task `exit_code` is a worker report; `exit_code_trust` says so, including for old tasks. The bundled foreground runner obtains its direct child's code with `subprocess.wait` and labels its **local result** `exit_code_source=subprocess_wait`. This is not an authenticated remote attestation, process-tree proof, or a correctness check. `accept` requires a different actor and nonempty source/diff/tests/runtime statements; the core does not execute those checks. Actor names are collaboration labels under one user's trust boundary.

No AST signature checker, additional heartbeat thread or automatic worktree was added. The runner already owns serialized heartbeats; they mean contact, not progress. `acquire_more` remains deferred: retaining one reservation while waiting for another can create circular waits. For now, stop safely, release with evidence, use plan to redeclare the complete resource set, then requeue and claim; all targets are acquired together or none are.
