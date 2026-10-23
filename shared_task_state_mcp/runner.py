"""Run an explicit foreground command under a shared task reservation.

The wrapper does not start a daemon, use a shell, or store command output in
shared state. Commands must remain foreground for their full lifecycle.
"""
from contextlib import contextmanager
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import threading
import time
import uuid

from shared_task_state import state
from .backend import Backend


_TERMINATE_GRACE_SECONDS = 2.0


class _RunnerSignal(BaseException):
    def __init__(self, signum):
        self.signum = signum


def _id(value, name):
    try:
        return state.safe_id(value)
    except (state.StateError, TypeError) as exc:
        raise ValueError(f"invalid {name}") from exc


def _read(backend, task_id):
    result = backend.read(task_id)
    task = result.get("task") if isinstance(result, dict) else None
    return task if isinstance(task, dict) and task.get("task_id") == task_id else None


def _read_safe(backend, task_id):
    try:
        return _read(backend, task_id)
    except Exception:
        return None


def _receipt(outcome):
    if not isinstance(outcome, dict) or outcome.get("committed") is not True:
        return None
    receipt = outcome.get("receipt")
    task = receipt.get("task") if isinstance(receipt, dict) else None
    return task if isinstance(task, dict) else None


def _request(op, task_id, actor, task, lease_id=None, **values):
    request = {
        "schema_version": 1,
        "request_id": uuid.uuid4().hex,
        "actor": actor,
        "task_id": task_id,
        "expected_revision": task["revision"],
        "op": op,
    }
    if lease_id is not None:
        request["lease_id"] = lease_id
    request.update(values)
    return request


def _owns(task, task_id, actor, lease_id, generation):
    return bool(
        isinstance(task, dict)
        and task.get("task_id") == task_id
        and task.get("status") == "running"
        and task.get("reservation_active") is True
        and task.get("worker") == actor
        and task.get("lease_id") == lease_id
        and task.get("generation") == generation
    )


def _bound(task, task_id, actor, lease_id, generation, executor, run_id):
    if not _owns(task, task_id, actor, lease_id, generation):
        return False
    binding = task.get("delegation")
    return bool(
        isinstance(binding, dict)
        and binding.get("controller") == task.get("created_by")
        and binding.get("worker") == actor
        and binding.get("generation") == generation
        and binding.get("executor") == executor
        and binding.get("run_id") == run_id
    )


def _revision_conflict(exc):
    return isinstance(exc, state.StateError) and "revision conflict" in str(exc).lower()


