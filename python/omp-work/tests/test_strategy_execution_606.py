"""OMP-606: real jail invocation, protocol boundary and failure accounting.

Jail tests deliberately fail (never skip) if this machine cannot sandbox.
Trusted worker probes exercise OS containment independently of AST rejection.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from omp_work.egress_sandbox import SandboxUnavailable
from omp_work.jobs.runner import LocalJailBackend, RunRequest, RunResult, _OUTPUT_LIMIT
from omp_work.research import strategy_execution as execution
from omp_work.research.strategy_protocol import MAX_PROPOSAL_BYTES, MAX_SOURCE_BYTES


VALID = b'def propose(steps, opened, *, seed=0, width=1):\n    return "leaf", seed + width\n'
STEPS = {"root": {"parent_id": None}, "leaf": {"parent_id": "root", "metric": 2}}


def invoke(source: bytes = VALID, **kwargs: object) -> execution.StrategyExecution:
    options = dict(seed=7, width=2, allowed_actions=["open"], resource_caps={"cpu": 1}, timeout=10)
    options.update(kwargs)
    return execution.execute_strategy(source, STEPS, ["root"], **options)


class InspectJail(LocalJailBackend):
    """Inspect/replace trusted transport for fault injection; execution is always real."""

    def __init__(self, replacement: str | None = None) -> None:
        self.replacement = replacement
        self.work: Path | None = None
        self.ready = threading.Event()
        self.result: RunResult | None = None
        self.files: set[str] = set()

    def run(self, request: RunRequest, cancel: threading.Event | None = None) -> RunResult:
        self.work = Path(request.workdir)
        self.files = {str(p.relative_to(self.work)) for p in self.work.rglob("*") if p.is_file()}
        if self.replacement is not None:
            (self.work / "strategy_worker.py").write_text(self.replacement, encoding="utf-8")
        self.ready.set()
        self.result = super().run(request, cancel)
        beat = self.work / "heartbeat"
        if beat.exists():
            last = beat.read_bytes()
            time.sleep(0.25)
            assert beat.read_bytes() == last, "worker still writes after backend terminated"
        return self.result


def install(monkeypatch: pytest.MonkeyPatch, jail: InspectJail) -> None:
    monkeypatch.setattr(execution, "LocalJailBackend", lambda: jail)


def test_valid_selection_keeps_controller_snapshot_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    jail = InspectJail()
    install(monkeypatch, jail)
    source = b'''def propose(steps, opened, *, seed=0, width=1):
    steps["leaf"]["metric"] = 999
    return "leaf", seed + width
'''
    result = invoke(source)
    assert result.failure is None, result
    assert result.selection == {"step_id": "leaf", "action": "open", "resources": {}}
    assert result.seed == 9
    assert STEPS["leaf"]["metric"] == 2
    assert result.usage.duration > 0 and result.usage.output_bytes > 0
    assert jail.files == {
        "input.json", "strategy_worker.py", "omp_work/__init__.py",
        "omp_work/research/__init__.py", "omp_work/v1/__init__.py",
        "omp_work/research/strategy_protocol.py", "omp_work/v1/canonical.py",
    }
    assert jail.work is not None and not jail.work.exists()


@pytest.mark.parametrize(("prefix", "failure"), [
    (b"import os\n", "forbidden_import"),
    (b"evaluator = {}\n", "evaluator_tampering"),
])
def test_forbidden_code_fails_in_real_jail(prefix: bytes, failure: str) -> None:
    result = invoke(prefix + VALID)
    assert result.selection is None and result.failure == failure
    assert result.usage.duration > 0 and result.usage.output_bytes > 0


@pytest.mark.parametrize(("source", "failure"), [
    (b'def propose(steps, opened, *, seed=0, width=1):\n    return 3\n', "crashed"),
    (b'def propose(steps, opened, *, seed=0, width=1):\n    return "x" * 5000, seed\n', "output_too_large"),
    (b'RESOURCES = {"cpu": 2}\n' + VALID, "invalid_selection"),
    (b'def propose(steps, opened, *, seed=0, width=1):\n    steps["fake"] = {}\n    return "fake", seed\n', "invalid_selection"),
])
def test_failed_attempts_keep_usage_without_selection(
    monkeypatch: pytest.MonkeyPatch, source: bytes, failure: str,
) -> None:
    jail = InspectJail()
    install(monkeypatch, jail)
    result = invoke(source)
    assert result.failure == failure and result.selection is None
    assert result.usage.duration > 0 and result.usage.output_bytes > 0
    assert result.seed == 7
    assert jail.result is not None
    assert result.usage.duration == jail.result.duration
    assert result.usage.output_bytes == len(jail.result.output)
    assert result.usage.exit_code == jail.result.exit_code


def test_kernel_memory_cap_fails_bounded_allocation_without_selection() -> None:
    source = b'def propose(steps, opened, *, seed=0, width=1):\n    blob = [0] * (32 * 1024 * 1024)\n    return "leaf", seed\n'
    result = invoke(source, memory_bytes=64 << 20)
    assert result.selection is None and result.failure == "crashed"
    assert result.usage.duration > 0 and result.usage.output_bytes > 0


@pytest.mark.parametrize("wire", [b"not json", b"x" * (MAX_PROPOSAL_BYTES + 1),
    json.dumps({"proposal": {"step_id": "leaf", "action": "open", "resources": {}},
                "seed": 8, "score": 999}).encode()])
def test_malformed_transport_never_supplies_selection(monkeypatch: pytest.MonkeyPatch, wire: bytes) -> None:
    jail = InspectJail(f"import sys\nsys.stdout.buffer.write({wire!r})\n")
    install(monkeypatch, jail)
    result = invoke()
    assert result.selection is None
    assert result.failure in {"invalid_output", "output_too_large"}
    assert result.usage.output_bytes == len(wire)
    assert result.usage.duration > 0


# Bounded, sleeping heartbeat; no busy loop or load generator.
HEARTBEAT = '''import os
import time
from pathlib import Path
os.setsid()
for i in range(30):
    with Path("/work/heartbeat").open("a") as f:
        f.write("x")
    time.sleep(0.1)
'''


@pytest.mark.parametrize("canceled", [False, True])
def test_real_jail_stops_expired_and_canceled_workers(
    monkeypatch: pytest.MonkeyPatch, canceled: bool,
) -> None:
    jail = InspectJail(HEARTBEAT)
    install(monkeypatch, jail)
    cancel = threading.Event()
    errors: list[str] = []

    def cancel_after_start() -> None:
        if not jail.ready.wait(5):
            errors.append("backend never started")
            cancel.set()
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if jail.work is not None and (jail.work / "heartbeat").exists():
                cancel.set()
                return
            time.sleep(0.02)
        errors.append("worker never started")
        cancel.set()

    thread = threading.Thread(target=cancel_after_start) if canceled else None
    if thread is not None:
        thread.start()
    started = time.monotonic()
    try:
        result = invoke(timeout=8 if canceled else 1.5, cancel=cancel)
    finally:
        if thread is not None:
            thread.join(timeout=6)
            assert not thread.is_alive()
    assert not errors
    assert result.failure == ("canceled" if canceled else "timed_out"), result
    assert result.selection is None and result.usage.exit_code == -9
    assert result.usage.duration > 0 and time.monotonic() - started < 7
    assert jail.work is not None and not jail.work.exists()


def test_real_jail_hides_outside_files_credentials_and_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = tmp_path / "outside-secret"
    sentinel.write_text("controller secret", encoding="utf-8")
    monkeypatch.setenv("STRATEGY_SECRET", "do not expose")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        probe = f'''import os
import socket
from pathlib import Path
assert not Path({str(sentinel)!r}).exists()
assert os.environ.get("STRATEGY_SECRET") is None
assert not Path("/work/omp_work/jobs").exists()
s = socket.socket()
s.settimeout(0.3)
try:
    s.connect(("127.0.0.1", {port}))
except OSError:
    pass
else:
    raise AssertionError("host network reachable")
finally:
    s.close()
print('{{"proposal":{{"step_id":"leaf","action":"open","resources":{{}}}},"seed":9}}')
'''
        install(monkeypatch, InspectJail(probe))
        result = invoke()
    assert result.failure is None, result
    assert result.selection is not None
    assert sentinel.read_text(encoding="utf-8") == "controller secret"


def test_sandbox_refusal_never_loads_source_on_host(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: LocalJailBackend, request: RunRequest, cancel: threading.Event | None = None) -> RunResult:
        raise SandboxUnavailable("refused")

    def host_load_forbidden(source: str) -> None:
        pytest.fail("candidate executed on controller")

    monkeypatch.setattr(LocalJailBackend, "run", refuse)
    monkeypatch.setattr(execution.strategy_protocol, "load_strategy", host_load_forbidden)
    result = invoke()
    assert result.selection is None and result.failure == "sandbox_unavailable"
    assert result.usage.duration > 0 and result.usage.exit_code is None


def test_input_caps_and_preexpired_attempts_never_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected() -> LocalJailBackend:
        pytest.fail("backend launched for rejected input")

    monkeypatch.setattr(execution, "LocalJailBackend", unexpected)
    assert invoke(b"x" * (MAX_SOURCE_BYTES + 1)).failure == "too_large"
    assert invoke(timeout=0).failure == "timed_out"
    cancel = threading.Event()
    cancel.set()
    assert invoke(cancel=cancel).failure == "canceled"
    assert invoke(resource_caps={"large": "x" * _OUTPUT_LIMIT}).failure == "input_too_large"
