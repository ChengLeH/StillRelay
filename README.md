[English](README.md) | [繁體中文](README.zh-TW.md)

# StillRelay

![StillRelay: three little agents passing tasks around one shared notebook](assets/stillrelay-hero.png)

**Little agents. Shared progress.**

One finds the clue. One builds the fix. One checks the work. Let each do its thing—without passing the entire transcript around.

StillRelay gives cooperating agents one durable place for tasks, progress and evidence. Workers run quietly. Delegators check in at intervals, inspect what changed, and step in when needed. A new chat can pick up the same task instead of starting the story again.

> Formerly **Shared Task State**. Existing package names, commands and MCP bindings stay compatible. Three characters illustrate the workflow; they are not an agent limit.

- **Quiet work, visible progress.** Keep verbose logs with the executor. Share confirmed steps, blockers and the next action.
- **Roles travel with tasks.** A can delegate to B while working on a different task from B.
- **Bring your own crew.** Configure execution-pool capacity for your hardware. Conflicting writes still wait their turn.
- **Done is not accepted.** A worker hands over evidence; another controller checks it.

[Connect through MCP](references/mcp.md) · [Silent delegation guide](references/delegation.md) · [Optional delegation Skill](skills/silent-delegation/SKILL.md)

**0.3 evidence update:** cumulative records no longer disappear when a client sends a shorter list. New fact writes need sources and a checking method; old facts stay visibly unverified. Optional local artifact checks reject missing files or hash mismatches inside configured output directories. Plain references remain unverified. [Contract, upgrade notes and trust limits](references/evidence.md).

## Less log reading, measured

We ran nine local trials on 0.2 with two MCP clients and three command phases per task. Quiet supervision kept the same state checks and skipped reading the worker's full log after each phase. These figures have not been rerun for the 0.3 evidence format.

| Output per phase | Verbose → quiet payload tokens, mean | Reduction |
| --- | ---: | ---: |
| 5 lines | 3,179 → 2,822 | 11.23% |
| 50 lines | 5,359 → 2,842 | 46.97% |
| 500 lines | 26,938 → 2,824 | 89.52% |

These are **supervisor-visible tool payloads**, counted with `o200k_base`, three trials per case. They are not total model-token or billing savings: no model inference was used, worker traffic is excluded, and results depend on log volume and check frequency. [Method and rerun instructions](benchmarks/README.md) · [Results and raw transcripts](benchmarks/results/2026-10-23/summary.md)

## Connect through MCP

