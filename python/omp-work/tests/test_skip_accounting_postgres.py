"""check_prerequisites refuses with a named cause and exit_code 2; all host access is injected."""

from __future__ import annotations

import copy
import errno
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omp_work import skip_accounting_postgres as sap
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
        assert owned._allocation is not None
        with pytest.raises(AttributeError):
            owned._allocation.root = sentinel  # type: ignore[misc]
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
