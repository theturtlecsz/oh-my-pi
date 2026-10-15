"""check_prerequisites refuses with a named cause and exit_code 2; all host access is injected."""

from __future__ import annotations

import copy
import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from omp_work import skip_accounting_postgres as sap
from omp_work import skip_accounting as sa
from omp_work.skip_accounting_postgres import PrerequisiteError, check_prerequisites



PG18 = "{} (PostgreSQL) 18.1\n"


class Host:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.missing: set[str] = set()
        self.overrides: dict[tuple[str, ...], Any] = {}
        self.library: str | None = "libseccomp.so.2"
        self.find_exc: Exception | None = None
        self.load_exc: Exception | None = None

    def which(self, tool: str) -> str | None:
        return None if tool in self.missing else f"/usr/bin/{tool}"

    def run(self, argv: list[str], **kwargs: Any) -> Any:
        assert kwargs["shell"] is False and kwargs["timeout"] == sap.COMMAND_TIMEOUT_SECONDS
        self.calls.append(argv)
        res = self.overrides.get(tuple(argv))
        if isinstance(res, BaseException):
            raise res
        if res is not None:
            return res
        out = PG18.format(argv[0]) if argv[-1] == "--version" else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    def find_library(self, name: str) -> str | None:
        assert name == "seccomp"
        if self.find_exc:
            raise self.find_exc
        return self.library

    def cdll(self, name: str) -> object:
        if self.load_exc:
            raise self.load_exc
        return object()

    def check(self, **kw: Any) -> None:
        check_prerequisites(
            platform=kw.pop("platform", "linux"),
            geteuid=kw.pop("geteuid", lambda: 1000),
            which=self.which,
            run=self.run,
            find_library=self.find_library,
            cdll=self.cdll,
            **kw,
        )


def cp(argv: tuple[str, ...], code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), code, out, err)


def refused(host: Host, **kw: Any) -> PrerequisiteError:
    with pytest.raises(PrerequisiteError) as info:
        host.check(**kw)
    assert info.value.exit_code == 2
    return info.value


def test_success_runs_versions_then_exact_probes() -> None:
    host = Host()
    host.check()
    assert host.calls == [
        ["initdb", "--version"],
        ["postgres", "--version"],
        ["pg_isready", "--version"],
        *[list(p) for p in sap.NAMESPACE_PROBES],
    ]
    assert host.calls[3] == "unshare --user --map-root-user --net --mount --pid --fork true".split()
    assert host.calls[4] == "unshare --user --map-root-user --mount --pid --fork --mount-proc true".split()
    assert host.calls[5] == (
        "unshare --user --map-root-user --net --mount --pid --fork unshare --pid --fork --mount-proc true".split()
    )


def test_non_linux_refused_before_geteuid() -> None:
    def boom() -> int:
        raise AssertionError("geteuid called")

    err = refused(Host(), platform="darwin", geteuid=boom)
    assert err.cause == "not_linux"


def test_root_refused() -> None:
    assert refused(Host(), geteuid=lambda: 0).cause == "running_as_root"


@pytest.mark.parametrize("tool", sap.REQUIRED_TOOLS)
def test_missing_tool(tool: str) -> None:
    host = Host()
    host.missing = {tool}
    assert refused(host).cause == f"missing_tool_{tool}"
    assert host.calls == []


@pytest.mark.parametrize("tool", sap.VERSIONED_TOOLS)
def test_wrong_and_malformed_version(tool: str) -> None:
    host = Host()
    host.overrides[(tool, "--version")] = cp((tool, "--version"), 0, f"{tool} (PostgreSQL) 17.4\n")
    err = refused(host)
    assert err.cause == f"version_{tool}_wrong_major"
    assert err.argv == (tool, "--version")
    host.overrides[(tool, "--version")] = cp((tool, "--version"), 0, "garbage\n", "warn")
    err = refused(host)
    assert err.cause == f"version_{tool}_unparseable"
    assert (err.stdout, err.stderr, err.status) == ("garbage\n", "warn", 0)


def test_version_launch_failure_and_timeout() -> None:
    host = Host()
    host.overrides[("postgres", "--version")] = PermissionError("denied")
    err = refused(host)
    assert (err.cause, err.argv) == ("version_postgres_launch_failed", ("postgres", "--version"))
    host.overrides[("postgres", "--version")] = subprocess.TimeoutExpired(
        ["postgres", "--version"], 5, output=b"partial", stderr=b"slow"
    )
    err = refused(host)
    assert (err.cause, err.stdout, err.stderr) == ("version_postgres_timeout", "partial", "slow")


def test_postgres_loader_error_preserved() -> None:
    host = Host()
    msg = "postgres: error while loading shared libraries: libicuuc.so.74: cannot open shared object file"
    host.overrides[("postgres", "--version")] = cp(("postgres", "--version"), 127, "", msg)
    err = refused(host)
    assert err.cause == "version_postgres_failed"
    assert err.status == 127 and err.stderr == msg
    assert "libicuuc.so.74" in str(err)


