"""Verifier input refusals, sandbox isolation, output, and failure contracts."""

from ctypes.util import find_library
from dataclasses import FrozenInstanceError
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import Mock

import pytest

from omp_work.egress_policy import Identity, MemoryRecorder
from omp_work.egress_sandbox import SandboxUnavailable
from omp_work.orchestrator import candidate_git, verify_tests, worker_sandbox


def test_result_is_immutable() -> None:
    result = verify_tests.TestResult(exit_code=1, output=b"failed")
    with pytest.raises(FrozenInstanceError):
        result.exit_code = 0
    with pytest.raises(FrozenInstanceError):
        result.output = b"passed"


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch) -> Mock:
    runner = Mock()
    monkeypatch.setattr(verify_tests, "run_sandboxed", runner)
    return runner


@pytest.mark.parametrize(
    "timeout", [0, -1, -0.5, float("nan"), float("inf"), -float("inf"), "1", None, True, False]
)
def test_invalid_timeout_refused_before_execution(
    tmp_path: Path, sandbox: Mock, timeout: object
) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        verify_tests.run_tests(["pytest"], tmp_path, timeout)
    sandbox.assert_not_called()


@pytest.mark.parametrize(
    "command", [[], (), "pytest", b"pytest", None, 1, [1], ["pytest", ""], [""], {"pytest"}]
)
def test_invalid_argv_refused_before_execution(
    tmp_path: Path, sandbox: Mock, command: object
) -> None:
    with pytest.raises(ValueError, match="command"):
        verify_tests.run_tests(command, tmp_path, 1)
    sandbox.assert_not_called()


@pytest.mark.parametrize(
    "kind", ["missing", "relative", "root", "root_alias", "file", "string", "git_dir", "git_file", "git_link"]
)
def test_invalid_snapshot_refused_before_execution(
    tmp_path: Path, sandbox: Mock, kind: str
) -> None:
    snapshot = tmp_path
    if kind == "missing":
        snapshot = tmp_path / "missing"
    elif kind == "relative":
        snapshot = Path(".")
    elif kind == "root":
        snapshot = Path(tmp_path.anchor)
    elif kind == "root_alias":
        snapshot = tmp_path.joinpath(*([".."] * len(tmp_path.parts[1:])))
    elif kind == "file":
        snapshot = tmp_path / "file"
        snapshot.write_bytes(b"not a directory")
    elif kind == "string":
        snapshot = str(tmp_path)
    elif kind == "git_dir":
        (tmp_path / ".git").mkdir()
    elif kind == "git_file":
        (tmp_path / ".git").write_text("gitdir: /producer/control.git")
    elif kind == "git_link":
        (tmp_path / ".git").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="snapshot"):
        verify_tests.run_tests(["pytest"], snapshot, 1)
    sandbox.assert_not_called()


