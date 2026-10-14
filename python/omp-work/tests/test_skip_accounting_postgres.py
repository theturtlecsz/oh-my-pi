"""check_prerequisites refuses with a named cause and exit_code 2; all host access is injected."""

from __future__ import annotations

import subprocess
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