def test_libseccomp_missing_discovery_and_load_failures() -> None:
    host = Host()
    host.library = None
    assert refused(host).cause == "libseccomp_missing"
    host.library = "libseccomp.so.2"
    host.find_exc = RuntimeError("ldconfig broke")
    assert refused(host).cause == "libseccomp_discovery_failed"
    host.find_exc = None
    host.load_exc = OSError("cannot open shared object file")
    err = refused(host)
    assert err.cause == "libseccomp_load_failed" and "cannot open" in err.stderr


@pytest.mark.parametrize("index", [1, 2, 3])
def test_each_probe_refusal_and_timeout(index: int) -> None:
    argv = sap.NAMESPACE_PROBES[index - 1]
    host = Host()
    host.overrides[argv] = cp(argv, 1, "o", "unshare: unshare failed: Operation not permitted")
    err = refused(host)
    assert (err.cause, err.argv, err.status) == (f"namespace_probe_{index}_failed", argv, 1)
    assert "Operation not permitted" in err.stderr and err.stdout == "o"
    host.overrides[argv] = subprocess.TimeoutExpired(list(argv), 5)
    err = refused(host)
    assert (err.cause, err.argv) == (f"namespace_probe_{index}_timeout", argv)
    host.overrides[argv] = FileNotFoundError("unshare")
    assert refused(host).cause == f"namespace_probe_{index}_launch_failed"


SEARCHABLE_MODE = stat.S_IFDIR | 0o755


def searchable_in_fixture(tmp_path: Path) -> Callable[[Path], int]:
    """Real modes inside the fixture tree; other-searchable above it.

    pytest's private ancestors cannot be chmod'd, so the mock stops at the fixture
    root and the private-ancestor test below still exercises the real mode check.
    """

    def probe(path: Path) -> int:
        return path.stat().st_mode if path.is_relative_to(tmp_path) else SEARCHABLE_MODE

    return probe


def prepare_base(tmp_path: Path) -> Path:
    tmp_path.chmod(0o755)
    base = tmp_path / "base"
    base.mkdir()
    return base


def owning_mkdtemp(created: list[Path]) -> Callable[..., str]:
    def mkdtemp(**kwargs: Any) -> str:
        path = tempfile.mkdtemp(**kwargs)
        created.append(Path(path))
        return path

    return mkdtemp


def temp_error(cause: str, **kw: Any) -> PrerequisiteError:
    with pytest.raises(PrerequisiteError) as info:
        sap.OwnedTemp.create(**kw)
    assert info.value.exit_code == 2
    assert info.value.cause == cause, info.value.detail
    return info.value


def test_create_exposes_0755_root_tmpdir_and_basetemp(tmp_path: Path) -> None:
    base = prepare_base(tmp_path)
    owned = sap.OwnedTemp.create(base=base, searchable=searchable_in_fixture(tmp_path))
    try:
        assert owned.root.parent == base
        assert owned.root.name.startswith(sap.ROOT_PREFIX)
        assert stat.S_IMODE(owned.root.stat().st_mode) == 0o755
        assert owned.tmpdir == owned.root / "tmp" and owned.tmpdir.is_dir()
        assert owned.basetemp == owned.root / "basetemp" and owned.basetemp.is_dir()
        assert stat.S_IMODE(owned.tmpdir.stat().st_mode) == 0o755
        assert stat.S_IMODE(owned.basetemp.stat().st_mode) == 0o755
        assert owned.env() == {"TMPDIR": str(owned.tmpdir)}
        assert not (owned.root / sap.PROBE_NAME).exists()
        assert owned.is_owned
        other = sap.OwnedTemp.create(base=base, searchable=searchable_in_fixture(tmp_path))
        try:
            assert other.root != owned.root
        finally:
            shutil.rmtree(other.root, ignore_errors=True)
    finally:
        shutil.rmtree(owned.root, ignore_errors=True)
    assert not owned.root.exists()


def test_factory_alone_grants_ownership_and_immutability(tmp_path: Path) -> None:
    base = prepare_base(tmp_path)
    owned = sap.OwnedTemp.create(base=base, searchable=searchable_in_fixture(tmp_path))
    try:
        assert owned.is_owned
        unowned = sap.OwnedTemp(root=owned.root, tmpdir=owned.tmpdir, basetemp=owned.basetemp)
        assert not unowned.is_owned
        with pytest.raises(AttributeError):
            owned.root = base  # type: ignore[misc]
        with pytest.raises(AttributeError):
            owned.tmpdir = base  # type: ignore[misc]
        with pytest.raises(AttributeError):
            owned.basetemp = base  # type: ignore[misc]
        assert not copy.copy(owned).is_owned
        assert not copy.deepcopy(owned).is_owned
        assert repr(owned) == f"OwnedTemp(root={owned.root!r}, tmpdir={owned.tmpdir!r}, basetemp={owned.basetemp!r})"
    finally:
        shutil.rmtree(owned.root, ignore_errors=True)


