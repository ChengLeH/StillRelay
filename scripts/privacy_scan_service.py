#!/usr/bin/env python3
"""Optional local-only scanner broker for callers already inside a sandbox.

No content is persisted or echoed. Configuration belongs to the trusted host.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
from privacy_guard import guard, PrivacyError

LIMIT = 1048576


def receive(connection):
    chunks = []
    size = 0
    while True:
        part = connection.recv(min(65536, LIMIT + 1 - size))
        if not part:
            break
        size += len(part)
        if size > LIMIT:
            raise PrivacyError('Scanner payload too large.')
        chunks.append(part)
    return json.loads(b''.join(chunks).decode('utf-8'))


def scan_client(address, value):
    payload = json.dumps(value, ensure_ascii=False).encode('utf-8')
    if len(payload) > LIMIT:
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(40)
        connection.connect(address)
        connection.sendall(payload)
        connection.shutdown(socket.SHUT_WR)
        return connection.recv(16) == b'OK\n'


def serve(address, scanner):
    path = Path(address)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        # Only remove a stale socket after proving that no service answers.
        import stat
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise PrivacyError('Socket destination is not a socket.')
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(1)
                probe.connect(address)
        except ConnectionRefusedError:
            path.unlink()
        else:
            raise PrivacyError('Scanner service already running.')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(address)
        path.chmod(0o600)
        server.listen(8)
        while True:
            connection, _ = server.accept()
            with connection:
                connection.settimeout(40)
                try:
                    value = receive(connection)
                    # Do not silently mask an opaque finding in the scanner path.
                    clean = guard(value, scanner)
                    outcome = b'OK\n' if clean == value else b'REJECT\n'
                except (PrivacyError, ValueError, OSError, TypeError, RecursionError):
                    outcome = b'REJECT\n'
                try:
                    connection.sendall(outcome)
                except OSError:
                    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--serve-config')
    args = parser.parse_args()
    try:
        if args.serve_config:
            config = json.loads(Path(args.serve_config).read_text(encoding='utf-8'))
            serve(args.socket, config['scanner'])
        else:
            value = json.loads(sys.stdin.buffer.read(LIMIT + 1))
            return 0 if scan_client(args.socket, value) else 1
    except (PrivacyError, ValueError, OSError, KeyError, TypeError, RecursionError):
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
