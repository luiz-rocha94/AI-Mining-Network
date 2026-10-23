"""Killable local worker process for declarative, schema-bound skill execution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

MAX_WORKER_INPUT_BYTES = 4 * 1024 * 1024
MAX_WORKER_OUTPUT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class WorkerLimits:
    timeout_seconds: float = 600
    max_cpu_seconds: float = 120
    max_memory_bytes: int = 512 * 1024 * 1024
    max_input_bytes: int = MAX_WORKER_INPUT_BYTES
    max_output_bytes: int = MAX_WORKER_OUTPUT_BYTES
    poll_interval_seconds: float = 0.05


class WorkerFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _worker_environment(work_dir: Path) -> dict[str, str]:
    """Pass only interpreter/runtime variables; caller credentials are omitted."""
    keys = (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
    )
    environment = {key: os.environ[key] for key in keys if key in os.environ}
    environment["HOME"] = str(work_dir)
    environment["TMPDIR"] = str(work_dir)
    if os.name == "nt":
        environment["USERPROFILE"] = str(work_dir)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _kill_process(
    process: subprocess.Popen[bytes],
    tracked_processes: dict[int, psutil.Process] | None = None,
) -> None:
    try:
        root = psutil.Process(process.pid)
        descendants = root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        descendants = []
    tracked = dict(tracked_processes or {})
    tracked.update({child.pid: child for child in descendants})
    for child in reversed(list(tracked.values())):
        try:
            child.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass
    for child in tracked.values():
        try:
            child.wait(timeout=2)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        finally:
            process.wait(timeout=2)


def _process_tree_usage(
    root: psutil.Process, tracked_processes: dict[int, psutil.Process]
) -> tuple[int, float]:
    """Return aggregate RSS and CPU time for the worker and its descendants."""
    try:
        processes = [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        processes = []
    except psutil.AccessDenied as exc:
        raise WorkerFailure(
            "RESOURCE_MONITOR_UNAVAILABLE",
            "cannot inspect the isolated worker process tree",
        ) from exc
    tracked_processes.update({child.pid: child for child in processes})
    memory_bytes = 0
    cpu_seconds = 0.0
    for child in tracked_processes.values():
        try:
            memory_bytes += child.memory_info().rss
            cpu = child.cpu_times()
            cpu_seconds += cpu.user + cpu.system
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise WorkerFailure(
                "RESOURCE_MONITOR_UNAVAILABLE",
                "cannot inspect worker CPU and memory usage",
            ) from exc
    return memory_bytes, cpu_seconds


def _run_managed_process(
    command: list[str],
    *,
    input_bytes: bytes,
    work_dir: Path,
    environment: dict[str, str],
    limits: WorkerLimits,
    cancel_event: threading.Event | None = None,
) -> bytes:
    if limits.timeout_seconds <= 0 or limits.max_memory_bytes < 16 * 1024 * 1024:
        raise WorkerFailure(
            "INVALID_LIMITS",
            "worker limits must allow positive time and at least 16 MiB of RAM",
        )
    if len(input_bytes) > limits.max_input_bytes:
        raise WorkerFailure(
            "INPUT_TOO_LARGE", "worker request exceeds the configured input limit"
        )
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=work_dir,
            env=environment,
            close_fds=True,
            start_new_session=(os.name != "nt"),
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
    except OSError as exc:
        raise WorkerFailure(
            "WORKER_START_FAILED", f"could not start the skill worker: {exc}"
        ) from exc

    observed = psutil.Process(process.pid)
    tracked_processes = {observed.pid: observed}
    deadline = time.monotonic() + limits.timeout_seconds
    first = True
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _kill_process(process, tracked_processes)
                raise WorkerFailure(
                    "CANCELLED",
                    "skill execution was cancelled and the worker was stopped",
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_process(process, tracked_processes)
                raise WorkerFailure(
                    "DEADLINE_EXCEEDED",
                    "skill execution exceeded its deadline; the worker was stopped",
                )
            try:
                stdout, _stderr = process.communicate(
                    input=input_bytes if first else None,
                    timeout=min(limits.poll_interval_seconds, remaining),
                )
                break
            except subprocess.TimeoutExpired:
                first = False
                try:
                    memory_bytes, cpu_seconds = _process_tree_usage(
                        observed, tracked_processes
                    )
                except WorkerFailure:
                    _kill_process(process, tracked_processes)
                    raise
                if memory_bytes > limits.max_memory_bytes:
                    _kill_process(process, tracked_processes)
                    raise WorkerFailure(
                        "MEMORY_LIMIT_EXCEEDED",
                        "skill worker exceeded its resident-memory limit",
                    )
                if cpu_seconds > limits.max_cpu_seconds:
                    _kill_process(process, tracked_processes)
                    raise WorkerFailure(
                        "CPU_LIMIT_EXCEEDED", "skill worker exceeded its CPU-time limit"
                    )
        if len(stdout) > limits.max_output_bytes:
            raise WorkerFailure(
                "OUTPUT_TOO_LARGE",
                "skill worker response exceeds the configured output limit",
            )
        if process.returncode != 0:
            raise WorkerFailure(
                "WORKER_FAILED", f"skill worker exited with code {process.returncode}"
            )
        return stdout
    finally:
        if process.poll() is None:
            _kill_process(process, tracked_processes)


def run_isolated_skill(
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
    cancel_event: threading.Event | None = None,
    scratch_root: Path | None = None,
    limits: WorkerLimits | None = None,
) -> dict:
    try:
        request = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkerFailure(
            "INVALID_REQUEST", "worker request must contain finite JSON values"
        ) from exc
    effective_limits = limits or WorkerLimits(timeout_seconds=timeout_seconds)
    if limits is not None:
        effective_limits = WorkerLimits(
            timeout_seconds=min(timeout_seconds, limits.timeout_seconds),
            max_cpu_seconds=limits.max_cpu_seconds,
            max_memory_bytes=limits.max_memory_bytes,
            max_input_bytes=limits.max_input_bytes,
            max_output_bytes=limits.max_output_bytes,
            poll_interval_seconds=limits.poll_interval_seconds,
        )
    if scratch_root is not None:
        scratch_root.mkdir(parents=True, exist_ok=True)
    package_root = str(Path(__file__).resolve().parent.parent)
    site_packages = sysconfig.get_paths()["purelib"]
    bootstrap = (
        "import runpy,sys; "
        f"sys.path[:0]={[package_root, site_packages]!r}; "
        "runpy.run_module('ai_mining_network.skill_worker', run_name='__main__')"
    )
    with tempfile.TemporaryDirectory(
        prefix="amn-skill-worker-", dir=scratch_root
    ) as raw_work_dir:
        work_dir = Path(raw_work_dir)
        stdout = _run_managed_process(
            [getattr(sys, "_base_executable", sys.executable), "-I", "-c", bootstrap],
            input_bytes=request,
            work_dir=work_dir,
            environment=_worker_environment(work_dir),
            limits=effective_limits,
            cancel_event=cancel_event,
        )
    try:
        response = json.loads(stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerFailure(
            "INVALID_WORKER_RESPONSE", "skill worker returned invalid JSON"
        ) from exc
    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
        raise WorkerFailure(
            "INVALID_WORKER_RESPONSE",
            "skill worker returned an invalid response envelope",
        )
    if not response["ok"]:
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        raise WorkerFailure(
            str(error.get("code", "WORKER_FAILED")),
            str(error.get("message", "worker failed")),
        )
    result = response.get("result")
    if not isinstance(result, dict):
        raise WorkerFailure(
            "INVALID_WORKER_RESPONSE", "skill worker result must be an object"
        )
    return result