def test_create_private_ancestor_refused_unchanged_and_no_leak(tmp_path: Path) -> None:
    prepare_base(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    private.chmod(0o700)
    before = stat.S_IMODE(private.stat().st_mode)
    created: list[Path] = []
    err = temp_error(
        "owned_temp_ancestor_not_searchable",
        base=private,
        searchable=searchable_in_fixture(tmp_path),
        mkdtemp=owning_mkdtemp(created),
    )
    assert str(private) in err.detail and "700" in err.detail
    assert stat.S_IMODE(private.stat().st_mode) == before == 0o700
    assert created and not created[0].exists()
    assert list(private.iterdir()) == []


def test_create_missing_base_refused_before_allocation(tmp_path: Path) -> None:
    missing = tmp_path / "absent"
    err = temp_error("owned_temp_mkdtemp_failed", base=missing, searchable=searchable_in_fixture(tmp_path))
    assert str(missing) in err.detail and err.stderr
    assert str(err.__cause__)
    assert not missing.exists()


def test_create_unwritable_base_refused_without_deleting_base(tmp_path: Path) -> None:
    base = prepare_base(tmp_path)
    base.chmod(0o500)
    try:
        def inject_eacces(**kwargs: Any) -> str:
            raise PermissionError(errno.EACCES, "Permission denied", str(base))

        mkdtemp_fn = inject_eacces if os.geteuid() == 0 else tempfile.mkdtemp
        err = temp_error(
            "owned_temp_mkdtemp_failed",
            base=base,
            searchable=searchable_in_fixture(tmp_path),
            mkdtemp=mkdtemp_fn,
        )
        assert str(base) in err.detail
        assert base.is_dir()
        assert list(base.iterdir()) == []
    finally:
        base.chmod(0o755)


def failing_mkdir_tmp(path: os.PathLike[str], mode: int) -> None:
    if Path(path).name == "tmp":
        raise OSError("mkdir tmp failed")
    os.mkdir(path, mode)


def failing_mkdir_basetemp(path: os.PathLike[str], mode: int) -> None:
    if Path(path).name == "basetemp":
        raise OSError("mkdir basetemp failed")
    os.mkdir(path, mode)


def failing_write_probe(directory: Path) -> None:
    raise OSError("probe write failed")


def failing_unlink_probe(directory: Path) -> None:
    raise OSError("probe unlink failed")


@pytest.mark.parametrize(
    ("seam_kwargs", "expected_cause", "expected_detail"),
    [
        ({"resolve": lambda p: (_ for _ in ()).throw(OSError("resolve failed"))}, "owned_temp_resolve_failed", "resolve"),
        ({"searchable": lambda p: (_ for _ in ()).throw(OSError("stat failed"))}, "owned_temp_ancestor_stat_failed", "stat ancestor"),
        ({"chmod": lambda *a: (_ for _ in ()).throw(OSError("chmod failed"))}, "owned_temp_chmod_failed", "chmod"),
        ({"mkdir": failing_mkdir_tmp}, "owned_temp_mkdir_failed", "tmp"),
        ({"mkdir": failing_mkdir_basetemp}, "owned_temp_mkdir_failed", "basetemp"),
        ({"probe": failing_write_probe}, "owned_temp_probe_failed", "probe"),
        ({"probe": failing_unlink_probe}, "owned_temp_probe_failed", "probe"),
    ],
)
def test_create_post_allocation_failure_rolls_back_root(
    tmp_path: Path,
    seam_kwargs: dict[str, Any],
    expected_cause: str,
    expected_detail: str,
) -> None:
    base = prepare_base(tmp_path)
    created: list[Path] = []
    call_kwargs: dict[str, Any] = {
        "base": base,
        "searchable": searchable_in_fixture(tmp_path),
        "mkdtemp": owning_mkdtemp(created),
    }
    call_kwargs.update(seam_kwargs)
    err = temp_error(expected_cause, **call_kwargs)
    assert expected_detail in err.detail
    if "ancestor" in expected_cause:
        assert str(base) in err.detail
    else:
        assert str(created[0]) in err.detail
    assert isinstance(err.__cause__, OSError)
    assert created and not created[0].exists()
    assert list(base.iterdir()) == []



@pytest.mark.parametrize(
    ("seam_kwargs", "expected_cause"),
    [
        ({"resolve": lambda p: (_ for _ in ()).throw(OSError("resolve failed"))}, "owned_temp_resolve_failed"),
        ({"searchable": lambda p: (_ for _ in ()).throw(OSError("stat failed"))}, "owned_temp_ancestor_stat_failed"),
        ({"chmod": lambda *a: (_ for _ in ()).throw(OSError("chmod failed"))}, "owned_temp_chmod_failed"),
        ({"mkdir": failing_mkdir_tmp}, "owned_temp_mkdir_failed"),
        ({"mkdir": failing_mkdir_basetemp}, "owned_temp_mkdir_failed"),
        ({"probe": failing_write_probe}, "owned_temp_probe_failed"),
        ({"probe": failing_unlink_probe}, "owned_temp_probe_failed"),
    ],
)
def test_create_rollback_failure_preserves_cause_and_surfaces_rollback(
    tmp_path: Path,
    seam_kwargs: dict[str, Any],
    expected_cause: str,
) -> None:
    base = prepare_base(tmp_path)

    def failing_rmtree(*args: Any, **kwargs: Any) -> Any:
        raise OSError("busy mount")

    call_kwargs: dict[str, Any] = {
        "base": base,
        "searchable": searchable_in_fixture(tmp_path),
        "rmtree": failing_rmtree,
    }
    call_kwargs.update(seam_kwargs)
    err = temp_error("owned_temp_rollback_failed", **call_kwargs)
    assert "busy mount" in err.detail
    assert err.stderr == "busy mount"
    original = err.__cause__
    assert isinstance(original, PrerequisiteError) and original.cause == expected_cause
    assert isinstance(original.__cause__, OSError)
    for leftover in base.iterdir():
        leftover.chmod(0o755)
        shutil.rmtree(leftover, ignore_errors=True)


def test_storage_probe_standalone(tmp_path: Path) -> None:
    base = prepare_base(tmp_path)
    d = base / "probe_dir"
    d.mkdir()
    sap._storage_probe(d)
    assert not (d / sap.PROBE_NAME).exists()

    def boom_write(*args: Any) -> int:
        raise OSError("write broke")

    with pytest.raises(OSError, match="write broke"):
        sap._storage_probe(d, write_bytes=boom_write)
    (d / sap.PROBE_NAME).unlink(missing_ok=True)

    def boom_remove(*args: Any) -> None:
        raise OSError("unlink broke")

    with pytest.raises(OSError, match="unlink broke"):
        sap._storage_probe(d, remove_path=boom_remove)
    (d / sap.PROBE_NAME).unlink(missing_ok=True)




def make_owned(tmp_path: Path) -> sap.OwnedTemp:
    return sap.OwnedTemp.create(base=prepare_base(tmp_path), searchable=searchable_in_fixture(tmp_path))


def test_cleanup_removes_created_storage(tmp_path: Path) -> None:
    owned = make_owned(tmp_path)
    try:
        owned.cleanup()
        assert not owned.root.exists()
        assert not owned.is_owned
    finally:
        shutil.rmtree(owned.root, ignore_errors=True)


def test_cleanup_unowned_with_path_shaped_fields_touches_nothing(tmp_path: Path) -> None:
    sentinel = tmp_path / "unrelated"
    sentinel.mkdir()
    (sentinel / "keep").write_text("x")
    calls: list[Path] = []
    fake = sap.OwnedTemp(sentinel, sentinel / "tmp", sentinel / "basetemp")
    fake.cleanup(rmtree=calls.append)
    assert calls == [] and (sentinel / "keep").read_text() == "x"
    with pytest.raises(TypeError):
        sap.OwnedTemp(sentinel, sentinel, sentinel, _allocation=object())  # type: ignore[arg-type]


def test_reassignment_and_copies_cannot_redirect_or_duplicate_authority(tmp_path: Path) -> None:
    owned = make_owned(tmp_path)
    sentinel = tmp_path / "unrelated"
    sentinel.mkdir()
    try:
        with pytest.raises(AttributeError):
            owned.root = sentinel  # type: ignore[misc]
        owned._root = sentinel  # private tampering must not redirect deletion
        for dup in (copy.copy(owned), copy.deepcopy(owned)):
            dup.cleanup()
            assert owned.is_owned
        # Even a copied field dictionary cannot mint factory authority.
        forged = sap.OwnedTemp(sentinel, sentinel, sentinel)
        forged.__dict__.update(owned.__dict__)
        forged.cleanup()
        assert owned.is_owned
        removed: list[Path] = []
        owned.cleanup(rmtree=lambda p: (removed.append(p), shutil.rmtree(p))[1])
        assert sentinel.is_dir()
        assert len(removed) == 1 and removed[0].parent == tmp_path / "base"
        assert not removed[0].exists()
    finally:
        for child in (tmp_path / "base").iterdir():
            shutil.rmtree(child, ignore_errors=True)


def test_cleanup_consumed_after_success_never_removes_reused_path(tmp_path: Path) -> None:
    owned = make_owned(tmp_path)
    root = owned.root
    try:
        owned.cleanup()
        root.mkdir()
        (root / "sentinel").write_text("x")
        calls: list[Path] = []
        owned.cleanup(rmtree=calls.append)
        assert calls == [] and (root / "sentinel").read_text() == "x"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_cleanup_absent_root_is_terminal_success_and_consumes(tmp_path: Path) -> None:
    owned = make_owned(tmp_path)
    root = owned.root
    try:
        shutil.rmtree(root)
        owned.cleanup()
        assert not owned.is_owned
        root.mkdir()
        (root / "sentinel").write_text("x")
        calls: list[Path] = []
        owned.cleanup(rmtree=calls.append)
        assert calls == [] and (root / "sentinel").read_text() == "x"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_cleanup_removal_error_surfaces_chained_and_keeps_authority(tmp_path: Path) -> None:
    owned = make_owned(tmp_path)
    try:

        def boom(path: Path) -> None:
            raise OSError(errno.EBUSY, "device busy")

        with pytest.raises(PrerequisiteError) as info:
            owned.cleanup(rmtree=boom)
        err = info.value
        assert err.exit_code == 2 and err.cause == "owned_temp_remove_failed"
        assert "remove" in err.detail and str(owned.root) in err.detail and "device busy" in err.detail
        assert isinstance(err.__cause__, OSError)
        assert owned.root.exists() and owned.is_owned
        owned.cleanup()
        assert not owned.root.exists()
    finally:
        shutil.rmtree(owned.root, ignore_errors=True)


@pytest.mark.parametrize("consumed", [False, True])
def test_reinitialization_cannot_redirect_or_rearm_cleanup(tmp_path: Path, consumed: bool) -> None:
    owned = make_owned(tmp_path)
    root = owned.root
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "sentinel").write_text("keep")
    try:
        if consumed:
            owned.cleanup()
            root.mkdir()
            (root / "sentinel").write_text("reused")
        owned.__init__(unrelated, unrelated, unrelated)
        calls: list[Path] = []

        def remove(path: Path) -> None:
            calls.append(path)
            shutil.rmtree(path)

        owned.cleanup(rmtree=remove)
        assert (unrelated / "sentinel").read_text() == "keep"
        if consumed:
            assert calls == []
            assert (root / "sentinel").read_text() == "reused"
        else:
            assert calls == [root]
            assert not root.exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_cleanup_inspection_permission_error_preserves_removal_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = make_owned(tmp_path)
    removal_error = PermissionError(errno.EACCES, "removal denied", str(owned.root))
    original_lstat = Path.lstat

    def denied(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == owned.root:
            raise PermissionError(errno.EACCES, "inspection denied", str(path))
        return original_lstat(path, *args, **kwargs)

    def remove(path: Path) -> None:
        raise removal_error

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "lstat", denied)
            with pytest.raises(PrerequisiteError) as info:
                owned.cleanup(rmtree=remove)
        err = info.value
        assert err.exit_code == 2 and err.cause == "owned_temp_remove_failed"
        assert "remove" in err.detail and str(owned.root) in err.detail and "removal denied" in err.detail
        assert err.__cause__ is removal_error
        assert owned.is_owned and owned.root.exists()
        owned.cleanup()
        assert not owned.root.exists()
    finally:
        shutil.rmtree(owned.root, ignore_errors=True)


