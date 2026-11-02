# Existing macOS Host compatibility

These are the existing Host schema-2 compatibility entry points, kept distinct from the public schema-3 state implementation. They must not initialize, replace, migrate, or repoint an existing database. Deploy only after checking the installed files against the expected baseline, making backups, and running both Host and public regression tests. The Host state module still imports its existing local `privacy_guard.py` and `evidence.py`.

The optional POSIX `process_group` heartbeat binding is accepted only when it equals the bound PID and that process is its group leader. Running group members retain the task reservation and count toward the existing local pool even if the original leader exits. An unavailable process-group check is treated conservatively. A detached process which leaves the group is outside this guarantee. The legacy local capacity runtime recognizes the same optional field; older leases are unchanged. Slack's adapter forwards the field without changing existing callers.

The running watch process and long-lived MCP processes must be restarted or reconnected before they can load changed Python source. Preserve the existing database, privacy scanner, MCP binding, and client permissions; this compatibility layer is not an installer or migration.
