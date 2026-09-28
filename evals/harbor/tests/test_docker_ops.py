"""docker argv helpers against the fake docker binary."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from omp_harbor_eval.docker_ops import (
    DockerError,
    apply_patch,
    compose_container,
    export_bundle,
    pid_killer,
    read_file,
    rpc_command,
    write_file,
)

DOCKER = Path(__file__).resolve().parent / "fake_docker.py"
SESSION = "/dev/shm/omp-250-s07-s01-session"


@pytest.fixture
def fake_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "docker"
    directory.mkdir()
    monkeypatch.setenv("FAKE_DOCKER_DIR", str(directory))
    _write_config(directory)
    return directory


def test_apply_patch_applies_and_records_stdin_hash(fake_dir: Path) -> None:
    repo = _repo(fake_dir)
    (repo / "a.txt").write_text("patched\n", encoding="utf-8")
    patch = _git(repo, "diff")
    _git(repo, "checkout", "--", "a.txt")
    assert (repo / "a.txt").read_text(encoding="utf-8") == "base\n"

    apply_patch("worker", patch, "/workspace", docker=str(DOCKER))

    assert (repo / "a.txt").read_text(encoding="utf-8") == "patched\n"
    record = _calls(fake_dir)[-1]
    assert record["argv"][1:] == ["exec", "-i", "worker", "git", "-C", "/workspace", "apply", "-"]
    assert record["stdin_sha256"] == hashlib.sha256(patch).hexdigest()


def test_apply_patch_rejects_a_bad_patch(fake_dir: Path) -> None:
    repo = _repo(fake_dir)
    patch = b"this is not a patch\n"
    with pytest.raises(DockerError):
        apply_patch("worker", patch, "/workspace", docker=str(DOCKER))
    assert (repo / "a.txt").read_text(encoding="utf-8") == "base\n"
    assert _calls(fake_dir)[-1]["stdin_sha256"] == hashlib.sha256(patch).hexdigest()


def test_export_bundle_clone_contains_the_uncommitted_edit(fake_dir: Path, tmp_path: Path) -> None:
    repo = _repo(fake_dir)
    (repo / "extra.txt").write_text("uncommitted\n", encoding="utf-8")
    dest = tmp_path / "worker.bundle"

    export_bundle("worker", "/workspace", dest, docker=str(DOCKER))

    clone = tmp_path / "clone"
    cloned = subprocess.run(
        ["git", "clone", str(dest), str(clone)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cloned.returncode == 0, cloned.stderr
    assert (clone / "extra.txt").read_text(encoding="utf-8") == "uncommitted\n"
    assert (clone / "a.txt").read_text(encoding="utf-8") == "base\n"
    author = subprocess.run(
        ["git", "log", "-1", "--format=%an %ae"],
        cwd=clone,
        capture_output=True,
        text=True,
        check=True,
    )
    assert author.stdout.strip() == "omp-harbor omp-harbor@localhost"


def test_export_bundle_bad_cp_leaves_no_dest(fake_dir: Path, tmp_path: Path) -> None:
    _repo(fake_dir)
    dest = tmp_path / "bundle-out"
    _write_config(fake_dir, fail=["bundle-out"])

    with pytest.raises(DockerError):
        export_bundle("worker", "/workspace", dest, docker=str(DOCKER))

    assert not dest.exists()
    commands = [record["argv"][1] for record in _calls(fake_dir)]
    assert "cp" in commands


def test_export_bundle_rejects_a_non_bundle(fake_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git_wrapper(tmp_path, monkeypatch, "not-a-bundle\n")
    dest = tmp_path / "worker.bundle"

    with pytest.raises(DockerError):
        export_bundle("worker", "/workspace", dest, docker=str(DOCKER))

    assert not dest.exists()
    assert any(record["argv"][1] == "cp" for record in _calls(fake_dir))


def test_export_bundle_accepts_a_v3_header(fake_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git_wrapper(tmp_path, monkeypatch, "# v3 git bundle\n")
    dest = tmp_path / "worker.bundle"

    export_bundle("worker", "/workspace", dest, docker=str(DOCKER))

    assert dest.read_bytes().startswith(b"# v3 git bundle\n")


def test_rpc_command_records_the_session_and_pid_killer_stops_it(fake_dir: Path, tmp_path: Path) -> None:
    seen = tmp_path / "seen.json"
    script = "import json, sys, time, pathlib\npathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[1:]))\ntime.sleep(30)\n"
    _write_config(fake_dir, omp=[sys.executable, "-c", script, str(seen)])
    omp_args = ["--mode", "rpc", "--model", "scripted/scripted", "--session", SESSION]
    command = rpc_command("worker", omp_args, docker=str(DOCKER))
    assert command == [str(DOCKER), "exec", "-i", "worker", "sh", "-c", 'echo $$ >/tmp/omp.pid; exec omp "$@"', "sh", *omp_args]

    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    pid: int | None = None
    try:
        _wait_for(seen, proc)
        recorded = json.loads(seen.read_text(encoding="utf-8"))
        assert recorded == [str(seen), "--session", SESSION]
        omp_argv = json.loads((fake_dir / "omp-argv.jsonl").read_text(encoding="utf-8").splitlines()[-1])["argv"]
        assert omp_argv[-2:] == ["--session", SESSION]
        pid = int((fake_dir / "root" / "tmp" / "omp.pid").read_text(encoding="utf-8").strip())
        pid_killer("worker", docker=str(DOCKER))(proc)
        proc.wait(timeout=5)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        _reap(proc, pid)


def test_compose_container_requires_exactly_one_id(fake_dir: Path) -> None:
    _write_config(fake_dir, containers={"worker": [], "workservice": ["svc"]})
    with pytest.raises(DockerError):
        compose_container("harborproj", "worker", docker=str(DOCKER))

    _write_config(fake_dir, containers={"worker": ["one", "two"], "workservice": ["svc"]})
    with pytest.raises(DockerError):
        compose_container("harborproj", "worker", docker=str(DOCKER))

    _write_config(fake_dir, containers={"worker": ["only"], "workservice": ["svc"]})
    assert compose_container("harborproj", "worker", docker=str(DOCKER)) == "only"
    assert _calls(fake_dir)[-1]["argv"][1:] == [
        "ps",
        "-q",
        "--filter",
        "label=com.docker.compose.project=harborproj",
        "--filter",
        "label=com.docker.compose.service=worker",
    ]


def test_read_file_roundtrips_bytes(fake_dir: Path) -> None:
    payload = b"\x00\xffmodels"
    write_file("worker", "/home/agent/.omp/agent/models.yml", payload, docker=str(DOCKER))
    assert read_file("worker", "/home/agent/.omp/agent/models.yml", docker=str(DOCKER)) == payload
    host = fake_dir / "root" / "home" / "agent" / ".omp" / "agent" / "models.yml"
    assert host.read_bytes() == payload


def test_fake_docker_answers_service_reads_and_detached_run(fake_dir: Path) -> None:
    _write_config(
        fake_dir,
        responses={"w1": {"/v1/health/ready": {"status": 200, "body": {"ok": True}}}},
    )
    script = "open('/workspace/ran','w').write('yes')"
    hit = subprocess.run(
        [str(DOCKER), "exec", "-i", "w1", "python", "-c", script, "http://127.0.0.1:8080/v1/health/ready"],
        capture_output=True,
        check=False,
    )
    assert hit.returncode == 0
    assert json.loads(hit.stdout) == {"status": 200, "body": {"ok": True}}
    assert not (fake_dir / "root" / "workspace" / "ran").exists()

    missed = subprocess.run(
        [str(DOCKER), "exec", "w1", "python", "-c", script, "http://127.0.0.1:8080/missing"],
        capture_output=True,
        check=False,
    )
    assert missed.returncode == 0, missed.stderr
    assert (fake_dir / "root" / "workspace" / "ran").read_text(encoding="utf-8") == "yes"

    started = subprocess.run([str(DOCKER), "run", "-d", "--network", "container:w1", "image"], capture_output=True, check=False)
    assert started.returncode == 0
    assert len(started.stdout.strip()) == 12
    assert subprocess.run([str(DOCKER), "rm", "w1"], capture_output=True, check=False).returncode == 0
    assert subprocess.run([str(DOCKER), "compose", "up"], capture_output=True, check=False).returncode == 0


def _write_config(fake_dir: Path, **overrides: object) -> None:
    config: dict[str, object] = {"containers": {}, "fail": [], "omp": [], "responses": {}}
    config.update(overrides)
    (fake_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _calls(fake_dir: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in (fake_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]


def _repo(fake_dir: Path) -> Path:
    repo = fake_dir / "root" / "workspace"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _git(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr.decode()
    return completed.stdout


def _git_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bundle_text: str) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    body = tmp_path / "bundle-body"
    body.write_bytes(bundle_text.encode())
    git_wrap = bindir / "git"
    git_wrap.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "bundle" ]; then\n'
        f"  cat {shlex.quote(str(body))} > \"$3\"\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    git_wrap.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def _wait_for(path: Path, proc: subprocess.Popen[bytes] | None = None) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return
        if proc is not None and proc.poll() is not None:
            break
        time.sleep(0.02)
    detail = ""
    if proc is not None and proc.stderr is not None:
        detail = proc.stderr.read().decode(errors="replace")
    code = proc.poll() if proc is not None else None
    raise AssertionError(f"timed out waiting for {path} rc={code} stderr={detail}")


def _reap(proc: subprocess.Popen[bytes], pid: int | None) -> None:
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    if pid is None:
        return
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    os.kill(pid, signal.SIGKILL)