class ChildDouble:
    """Popen-like child recording every call into a shared trace."""

    def __init__(
        self,
        name: str,
        trace: list[str],
        *,
        exited: bool = False,
        fail: dict[str, BaseException] | None = None,
        wait_results: list[BaseException | None] | None = None,
    ) -> None:
        self.name = name
        self.pid = 4242
        self.trace = trace
        self.exited = exited
        self.fail = fail or {}
        self.wait_results = list(wait_results or [])

    def _step(self, op: str) -> None:
        self.trace.append(f"{self.name}.{op}")
        if op in self.fail:
            raise self.fail[op]

    def poll(self) -> int | None:
        self._step("poll")
        return 0 if self.exited else None

    def terminate(self) -> None:
        self._step("terminate")

    def kill(self) -> None:
        self._step("kill")

    def wait(self, timeout: float | None = None) -> int:
        assert timeout == sap.COMMAND_TIMEOUT_SECONDS
        self.trace.append(f"{self.name}.wait")
        if self.wait_results:
            result = self.wait_results.pop(0)
            if result is not None:
                raise result
        elif "wait" in self.fail:
            raise self.fail["wait"]
        return 0


def timeout() -> subprocess.TimeoutExpired:
    return subprocess.TimeoutExpired("child", sap.COMMAND_TIMEOUT_SECONDS)


