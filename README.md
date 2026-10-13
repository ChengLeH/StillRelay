[English](README.md) | [繁體中文](README.zh-TW.md)

# Shared Task State

A portable skill for cooperating agents to share task identity, verified progress, and resource reservations on one host. Agents maintain local state directly; no dispatcher, Slack, MCP, or platform hooks are required. The helper uses Python 3.10+ and the standard library.

Install the **entire `shared-task-state` folder** in a skill directory supported by your platform. Copying only `SKILL.md` omits the helper. Commands below are relative to that folder; agents should use the helper's full installed path when working elsewhere.

## Start with the primary agent

1. Verify that participants can access the same configuration, database, and exchange directory. Check each agent's actual read, write, and execution permissions.
2. The primary agent runs `python3 scripts/state.py bootstrap` once. Other agents use `python3 scripts/state.py join` to validate and join that existing state. Do not initialize a database per agent.
3. Run `python3 scripts/state.py doctor` to check that configuration, database, and exchange share the same `state_id`. For file-only agents, also verify that a consumer actually processes their inbox requests.
4. Follow [SKILL.md](SKILL.md). Continue by exact task ID and save verified results and the next action.

Agents with command execution use `request --file <json>` to commit a transaction and export updated projections directly. **No daemon is needed.** File-only agents write to `inbox/` and read `receipts/`; an execution-capable agent must call `tick`, or an optional `watch` process can consume requests continuously. Installing skill instructions alone does not process requests.

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

The 45-test suite passes locally on macOS and on GitHub-hosted macOS, Linux, and Windows runners with Python 3.10 and 3.14. These checks cover the helper; they do not establish integration with every agent platform or user permission setup. Cross-host use and network filesystems are outside the first release's verified scope. See the [automated test results](https://github.com/ChengLeH/shared-task-state/actions/workflows/tests.yml). See the [protocol](references/protocol.md) for request formats.

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
