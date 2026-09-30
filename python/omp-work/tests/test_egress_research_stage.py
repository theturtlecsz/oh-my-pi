"""Tests for the research-stage sandbox launcher (OMP-431-s08).

Refusals are proven without any process: a research identity on
:func:`run_sandboxed` raises ``research_stage_launcher``, and
:func:`run_research_stage` refuses a worktree, context paths, and a credential
env before launching. The shared body is driven with faked tools so the config
handed to the helper carries ``root``; the helper then fails closed on that
root before the sandbox is signalled ready.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pytest

from omp_work import egress_sandbox, egress_sandbox_helper
from omp_work.egress_policy import Identity, MemoryRecorder
from omp_work.egress_sandbox import (
    ResearchStageRefused,
    run_research_stage,
    run_sandboxed,
)


def _identity(stage: str) -> Identity:
    return Identity(
        workspace_id="ws-research-431",
        project_id="proj-research-431",
        mission_id="mission-research-431",
        worker_id="worker-research-431",
        stage=stage,
    )


def _forbid_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no process may start before the refusal")

    monkeypatch.setattr(egress_sandbox.subprocess, "run", boom)
    monkeypatch.setattr(egress_sandbox.subprocess, "Popen", boom)


def test_run_sandboxed_research_identity_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = MemoryRecorder()
    sockets_root = tmp_path / "sockroot"
    _forbid_processes(monkeypatch)

    with pytest.raises(ResearchStageRefused) as exc:
        run_sandboxed(
            ["true"],
            _identity("research"),
            recorder,
            None,
            None,
            5.0,
            sockets_root=sockets_root,
        )

    assert exc.value.code == "research_stage_launcher"
    assert recorder.records == []
    assert not sockets_root.exists()


def test_research_stage_worktree_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = MemoryRecorder()
    sockets_root = tmp_path / "sockroot"
    _forbid_processes(monkeypatch)

    with pytest.raises(ResearchStageRefused) as exc:
        run_research_stage(
            ["true"],
            _identity("research"),
            recorder,
            "/tmp/worktree",
            None,
            5.0,
            sockets_root=sockets_root,
        )

    assert exc.value.code == "worktree_not_allowed"
    assert recorder.records == []
    assert not sockets_root.exists()


def test_research_stage_context_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = MemoryRecorder()
    sockets_root = tmp_path / "sockroot"
    _forbid_processes(monkeypatch)

    with pytest.raises(ResearchStageRefused) as exc:
        run_research_stage(
            ["true"],
            _identity("research"),
            recorder,
            None,
            None,
            5.0,
            sockets_root=sockets_root,
            context_paths=("/tmp/context",),
        )

    assert exc.value.code == "context_not_allowed"
    assert recorder.records == []
    assert not sockets_root.exists()


def test_research_stage_credential_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = MemoryRecorder()
    sockets_root = tmp_path / "sockroot"
    _forbid_processes(monkeypatch)

    with pytest.raises(ResearchStageRefused) as exc:
        run_research_stage(
            ["true"],
            _identity("research"),
            recorder,
            None,
            {"PATH": "/usr/bin", "GH_TOKEN": "secret"},
            5.0,
            sockets_root=sockets_root,
        )

    assert exc.value.code == "credential_not_allowed"
    assert recorder.records == []
    assert not sockets_root.exists()


class _Probe:
    returncode = 0
    stderr = b""


class _HelperProc:
    def __init__(self) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


def _capture_helper_config(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, Any]]
) -> None:
    """Fake the sandbox tools so the shared body runs and records its config."""

    def fake_run(*args: Any, **kwargs: Any) -> _Probe:
        return _Probe()

    def fake_popen(argv: list[str], *args: Any, **kwargs: Any) -> _HelperProc:
        config = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        captured.append(config)
        Path(config["setup_ok"]).touch()
        return _HelperProc()

    monkeypatch.setattr(egress_sandbox.subprocess, "run", fake_run)
    monkeypatch.setattr(egress_sandbox.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(egress_sandbox.shutil, "which", lambda name, path=None: f"/usr/bin/{name}")


def test_research_stage_hands_helper_research_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, Any]] = []
    _capture_helper_config(monkeypatch, captured)
    recorder = MemoryRecorder()
    cwd = tmp_path / "research-cwd"

    rc = run_research_stage(
        ["true"],
        _identity("research"),
        recorder,
        None,
        {"PATH": "/usr/bin", "LANG": "C", "TERM": "xterm", "TZ": "UTC", "LC_ALL": "C"},
        5.0,
        sockets_root=tmp_path / "sockroot",
        cwd=cwd,
    )

    assert rc == 0
    assert len(captured) == 1
    assert captured[0]["root"] == "research"
    assert captured[0]["workdir"] == str(cwd)


def test_sandboxed_hands_helper_no_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, Any]] = []
    _capture_helper_config(monkeypatch, captured)

    rc = run_sandboxed(
        ["true"],
        _identity("repository"),
        MemoryRecorder(),
        None,
        None,
        5.0,
        sockets_root=tmp_path / "sockroot",
    )

    assert rc == 0
    assert captured[0]["root"] is None


def test_helper_root_fails_closed_before_setup_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_ok = tmp_path / "setup_ok"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "root": "research",
                "record_sock": str(tmp_path / "record.sock"),
                "setup_ok": str(setup_ok),
                "argv": ["true"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["egress_sandbox_helper", str(config_path)])

    with pytest.raises(SystemExit) as exc:
        egress_sandbox_helper.main()

    assert exc.value.code == 125
    assert not setup_ok.exists()