def test_cleanup_children_terminate_wait_then_remove(tmp_path: Path) -> None:
    owned = make_owned(tmp_path)
    trace: list[str] = []
    try:
        owned.cleanup([ChildDouble("a", trace)], rmtree=lambda p: (trace.append("rmtree"), shutil.rmtree(p))[1])
        assert trace == ["a.poll", "a.terminate", "a.wait", "rmtree"]
        assert not owned.root.exists() and not owned.is_owned
    finally:
        shutil.rmtree(owned.root, ignore_errors=True)


def test_cleanup_children_timeout_escalates_to_kill_then_bounded_wait(tmp_path: Path) -> None:
    owned = make_owned(tmp_path)
    trace: list[str] = []
    child = ChildDouble("a", trace, wait_results=[timeout(), None])
    try:
        owned.cleanup([child], rmtree=lambda p: (trace.append("rmtree"), shutil.rmtree(p))[1])
        assert trace == ["a.poll", "a.terminate", "a.wait", "a.kill", "a.wait", "rmtree"]
    finally:
        shutil.rmtree(owned.root, ignore_errors=True)


def test_cleanup_children_exited_reaped_and_all_reaped_before_removal(tmp_path: Path) -> None:
    owned = make_owned(tmp_path)
    trace: list[str] = []
    children = [ChildDouble("a", trace, exited=True), ChildDouble("b", trace), ChildDouble("c", trace)]
    try:
        owned.cleanup(children, rmtree=lambda p: (trace.append("rmtree"), shutil.rmtree(p))[1])
        assert trace == [
            "a.poll", "a.wait",
            "b.poll", "b.terminate", "b.wait",
            "c.poll", "c.terminate", "c.wait",
            "rmtree",
        ]  # fmt: skip
    finally:
        shutil.rmtree(owned.root, ignore_errors=True)


