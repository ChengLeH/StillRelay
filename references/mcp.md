[English](mcp.md) | [繁體中文](mcp.zh-TW.md)

# Install and connect the MCP server

The optional `shared-task-state-mcp` package runs on the same host as your task state. Each MCP client launches its own stdio process; the processes share one configured SQLite database and the existing task protocol. No account, API key, network listener, dispatcher, or proprietary Host installation is needed.

Track this setup as one local work item. First make only the minimum configuration/overview lookup needed to find the existing state and task; create or reuse the task, then immediately record that lookup as its first checkpoint. For a first-time installation, do only what is needed to make shared state available, then create the first task and record the actual setup actions and results, including the `init` or bootstrap path, before further local work. Continue the same task through client configuration, reload, and status verification; do not create a separate task just to test the connection.

## Install

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/getting-started/installation/). On macOS, Linux, or Windows:

```sh
uv tool install 'git+https://github.com/ChengLeH/StillRelay@main'
```

If the commands are not found, run `uv tool update-shell` and open a new terminal. Alternatively, install this repository in a persistent virtual environment with `python -m pip install .`. This package is distributed from GitHub; these instructions do not assume a PyPI release. Replace `main` with an audited commit SHA to pin an installation. Upgrade an existing uv installation with `uv tool upgrade shared-task-state-mcp`.

The wheel contains the portable state core, privacy guard and MCP entry. Existing source-based skill installations do not need the SDK. Installing this package does not register a client or start a service.

## Choose the existing state, or initialize once

For an existing installation, use the same absolute config path as its other agents:

```sh
shared-task-state-mcp --config '/absolute/path/config.json' client-config
```

Missing, incompatible or mismatched state is rejected. The MCP server never initializes, migrates or replaces a database. If your existing installation uses a different schema, keep its matching adapter until you explicitly migrate; do not point this package at a private Host database with a different schema.

For a **new** installation, explicitly initialize once:

```sh
shared-task-state-mcp init
```

