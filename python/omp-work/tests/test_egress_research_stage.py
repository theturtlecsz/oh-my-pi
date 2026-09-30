"""Tests for the research-stage sandbox launcher (OMP-431-s08).

Refusals are proven without any process: a research identity on
:func:`run_sandboxed` raises ``research_stage_launcher``, and
:func:`run_research_stage` refuses a worktree, context paths, and a credential
env before launching. The shared body is driven with faked tools so the config
handed to the helper carries ``root``. Namespace tests use the s06 unshare
probe and skip when it fails. A research root enters the filesystem jail: the
worker sees only the jail root, and a missing or empty CA certificate leaves
setup_ok absent so the runner raises :class:`SandboxUnavailable`.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from omp_work import egress_sandbox, egress_sandbox_helper
from omp_work.egress_gateway import EgressGateway
from omp_work.egress_policy import Identity, MemoryRecorder, ProjectEgress, build_policy
from omp_work.egress_sandbox import (
    ResearchStageRefused,
    SandboxUnavailable,
    run_research_stage,
    run_sandboxed,
)
from omp_work.egress_tls import GatewayCA


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


def test_research_stage_cwd_refused(
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
            cwd="/tmp/cwd",
        )

    assert exc.value.code == "worktree_not_allowed"
    assert recorder.records == []
    assert not sockets_root.exists()


def test_research_stage_refusal_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = MemoryRecorder()
    sockets_root = tmp_path / "sockroot"
    _forbid_processes(monkeypatch)

    # workdir wins over context and credentials
    with pytest.raises(ResearchStageRefused) as exc:
        run_research_stage(
            ["true"],
            _identity("research"),
            recorder,
            "/tmp/worktree",
            {"GH_TOKEN": "secret"},
            5.0,
            sockets_root=sockets_root,
            context_paths=("/tmp/context",),
        )
    assert exc.value.code == "worktree_not_allowed"

    # cwd wins over context and credentials
    with pytest.raises(ResearchStageRefused) as exc:
        run_research_stage(
            ["true"],
            _identity("research"),
            recorder,
            None,
            {"GH_TOKEN": "secret"},
            5.0,
            sockets_root=sockets_root,
            cwd="/tmp/cwd",
            context_paths=("/tmp/context",),
        )
    assert exc.value.code == "worktree_not_allowed"

    # context wins over credentials
    with pytest.raises(ResearchStageRefused) as exc:
        run_research_stage(
            ["true"],
            _identity("research"),
            recorder,
            None,
            {"GH_TOKEN": "secret"},
            5.0,
            sockets_root=sockets_root,
            context_paths=("/tmp/context",),
        )
    assert exc.value.code == "context_not_allowed"

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

    rc = run_research_stage(
        ["true"],
        _identity("research"),
        recorder,
        None,
        {"PATH": "/usr/bin", "LANG": "C", "TERM": "xterm", "TZ": "UTC", "LC_ALL": "C"},
        5.0,
        sockets_root=tmp_path / "sockroot",
    )

    assert rc == 0
    assert len(captured) == 1
    assert captured[0]["root"] == "research"
    assert captured[0]["workdir"] is None


def test_research_stage_env_none_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, Any]] = []
    _capture_helper_config(monkeypatch, captured)

    rc = run_research_stage(
        ["true"],
        _identity("research"),
        MemoryRecorder(),
        None,
        None,
        5.0,
        sockets_root=tmp_path / "sockroot",
    )

    assert rc == 0
    assert len(captured) == 1
    assert captured[0]["root"] == "research"
    assert captured[0]["env"] is None
    assert captured[0]["workdir"] is None


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


def test_helper_unknown_root_fails_closed_before_setup_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_ok = tmp_path / "setup_ok"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "root": "unknown",
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


def _unshare_probe_fails() -> bool:
    """The s06 namespace probe. Namespace tests skip when this cannot unshare."""
    try:
        probe = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--net", "--mount", "--pid", "--fork", "true"],
            capture_output=True,
            timeout=5,
        )
        return probe.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        return True


_skip_namespace = pytest.mark.skipif(_unshare_probe_fails(), reason="unshare probe failed")


def test_research_worker_env_is_closed() -> None:
    env = egress_sandbox_helper._worker_env(
        {
            "root": "research",
            "env": {
                "LANG": "C.UTF-8",
                "TERM": "dumb",
                "TZ": "UTC",
                "LC_ALL": "C",
                "GH_TOKEN": "nope",
                "HOME": "/root",
            },
            "proxy_sock": "/tmp/proxy.sock",
            "ca_cert_path": "/tmp/host-ca.crt",
        },
        None,
    )
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/research"
    assert env["LANG"] == "C.UTF-8"
    assert env["LC_ALL"] == "C"
    assert "GH_TOKEN" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert env["HTTP_PROXY"] == "http://127.0.0.1:3128"
    assert env["NO_PROXY"] == ""
    assert env["SSL_CERT_FILE"] == "/etc/ssl/certs/ca-certificates.crt"
    assert set(env) == {
        "PATH",
        "LANG",
        "TERM",
        "TZ",
        "LC_ALL",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "GIT_SSL_CAINFO",
    }


def _gateway(tmp_path: Path, ca: GatewayCA, recorder: MemoryRecorder) -> EgressGateway:
    """An s07-style gateway: real proxy socket, policy, and this CA."""
    policy = build_policy(
        ProjectEgress(registries=(), remotes=(), decision_id="dec-research-jail"),
        (),
        "proxy.test:8443",
    )
    return EgressGateway(
        policy_source=lambda identity, now: policy,
        recorder=recorder,
        fetch=None,
        resolver=lambda host, port: (),
        connector=lambda ip, port, timeout: None,
        model_proxy_upstream=("upstream.test", 9443),
        clock=lambda: datetime.now(UTC),
        tls=ca,
    )


def _json_line(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return json.loads(stripped)
    raise AssertionError(f"worker printed no JSON: {text!r}")


@_skip_namespace
@pytest.mark.parametrize("kind", ["missing", "empty"])
def test_research_stage_bad_ca_is_unavailable(tmp_path: Path, kind: str) -> None:
    ca_path = tmp_path / "ca.crt"
    if kind == "empty":
        ca_path.write_bytes(b"")
    with pytest.raises(SandboxUnavailable) as exc:
        run_research_stage(
            ["/usr/bin/true"],
            _identity("research"),
            MemoryRecorder(),
            None,
            None,
            30.0,
            sockets_root=tmp_path / "sockroot",
            ca_cert_path=ca_path,
        )
    assert "setup failed" in str(exc.value)


@_skip_namespace
def test_research_jail_hides_host(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=git_env)
    (repo / "README").write_text("research-secret\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True, env=git_env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=git_env)

    credential = tmp_path / "credential"
    credential.write_text("ghp_research_secret\n", encoding="utf-8")
    ca = GatewayCA(tmp_path / "tls")
    ca_key = tmp_path / "tls" / "ca.key"
    assert ca_key.is_file()
    assert ca.ca_cert_path.read_bytes()

    recorder = MemoryRecorder()
    gateway = _gateway(tmp_path, ca, recorder)
    host_home = str(Path.home())
    spec = {
        "paths": [str(repo), str(credential), str(ca_key), host_home, "/run", "/var", "/root"],
    }
    code = (
        "import base64, json, os, subprocess\n"
        "from pathlib import Path\n"
        f"spec = json.loads({json.dumps(json.dumps(spec))})\n"
        "paths = spec['paths']\n"
        "leaks = []\n"
        "proc = '/proc'\n"
        "if os.path.isdir(proc):\n"
        "    for name in os.listdir(proc):\n"
        "        if not name.isdigit():\n"
        "            continue\n"
        "        root = '/proc/' + name + '/root'\n"
        "        for path in paths:\n"
        "            if os.path.lexists(root + path):\n"
        "                leaks.append(root + path)\n"
        "git = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True)\n"
        "ca = Path('/etc/ssl/certs/ca-certificates.crt').read_bytes()\n"
        "print(json.dumps({\n"
        "    'root': sorted(os.listdir('/')),\n"
        "    'cwd': os.getcwd(),\n"
        "    'home': str(Path.home()),\n"
        "    'absent': {path: os.path.lexists(path) for path in paths},\n"
        "    'leaks': leaks,\n"
        "    'git_rc': git.returncode,\n"
        "    'env': dict(os.environ),\n"
        "    'ca_b64': base64.b64encode(ca).decode('ascii'),\n"
        "}))\n"
    )
    rc = run_research_stage(
        ["/usr/bin/python3", "-c", code],
        _identity("research"),
        recorder,
        None,
        {"LANG": "C.UTF-8", "TERM": "dumb", "TZ": "UTC", "LC_ALL": "C", "LC_CTYPE": "C"},
        45.0,
        sockets_root=tmp_path / "sockroot",
        gateway=gateway,
        ca_cert_path=ca.ca_cert_path,
    )
    out, err = capfd.readouterr()
    assert rc == 0, err
    payload = _json_line(out)
    assert payload["root"] == [
        "bin",
        "dev",
        "etc",
        "home",
        "lib",
        "lib64",
        "proc",
        "sbin",
        "tmp",
        "usr",
    ]
    assert payload["cwd"] == "/home/research"
    assert payload["home"] == "/home/research"
    assert payload["absent"] == {path: False for path in spec["paths"]}
    assert payload["leaks"] == []
    assert payload["git_rc"] != 0
    expected_env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "TZ": "UTC",
        "LC_ALL": "C",
        "LC_CTYPE": "C",
        "HOME": "/home/research",
        "NO_PROXY": "",
        "no_proxy": "",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        expected_env[name] = "http://127.0.0.1:3128"
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO"):
        expected_env[name] = "/etc/ssl/certs/ca-certificates.crt"
    assert payload["env"] == expected_env
    assert base64.b64decode(payload["ca_b64"]) == ca.ca_cert_path.read_bytes()
