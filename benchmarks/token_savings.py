#!/usr/bin/env python3
"""Measure supervisor-visible payload tokens for a deterministic local workload."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

CASES = (("low", 5), ("medium", 50), ("high", 500))
PHASES = 3
WORKER = (
    "import sys\n"
    "phase, count = map(int, sys.argv[1:])\n"
    "for i in range(1, count + 1):\n"
    " print(f'phase={phase:02d} line={i:04d} status=ok payload=deterministic')\n"
)


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def record(path: Path, source: str, role: str, kind: str, tool: str,
           payload: str, encoding: Any) -> dict[str, Any]:
    item = {
        "source": source,
        "role": role,
        "kind": kind,
        "tool": tool,
        "payload": payload,
        "payload_bytes_utf8": len(payload.encode("utf-8")),
        "payload_tokens_o200k_base": len(encoding.encode(payload)),
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(compact(item) + "\n")
    return item


class PairedTranscript:
    """Store exact common MCP exchanges and baseline-only local log operations."""

    def __init__(self, verbose: Path, quiet: Path, encoding: Any):
        self.paths = {"verbose": verbose, "quiet": quiet}
        self.encoding = encoding

    def common_mcp(self, tool: str, arguments: dict[str, Any], raw_result: str) -> None:
        call = compact({"name": tool, "arguments": arguments})
        for path in self.paths.values():
            record(path, "mcp", "assistant", "tool_call", tool, call, self.encoding)
            record(path, "mcp", "tool", "tool_result", tool, raw_result, self.encoding)

    def executor_log_read(self, log_path: Path, phase: int) -> str:
        arguments = {"path": str(log_path), "phase": phase}
        record(self.paths["verbose"], "local_executor", "assistant", "tool_call",
               "executor_log_read", compact(arguments), self.encoding)
        content = log_path.read_text(encoding="utf-8")
        record(self.paths["verbose"], "local_executor", "tool", "tool_result",
               "executor_log_read", content, self.encoding)
        return content


async def mcp_call(session: Any, client: str, tool: str, arguments: dict[str, Any],
                   wire: Path, encoding: Any) -> tuple[dict[str, Any], str]:
    request = compact({"name": tool, "arguments": arguments})
    record(wire, client, "client", "request", tool, request, encoding)
    response = await session.call_tool(tool, arguments)
    texts = [item.text for item in response.content if getattr(item, "text", None) is not None]
    if len(texts) != 1:
        raise RuntimeError(f"{client}.{tool} returned {len(texts)} text blocks")
    raw = texts[0]
    record(wire, client, "server", "response", tool, raw, encoding)
    data = getattr(response, "structured_content", None)
    if not isinstance(data, dict):
        data = json.loads(raw)
    if response.is_error or data.get("ok") is False:
        raise RuntimeError(f"{client}.{tool} failed: {raw}")
    return data, raw


def metrics(path: Path) -> dict[str, int]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    calls = [row for row in rows if row["kind"] == "tool_call"]
    results = [row for row in rows if row["kind"] == "tool_result"]
    total = lambda key: sum(row[key] for row in rows)
    return {
        "tool_calls": len(calls),
        "tool_results": len(results),
        "mcp_payload_tokens": sum(row["payload_tokens_o200k_base"] for row in rows
                                  if row["source"] == "mcp"),
        "local_executor_payload_tokens": sum(row["payload_tokens_o200k_base"] for row in rows
                                             if row["source"] == "local_executor"),
        "total_payload_tokens": total("payload_tokens_o200k_base"),
        "total_payload_bytes_utf8": total("payload_bytes_utf8"),
    }


def wire_metrics(path: Path, client: str) -> dict[str, int]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows = [row for row in rows if row["source"] == client]
    return {
        "mcp_calls": sum(row["kind"] == "request" for row in rows),
        "mcp_results": sum(row["kind"] == "response" for row in rows),
        "payload_tokens_o200k_base": sum(row["payload_tokens_o200k_base"] for row in rows),
        "payload_bytes_utf8": sum(row["payload_bytes_utf8"] for row in rows),
    }


def run_command(phase: int, count: int, log_path: Path, cwd: Path) -> dict[str, Any]:
    completed = subprocess.run([sys.executable, "-c", WORKER, str(phase), str(count)],
                               cwd=cwd, capture_output=True, text=True, timeout=30)
    if completed.returncode:
        raise RuntimeError(f"worker command failed: {completed.stderr[-1000:]}")
    log_path.write_text(completed.stdout, encoding="utf-8")
    lines = completed.stdout.splitlines()
    expected = [f"phase={phase:02d} line={i:04d} status=ok payload=deterministic"
                for i in range(1, count + 1)]
    if lines != expected:
        raise RuntimeError(f"phase {phase} output did not match the deterministic expectation")
    return {
        "exit_code": completed.returncode,
        "line_count": len(lines),
        "stdout_bytes_utf8": len(completed.stdout.encode("utf-8")),
        "stdout_sha256": hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest(),
    }


async def run_trial(case: str, count: int, trial: int, out: Path, runtime_root: Path | None,
                    encoding: Any, mcp: Any) -> dict[str, Any]:
    ClientSession, StdioServerParameters, stdio_client = mcp
    out.mkdir(parents=True)
    logs = out / "worker-output"
    logs.mkdir()
    verbose_path, quiet_path, wire = (
        out / "supervisor-verbose.jsonl",
        out / "supervisor-quiet.jsonl",
        out / "mcp-wire.jsonl",
    )
    pair = PairedTranscript(verbose_path, quiet_path, encoding)
    task_id = f"token-bench-{case}-{trial:02d}"
    runtime_cwd = runtime_root or Path(tempfile.gettempdir())
    worker_calls: list[str] = []
    controller_calls: list[str] = []
    command_results = []

    with tempfile.TemporaryDirectory(prefix="shared-task-supervision-bench-") as scratch:
        scratch = Path(scratch)
        config, state_root, exchange = scratch / "config.json", scratch / "root", scratch / "exchange"
        init = subprocess.run(
            [sys.executable, "-m", "shared_task_state_mcp", "--config", str(config),
             "init", "--root", str(state_root), "--exchange", str(exchange)],
            cwd=runtime_cwd, capture_output=True, text=True, timeout=30,
        )
        if init.returncode:
            raise RuntimeError(f"disposable state init failed: {init.stderr[-1000:]}")
        state_id = json.loads(init.stdout)["state_id"]
        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "shared_task_state_mcp", "--config", str(config), "--state-id", state_id],
            cwd=runtime_cwd,
        )

        async with stdio_client(server) as a_streams:
            async with ClientSession(*a_streams) as controller:
                await controller.initialize()
                async with stdio_client(server) as b_streams:
                    async with ClientSession(*b_streams) as worker:
                        await worker.initialize()
                        tools = {tool.name for tool in (await controller.list_tools()).tools}
                        required = {"shared_task_status", "shared_task_apply", "shared_task_supervise"}
                        if not required <= tools:
                            raise RuntimeError("runtime must expose shared_task_supervise; no read fallback is used")

                        async def controller_call(tool: str, args: dict[str, Any]):
                            controller_calls.append(tool)
                            data, raw = await mcp_call(controller, "ControllerA", tool, args, wire, encoding)
                            pair.common_mcp(tool, args, raw)
                            return data

                        async def worker_call(tool: str, args: dict[str, Any]):
                            worker_calls.append(tool)
                            return await mcp_call(worker, "WorkerB", tool, args, wire, encoding)

                        status = await controller_call("shared_task_status", {})
                        if status["state_id"] != state_id:
                            raise RuntimeError("ControllerA joined the wrong state")
                        create = {
                            "schema_version": 1, "request_id": f"{task_id}-create",
                            "actor": "ControllerA", "op": "create", "task_id": task_id,
                            "execution_pool": "local", "resources": [{"kind": "unscoped", "mode": "write"}],
                            "fields": {
                                "title": f"Token experiment {case} trial {trial}",
                                "goal": "Run three deterministic local command phases and report compact evidence.",
                                "phase": "prepare", "summary": "",
                                "next_step": "WorkerB claims and runs the fixed phases.",
                            },
                        }
                        await controller_call("shared_task_apply", {"request": create})
                        task = (await worker_call("shared_task_read", {"task_id": task_id}))[0]["task"]
                        claimed, _ = await worker_call("shared_task_apply", {"request": {
                            "schema_version": 1, "request_id": f"{task_id}-claim", "actor": "WorkerB",
                            "op": "claim", "task_id": task_id, "expected_revision": task["revision"],
                        }})
                        task = claimed["receipt"]["task"]
                        revision, lease = task["revision"], task["lease_id"]
                        bound, _ = await worker_call("shared_task_apply", {"request": {
                            "schema_version": 1, "request_id": f"{task_id}-bind", "actor": "WorkerB",
                            "op": "bind", "task_id": task_id, "expected_revision": revision,
                            "lease_id": lease,
                            "execution": {"executor": "command", "run_id": f"{task_id}-run"},
                        }})
                        revision = bound["receipt"]["task"]["revision"]
                        cursor, digests = 0, []

                        for phase in range(1, PHASES + 1):
                            log_path = logs / f"phase-{phase:02d}.stdout.txt"
                            cmd = run_command(phase, count, log_path, out)
                            command_results.append(cmd)
                            digests.append(cmd["stdout_sha256"])
                            relative_log = log_path.relative_to(out).as_posix()
                            fact = (f"phase={phase:02d} lines={count} sha256={cmd['stdout_sha256']} "
                                    f"artifact={relative_log}")
                            summary = f"Phase {phase:02d}: verified {count} lines; sha256={cmd['stdout_sha256']}."
                            update = {
                                "schema_version": 1, "request_id": f"{task_id}-phase-{phase:02d}",
                                "actor": "WorkerB", "op": "update", "task_id": task_id,
                                "expected_revision": revision, "lease_id": lease,
                                "fields": {
                                    "phase": f"phase-{phase:02d}", "summary": summary,
                                    "last_confirmed_step": summary,
                                    "next_step": "Run the next phase." if phase < PHASES else "Finish the task.",
                                    "verified_facts": [fact],
                                },
                            }
                            updated, _ = await worker_call("shared_task_apply", {"request": update})
                            revision = updated["receipt"]["task"]["revision"]

                            args = {"task_ids": [task_id], "after_seq": cursor}
                            supervised = await controller_call("shared_task_supervise", args)
                            if supervised.get("state_id") != state_id:
                                raise RuntimeError("supervision returned a different state")
                            summary_task = supervised["tasks"][0]
                            event = summary_task["last_reported_progress_event"]
                            if (summary_task["progress"]["phase"] != f"phase-{phase:02d}"
                                    or event is None or event["seq"] <= cursor
                                    or event["progress"]["summary"] != summary):
                                raise RuntimeError(f"supervision missed phase {phase} progress")
                            raw_progress = compact(supervised)
                            first_line = f"phase={phase:02d} line=0001 status=ok payload=deterministic"
                            if first_line in raw_progress:
                                raise RuntimeError("supervision response unexpectedly contains worker log text")
                            cursor = supervised["snapshot_seq"]

                            read_text = pair.executor_log_read(log_path, phase)
                            if len(read_text.splitlines()) != count or hashlib.sha256(
                                    read_text.encode("utf-8")).hexdigest() != cmd["stdout_sha256"]:
                                raise RuntimeError(f"executor log verification failed in phase {phase}")

                        digest = hashlib.sha256("".join(digests).encode("ascii")).hexdigest()
                        finished, _ = await worker_call("shared_task_apply", {"request": {
                            "schema_version": 1, "request_id": f"{task_id}-finish", "actor": "WorkerB",
                            "op": "finish", "task_id": task_id, "expected_revision": revision,
                            "lease_id": lease, "exit_code": 0,
                            "fields": {
                                "phase": "complete",
                                "summary": f"Completed {PHASES} phases; output digest={digest}.",
                                "last_confirmed_step": "All phase outputs matched deterministic expectations.",
                                "next_step": "ControllerA starts an independent review.",
                            },
                        }})
                        done = finished["receipt"]["task"]
                        started = await controller_call("shared_task_apply", {"request": {
                            "schema_version": 1, "request_id": f"{task_id}-review-start",
                            "actor": "ControllerA", "op": "review_start", "task_id": task_id,
                            "expected_revision": done["revision"],
                        }})
                        review = started["receipt"]["task"]
                        bound, _ = await controller_call("shared_task_apply", {"request": {
                            "schema_version": 1, "request_id": f"{task_id}-review-bind",
                            "actor": "ControllerA", "op": "review_bind", "task_id": task_id,
                            "expected_revision": review["revision"],
                            "review_lease_id": review["review"]["lease_id"],
                            "execution": {"executor": "benchmark-fixture", "run_id": f"{task_id}-review-run"},
                        }})
                        review = bound["receipt"]["task"]
                        passed, _ = await controller_call("shared_task_apply", {"request": {
                            "schema_version": 1, "request_id": f"{task_id}-review-result",
                            "actor": "ControllerA", "op": "review_result", "task_id": task_id,
                            "expected_revision": review["revision"],
                            "review_lease_id": review["review"]["lease_id"], "decision": "pass",
                            "checks": {
                                "source": "The harness ran the same fixed local Python command in each phase.",
                                "diff": "Each trial used a fresh disposable state and wrote only benchmark artifacts.",
                                "tests": "Every output line matched the deterministic expected sequence.",
                                "runtime": f"WorkerB finished with exit_code=0 and digest={digest}.",
                            },
                        }})
                        reviewed = passed["receipt"]["task"]
                        accepted, _ = await worker_call("shared_task_apply", {"request": {
                            "schema_version": 1, "request_id": f"{task_id}-accept",
                            "actor": "WorkerB", "op": "accept", "task_id": task_id,
                            "expected_revision": reviewed["revision"],
                        }})
                        if accepted["receipt"]["task"]["status"] != "accepted":
                            raise RuntimeError("ControllerA did not accept the completed task")

    expected_controller = ["shared_task_status", "shared_task_apply"] + [
        "shared_task_supervise"] * PHASES + ["shared_task_apply"] * 3
    expected_worker = ["shared_task_read", "shared_task_apply", "shared_task_apply"] + [
        "shared_task_apply"] * PHASES + ["shared_task_apply"] * 2
    if controller_calls != expected_controller or worker_calls != expected_worker:
        raise RuntimeError("MCP call sequence changed unexpectedly")

    verbose, quiet = metrics(verbose_path), metrics(quiet_path)
    if verbose["mcp_payload_tokens"] != quiet["mcp_payload_tokens"]:
        raise RuntimeError("paired transcripts do not contain the same actual MCP payloads")
    if verbose["local_executor_payload_tokens"] == 0 or quiet["local_executor_payload_tokens"] != 0:
        raise RuntimeError("local executor log reads were not limited to verbose mode")
    saved = verbose["total_payload_tokens"] - quiet["total_payload_tokens"]
    if saved <= 0:
        raise RuntimeError("verbose transcript should contain more measured payload tokens")

    return {
        "case": case, "trial": trial, "lines_per_phase": count, "phase_count": PHASES,
        "task_id": task_id, "state_id": state_id,
        "correctness": {
            "accepted_task_status": "accepted",
            "phase_exit_codes": [item["exit_code"] for item in command_results],
            "observed_output_lines": sum(item["line_count"] for item in command_results),
            "expected_output_lines": PHASES * count,
            "phase_sha256": [item["stdout_sha256"] for item in command_results],
        },
        "worker_command_stdout_bytes_utf8": sum(item["stdout_bytes_utf8"] for item in command_results),
        "supervisor_visible_payloads": {
            "verbose": verbose, "quiet": quiet,
            "savings": {
                "numerator_tokens": saved,
                "denominator_tokens": verbose["total_payload_tokens"],
                "percent": 100 * saved / verbose["total_payload_tokens"],
                "payload_bytes_removed": verbose["total_payload_bytes_utf8"] - quiet["total_payload_bytes_utf8"],
            },
        },
        "actual_mcp_wire": {
            "ControllerA": wire_metrics(wire, "ControllerA"),
            "WorkerB": wire_metrics(wire, "WorkerB"),
        },
        "artifacts": {
            "verbose": str(verbose_path), "quiet": str(quiet_path),
            "mcp_wire": str(wire), "worker_output": str(logs),
        },
    }


def aggregate(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for case, count in CASES:
        selected = [trial for trial in trials if trial["case"] == case]
        denominator = sum(t["supervisor_visible_payloads"]["savings"]["denominator_tokens"]
                          for t in selected)
        saved = sum(t["supervisor_visible_payloads"]["savings"]["numerator_tokens"]
                    for t in selected)
        mean = lambda mode, key: sum(t["supervisor_visible_payloads"][mode][key]
                                     for t in selected) / len(selected)
        rows.append({
            "case": case, "lines_per_phase": count, "trials": len(selected),
            "mean_verbose_tokens": mean("verbose", "total_payload_tokens"),
            "mean_quiet_tokens": mean("quiet", "total_payload_tokens"),
            "mean_tokens_saved": saved / len(selected),
            "numerator_tokens": saved, "denominator_tokens": denominator,
            "percent_of_verbose_tokens": 100 * saved / denominator,
            "mean_verbose_bytes": mean("verbose", "total_payload_bytes_utf8"),
            "mean_quiet_bytes": mean("quiet", "total_payload_bytes_utf8"),
            "mean_worker_mcp_tokens": sum(
                t["actual_mcp_wire"]["WorkerB"]["payload_tokens_o200k_base"] for t in selected) / len(selected),
            "mean_worker_mcp_bytes": sum(
                t["actual_mcp_wire"]["WorkerB"]["payload_bytes_utf8"] for t in selected) / len(selected),
        })
    return rows


def markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Bounded supervisor payload token experiment", "",
        "This is an observed supervisory-channel input comparison for two MCP participants, ControllerA and WorkerB, plus instrumented local executor-log reads. It uses real shared_task_supervise calls and real local file reads; no model inference or provider calls were made. It does not measure total agent tokens, model context accounting, provider usage, or billing.", "",
        "For each trial, WorkerB runs three deterministic command phases once and stores only bounded phase/count/hash/artifact evidence in shared state. ControllerA makes the same status, create, and shared_task_supervise calls in both modes; the exact supervision requests and replies are preserved verbatim in both transcript files. In verbose mode the harness additionally reads each local phase log and records the actual path argument and returned text as an executor_log_read operation. Quiet mode omits these local reads. The raw MCP call/result stream is captured once in mcp-wire.jsonl; WorkerB traffic is reported separately.", "",
        "Counts use tiktoken o200k_base on each serialized tool-call or tool-result payload separately. This is an encoder convention, not GPT-6 token accounting. The percentages refer only to measured ControllerA MCP payloads plus baseline local executor-log payloads; they are not an overall agent-token savings estimate.", "",
        "| Case | Lines / phase | Trials | Mean verbose tokens | Mean quiet tokens | Mean saved | Numerator / denominator | Saved / verbose | Mean verbose / quiet bytes | WorkerB MCP tokens / bytes |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in result["aggregate_by_case"]:
        lines.append(
            f"| {row['case']} | {row['lines_per_phase']} | {row['trials']} | "
            f"{row['mean_verbose_tokens']:.1f} | {row['mean_quiet_tokens']:.1f} | "
            f"{row['mean_tokens_saved']:.1f} | {row['numerator_tokens']} / {row['denominator_tokens']} | "
            f"{row['percent_of_verbose_tokens']:.2f}% | "
            f"{row['mean_verbose_bytes']:.1f} / {row['mean_quiet_bytes']:.1f} | "
            f"{row['mean_worker_mcp_tokens']:.1f} / {row['mean_worker_mcp_bytes']:.1f} |"
        )
    lines += ["", "## Artifacts", ""]
    for trial in result["trials"]:
        lines.append(
            f"- {trial['case']} trial {trial['trial']}: verbose {trial['artifacts']['verbose']}, "
            f"quiet {trial['artifacts']['quiet']}, wire {trial['artifacts']['mcp_wire']}, "
            f"stdout {trial['artifacts']['worker_output']}."
        )
    lines += ["", "Example: python benchmarks/token_savings.py --output /path/to/new-results --trials 3",
              "", "Use --runtime-root only when serving from a source checkout instead of the installed package. "
              "The selected output directory must be empty. The harness creates fresh temporary state per trial.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New or empty result directory")
    parser.add_argument("--trials", type=int, default=3, help="Trials per volume (1-10; default 3)")
    parser.add_argument("--runtime-root", type=Path,
                        help="Optional source checkout used as cwd for the MCP server and init command")
    args = parser.parse_args()
    if not 1 <= args.trials <= 10:
        parser.error("--trials must be between 1 and 10")
    try:
        import tiktoken
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        encoding = tiktoken.get_encoding("o200k_base")
    except Exception as exc:
        parser.error(f"installed mcp/tiktoken/o200k_base required: {exc}")

    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error(f"output directory is not empty: {output}")
    runtime_root = args.runtime_root.expanduser().resolve() if args.runtime_root else None
    mcp = (ClientSession, StdioServerParameters, stdio_client)
    trials = []
    for case, count in CASES:
        for trial in range(1, args.trials + 1):
            out = output / "artifacts" / case / f"trial-{trial:02d}"
            trial_result = asyncio.run(run_trial(case, count, trial, out, runtime_root, encoding, mcp))
            trials.append(trial_result)
            print(f"completed {case} trial {trial}/{args.trials}", flush=True)

    result = {
        "schema_version": 2,
        "measurement_scope": (
            "observed supervisory-channel input: exact shared_task_supervise MCP payloads "
            "plus baseline-only local executor_log_read payloads; no model inference or provider calls"
        ),
        "not_measured": ["total agent tokens", "model context accounting", "provider usage", "billing"],
        "tokenizer": {
            "library": "tiktoken",
            "library_version": importlib.metadata.version("tiktoken"),
            "encoding": "o200k_base",
            "basis": "sum of token counts for each exact serialized tool call and result payload separately",
        },
        "runtime_root": str(runtime_root) if runtime_root else "installed package",
        "workload": {
            "phases": PHASES, "lines_per_phase": dict(CASES), "trials_per_case": args.trials,
            "mcp_participants": ["ControllerA", "WorkerB"],
            "supervisor_modes": {
                "verbose": "common actual ControllerA MCP exchanges plus one actual local executor-log read per phase",
                "quiet": "the same actual ControllerA MCP exchanges; logs remain local and unread by supervisor",
            },
            "same_common_mcp_payloads": True,
        },
        "aggregate_by_case": aggregate(trials),
        "trials": trials,
        "output_directory": str(output),
    }
    (output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
    (output / "summary.md").write_text(markdown(result), encoding="utf-8")
    print(f"results: {output / 'results.json'}")
    print(f"summary: {output / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
