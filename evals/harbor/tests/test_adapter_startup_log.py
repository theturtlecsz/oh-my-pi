"""When omp fails to start, the adapter copies the named startup log into evidence."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from omp_harbor_eval import (
    EvidenceWriter,
    RpcAdapter,
    ServiceProbe,
    load_evidence,
    load_fixture,
)

LOG_PATH = "/home/agent/.omp/logs/omp.2026-09-28.60.log"
LOG_BYTES = b"loadExtensions still open\n"
STDERR = (
    "Still starting after 10s — phase: loadExtensions\n"
    f"  logs: {LOG_PATH} · re-run with PI_DEBUG_STARTUP=1 for streaming phase markers\n"
)


def _write_fixture(root: Path) -> object:
    directory = root / "f1"
    directory.mkdir(parents=True)
    fixture = {
        "id": "f1",
        "scored_experiment": "structured-amendment",
        "seed_patch": "",
        "solution_patch": "",
        "independent_tests": [],
        "scenario": "scenario.json",
        "rules": [],
    }
    scenario = {
        "command": "/execute",
        "terminal": {"pointer": "/work_item/state", "in": ["completed"]},
        "model_script": [],
        "ui_script": [],
        "timeout_s": 3.0,
    }
    (directory / "fixture.json").write_text(json.dumps(fixture) + "\n", encoding="utf-8")
    (directory / "scenario.json").write_text(json.dumps(scenario) + "\n", encoding="utf-8")
    return load_fixture(root, "f1")


def _open_sealed(directory: Path):
    digest = hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    return load_evidence(directory, digest)


def test_startup_failure_copies_the_worker_omp_log(tmp_path: Path) -> None:
    """A process that dies during startup names its omp log; the adapter stores those bytes."""

    fixture = _write_fixture(tmp_path / "fixtures")
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir, "run-1", "nonce-1", fixture.id, fixture.digest, "known_good", fixture.scored_experiment
    )
    script = tmp_path / "fail_rpc.py"
    script.write_text(
        "import sys, time\n"
        f"sys.stderr.write({STDERR!r})\n"
        "sys.stderr.flush()\n"
        "time.sleep(0.3)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    seen: list[str] = []

    def reader(path: str) -> bytes:
        seen.append(path)
        if path == LOG_PATH:
            return LOG_BYTES
        raise FileNotFoundError(path)

    adapter = RpcAdapter(
        [sys.executable, str(script)],
        tmp_path,
        None,
        ServiceProbe("http://127.0.0.1:9", "token", "00000000-0000-4000-8000-0000000000aa"),
        writer,
        fixture.scenario,
        session_reader=reader,
    )
    outcome = adapter.run()
    assert outcome == "harness_error"
    assert LOG_PATH in seen

    loaded = _open_sealed(evidence_dir)
    assert loaded.read_bytes("omp-startup.log") == LOG_BYTES
    reason = loaded.read_json("outcome.json")["reason"]
    assert isinstance(reason, str)
    assert LOG_PATH in reason
    assert loaded.read_json("outcome.json")["prompts_sent"] == 0