def test_cleanup_empty_children_removes_storage(tmp_path: Path) -> None:
    owned = make_owned(tmp_path)
    try:
        owned.cleanup([])
        assert not owned.root.exists() and not owned.is_owned
    finally:
        shutil.rmtree(owned.root, ignore_errors=True)


@pytest.mark.parametrize(
    ("operation", "kwargs", "expected"),
    [
        ("poll", {"fail": {"poll": OSError(errno.EIO, "poll boom")}}, ["a.poll"]),
        ("terminate", {"fail": {"terminate": PermissionError(errno.EPERM, "term boom")}}, ["a.poll", "a.terminate"]),
        ("wait", {"wait_results": [OSError(errno.ECHILD, "wait boom")]}, ["a.poll", "a.terminate", "a.wait"]),
        (
            "kill",
            {"wait_results": [timeout()], "fail": {"kill": OSError(errno.EPERM, "kill boom")}},
            ["a.poll", "a.terminate", "a.wait", "a.kill"],
        ),
        (
            "wait_after_kill",
            {"wait_results": [timeout(), OSError(errno.ECHILD, "post boom")]},
            ["a.poll", "a.terminate", "a.wait", "a.kill", "a.wait"],
        ),
        (
            "wait_after_kill",
            {"wait_results": [timeout(), timeout()]},
            ["a.poll", "a.terminate", "a.wait", "a.kill", "a.wait"],
        ),
    ],
)
def test_cleanup_child_failure_retains_storage_with_context(
    tmp_path: Path, operation: str, kwargs: dict[str, Any], expected: list[str],
) -> None:
    owned = make_owned(tmp_path)
    (owned.root / "sentinel").write_text("keep")
    trace: list[str] = []
    removed: list[Path] = []
    child = ChildDouble("a", trace, **kwargs)
    try:
        with pytest.raises(PrerequisiteError) as info:
            owned.cleanup([child], rmtree=removed.append)
        err = info.value
        assert err.exit_code == 2 and err.cause == f"owned_temp_child_{operation}_failed"
        assert "4242" in err.detail and str(owned.root) in err.detail and operation in err.detail
        assert isinstance(err.__cause__, (OSError, subprocess.TimeoutExpired))
        assert str(err.__cause__) in err.detail
        assert trace == expected and removed == []
        assert (owned.root / "sentinel").read_text() == "keep" and owned.is_owned
        # Authority retained: a retry with healthy children removes storage.
        owned.cleanup([ChildDouble("b", [])])
        assert not owned.root.exists()
    finally:
        shutil.rmtree(owned.root, ignore_errors=True)


def test_cleanup_failed_child_does_not_skip_later_children(tmp_path: Path) -> None:
    owned = make_owned(tmp_path)
    trace: list[str] = []
    bad = ChildDouble("a", trace, fail={"terminate": OSError(errno.EPERM, "nope")})
    good = ChildDouble("b", trace)
    try:
        with pytest.raises(PrerequisiteError):
            owned.cleanup([bad, good], rmtree=lambda p: trace.append("rmtree"))
        assert trace == ["a.poll", "a.terminate", "b.poll", "b.terminate", "b.wait"]
        assert owned.root.exists()
    finally:
        shutil.rmtree(owned.root, ignore_errors=True)


def test_cleanup_consumed_handle_ignores_children(tmp_path: Path) -> None:
    owned = make_owned(tmp_path)
    owned.cleanup()
    trace: list[str] = []
    owned.cleanup([ChildDouble("a", trace)])
    assert trace == []


def test_cleanup_reaps_real_child(tmp_path: Path) -> None:
    owned = make_owned(tmp_path)
    child = subprocess.Popen(["sleep", "30"])
    try:
        owned.cleanup([child])
        assert child.returncode is not None
        assert not owned.root.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=sap.COMMAND_TIMEOUT_SECONDS)
        shutil.rmtree(owned.root, ignore_errors=True)


@pytest.fixture
def launch_suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Scope prerequisite substitution to these launcher tests, never the public CLI.
    monkeypatch.setattr(sap, "check_prerequisites", lambda: None)
    monkeypatch.setattr(sap, "_stat_mode", lambda path: stat.S_IFDIR | 0o755)
    for key in os.environ:
        if key.startswith("OMP_ACCOUNTING_"):
            monkeypatch.delenv(key)
    suite = tmp_path / "suite"
    suite.mkdir()
    monkeypatch.setattr(sap, "_suite_path", lambda: suite)
    return suite


def printed_receipt(output: str) -> dict[str, Any]:
    rows = [json.loads(line.removeprefix("receipt: ")) for line in output.splitlines() if line.startswith("receipt: ")]
    assert len(rows) == 1, output
    return rows[0]


