"""Controller-owned, fail-closed execution of generated selection strategies.

Only the small protocol runtime and the sanitized invocation enter the jail.
Usage is measured by the backend, never supplied by candidate code. CPU/memory
limits constrain the worker; killed workers have no inferred CPU accounting.
"""

from __future__ import annotations

import json
import math
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omp_work.egress_sandbox import SandboxUnavailable
from omp_work.jobs.runner import BackendIncompatible, LocalJailBackend, RunRequest, _OUTPUT_LIMIT
from omp_work.research import strategy_protocol
from omp_work.v1 import canonical


@dataclass(frozen=True)
class StrategyUsage:
    """Backend wall duration and captured output bytes (bounded by backend cap)."""

    duration: float
    output_bytes: int = 0
    exit_code: int | None = None


@dataclass(frozen=True)
class StrategyExecution:
    selection: dict[str, Any] | None
    seed: int
    failure: str | None
    reasons: tuple[str, ...]
    usage: StrategyUsage


def execute_strategy(
    source: bytes,
    steps: Mapping[str, Any],
    opened: Iterable[Any],
    *,
    seed: int = 0,
    width: int = 1,
    allowed_actions: Iterable[str],
    resource_caps: Mapping[str, Any],
    timeout: float,
    cancel: threading.Event | None = None,
    memory_bytes: int = 128 << 20,
    cpu_seconds: int = 5,
) -> StrategyExecution:
    """Return only a validated selection, or a failed attempt with measured usage.

    ``resource_caps`` limits declared proposal resources. ``memory_bytes`` and
    ``cpu_seconds`` are kernel limits; ``timeout`` bounds wall time. No caller
    environment, repository, evaluator, or credential files are mounted.
    """
    started = time.monotonic()

    def failed(reason: str) -> StrategyExecution:
        return StrategyExecution(None, seed, reason, (), StrategyUsage(time.monotonic() - started))

    if cancel is not None and cancel.is_set():
        return failed("canceled")
    if not math.isfinite(timeout) or timeout <= 0:
        return failed("timed_out")
    if memory_bytes <= 0 or cpu_seconds <= 0:
        return failed("invalid_limits")
    if len(source) > strategy_protocol.MAX_SOURCE_BYTES:
        return failed("too_large")
    try:
        text = source.decode("utf-8")
        snap_steps, snap_opened = strategy_protocol.sanitized_snapshot(steps, opened)
        actions = tuple(allowed_actions)
        caps = dict(resource_caps)
        payload = json.dumps({
            "source": text, "steps": snap_steps, "opened": list(snap_opened),
            "seed": seed, "width": width, "allowed_actions": actions,
            "resource_caps": caps, "memory_bytes": memory_bytes, "cpu_seconds": cpu_seconds,
        }, allow_nan=False).encode("utf-8")
    except (UnicodeError, TypeError, ValueError, OverflowError, RecursionError):
        return failed("invalid_input")
    if len(payload) > _OUTPUT_LIMIT:
        return failed("input_too_large")

    try:
        with tempfile.TemporaryDirectory(prefix="omp-strategy-") as directory:
            work = Path(directory)
            # Empty package initializers avoid importing the controller package graph.
            for package in ("omp_work", "omp_work/research", "omp_work/v1"):
                target = work / package
                target.mkdir(parents=True, exist_ok=True)
                (target / "__init__.py").write_bytes(b"")
            for module, target in (
                (Path(strategy_protocol.__file__), "omp_work/research/strategy_protocol.py"),
                (Path(canonical.__file__), "omp_work/v1/canonical.py"),
                (Path(__file__).with_name("strategy_worker.py"), "strategy_worker.py"),
            ):
                (work / target).write_bytes(module.read_bytes())
            (work / "input.json").write_bytes(payload)
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                return failed("timed_out")
            result = LocalJailBackend().run(RunRequest(
                ["/usr/bin/python3", "-B", "/work/strategy_worker.py"],
                work, timeout=remaining,
            ), cancel)
    except (SandboxUnavailable, BackendIncompatible, OSError):
        return failed("sandbox_unavailable")

    usage = StrategyUsage(result.duration, len(result.output), result.exit_code)

    def rejected(reason: str, reasons: tuple[str, ...] = ()) -> StrategyExecution:
        return StrategyExecution(None, seed, reason, reasons, usage)

    if cancel is not None and cancel.is_set():
        return rejected("canceled")
    if result.status != "completed":
        return rejected(result.status)
    if time.monotonic() - started >= timeout:
        return rejected("timed_out")
    if len(result.output) > strategy_protocol.MAX_PROPOSAL_BYTES:
        return rejected("output_too_large")
    try:
        response = json.loads(result.output)
        if not isinstance(response, dict):
            return rejected("invalid_output")
        if set(response) == {"failure"} and isinstance(response["failure"], str):
            return rejected(response["failure"])
        if set(response) != {"proposal", "seed"} or type(response["seed"]) is not int:
            return rejected("invalid_output")
        reasons = strategy_protocol.validate_proposal(
            response["proposal"], snap_steps, snap_opened,
            allowed_actions=actions, resource_caps=caps,
        )
        if reasons:
            return rejected("invalid_selection", tuple(reasons))
        # Reject JSON non-finite values even where the legacy protocol permits them.
        json.dumps(response, allow_nan=False)
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        return rejected("invalid_output")
    return StrategyExecution(response["proposal"], response["seed"], None, (), usage)