def _owned_directory(path, *, create):
    if path.is_symlink():
        raise OSError("unsafe runner directory symlink")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    try:
        info = os.lstat(path)
    except OSError:
        raise
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("runner path is not a directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise OSError("runner directory is owned by another user")
    if create:
        os.chmod(path, 0o700)


def _logs(state_root, task_id, run_id):
    root = Path(state_root).expanduser()
    _owned_directory(root, create=False)
    root = root.resolve()
    runs_dir = root / "runs"
    _owned_directory(runs_dir, create=True)
    task_dir = runs_dir / task_id
    _owned_directory(task_dir, create=True)
    run_dir = task_dir / run_id
    _owned_directory(run_dir, create=True)
    stdout_path, stderr_path = run_dir / "stdout.log", run_dir / "stderr.log"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    stdout_fd = os.open(stdout_path, flags, 0o600)
    try:
        stderr_fd = os.open(stderr_path, flags, 0o600)
    except BaseException:
        os.close(stdout_fd)
        stdout_path.unlink(missing_ok=True)
        raise
    return os.fdopen(stdout_fd, "wb"), os.fdopen(stderr_fd, "wb"), {
        "stdout": str(stdout_path), "stderr": str(stderr_path),
    }, str(run_dir)


def _result(task_id, run_id, status, reserved, logs=None, exit_code=None,
            error=None, task_status=None, **extra):
    result = {
        "ok": error is None,
        "task_id": task_id,
        "run_id": run_id,
        "status": status,
        "task_status": task_status,
        "reservation_active": bool(reserved),
        "exit_code": exit_code,
        "exit_code_source": "subprocess_wait" if exit_code is not None else None,
        "logs": logs,
    }
    if error:
        result["error"] = error
    result.update(extra)
    return result


def _release(backend, task_id, actor, lease_id, generation, executor, run_id,
             require_binding, evidence):
    """Release this lease only after a read proves that no child was started."""
    for attempt in range(2):
        current = _read_safe(backend, task_id)
        if not _owns(current, task_id, actor, lease_id, generation):
            return False, current
        if require_binding:
            if not _bound(current, task_id, actor, lease_id, generation, executor, run_id):
                return False, current
        elif current.get("delegation") is not None:
            return False, current
        request = _request("release", task_id, actor, current, lease_id,
                           stop_confirmed=True, stop_evidence=evidence)
        try:
            outcome = backend.apply(request)
        except _RunnerSignal:
            raise
        except Exception as exc:
            resolved = _read_safe(backend, task_id)
            if (resolved and resolved.get("retired_lease_id") == lease_id
                    and resolved.get("reservation_active") is False):
                return True, resolved
            if attempt == 0 and _revision_conflict(exc):
                continue
            return False, resolved
        released = _receipt(outcome)
        if (released and released.get("retired_lease_id") == lease_id
                and released.get("reservation_active") is False):
            return True, released
        resolved = _read_safe(backend, task_id)
        if (resolved and resolved.get("retired_lease_id") == lease_id
                and resolved.get("reservation_active") is False):
            return True, resolved
        return False, resolved
    return False, _read_safe(backend, task_id)


def _mutate(backend, task_id, actor, lease_id, generation, executor, run_id,
            op, values, completed, retry_conflict=False):
    """Apply from a fresh revision; resolve unknown outcomes by reading state."""
    for attempt in range(2 if retry_conflict else 1):
        current = _read(backend, task_id)
        if not _bound(current, task_id, actor, lease_id, generation, executor, run_id):
            return None, current
        request = _request(op, task_id, actor, current, lease_id, **values)
        try:
            outcome = backend.apply(request)
        except _RunnerSignal:
            raise
        except Exception as exc:
            if attempt == 0 and retry_conflict and _revision_conflict(exc):
                continue
            resolved = _read_safe(backend, task_id)
            return (resolved if completed(resolved, current) else None), resolved
        committed = _receipt(outcome)
        if completed(committed, current):
            return committed, committed
        resolved = _read_safe(backend, task_id)
        return (resolved if completed(resolved, current) else None), resolved
    return None, _read_safe(backend, task_id)


def _finish_seen(backend, task_id, lease_id, exit_code):
    status = "worker_done" if exit_code == 0 else "blocked"

    def matches(task):
        return bool(task and task.get("retired_lease_id") == lease_id
                    and task.get("exit_code") == exit_code
                    and task.get("status") == status
                    and task.get("reservation_active") is False)

    current = _read_safe(backend, task_id)
    if matches(current):
        return current
    history = getattr(backend, "history", None)
    if not callable(history):
        return None
    cursor = 0
    try:
        while True:
            page = history(task_id, cursor, 100)
            for event in page.get("events", []):
                if matches(event.get("task")):
                    return current or event["task"]
            if not page.get("has_more"):
                return None
            next_cursor = page.get("next_after_seq")
            if not isinstance(next_cursor, int) or next_cursor <= cursor:
                return None
            cursor = next_cursor
    except Exception:
        return None


@contextmanager
def _signals():
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {}

    def interrupt(signum, _frame):
        raise _RunnerSignal(signum)

    watched = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        watched.append(signal.SIGTERM)
    try:
        for signum in watched:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupt)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _stop_child(process):
    try:
        code = process.poll()
    except Exception:
        return False, None
    if code is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        except Exception:
            try:
                if process.poll() is None:
                    return False, None
            except Exception:
                return False, None
        try:
            code = process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                code = process.wait()
            except Exception:
                return False, None
        except Exception:
            return False, None
    return True, code


def _argv(command):
    if isinstance(command, (str, bytes)) or not isinstance(command, (list, tuple)):
        raise TypeError("command must be an argv list or tuple; shell strings are not accepted")
    argv = list(command)
    if not argv or not isinstance(argv[0], str) or not argv[0]:
        raise ValueError("command argv must start with a nonempty executable")
    if any(not isinstance(arg, str) or "\0" in arg for arg in argv):
        raise ValueError("command arguments must be strings without NUL characters")
    return argv


def exit_status(result):
    """Return command failure to shell callers; unresolved lifecycle is failure."""
    code = result.get("exit_code")
    if result.get("ok") and isinstance(code, int) and not isinstance(code, bool) and 0 <= code <= 255:
        return code
    return 1