For MCP-capable clients such as Antigravity or Hermes, install the optional local server from this repository using [uv](https://docs.astral.sh/uv/guides/tools/):

```sh
uv tool install 'git+https://github.com/ChengLeH/StillRelay@main'
shared-task-state-mcp init
shared-task-state-mcp client-config --client antigravity
```

Run `init` once for a **new** installation. If you already have task state, select its configuration with `--config /absolute/path/config.json` before the subcommand; do not create a second state. Merge the generated server entry into your client's existing settings, then reload MCP. Use `--client hermes` for Hermes. The generated binding includes the existing state UUID and the installed runtime's absolute path; no hand-written Python paths are needed.

The server uses stdio, exposes structured task and supervision tools, and opens no network port. Every client accesses the same configured database. `--read-only` removes the mutation tool entirely. This MCP installation adds the official Python MCP SDK; the standalone skill/CLI below remains standard-library-only. See the [MCP installation guide, client examples, and copyable agent prompt](references/mcp.md).

## Start with the primary agent

1. Verify that participants can access the same configuration, database, and exchange directory. Check each agent's actual read, write, and execution permissions.
2. The primary agent runs `python3 scripts/state.py bootstrap` once. Other agents use `python3 scripts/state.py join` to validate and join that existing state. Do not initialize a database per agent.
3. Run `python3 scripts/state.py doctor` to check that configuration, database, and exchange share the same `state_id`. File-only agents need an input-validating tool or gateway before writing shared files.
4. Follow [SKILL.md](SKILL.md). Continue by exact task ID and save verified results and the next action.

Agents with command execution use `request --stdin` to commit and export directly. **No daemon is needed.** The shared file channel is disabled by default; see Keep secrets out of the shared notebook below for requirements and migration.

## Bootstrap and join

Select the common configuration with `--config <path>` **before the subcommand**, or with `SHARED_TASK_STATE_CONFIG`. Otherwise, platform defaults apply:

| Platform | Configuration | Default data root |
| --- | --- | --- |
| macOS | `~/Library/Application Support/shared-task-state/config.json` | `data/` beside the configuration |
| Linux | `$XDG_CONFIG_HOME/shared-task-state/config.json`, otherwise `~/.config/shared-task-state/config.json` | `$XDG_DATA_HOME/shared-task-state`, otherwise `~/.local/share/shared-task-state` |
| Windows | `%LOCALAPPDATA%/shared-task-state/config.json`, otherwise `~/AppData/Local/shared-task-state/config.json` | `data/` beside the configuration |

On the first bootstrap, `--root` and `--exchange` can specify explicit locations. Once configuration exists, bootstrap cannot redirect it elsewhere. Configuration, database, and the exchange directory's `state.json` share one UUID. A file lock and pending manifest protect initialization against concurrent attempts and interruptions. `join` validates existing state without creating a database. Older formats require an explicit migration; the helper does not silently create a replacement state.

```text
python3 scripts/state.py bootstrap
python3 scripts/state.py join
python3 scripts/state.py doctor
python3 scripts/state.py overview
python3 scripts/state.py read <task_id>
python3 scripts/state.py request --file <request.json>
python3 scripts/state.py history <task_id>
```

On Windows, replace `python3` with `py -3` when using the Python launcher:

```text
py -3 scripts/state.py bootstrap
py -3 scripts/state.py join
```

Give every agent the same configuration path and the primary agent's state UUID:

```text
python3 scripts/state.py --config "<absolute-common-config-path>" join --state-id "<expected-state-uuid>"
```

If a container or cloud agent cannot see the same home directory, provide the shared location or an existing authorized tool entry point. Report missing access instead of creating a separate state. Bootstrap does not install a background service, change platform settings, or increase permissions.

The `local` and `cloud` execution pools are unlimited by default. To limit local workload, choose `bootstrap --local-capacity 2`; two is an optional setting, not a built-in global cap. Resource conflicts are checked independently: distinct targets may run concurrently, overlapping targets allow only one writer, and readers can share. Use `unscoped` with `write` for conservative reservations when targets cannot be specified; it conflicts with reads and writes on other targets.

## Lost contact and safe continuation

An expired heartbeat means lost contact, not proof of termination. An `interrupted` task retains `reservation_active` and its target reservations so another writer cannot take over prematurely.

Both release and requeue require `stop_confirmed=true` and nonempty `stop_evidence`; requeue also requires `resume_evidence`. A verified live original process causes rejection. When native process checks cannot establish termination, the operator must investigate and explicitly confirm that work has stopped or authorize clearing the reservation. A timeout alone is not evidence. Check unknown external side effects before deciding how to continue; do not replay them automatically.

## Permissions and platform scope

This is a cooperation contract for one host within one user's trust boundary. `actor` identifies a participant, and leases check ownership; neither provides account authentication or an enforced sandbox. The skill preserves platform permissions and cannot prevent programs that bypass its entry points from changing resources.

The test workflow covers the helper and installed MCP transport on macOS, Linux and Windows with Python 3.10 and 3.14. These checks cover the helper; they do not establish integration with every agent platform or user permission setup. Cross-host use and network filesystems are outside the first release's verified scope. See the [automated test results](https://github.com/ChengLeH/StillRelay/actions/workflows/tests.yml). See the [protocol](references/protocol.md) for request formats.

## Tests

From the installed folder or repository root:

```text
python3 -m unittest discover -s tests -p "test_*.py"
```

On Windows, use `py -3 -m unittest discover -s tests -p "test_*.py"`.

## Sharing safely

Share source files only. Keep configuration, task data, databases, exchange directories, credentials, and backups out of the repository. Optional platform integrations should preserve the same state identity and permission boundaries.

## License

[MIT](LICENSE). Copyright holder: ChengLeH.

## Keep secrets out of the shared notebook

Use `python3 scripts/state.py request --stdin` and pass request JSON directly on standard input. This commits and exports synchronously without retaining an incoming request file. `request --file` remains available for an already sanitized, private input file; the caller owns that file and its cleanup. Never copy credentials, environment dumps, full logs, or conversations into task state. Record only their purpose and an approved reference.

The shared write boundary recursively masks known provider tokens, credential assignments, sensitive JSON fields and complete or truncated private-key blocks. Credential-shaped identifiers, source keys, origin and resource metadata are rejected instead of masked, preserving identity and lock behavior. Historical reads, task snapshots, reports, results, and projections pass through the same guard. Request digests use the original input so distinct secret-bearing requests cannot silently share an idempotency receipt.

Optionally configure `privacy_scanner` as an argument array for a trusted offline scanner reading sanitized JSON on stdin. Exit 0 allows it; findings, timeout, missing executable and other failures reject the operation. No scanner is downloaded or required by the portable package. Configure the scanner's network isolation separately; the privacy-scanner launcher strips inherited environment credentials. Unknown or encoded credentials may evade pattern detection, so this is not a credential vault or an access-control boundary.

Shared file requests are **disabled by default**, including configurations that omit `allow_file_requests`. Existing installations that use inbox must switch to direct submission before upgrading. Read-only overview/task access is unchanged. A file-only agent needs a tool or gateway that validates input before writing shared files; merely loading this skill cannot create that capability. Administrators may explicitly set `allow_file_requests: true` for a trusted file channel, but raw requests can be read before consumption and remain after a crash or partial write. Valid processed inputs are deleted only after export and receipt publication; this cleanup does not remove the exposure window. OS file permissions cannot separate agents using the same account.

For a macOS/Linux caller already in a sandbox that cannot launch a nested scanner sandbox, optionally run `scripts/privacy_scan_service.py --socket <local-socket> --serve-config <host-settings.json>` as a trusted host process. The host settings contain `{"scanner": ["<offline-scanner-command>", "<args>"]}`. Set the state's `privacy_scanner` to `["<absolute-python>", "<absolute-privacy_scan_service.py>", "--socket", "<local-socket>"]`. The broker uses only a local Unix socket, never persists or echoes content, bounds input and fails closed when unavailable. The host owns startup and restart; no service is automatically installed. This optional helper is tested on macOS/Linux; Windows uses the direct scanner command. It does not authenticate separate OS users or grant file access.

## A shared notebook, not a secret vault

You do **not** need Betterleaks to get the built-in protection. Before saving, StillRelay masks recognizable credentials: common provider API tokens, passwords and secret-valued fields, Bearer/Basic authorization, private-key blocks, and credentials inside URLs. Nested values, task history and returned state use the same boundary. Sensitive identity or resource metadata is rejected rather than renamed to a shared mask.

That is credential protection, not a promise to recognize every private sentence. **Email addresses, phone numbers, names, addresses and arbitrary personal information are not comprehensively redacted.** Share confirmed progress and evidence locations, not customer records, raw prompts or complete logs. The quiet runner keeps raw output in local files outside the shared exchange; those files can still contain sensitive output. POSIX directory/file permissions are `0700`/`0600`; on Windows, keep the data directory’s ACL private. Choose your own log-retention policy.

Want another layer? Configure an offline `privacy_scanner` adapter for Betterleaks or another scanner. It is optional, never downloaded or enabled behind your back. Once configured, a finding, crash, timeout or unavailable scanner blocks the operation. See [credential handling and scanner setup](#keep-secrets-out-of-the-shared-notebook) and [the delegation guide](references/delegation.md).