This uses the same [platform defaults](../README.md#bootstrap-and-join) as the CLI. To choose custom locations:

```sh
shared-task-state-mcp --config '/absolute/path/config.json' init --root '/absolute/path/data' --exchange '/absolute/path/exchange'
```

`init` verifies an existing installation rather than overwriting it. All agents must use the same config/database/exchange, with permission to access them. A container or cloud client does not gain access to your Mac through stdio; it needs an existing authorized transport or a separately authenticated remote MCP service. This package does not provide an HTTP endpoint.

## Antigravity

```sh
shared-task-state-mcp client-config --client antigravity
```

For a custom config path, put `--config` before `client-config`. Merge the generated `mcpServers.shared-task-state` entry into Antigravity's existing MCP configuration. It uses the installed interpreter's absolute path, `-m shared_task_state_mcp`, the absolute state config and its pinned state UUID. Preserve other entries.

The [official Antigravity documentation](https://antigravity.google/docs/mcp) describes **MCP Servers → Manage MCP Servers → View raw config** in the IDE, or `/mcp` in the CLI. Current global settings use `~/.gemini/config/mcp_config.json`; workspace settings use `.agents/mcp_config.json`. Reload the server and ask it to call `shared_task_status`. The result must show the expected state UUID. Client settings paths can change; use the client's settings UI if its installed version differs.

Generated settings contain local paths; keep them out of Git. Tool approval settings remain the client's responsibility. To expose only reads, generate a server configuration that omits the mutation tool:

```sh
shared-task-state-mcp --read-only client-config --client antigravity
```

## Hermes

```sh
shared-task-state-mcp client-config --client hermes
```

Merge the generated `mcp_servers.shared-task-state` entry into the active Hermes profile's `config.yaml` (the default profile uses `~/.hermes/config.yaml`). Run `hermes mcp test shared-task-state`, then `/reload-mcp` or restart the relevant agent. Check that `shared_task_status` returns the expected state UUID. See the [official Hermes MCP documentation](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/mcp.md). This is a custom MCP entry; it does not need admission to the Hermes catalog.

## Direct uvx connection and other clients

Clients accepting `command` and `args` can use the generated standard configuration. For a transient runner, after explicitly initializing or joining state, this entry downloads and runs the package without a permanent tool installation:

```json
{
  "mcpServers": {
    "shared-task-state": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/ChengLeH/StillRelay@main", "shared-task-state-mcp"]
    }
  }
}
```

This uses the shared platform-default config or `SHARED_TASK_STATE_CONFIG`. Append `--config` and its absolute path, plus `--state-id` and the expected UUID, when selecting a particular existing state. If a graphical client cannot locate `uvx`, use its absolute executable path. Pin the Git ref for reproducibility. Generate absolute interpreter bindings only from a **persistent** uv tool/pip/virtual-environment install; an interpreter path printed from a transient uvx cache can disappear when that cache is cleaned.

## Tools and retry semantics

| Tool | Effect |
| --- | --- |
| `shared_task_status` | State UUID, event sequence, task count, scanner availability, and `review_contract_version`/`review_capabilities`; read-only |
| `shared_task_overview` | Task summaries, paged by `after_task_id`, `limit` 1–100 |
| `shared_task_read` | Exact task ID, latest revision and lease |
| `shared_task_history` | Events paged by `after_seq`, `limit` 1–100 |
| `shared_task_supervise` | Read a compact, consistent snapshot of selected tasks and distinguish heartbeat contact from reported progress |
| `shared_task_apply` | Shared create/plan/claim/update/finish/review/requeue/accept protocol; same as CLI requests |

The reads use read-only transactions and never reconcile expired heartbeats. They cannot prove a worker has stopped. Read results are bounded to 2 MiB; reduce the page size for large history. Writes use the same [`request` protocol](protocol.md), optimistic revisions, leases, resource reservations, required review for new tasks, and privacy checks as the CLI. `review_contract_version` advertises the lifecycle contract; `review_capabilities` lists supported operations. MCP does not accept process-ID binding. The server records review run metadata but cannot launch a fresh sub-agent or prove its isolation; use the host's actual native launch capability and leave the task pending if none is available.

`shared_task_apply` returns a durable `receipt`. `committed=true` means the request is saved, even when `ok=false` and `projection_updated=false` after an export failure. Retry with the **same request_id and identical original content** to finish projection; never create a new request to repeat an unknown write. Changed payload under the same request ID is rejected. After transport interruption or an ambiguous error, reread the exact task and check the outcome before retrying. MCP cancellation cannot roll back a request already committing in its worker thread.

The package retains the core credential guard and any configured offline `privacy_scanner`; scanner failure blocks access. It does not install Betterleaks, Headroom or a network authentication layer. Those belong to an administrator's optional surrounding pipeline. `actor` is not authentication; agents using the same OS account have the same account's filesystem permissions. No remote exposure is enabled.

## Ask an agent to configure it

Give a local agent with installation and settings-write permissions this prompt:

> Set this up as one tracked local work item. First make only the minimum configuration/overview lookup needed to find the existing shared state and exact task; create or reuse that task and immediately record the lookup as its first checkpoint. If no shared state exists, ask before initializing it. For a new installation, do only the setup required to make the state available; then create the first task and immediately record the actual installation/configuration actions and results, including whether `init` or another bootstrap path was used. Continue under the same task while installing the MCP, generating and merging only the server entry, reloading MCP, and verifying `shared_task_status` against the expected state UUID. Do not create a separate task solely as a connection test. Keep local config, databases, credentials, and backups out of Git.

This is an instruction for an agent that can edit local settings, not a claim of a built-in natural-language installer or marketplace listing. The automated acceptance test covers the installed package and actual MCP transport; Antigravity/Hermes application UI sessions require validation on the user's installed clients.

## Remove or recover

Disable or remove this server's client entry, reload the client, then run `uv tool uninstall shared-task-state-mcp` if desired. Your task config, database and exchange remain. Reinstall a previous pinned Git revision to roll back package code; use a version compatible with your state schema. Initialization and config generation do not edit client settings, and no background service needs stopping beyond the client-owned stdio process.

## StillRelay silent delegation

StillRelay is the display name of Shared Task State. Existing commands, package and server bindings remain unchanged. The optional run binding and readonly supervision tool are described in the [delegation guide](delegation.md). Connecting MCP alone does not start workers or install a timer.