def read_only_receipt(out: Path) -> sa.Receipt:
    paths = list(out.glob(f"*{sa.RECEIPT_SUFFIX}"))
    assert len(paths) == 1
    return sa.read_receipt(paths[0])


def unit_suite(suite: Path, out: Path, run: str) -> sa.Receipt:
    root = sap.repo_root()
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    env.pop("OMP_WORK_POSTGRES_INTEGRATION", None)
    env.update({sa.ENV_DIR: str(out), sa.ENV_RUN: run, sa.ENV_CHECK: "unit", sa.ENV_ROOT: str(root)})
    proc = subprocess.run([
        sys.executable, "-m", "pytest", str(suite), "-p", "omp_work.skip_accounting",
        "-p", "no:cacheprovider", f"--rootdir={root}", f"--basetemp={out.parent / 'unit-temp'}",
    ], cwd=root, env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return read_only_receipt(out)


def test_launch_enables_db_skips_and_keeps_downstream_teardown_evidence(
    launch_suite: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str],
) -> None:
    (launch_suite / "test_lane.py").write_text(textwrap.dedent('''
        import os
        import pytest

        @pytest.fixture
        def database():
            if os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1":
                pytest.skip("setup DB needs integration flag")

        def test_setup_db(database):
            assert os.environ["OMP_WORK_POSTGRES_INTEGRATION"] == "1"

        def test_body_db():
            if os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1":
                pytest.skip("body DB needs integration flag")

        def test_namespace_only():
            # No DB marker: this node must still be selected by the alternate lane.
            pass

        @pytest.fixture
        def downstream():
            yield
            pytest.skip("downstream teardown: exact reason\\nsecond line")

        def test_downstream(downstream):
            pass
    '''))
    unit = unit_suite(launch_suite, tmp_path / "unit", "paired")
    alt_out = tmp_path / "alternate"
    assert sap.main(["--out", str(alt_out), "--run", "paired"]) == 0
    alternate = read_only_receipt(alt_out)
    assert alternate.complete and set(alternate.selected) == set(unit.selected)
    by_name = {node.split("::")[-1]: phases for node, phases in alternate.nodes.items()}
    for name in ("test_setup_db", "test_body_db", "test_namespace_only"):
        assert {phase.outcome for phase in by_name[name].values()} == {"passed"}
    result = sa.reconcile(unit, [alternate])
    assert {node.split("::")[-1] for node in result.covered} == {"test_setup_db", "test_body_db"}
    assert {node.split("::")[-1] for node in result.pending} == {"test_downstream"}
    assert by_name["test_downstream"]["call"].outcome == "passed"
    assert by_name["test_downstream"]["teardown"] == sa.Phase("skipped", "downstream teardown: exact reason\nsecond line")
    printed = printed_receipt(capfd.readouterr().out)
    assert printed["nodes"] == alternate.to_json()["nodes"]
    # Explicit output survives while owned temp storage has already been removed.
    basetemp = next(arg.split("=", 1)[1] for arg in alternate.argv if arg.startswith("--basetemp="))
    assert not Path(basetemp).parent.exists()


def test_launch_failure_does_not_stop_later_nodes_or_inherit_selectors(
    launch_suite: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    for key, value in {
        "PYTEST_ADDOPTS": f"-x --maxfail=1 -k test_failure --basetemp={tmp_path}",
        "PYTEST_PLUGINS": "nonexistent_plugin", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "OMP_INSTALLED_ONLY": "1", "OMP584_FLOOD_SOURCE": "bad", "FLOOD_SOURCE": "bad",
        "OMP_ACCOUNTING_EXTRA": "keep", "OMP_WORK_POSTGRES_INTEGRATION": "0",
        "EXPECTED_PATH": os.environ["PATH"], "EXPECTED_PYTHON": sys.executable,
    }.items():
        monkeypatch.setenv(key, value)
    (launch_suite / "pytest.ini").write_text("[pytest]\naddopts = -x --maxfail=1 -k test_failure\n")
    (launch_suite / "test_failure.py").write_text(textwrap.dedent('''
        import os
        import sys
        from pathlib import Path

        def test_failure():
            assert False, "deliberate failure"

        def test_later(tmp_path, pytestconfig):
            # Pytest itself creates these two runtime entries after launch.
            assert {key: value for key, value in os.environ.items()
                    if key.startswith(("PYTEST_", "OMP_INSTALLED_", "FLOOD_"))
                    and key not in ("PYTEST_CURRENT_TEST", "PYTEST_VERSION")} == {}
            assert "OMP584_FLOOD_SOURCE" not in os.environ
            assert os.environ["OMP_WORK_POSTGRES_INTEGRATION"] == "1"
            assert os.environ["OMP_ACCOUNTING_EXTRA"] == "keep"
            assert os.environ["PATH"] == os.environ["EXPECTED_PATH"]
            assert sys.executable == os.environ["EXPECTED_PYTHON"]
            assert not pytestconfig.pluginmanager.hasplugin("cacheprovider")
            storage = Path(os.environ["TMPDIR"]).parent
            assert storage.name.startswith("omp-skip-pg-")
            assert tmp_path.is_relative_to(storage / "basetemp")
    '''))
    out = tmp_path / "out"
    assert sap.main(["--out", str(out)]) == 1
    receipt = read_only_receipt(out)
    assert receipt.complete and receipt.exitstatus == 1
    calls = {node.split("::")[-1]: phases["call"].outcome for node, phases in receipt.nodes.items()}
    assert calls == {"test_failure": "failed", "test_later": "passed"}
    output = capfd.readouterr().out
    assert printed_receipt(output)["exitstatus"] == 1
    summary = json.loads(next(line.removeprefix("summary: ") for line in output.splitlines() if line.startswith("summary: ")))
    assert summary["phases"] == {"setup/passed": 2, "call/failed": 1, "call/passed": 1, "teardown/passed": 2}


@pytest.mark.parametrize("override", [False, True])
def test_launch_inherits_accounting_and_cli_overrides_only_out_and_run(
    launch_suite: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str], override: bool,
) -> None:
    (launch_suite / "test_inherited.py").write_text("def test_run():\n    pass\n")
    inherited = tmp_path / "inherited"
    monkeypatch.setenv(sa.ENV_DIR, str(inherited))
    monkeypatch.setenv(sa.ENV_RUN, "inherited-run")
    monkeypatch.setenv(sa.ENV_CHECK, "inherited-check")
    monkeypatch.setenv(sa.ENV_ROOT, str(tmp_path))
    out = tmp_path / "explicit" if override else inherited
    argv = ["--out", str(out), "--run", "explicit-run"] if override else []
    assert sap.main(argv) == 0
    receipt = read_only_receipt(out)
    assert receipt.complete and receipt.check == "inherited-check"
    assert receipt.run_id == ("explicit-run" if override else "inherited-run")
    assert receipt.selected == ["suite/test_inherited.py::test_run"]
    if override:
        assert not inherited.exists()
    assert printed_receipt(capfd.readouterr().out)["path"].startswith(str(out))


