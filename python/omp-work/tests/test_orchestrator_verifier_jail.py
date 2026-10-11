"""Verifier sandbox config transport and closed worker environment contracts."""

import json
import os
from pathlib import Path
import site
import subprocess
import sys
from unittest.mock import Mock

import pytest

import omp_work
from omp_work import egress_research_jail as jail
from omp_work.orchestrator import worker_sandbox
from omp_work.egress_policy import Identity, MemoryRecorder

from omp_work.egress_sandbox import SandboxUnavailable, _mount_config, run_sandboxed
from omp_work.egress_sandbox_helper import _worker_profile_env


BASE_ENV = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "TERM": "xterm",
    "TZ": "UTC",
    "OMP_TASK_TOKEN": "task-token",
    "OMP_WORK_URL": "http://127.0.0.1:8000",
}
PYTHON_ENV = {"PYTHONPATH": "/opt/runtime", "PYTHONSAFEPATH": "1"}


def test_defaults_omit_verifier_config_and_python_environment() -> None:
    assert _mount_config() == {}
    env = {**BASE_ENV, **PYTHON_ENV, "HOME": "/home/worker"}
    assert _worker_profile_env({"env": env}) == BASE_ENV


def test_verifier_config_serializes_paths_and_allows_only_python_environment() -> None:
    config = _mount_config(
        worktree_readonly=True,
        ro_dirs=(Path("/repo/.git"), "/opt/runtime"),
        audit_dir=Path("/repo/.omp"),
        verifier_runtime=True,
    )
    assert config == {
        "worktree_readonly": True,
        "ro_dirs": ["/repo/.git", "/opt/runtime"],
        "audit_dir": "/repo/.omp",
        "verifier_runtime": True,
    }
    env = {**BASE_ENV, **PYTHON_ENV, "HOME": "/home/worker"}
    assert _worker_profile_env({**config, "env": env}) == {**BASE_ENV, **PYTHON_ENV}


def test_verifier_drops_empty_python_values_and_preserves_model_manifest_environment() -> None:
    model_env = {
        "OMP_MODEL_PROXY_URL": "http://127.0.0.1:9000",
        "OMP_MODEL_TOKEN": "model-token",
        "OMP_WORKER_MANIFEST": "/work/manifest.json",
    }
    env = {
        **BASE_ENV,
        **model_env,
        "PYTHONPATH": "",
        "PYTHONSAFEPATH": "",
        "HOME": "/home/worker",
        "OTHER": "excluded",
    }
    assert _worker_profile_env({"env": env, "verifier_runtime": True}) == {**BASE_ENV, **model_env}


@pytest.mark.parametrize("binder", [jail._rbind_readonly, jail._bind_rw_dir])
def test_directory_bind_accepts_existing_target(tmp_path, monkeypatch, binder) -> None:
    target = tmp_path / "existing"
    target.mkdir()
    calls = []
    monkeypatch.setattr(jail, "_mount", lambda *args: calls.append(args))
    monkeypatch.setattr(jail, "_read_mountinfo", lambda: "")

    binder("/source", target)

    assert calls[0][:3] == ("/source", str(target), None)
    if binder is jail._rbind_readonly:
        assert calls[-1][3] & jail._MS_RDONLY
    else:
        assert calls[0][3] == jail._MS_BIND


def test_worker_directory_mounts_preserve_parent_and_audit_overlay(tmp_path, monkeypatch) -> None:
    parent = tmp_path / "repo"
    worktree = parent / "worktrees" / "job"
    audit = worktree / ".omp"
    audit.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(jail, "_mount", lambda *args: calls.append(args))
    monkeypatch.setattr(jail, "_read_mountinfo", lambda: "")

    new = jail._build_worker_root(tmp_path / "config.json", {
        "worktree": str(worktree),
        "worktree_readonly": True,
        "ro_dirs": [str(worktree), str(parent)],
        "audit_dir": str(audit),
    })

    binds = [call for call in calls if call[0] in {str(worktree), str(parent), str(audit)}]
    assert binds == [
        (str(worktree), str(new / "work"), None, jail._MS_BIND | jail._MS_REC),
        (str(parent), str(new / str(parent).lstrip("/")), None, jail._MS_BIND | jail._MS_REC),
        (str(worktree), str(new / str(worktree).lstrip("/")), None, jail._MS_BIND | jail._MS_REC),
        (str(audit), str(new / "work" / ".omp"), None, jail._MS_BIND),
    ]


@pytest.mark.parametrize("field", ["ro_dirs", "audit_dir"])
@pytest.mark.parametrize("invalid", ["/", "relative", "", 42])
def test_invalid_verifier_directory_rejected_before_mounts(tmp_path, monkeypatch, field, invalid) -> None:
    calls = []
    monkeypatch.setattr(jail, "_mount", lambda *args: calls.append(args))
    monkeypatch.setattr(jail, "_read_mountinfo", lambda: "")
    config = {"worktree": str(tmp_path), "worktree_readonly": True,
              field: [invalid] if field == "ro_dirs" else invalid}

    with pytest.raises(SystemExit) as exc:
        jail._build_worker_root(tmp_path / "config.json", config)

    assert exc.value.code == 1
    assert calls == []