def run(config, task_id, actor, command, cwd=None, executor="command",
        heartbeat_seconds=None, run_id=None, expected_state_id=None):
    """Run argv under a claimed task lease and return compact status and log paths."""
    task_id, actor = _id(task_id, "task_id"), _id(actor, "actor")
    executor = _id(executor, "executor")
    run_id = _id(run_id if run_id is not None else uuid.uuid4().hex, "run_id")
    argv = _argv(command)
    if cwd is not None:
        cwd = os.fspath(cwd)
        if not isinstance(cwd, str) or "\0" in cwd:
            raise ValueError("cwd must be a text filesystem path")
    config = os.fspath(config)
    if not isinstance(config, str):
        raise TypeError("config must be a text filesystem path")

    try:
        settings = state._read_config(config)
        backend = Backend(config, expected_state_id or settings["state_id"])
    except Exception:
        return _result(task_id, run_id, "not_started", False, error="configuration_unavailable")
    ttl = settings["heartbeat_seconds"]
    if heartbeat_seconds is None:
        interval = ttl / 3
    else:
        try:
            interval = float(heartbeat_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("heartbeat_seconds must be positive and below the configured timeout") from exc
        if (isinstance(heartbeat_seconds, bool) or not math.isfinite(interval)
                or interval <= 0 or interval >= ttl):
            raise ValueError("heartbeat_seconds must be positive and below the configured timeout")

    try:
        out, err, logs, report_ref = _logs(settings["root"], task_id, run_id)
    except Exception:
        return _result(task_id, run_id, "not_started", False, error="local_log_setup_failed")

    with out, err:
        try:
            queued = _read(backend, task_id)
        except Exception:
            return _result(task_id, run_id, "not_started", False, logs, error="task_read_failed")
        if queued.get("status") != "queued" or queued.get("reservation_active"):
            return _result(task_id, run_id, "not_started", queued.get("reservation_active", False),
                           logs, task_status=queued.get("status"), error="task_not_queued")

        try:
            claim_outcome = backend.apply(_request("claim", task_id, actor, queued))
        except state.StateError:
            current = _read_safe(backend, task_id)
            return _result(task_id, run_id, "not_started",
                           current.get("reservation_active", False) if current else False,
                           logs, task_status=current.get("status") if current else None,
                           error="claim_rejected")
        except Exception:
            current = _read_safe(backend, task_id)
            return _result(task_id, run_id, "reservation_retained",
                           current.get("reservation_active", True) if current else True,
                           logs, task_status=current.get("status") if current else None,
                           error="claim_outcome_unknown")

        if isinstance(claim_outcome, dict) and claim_outcome.get("committed") is False:
            current = _read_safe(backend, task_id)
            return _result(task_id, run_id, "not_started",
                           current.get("reservation_active", False) if current else False,
                           logs, task_status=current.get("status") if current else None,
                           error="claim_rejected")

        claim = _receipt(claim_outcome)
        lease_id = claim.get("lease_id") if claim else None
        generation = claim.get("generation") if claim else None
        if (not isinstance(lease_id, str) or not isinstance(generation, int)
                or isinstance(generation, bool)
                or not _owns(claim, task_id, actor, lease_id, generation)):
            current = _read_safe(backend, task_id)
            return _result(task_id, run_id, "reservation_retained",
                           current.get("reservation_active", True) if current else True,
                           logs, task_status=current.get("status") if current else None,
                           error="claim_outcome_unknown")

        bind_known_failure = False
        try:
            current = _read(backend, task_id)
            if not _owns(current, task_id, actor, lease_id, generation) or current.get("delegation") is not None:
                current = None
                bound = None
            else:
                bind_request = _request(
                    "bind", task_id, actor, current, lease_id,
                    execution={"executor": executor, "run_id": run_id,
                               "report_ref": report_ref},
                )
                try:
                    outcome = backend.apply(bind_request)
                except _RunnerSignal:
                    raise
                except state.StateError:
                    # State validation/revision/ownership failures are returned
                    # before a commit. Other exceptions can lose a committed
                    # response and therefore leave the binding outcome unknown.
                    outcome = None
                    bind_known_failure = True
                except Exception:
                    outcome = None
                if isinstance(outcome, dict) and outcome.get("committed") is False:
                    bind_known_failure = True
                bound = _receipt(outcome)
                if not _bound(bound, task_id, actor, lease_id, generation, executor, run_id):
                    current = _read_safe(backend, task_id)
                    bound = current if _bound(current, task_id, actor, lease_id, generation, executor, run_id) else None
        except Exception:
            current, bound = None, None

        if bound is None:
            if not bind_known_failure:
                current = _read_safe(backend, task_id) or current
                return _result(task_id, run_id, "reservation_retained",
                               current.get("reservation_active", True) if current else True,
                               logs, task_status=current.get("status") if current else None,
                               error="bind_outcome_unknown")
            released, current = _release(
                backend, task_id, actor, lease_id, generation, executor, run_id,
                False, "No child was launched because run binding did not commit.",
            )
            return _result(task_id, run_id, "not_started" if released else "reservation_retained",
                           current.get("reservation_active", True) if current else not released,
                           logs, task_status=current.get("status") if current else None,
                           error="bind_rejected" if released else "bind_outcome_unknown")

        process, spawn_attempted = None, False
        try:
            with _signals():
                spawn_attempted = True
                try:
                    process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                               stdout=out, stderr=err, shell=False)
                except (OSError, ValueError):
                    released, current = _release(
                        backend, task_id, actor, lease_id, generation, executor, run_id,
                        True, "No child process was created; command launch failed.",
                    )
                    return _result(task_id, run_id, "not_started" if released else "reservation_retained",
                                   current.get("reservation_active", True) if current else not released,
                                   logs, task_status=current.get("status") if current else None,
                                   error="command_launch_failed" if released else "launch_outcome_unknown")

                due = time.monotonic() + interval
                while True:
                    try:
                        exit_code = process.wait(timeout=max(0.0, due - time.monotonic()))
                        break
                    except subprocess.TimeoutExpired:
                        if process.poll() is not None:
                            exit_code = process.returncode
                            break
                        before = _read(backend, task_id)
                        if not _bound(before, task_id, actor, lease_id, generation, executor, run_id):
                            _stop_child(process)
                            return _result(task_id, run_id, "reservation_retained", True, logs,
                                           task_status=before.get("status") if before else None,
                                           error="lease_outcome_unknown")

                        def heartbeat_done(task, previous):
                            return bool(_bound(task, task_id, actor, lease_id, generation, executor, run_id)
                                        and task.get("revision", 0) > previous.get("revision", 0)
                                        and task.get("heartbeat_epoch", 0) > previous.get("heartbeat_epoch", 0))

                        heartbeat, current = _mutate(
                            backend, task_id, actor, lease_id, generation, executor, run_id,
                            "heartbeat", {}, heartbeat_done, retry_conflict=True,
                        )
                        if heartbeat is None:
                            _stop_child(process)
                            return _result(task_id, run_id, "reservation_retained", True, logs,
                                           task_status=current.get("status") if current else None,
                                           error="heartbeat_outcome_unknown")
                        due = time.monotonic() + interval

            out.flush()
            err.flush()
            expected = "worker_done" if exit_code == 0 else "blocked"
            completed = lambda task, _before: bool(
                task and task.get("retired_lease_id") == lease_id
                and task.get("exit_code") == exit_code
                and task.get("status") == expected
                and task.get("reservation_active") is False
            )
            finished, current = _mutate(
                backend, task_id, actor, lease_id, generation, executor, run_id,
                "finish", {"exit_code": exit_code}, completed, retry_conflict=True,
            )
            if finished is None:
                finished = _finish_seen(backend, task_id, lease_id, exit_code)
            if finished is None:
                return _result(task_id, run_id, "reservation_retained",
                               current.get("reservation_active", True) if current else True,
                               logs, exit_code=exit_code,
                               task_status=current.get("status") if current else None,
                               error="finish_outcome_unknown")
            return _result(task_id, run_id, finished["status"],
                           finished.get("reservation_active", False), logs,
                           exit_code=exit_code, task_status=finished["status"])
        except _RunnerSignal as caught:
            child_code, reaped = None, False
            if process is not None:
                reaped, child_code = _stop_child(process)
            elif not spawn_attempted:
                released, current = _release(
                    backend, task_id, actor, lease_id, generation, executor, run_id,
                    True, "No child was launched before the runner was interrupted.",
                )
                return _result(task_id, run_id, "not_started" if released else "reservation_retained",
                               current.get("reservation_active", True) if current else not released,
                               logs, task_status=current.get("status") if current else None,
                               error="interrupted_before_launch" if released else "interruption_outcome_unknown",
                               signal=signal.Signals(caught.signum).name)
            return _result(task_id, run_id, "reservation_retained", True, logs,
                           exit_code=child_code, task_status="running" if reaped else "unknown",
                           error="interrupted_with_reservation_retained",
                           direct_child_reaped=reaped, signal=signal.Signals(caught.signum).name)
        except Exception:
            if process is not None:
                try:
                    if process.poll() is None:
                        _stop_child(process)
                except Exception:
                    pass
            current = _read_safe(backend, task_id)
            return _result(task_id, run_id, "reservation_retained",
                           current.get("reservation_active", True) if current else True,
                           logs, task_status=current.get("status") if current else None,
                           error="runner_outcome_unknown")
