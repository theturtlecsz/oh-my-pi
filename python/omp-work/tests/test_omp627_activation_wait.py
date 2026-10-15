"""Activation waits for flood's poll cadence without an explicit timeout."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time

import pytest

from omp622_rollout_helpers import ASSETS, SCRIPT, create_checkout


@pytest.mark.parametrize(
    ("config", "activated"),
    [
        ({"poll_seconds": 5}, True),
        (None, True),
        ({"poll_seconds": 1}, False),
        ({"poll_seconds": True}, True),
        ({"poll_seconds": 0}, True),
        ({"poll_seconds": "1"}, True),
        ({"poll_seconds": float("nan")}, True),
        ({"poll_seconds": float("inf")}, True),
    ],
    ids=["poll-5", "missing-config", "poll-1", "bool", "zero", "string", "nan", "infinity"],
)
def test_activation_waits_two_poll_intervals(
    tmp_path: Path, config: dict[str, object] | None, activated: bool,
) -> None:
    """Proof after four seconds succeeds only when the default budget covers it."""
    source, env = create_checkout(tmp_path)
    env.pop("OMP584_POLL_TIMEOUT", None)
    rules = source / "planner-independence.md"
    rules.write_bytes((ASSETS / "omp584-planner-rules.md").read_bytes())
    subprocess.run(
        ["git", "add", "planner-independence.md"], cwd=source, env=env, check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "fixture rules"], cwd=source, env=env, check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, env=env, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    if config is not None:
        (source / "flood.json").write_text(json.dumps(config))
    stage = tmp_path / "stage"
    stage.mkdir()
    for name in ("flood.py", "planner-independence.md"):
        (stage / name).write_bytes((source / name).read_bytes())
    (stage / "target_commit").write_text(head + "\n")
    (stage / "flood.log.offset").write_text("0\n")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    env.update(
        OMP584_FLOOD_DIR=str(source),
        OMP584_STAGE=str(stage),
        OMP584_EVIDENCE_DIR=str(evidence),
        OMP584_PROMPT_PATCH="omp584-planner-prompt.patch",
    )
    record = {
        "event": "loaded",
        "at": "2026-10-15T12:00:04Z",
        "code_sha256": hashlib.sha256((source / "flood.py").read_bytes()).hexdigest(),
        "rules_sha256": hashlib.sha256(rules.read_bytes()).hexdigest(),
        "external_head": head,
    }
    started = time.monotonic()

    def append_reload() -> None:
        time.sleep(max(0, started + 4 - time.monotonic()))
        with (source / "flood.log").open("a") as log:
            log.write(
                f'{record["at"]} [reload] flood.py or flood.json changed: exec in place\n'
                f'{record["at"]} loaded {json.dumps(record)}\n'
            )

    writer = threading.Thread(target=append_reload)
    writer.start()
    try:
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; omp584_activate', "bash", str(SCRIPT)],
            env=env, capture_output=True, text=True, timeout=20,
        )
        elapsed = time.monotonic() - started
    finally:
        writer.join()

    assert result.returncode == (0 if activated else 1), result.stdout + result.stderr
    if activated:
        assert elapsed > 3
        rollout = json.loads((evidence / "rollout.json").read_text())
        assert rollout["activated_at"] == record["at"]
    else:
        assert "Activation timeout" in result.stderr
        assert not (evidence / "rollout.json").exists()