def test_standalone_defaults_emit_complete_receipt_before_disposal(
    launch_suite: Path, tmp_path: Path,
) -> None:
    root = sap.repo_root()
    (launch_suite / "test_defaults.py").write_text(textwrap.dedent(f'''
        import os
        import pytest
        from pathlib import Path
        def test_defaults():
            if os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1":
                pytest.skip("requires alternate DB lane")
            assert Path.cwd() == Path({str(root)!r})
            assert os.environ["OMP_ACCOUNTING_ROOT"] == {str(root)!r}
            assert os.environ["OMP_WORK_POSTGRES_INTEGRATION"] == "1"
    '''))
    # Exercise standalone process entry with scoped host/suite substitutions only.
    proc = subprocess.run([sys.executable, "-c", textwrap.dedent('''
        import sys
        from pathlib import Path
        from omp_work import skip_accounting_postgres as sap
        sap.check_prerequisites = lambda: None
        sap._stat_mode = lambda path: 0o40755
        sap._suite_path = lambda: Path(sys.argv[1])
        raise SystemExit(sap.main([]))
    '''), str(launch_suite)], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "UsageError" not in proc.stdout + proc.stderr
    printed = printed_receipt(proc.stdout)
    assert printed["complete"] and printed["exitstatus"] == 0
    assert printed["check"] == "work-ledger-integration"
    UUID(printed["run_id"])
    receipt_path = Path(printed.pop("path"))
    assert not receipt_path.parent.parent.exists()
    # Parse the printed evidence via the real reader; default cleanup loses no evidence.
    saved = tmp_path / "saved.skip-receipt.json"
    saved.write_text(json.dumps(printed))
    receipt = sa.read_receipt(saved)
    assert receipt.fingerprint == sa.fingerprint(root)
    assert receipt.collected == receipt.selected
    assert len(receipt.nodes) == 1 and next(iter(receipt.nodes.values()))["call"].outcome == "passed"
    unit = unit_suite(launch_suite, tmp_path / "unit", receipt.run_id)
    result = sa.reconcile(unit, [receipt])
    assert result.covered == receipt.selected and not result.pending


def test_launch_prerequisite_refusal_never_executes_pytest(
    launch_suite: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str],
) -> None:
    sentinel = launch_suite / "executed"
    (launch_suite / "test_never.py").write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n")

    def refuse() -> None:
        raise PrerequisiteError(
            "namespace_probe_1_failed", "unshare denied", argv=["unshare", "--user"],
            status=1, stdout="probe output", stderr="permission denied",
        )

    monkeypatch.setattr(sap, "check_prerequisites", refuse)
    assert sap.main([]) == 2
    captured = capfd.readouterr()
    assert "namespace_probe_1_failed" in captured.err and "unshare denied" in captured.err
    assert "probe output" in captured.err and "permission denied" in captured.err
    assert "unshare" in captured.err and "status: 1" in captured.err
    assert not sentinel.exists() and "test session starts" not in captured.out


@pytest.mark.parametrize("args", [["--suite", "tests"], ["-x"], ["--ou", "tmp"], ["tests"]])
def test_cli_rejects_selectors_and_abbreviations(args: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        sap.main(args)
    assert exc.value.code == 2