@pytest.fixture
def namespace_support() -> None:
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--net", "--mount", "--pid", "--fork", "true"],
        capture_output=True, timeout=5,
    )
    if probe.returncode != 0:
        pytest.skip(probe.stderr.decode(errors="replace"))


@pytest.fixture
def verifier_paths(tmp_path):
    parent = tmp_path / "repo"
    worktree = parent / "worktrees" / "job"
    audit = worktree / ".omp"
    audit.mkdir(parents=True)
    (worktree / "tracked").write_text("unchanged")
    (parent / "verifier_fixture.py").write_text("VALUE = 431")
    key = tmp_path / "key"
    key.write_text("secret")
    return parent, worktree, audit, key


def _run_verifier_probe(paths, probe, **options):
    parent, worktree, _audit, _key = paths
    output = bytearray()
    ro_dirs = list(options.pop("ro_dirs", ()))
    for d in (sys.base_prefix, sys.prefix):
        resolved = str(Path(d).resolve())
        if not resolved.startswith("/usr") and resolved not in ro_dirs:
            ro_dirs.append(resolved)
    rc = run_sandboxed(
        [os.path.realpath(sys.executable), "-c", probe],
        Identity("workspace", "project", "mission", "worker", "repository"),
        MemoryRecorder(), worktree,
        {**BASE_ENV, "PYTHONPATH": str(parent), "PYTHONSAFEPATH": "1"},
        30, root="worker", worktree=worktree, output=output,
        ro_dirs=ro_dirs, **options,
    )
    assert rc == 0, output.decode(errors="replace")
    return json.loads(output)


def test_verifier_mounts_enforce_readonly_except_audit(namespace_support, verifier_paths) -> None:
    parent, worktree, audit, key = verifier_paths
    probe = f"""
import errno, json, os
from pathlib import Path
import verifier_fixture
assert verifier_fixture.VALUE == 431
for name in ['/work/tracked', {str(parent / 'new')!r}, {str(worktree / 'tracked')!r}, {str(key)!r}]:
    try:
        Path(name).write_text('tampered')
    except OSError as exc:
        assert exc.errno == errno.EROFS, (name, exc)
    else:
        raise AssertionError('writable: ' + name)
assert Path({str(key)!r}).read_text() == 'secret'
Path('/work/.omp/report').write_text('verified')
print(json.dumps({{name: os.environ.get(name) for name in ('PYTHONPATH', 'PYTHONSAFEPATH')}}))
"""
    env = _run_verifier_probe(
        verifier_paths, probe, worktree_readonly=True,
        ro_dirs=[parent, worktree], audit_dir=audit, verifier_runtime=True, ro_binds=[key],
    )
    assert env == {"PYTHONPATH": str(parent), "PYTHONSAFEPATH": "1"}
    assert (audit / "report").read_text() == "verified"
    assert (worktree / "tracked").read_text() == "unchanged"
    assert key.read_text() == "secret"
    assert not (parent / "new").exists()


def test_readonly_flag_alone_hides_extra_paths_and_python_environment(namespace_support, verifier_paths) -> None:
    parent, _worktree, audit, key = verifier_paths
    probe = f"""
import errno, json, os
from pathlib import Path
for name in ['/work/tracked', '/work/.omp/report']:
    try:
        Path(name).write_text('tampered')
    except OSError as exc:
        assert exc.errno == errno.EROFS, (name, exc)
    else:
        raise AssertionError('writable: ' + name)
assert not Path({str(parent)!r}).exists()
assert not Path({str(key)!r}).exists()
print(json.dumps({{name: os.environ.get(name) for name in ('PYTHONPATH', 'PYTHONSAFEPATH')}}))
"""
    assert _run_verifier_probe(verifier_paths, probe, worktree_readonly=True) == {
        "PYTHONPATH": None, "PYTHONSAFEPATH": None,
    }
    assert not (audit / "report").exists()


@pytest.mark.parametrize("field", ["ro_dirs", "audit_dir"])
def test_root_directory_bind_fails_setup_without_running_worker(namespace_support, verifier_paths, field) -> None:
    _parent, worktree, audit, _key = verifier_paths
    marker = worktree / "marker"
    probe = "from pathlib import Path; Path('/work/marker').touch(); Path('/work/.omp/report').touch()"
    with pytest.raises(SandboxUnavailable, match="Sandbox setup failed"):
        _run_verifier_probe(
            verifier_paths, probe, worktree_readonly=True,
            **{field: ["/"] if field == "ro_dirs" else "/"},
        )
    assert not marker.exists()
    assert not (audit / "report").exists()


def test_worker_without_readonly_flag_keeps_writable_worktree(namespace_support, verifier_paths) -> None:
    _parent, worktree, _audit, _key = verifier_paths
    probe = "from pathlib import Path; Path('/work/tracked').write_text('updated'); print('{}')"
    assert _run_verifier_probe(verifier_paths, probe) == {}
    assert (worktree / "tracked").read_text() == "updated"