@pytest.mark.parametrize("timeout", [1, 0.25])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_runs_argv_in_readonly_worker_with_closed_environment_and_captured_output(
    tmp_path: Path, sandbox: Mock, monkeypatch: pytest.MonkeyPatch,
    timeout: int | float, exit_code: int,
) -> None:
    monkeypatch.setenv("VERIFIER_SIGNING_KEY", "must-not-enter-jail")
    captured = b"stdout\n\xffstderr\n"

    def run(*args: object, **kwargs: object) -> int:
        output = kwargs["output"]
        assert isinstance(output, bytearray)
        output.extend(captured)
        return exit_code

    sandbox.side_effect = run
    command = ("pytest", "tests/test_feature.py")
    result = verify_tests.run_tests(command, tmp_path, timeout)
    args, kwargs = sandbox.call_args
    assert args[0] == list(command)
    assert isinstance(args[0], list)
    assert args[1] == Identity("worker", "verifier", "verifier", "verifier", "repository")
    assert isinstance(args[2], MemoryRecorder)
    assert args[3:] == (
        "/work",
        {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TERM": "dumb", "TZ": "UTC"},
        timeout,
    )
    assert kwargs == {
        "root": "worker", "worktree": tmp_path,
        "output": bytearray(captured), "worktree_readonly": True,
    }
    assert sandbox.call_count == 1
    assert result.exit_code == exit_code
    assert result.output == captured
    assert isinstance(result.output, bytes)


@pytest.mark.parametrize(
    "error", [SandboxUnavailable("setup failed"), subprocess.TimeoutExpired(["pytest"], 1)]
)
def test_sandbox_failure_propagates_without_retry(
    tmp_path: Path, sandbox: Mock, error: Exception
) -> None:
    sandbox.side_effect = error
    with pytest.raises(type(error)) as raised:
        verify_tests.run_tests(["pytest"], tmp_path, 1)
    assert raised.value is error
    assert sandbox.call_count == 1


@pytest.fixture
def nested_procfs_host() -> None:
    """Skip only real nested launches when userns procfs mounts are refused."""
    try:
        probe = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--mount", "--pid", "--fork", "--mount-proc", "true"],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        pytest.skip(f"userns procfs probe unavailable: {error}")
    if probe.returncode:
        pytest.skip(f"userns procfs mounts unsupported: {probe.stderr.decode(errors='replace')}")


def _nested_sandbox_unavailable(error: str) -> None:
    if (
        error.startswith("Sandbox unshare probe failed (exit code ")
        and "unshare: write failed /proc/self/uid_map: Operation not permitted" in error
    ):
        pytest.skip(f"nested user namespaces refused inside verifier jail: {error}")
    pytest.fail(error)


@pytest.mark.parametrize("error, refused", [
    ("Sandbox unshare probe failed (exit code 1): "
     "unshare: write failed /proc/self/uid_map: Operation not permitted", True),
    ("Sandbox unshare probe failed (exit code 1): "
     "unshare: unshare failed: Operation not permitted", False),
    ("Sandbox unshare probe failed (exit code 1): "
     "unshare: mount /proc failed: Operation not permitted", False),
    ("Required sandbox tool 'unshare' not found on PATH", False),
])
def test_nested_sandbox_unavailable_skips_only_uid_map_refusal(
    error: str, refused: bool,
) -> None:
    expected = pytest.skip.Exception if refused else pytest.fail.Exception
    with pytest.raises(expected) as raised:
        _nested_sandbox_unavailable(error)
    assert error in str(raised.value)
    if refused:
        assert "nested user namespaces" in str(raised.value)


# This driver runs inside the outer verifier jail, with the real installed
# runtime, Git metadata, signing key, audit overlay, and WorkService relay.
_VERIFIER_DRIVER = r'''
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request
from omp_work.egress_sandbox import SandboxUnavailable
from omp_work.orchestrator.verify_candidate import prepare_candidate
from omp_work.orchestrator.verify_tests import run_tests

config = json.loads(sys.argv[1])
python = sys.executable
key = Path(config["key"])
verdict = Path("/work/.omp/verdict.json")
assert key.read_bytes() == b"planted-verifier-key"
assert verdict.read_bytes() == b"signer-verdict"
with urllib.request.urlopen(os.environ["OMP_WORK_URL"], timeout=3) as response:
    assert response.read() == b"relay-reached"
os.environ["VERIFIER_SIGNING_KEY"] = "outer-verifier-secret"
Path("/tmp/outer-secret").write_bytes(b"outer-tmp")

with prepare_candidate(config["task"], Path("/work")) as snapshot:
    assert snapshot.joinpath("payload").read_bytes() == b"frozen-commit-bytes"
    try:
        good = run_tests([python, "-c", r"""
import os
from pathlib import Path
assert Path.cwd() == Path('/work')
assert Path('payload').read_bytes() == b'frozen-commit-bytes'
assert dict(os.environ) == {
    'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'TERM': 'dumb', 'TZ': 'UTC'
}
assert not Path('/work/.git').exists()
assert not Path('/tmp/outer-secret').exists()
Path('/tmp/inner-only').write_bytes(b'private-tmp')
assert Path('/tmp/inner-only').read_bytes() == b'private-tmp'
os.write(1, b'good-stdout\n')
os.write(2, b'good-stderr\n')
"""], snapshot, 15)
    except SandboxUnavailable as error:
        # Distinguish nested setup refusal from driver/assertion failures.
        Path("/work/.omp/nested-sandbox-unavailable.txt").write_text(str(error))
        sys.exit(77)
    assert good.exit_code == 0, good.output
    assert good.output == b"good-stdout\ngood-stderr\n", good.output
    assert not Path("/tmp/inner-only").exists()
    assert Path("/tmp/outer-secret").read_bytes() == b"outer-tmp"

    failed = run_tests([python, "-c", "import sys; sys.exit(7)"], snapshot, 15)
    assert failed.exit_code == 7, failed
    large = run_tests([python, "-c", "import os; os.write(1, b'x' * (2 * 1024 * 1024))"], snapshot, 15)
    assert large.exit_code == 0
    assert large.output == b"x" * (1024 * 1024), len(large.output)

    # Use a parent-namespace PID above the inner namespace's PID range, so
    # an inner process with the same numeric PID cannot masquerade as parent.
    for _ in range(16):
        subprocess.run(["/bin/true"], check=True)
    parent = subprocess.Popen([python, "-c", "import time; time.sleep(60)"])
    try:
        assert Path(f"/proc/{parent.pid}/root{key}").read_bytes() == b"planted-verifier-key"
        assert b"VERIFIER_SIGNING_KEY=outer-verifier-secret" in Path(f"/proc/{parent.pid}/environ").read_bytes()
        paths = [str(key), config["sibling"], "/tmp/outer-secret"]
        for pid in (parent.pid, config["host_pid"]):
            paths.extend([
                f"/proc/{pid}/root{key}",
                f"/proc/{pid}/root/work/.omp/verdict.json",
                f"/proc/{pid}/environ", f"/proc/{pid}/mem",
            ])
        attack = run_tests([python, "-c", r"""
import json
from pathlib import Path
import sys
import urllib.request
paths, relay = json.loads(sys.argv[1])
for path in paths:
    try:
        with open(path, 'rb') as file:
            file.read(1)
    except OSError:
        pass
    else:
        raise AssertionError(('read escaped jail', path))
for path in ['/work/payload', '/work/new-file', '/work/.omp/verdict.json', *paths]:
    try:
        Path(path).write_bytes(b'attacker-overwrite')
    except OSError:
        pass
    else:
        raise AssertionError(('write escaped jail', path))
try:
    urllib.request.urlopen(relay, timeout=1)
except OSError:
    pass
else:
    raise AssertionError('inner command reached WorkService relay')
print('attacks-denied')
""", json.dumps([paths, os.environ["OMP_WORK_URL"]])], snapshot, 15)
        assert attack.exit_code == 0, attack.output
        assert attack.output == b"attacks-denied\n", attack.output
    finally:
        parent.terminate()
        parent.wait(timeout=5)

    # Observe both sleeping processes from the parent PID namespace before
    # timeout, then prove those exact processes disappeared after cancellation.
    marker = "verify-tests-fork-" + config["task"]["candidate_commit"]
    observed = set()
    stop = threading.Event()
    def observe():
        while not stop.is_set():
            for entry in Path('/proc').iterdir():
                if not entry.name.isdecimal():
                    continue
                try:
                    argv = (entry / 'cmdline').read_bytes().split(b'\0')
                except OSError:
                    continue
                if argv[:3] == [python.encode(), b"-c", sleeper.encode()] and marker.encode() in argv:
                    observed.add(entry)
            stop.wait(0.02)
    sleeper = "import os, time; os.fork(); time.sleep(60)"
    observer = threading.Thread(target=observe)
    observer.start()
    try:
        try:
            run_tests([python, "-c", sleeper, marker], snapshot, 4)
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError('sleeping command did not time out')
    finally:
        stop.set()
        observer.join(timeout=2)
    assert len(observed) >= 2, observed
    deadline = time.monotonic() + 3
    while any(path.exists() for path in observed) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert all(not path.exists() for path in observed), observed
    assert snapshot.joinpath("payload").read_bytes() == b"frozen-commit-bytes"

assert not snapshot.exists()
assert key.read_bytes() == b"planted-verifier-key"
assert verdict.read_bytes() == b"signer-verdict"
Path("/work/.omp/audit-report.txt").write_text("nested-contracts-passed")
'''


def test_real_nested_verifier_jail_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested_procfs_host: None,
) -> None:
    repo = tmp_path / "repository"
    repo.mkdir()
    git_env = {
        "GIT_AUTHOR_NAME": "Verifier Test", "GIT_AUTHOR_EMAIL": "verifier@example.test",
        "GIT_COMMITTER_NAME": "Verifier Test", "GIT_COMMITTER_EMAIL": "verifier@example.test",
    }

    def git(*args: str) -> str:
        return candidate_git.git(repo, *args, extra_env=git_env).decode().strip()

    git("init", "-b", "main")
    (repo / "payload").write_bytes(b"base-bytes")
    git("add", "payload")
    git("-c", "commit.gpgsign=false", "commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    (repo / "payload").write_bytes(b"frozen-commit-bytes")
    git("-c", "commit.gpgsign=false", "commit", "-am", "candidate")
    commit = git("rev-parse", "HEAD")
    root = tmp_path / "worktrees"
    root.mkdir()
    worktree = candidate_git.add_worktree(repo, root, "verifier", commit)
    sibling = candidate_git.add_worktree(repo, root, "sibling", commit)
    (sibling / "sentinel").write_bytes(b"sibling-secret")
    (worktree / ".omp").mkdir()
    verdict = worktree / ".omp/verdict.json"
    verdict.write_bytes(b"signer-verdict")
    key = tmp_path / "verifier.key"
    key.write_bytes(b"planted-verifier-key")
    task = {
        "candidate_sha256": candidate_git.audit_inputs(worktree, base, commit)["diff_sha256"],
        "attempt_id": "123e4567-e89b-12d3-a456-426614174000",
        "verifier_id": "verifier", "key_path": str(key),
        "test_command": ["/bin/true"], "criteria": [],
        "candidate_commit": commit, "base_commit": base,
        "allowed_paths": ["payload"], "timeout_seconds": 15.0,
    }

    class RelayHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"relay-reached")

        def log_message(self, format: str, *args: object) -> None:
            pass

    with HTTPServer(("127.0.0.1", 0), RelayHandler) as service:
        thread = threading.Thread(target=service.serve_forever)
        thread.start()
        monkeypatch.setenv("OMP_WORKTREES_DIR", str(root))
        try:
            exit_code = worker_sandbox.run_worker(
                [str(Path(sys.executable).resolve()), "-c", _VERIFIER_DRIVER, json.dumps({
                    "key": str(key), "sibling": str(sibling / "sentinel"),
                    "host_pid": os.getpid(), "task": task,
                })],
                identity="verifier", worktree=worktree, token="verifier-test-token",
                workservice_socket=("127.0.0.1", service.server_port),
                ro_binds=(key,), timeout=90,
            )
        finally:
            service.shutdown()
            thread.join(timeout=3)
    if exit_code == 77:
        _nested_sandbox_unavailable(
            (worktree / ".omp/nested-sandbox-unavailable.txt").read_text()
        )
    assert exit_code == 0
    assert (worktree / ".omp/audit-report.txt").read_text() == "nested-contracts-passed"
    assert verdict.read_bytes() == b"signer-verdict"
    assert key.read_bytes() == b"planted-verifier-key"
    assert (sibling / "sentinel").read_bytes() == b"sibling-secret"
    assert (worktree / "payload").read_bytes() == b"frozen-commit-bytes"


