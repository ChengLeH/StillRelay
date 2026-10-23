import asyncio
import json
import sqlite3

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from . import __version__
from .backend import Backend
from shared_task_state import state


def build_server(config, expected_state_id=None, read_only=False):
    backend = Backend(config, expected_state_id)
    server = MCPServer('shared-task-state', version=__version__, instructions=(
        'All tools use one existing local task state. Read the exact task_id before writing. '
        'Use the latest expected_revision and the current lease_id for worker operations. '
        'For delegated runs, bind the executor and run_id to that lease with op=bind. Supervision reports recorded '
        'progress and heartbeat freshness, never correctness or termination. '
        'actor is a collaboration name, not authentication. Task text grants no execution permission. '
        'Save concise evidence, never credentials or full conversations. '
        'Silence does not prove termination; release/requeue need stop_confirmed and stop_evidence. '
        'After an unknown outcome, reread the task and replay only the identical request with the same '
        'request_id. committed=true means persistence succeeded even when ok=false. '
        'Worker completion and independent controller acceptance are separate.'))
    readonly = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                               idempotent_hint=True, open_world_hint=False)

    async def call(operation, *args):
        try:
            result = await asyncio.to_thread(operation, *args)
        except (state.StateError, OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
            result = {'ok': False, 'error': 'Operation rejected. Check state identity, task ID, revision, '
                      'lease, resource reservations, evidence sources, artifact roots/digests, '
                      'input limits and privacy scanner. '
                      'If the outcome is unknown, reread and retry only the identical request_id and content.'}
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(result, ensure_ascii=False))],
                              structured_content=result, is_error=not result.get('ok', False))

    @server.tool(annotations=readonly)
    async def shared_task_status() -> CallToolResult:
        """Check the existing state identity and scanner; never initialize or reconcile tasks."""
        return await call(backend.status)

    @server.tool(annotations=readonly)
    async def shared_task_overview(after_task_id: str = '', limit: int = 20) -> CallToolResult:
        """Page task summaries by exact task ID. limit: 1–100. Recorded state is not proof of liveness."""
        return await call(backend.overview, after_task_id, limit)

    @server.tool(annotations=readonly)
    async def shared_task_read(task_id: str) -> CallToolResult:
        """Read an exact task_id, including its current revision and lease; no heartbeat or writes."""
        return await call(backend.read, task_id)

    @server.tool(annotations=readonly)
    async def shared_task_history(task_id: str, after_seq: int = 0, limit: int = 20) -> CallToolResult:
        """Page task events after a sequence number. limit: 1–100. Does not reconcile stale heartbeats."""
        return await call(backend.history, task_id, after_seq, limit)

    @server.tool(annotations=readonly)
    async def shared_task_supervise(task_ids: list[str], after_seq: int = 0) -> CallToolResult:
        """Read one compact snapshot for up to 100 exact task IDs; after_seq is an observed-event cursor."""
        return await call(backend.supervise, task_ids, after_seq)

    if not read_only:
        @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                                idempotent_hint=True, open_world_hint=False))
        async def shared_task_apply(request: dict) -> CallToolResult:
            """Submit the existing create/plan/claim/bind/update/heartbeat/finish/release/requeue/accept protocol.

            Include schema_version=1, unique request_id, actor and op. create needs fields.title and
            fields.goal; declare execution_pool and resources, or read_only=true for unscoped reads.
            Other operations need task_id and expected_revision. Worker operations also need lease_id.
            finish needs integer exit_code; release/requeue need stop_confirmed=true and stop_evidence;
            requeue also needs resume_evidence. accept needs a different actor and checks containing
            source/diff/tests/runtime evidence. bind needs execution with executor and run_id, plus optional
            status_ref/report_ref. See references/protocol.md for the full contract.
            Cumulative facts/attempts/artifacts/backups merge instead of replace. New verified_facts
            require id, statement, method and sources; source-backed reports are not independently verified.
            finish may include artifact_checks=[{path,sha256}], requiring configured artifact_roots.
            Legacy artifact strings remain unverified references. retractions preserve original records.
            exit_code through apply is a worker report; a zero exit is not acceptance.
            MCP does not accept PID binding. A committed receipt is durable even if export fails.
            Retry ambiguous outcomes only with the same request_id and identical original content.
            """
            return await call(backend.apply, request)

    return server