@pytest.fixture
def verifier_launch(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    worktree = root / "job"
    (worktree / ".omp").mkdir(parents=True)
    commondir = tmp_path / "repo" / ".git"
    gitdir = commondir / "worktrees" / "job"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..")
    (worktree / ".git").write_text(f"gitdir: {gitdir}")
    monkeypatch.setenv("OMP_WORKTREES_DIR", str(root))
    home = tmp_path / "home"
    parent = home / "runtime"
    (parent / "omp_work").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(omp_work, "__file__", str(parent / "omp_work" / "__init__.py"))
    monkeypatch.setattr(site, "getsitepackages", lambda: [])
    spy = Mock(return_value=0)
    monkeypatch.setattr(worker_sandbox, "run_sandboxed", spy)

    def launch():
        return worker_sandbox.run_worker(
            ["/usr/bin/python3", "-m", "omp_work.orchestrator.verifier"],
            worktree=worktree, token="verifier-token",
            workservice_socket=("127.0.0.1", 8000), identity="verifier", timeout=5,
        )

    return launch, spy, parent, worktree, commondir, gitdir


def test_real_verifier_uses_live_deduplicated_runtime_roots(verifier_launch, tmp_path, monkeypatch):
    launch, spy, parent, worktree, commondir, gitdir = verifier_launch
    packages = tmp_path / "site-packages"
    packages.mkdir()
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(packages), str(parent), str(packages)])
    monkeypatch.setenv("PYTHONPATH", "/work")
    monkeypatch.setenv("PYTHONSAFEPATH", "poison")

    assert launch() == 0

    spy.assert_called_once()
    env = spy.call_args.args[4]
    roots = (parent, packages)
    assert env["PYTHONPATH"].split(os.pathsep) == [str(root) for root in roots]
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(Path(omp_work.__file__).resolve().parent.parent)
    assert env["PYTHONSAFEPATH"] == "1"
    assert set(env) == {*BASE_ENV, "PYTHONPATH", "PYTHONSAFEPATH"}
    assert "/work" not in env["PYTHONPATH"]
    assert str(worktree) not in env["PYTHONPATH"]
    assert spy.call_args.kwargs["ro_dirs"] == (commondir, gitdir, *roots)
    assert spy.call_args.kwargs["ro_dirs"][2:] == roots
    assert spy.call_args.kwargs["verifier_runtime"] is True
    assert spy.call_args.kwargs["worktree_readonly"] is True
    assert spy.call_args.kwargs["audit_dir"] == worktree / ".omp"


@pytest.mark.parametrize("root", ["/", "home", "cwd", "rel", "user-site", "/work", "worktree"])
def test_verifier_refuses_unsafe_site_root_before_launch(verifier_launch, monkeypatch, root):
    launch, spy, _parent, worktree, _commondir, _gitdir = verifier_launch
    root = {"home": str(Path.home()), "cwd": str(Path.cwd()),
            "user-site": site.getusersitepackages(), "worktree": str(worktree)}.get(root, root)
    monkeypatch.setattr(site, "getsitepackages", lambda: [root])

    with pytest.raises(worker_sandbox.WorkerSandboxRefused) as exc:
        launch()

    assert exc.value.code == "runtime_not_allowed"
    spy.assert_not_called()


@pytest.mark.parametrize("package_file", ["/omp_work/__init__.py", "missing"])
def test_verifier_refuses_root_or_missing_package_parent(verifier_launch, tmp_path, monkeypatch, package_file):
    launch, spy, _parent, _worktree, _commondir, _gitdir = verifier_launch
    if package_file == "missing":
        package_file = str(tmp_path / "missing" / "omp_work" / "__init__.py")
    monkeypatch.setattr(omp_work, "__file__", package_file)

    with pytest.raises(worker_sandbox.WorkerSandboxRefused) as exc:
        launch()

    assert exc.value.code == "runtime_not_allowed"
    spy.assert_not_called()


def test_verifier_omits_missing_site_directory(verifier_launch, tmp_path, monkeypatch):
    launch, spy, parent, _worktree, _commondir, _gitdir = verifier_launch
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(tmp_path / "missing-site")])

    assert launch() == 0

    spy.assert_called_once()
    assert spy.call_args.args[4]["PYTHONPATH"] == str(parent)
    assert spy.call_args.kwargs["ro_dirs"][2:] == (parent,)


def test_verifier_rejects_audit_symlink_before_runtime_discovery(verifier_launch, tmp_path, monkeypatch):
    launch, spy, _parent, worktree, _commondir, _gitdir = verifier_launch
    audit = worktree / ".omp"
    audit.rmdir()
    audit.symlink_to(tmp_path, target_is_directory=True)
    discovery = Mock(return_value=[])
    monkeypatch.setattr(site, "getsitepackages", discovery)

    with pytest.raises(worker_sandbox.WorkerSandboxRefused) as exc:
        launch()

    assert exc.value.code == "audit_dir_not_allowed"
    discovery.assert_not_called()
    spy.assert_not_called()