def test_real_unshare_probe_refusal_never_executes_command(tmp_path: Path) -> None:
    """Inherited seccomp denies real unshare; namespace enforcement stays intact."""
    library = find_library("seccomp")
    if library is None:
        pytest.skip("libseccomp unavailable for kernel-enforced unshare refusal")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    sentinel = tmp_path / "command-executed"
    driver = r'''
import ctypes
import errno
from pathlib import Path
import sys
from omp_work.egress_sandbox import SandboxUnavailable
from omp_work.orchestrator.verify_tests import run_tests

seccomp = ctypes.CDLL(sys.argv[1], use_errno=True)
seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
seccomp.seccomp_init.restype = ctypes.c_void_p
seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
context = seccomp.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
assert context
try:
    syscall = seccomp.seccomp_syscall_resolve_name(b'unshare')
    assert syscall >= 0
    assert seccomp.seccomp_rule_add(context, 0x00050000 | errno.EPERM, syscall, 0) == 0
    assert seccomp.seccomp_load(context) == 0
finally:
    seccomp.seccomp_release(context)
try:
    run_tests(['/usr/bin/touch', sys.argv[3]], Path(sys.argv[2]), 5)
except SandboxUnavailable as error:
    assert 'unshare probe failed' in str(error), error
else:
    raise AssertionError('kernel unshare refusal did not fail closed')
assert not Path(sys.argv[3]).exists()
print('setup-refused-command-not-executed')
'''
    result = subprocess.run(
        [sys.executable, "-c", driver, library, str(snapshot), str(sentinel)],
        env={**os.environ, "PYTHONPATH": str(Path(verify_tests.__file__).parents[2])},
        capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == b"setup-refused-command-not-executed\n"
    assert not sentinel.exists()
