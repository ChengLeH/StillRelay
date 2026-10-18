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
