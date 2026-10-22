"""Installable CLI: explicit initialization, client config generation, and stdio serving."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from shared_task_state import state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', help='Existing shared configuration; defaults to SHARED_TASK_STATE_CONFIG or platform layout')
    parser.add_argument('--state-id', help='Expected shared state UUID; recommended for client bindings')
    parser.add_argument('--read-only', action='store_true', help='Omit the mutation tool from the MCP catalog')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('serve', help='Run the stdio server (default)')
    init = sub.add_parser('init', help='Explicitly bootstrap once, or verify the existing state')
    init.add_argument('--root')
    init.add_argument('--exchange')
    init.add_argument('--local-capacity', type=int)
    client = sub.add_parser('client-config', help='Print configuration; does not edit any client settings')
    client.add_argument('--client', choices=['mcp', 'antigravity', 'hermes'], default='mcp')
    supervise = sub.add_parser('supervise', help='Read a compact supervision snapshot')
    supervise.add_argument('--task-id', action='append', required=True)
    supervise.add_argument('--after-seq', type=int, default=0)
    runner = sub.add_parser('run', help='Run an explicit command quietly under a task lease')
    runner.add_argument('--task-id', required=True)
    runner.add_argument('--actor', required=True)
    runner.add_argument('--executor', default='command')
    runner.add_argument('--cwd')
    runner.add_argument('--heartbeat-seconds', type=float)
    runner.add_argument('--run-id')
    runner.add_argument('argv', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        config = Path(args.config).expanduser().resolve() if args.config else state.default_config()
        if args.command == 'init':
            result = (state.join(config, args.state_id) if args.state_id else
                      state.bootstrap(config, args.root, args.exchange, args.local_capacity))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == 'client-config':
            joined = state.join(config, args.state_id)
            entry = {'command': sys.executable, 'args': ['-m', 'shared_task_state_mcp',
                     '--config', str(config.resolve()), '--state-id', joined['state_id']]}
            if args.read_only:
                entry['args'].append('--read-only')
            if args.client == 'hermes':
                print('mcp_servers:\n  shared-task-state:\n    command: ' + json.dumps(entry['command']) +
                      '\n    args: ' + json.dumps(entry['args']))
            else:
                print(json.dumps({'mcpServers': {'shared-task-state': entry}}, ensure_ascii=False, indent=2))
        elif args.command == 'run':
            if args.read_only:
                raise state.StateError('read-only mode cannot launch a worker')
            state.join(config, args.state_id)
            from .runner import run, exit_status
            command = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
            result = run(config, args.task_id, args.actor, command, args.cwd,
                         args.executor, args.heartbeat_seconds, args.run_id,
                         expected_state_id=args.state_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return exit_status(result)
        elif args.command == 'supervise':
            from .backend import Backend
            print(json.dumps(Backend(config, args.state_id).supervise(args.task_id, args.after_seq),
                             ensure_ascii=False, indent=2))
        else:
            from .server import build_server
            build_server(config, args.state_id, args.read_only).run(transport='stdio')
        return 0
    except (state.StateError, OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
        print('Shared Task State is unavailable or rejected the configuration. '
              'Join the existing shared state, or explicitly run init once for a new installation. '
              'Check the state identity, storage access and privacy scanner; no replacement state was created by serve.',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
