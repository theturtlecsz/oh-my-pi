"""Tests for the omp-owner-action approve action (OMP-546-s03, D44)."""

from __future__ import annotations

import base64
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import re
import sys
from pathlib import Path

import omp_work
import pytest
from omp_work.v1.models import Approval

REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY_PATH = REPO_ROOT / "infra/automation-user/omp-owner-action"
CONTRACT_DIR = REPO_ROOT / "python/omp-work/src/omp_work/contracts/v1"
STDIN_LIMIT = 32 * 1024 * 1024


@pytest.fixture(scope="module")
def gateway():
    name = "omp_owner_action_approve"
    loader = importlib.machinery.SourceFileLoader(name, str(GATEWAY_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def schema_bytes() -> bytes:
    return (CONTRACT_DIR / "schema.json").read_bytes()


@pytest.fixture(scope="module")
def issue_enum(schema_bytes: bytes) -> list[str]:
    schema = json.loads(schema_bytes)
    enum = schema["models"]["Approval"]["properties"]["issue"]["enum"]
    assert isinstance(enum, list)
    assert all(isinstance(item, str) for item in enum)
    return list(enum)


@pytest.fixture(scope="module")
def omp_issue(issue_enum: list[str]) -> str:
    for item in issue_enum:
        if re.fullmatch(r"OMP-[0-9]+", item):
            return item
    raise AssertionError("Approval issue enum has no OMP key")


@pytest.fixture(scope="module")
def contract_files() -> dict[str, str]:
    manifest = json.loads((CONTRACT_DIR / "manifest.json").read_text(encoding="utf-8"))
    paths = manifest["paths"]
    assert paths == sorted(set(paths))
    assert "manifest.json" in paths
    assert "approval.json" not in paths
    assert "contract.json" in paths
    files: dict[str, str] = {}
    for rel in paths:
        files[rel] = base64.b64encode((CONTRACT_DIR / rel).read_bytes()).decode("ascii")
    return files


def _fixed(gateway, tmp_path: Path):
    log_file = tmp_path / "action.log"
    log_file.touch(mode=0o600)
    return gateway.Fixed(
        log=log_file,
        flood=tmp_path / "flood",
        clone=tmp_path / "clone",
        python=sys.executable,
        bash="/bin/bash",
        ssh_keygen="/usr/bin/ssh-keygen",
        signing_key=tmp_path / "signing.key",
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "USER": "isolated_thetu",
            "HOME": str(tmp_path / "home"),
            "LANG": "C.UTF-8",
        },
    )


def _stdin(files: dict[str, str]) -> bytes:
    return json.dumps({"files": files}).encode("utf-8")


def _log_entry(fixed) -> dict:
    lines = [
        line
        for line in fixed.log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    return json.loads(lines[0])


def _snapshot(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_file():
            found[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return found


def _encoded(
    paths: list[str], schema: bytes, extra: dict[str, bytes]
) -> dict[str, str]:
    blobs = dict(extra)
    blobs["schema.json"] = schema
    blobs["manifest.json"] = json.dumps({"paths": paths}).encode("utf-8")
    return {path: base64.b64encode(blobs[path]).decode("ascii") for path in paths}


def _assert_refused(
    gateway, tmp_path: Path, argv: list[str], files: dict[str, str], capsys
) -> dict:
    fixed = _fixed(gateway, tmp_path)
    out = io.StringIO()
    rc = gateway.main(argv, fixed=fixed, stdin=io.BytesIO(_stdin(files)), stdout=out)
    assert rc == 2
    assert out.getvalue() == ""
    entry = _log_entry(fixed)
    assert entry["action"] == "approve"
    assert entry["args"] == argv[1:]
    assert entry["rc"] == 2
    assert set(entry) == {"at", "caller", "action", "args", "rc"}
    assert len(fixed.log.read_text(encoding="utf-8")) < 2000
    captured = capsys.readouterr()
    assert "refused" in captured.err
    assert captured.out == ""
    written = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert written == [fixed.log]
    return entry


def test_approve_prints_attested_approval(
    gateway, tmp_path, contract_files, omp_issue, capsys
):
    fixed = _fixed(gateway, tmp_path)
    before = _snapshot(CONTRACT_DIR)
    out = io.BytesIO()
    rc = gateway.main(
        ["approve", "--issue", omp_issue],
        fixed=fixed,
        stdin=io.BytesIO(_stdin(contract_files)),
        stdout=out,
    )
    assert rc == 0
    text = out.getvalue().decode("utf-8")
    assert text.endswith("\n")
    assert text.count("\n") == 1
    payload = json.loads(text)
    assert (
        text
        == json.dumps(
            {
                "contract_version": "work.omp.dev/v1",
                "contract_sha256": payload["contract_sha256"],
                "approved_by": "owner",
                "approved_at": payload["approved_at"],
                "issue": omp_issue,
                "attestation": payload["attestation"],
            }
        )
        + "\n"
    )
    assert payload["contract_sha256"] == omp_work.contract_sha256()
    assert payload["attestation"] == omp_work.approval_attestation(
        payload["contract_sha256"], omp_issue, payload["approved_at"]
    )
    Approval.model_validate(payload)
    assert _snapshot(CONTRACT_DIR) == before
    log_text = fixed.log.read_text(encoding="utf-8")
    assert payload["contract_sha256"] not in log_text
    assert payload["attestation"] not in log_text
    assert len(log_text) < 2000
    entry = _log_entry(fixed)
    assert entry["action"] == "approve"
    assert entry["args"] == ["--issue", omp_issue]
    assert entry["rc"] == 0
    assert set(entry) == {"at", "caller", "action", "args", "rc"}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    written = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert written == [fixed.log]


def test_approve_prints_on_sys_stdout(
    gateway, tmp_path, contract_files, omp_issue, capsys
):
    fixed = _fixed(gateway, tmp_path)
    rc = gateway.main(
        ["approve", "--issue", omp_issue],
        fixed=fixed,
        stdin=_stdin(contract_files),
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["contract_sha256"] == omp_work.contract_sha256()
    assert payload["issue"] == omp_issue
    Approval.model_validate(payload)


def test_issue_not_in_enum(gateway, tmp_path, contract_files, issue_enum, capsys):
    assert "OMP-1" not in issue_enum
    _assert_refused(
        gateway, tmp_path, ["approve", "--issue", "OMP-1"], contract_files, capsys
    )


def test_issue_removed_from_stdin_schema(
    gateway, tmp_path, contract_files, schema_bytes, omp_issue, capsys
):
    schema = json.loads(schema_bytes)
    enum = schema["models"]["Approval"]["properties"]["issue"]["enum"]
    schema["models"]["Approval"]["properties"]["issue"]["enum"] = [
        item for item in enum if item != omp_issue
    ]
    files = dict(contract_files)
    files["schema.json"] = base64.b64encode(json.dumps(schema).encode("utf-8")).decode(
        "ascii"
    )
    _assert_refused(gateway, tmp_path, ["approve", "--issue", omp_issue], files, capsys)


def test_missing_file(gateway, tmp_path, contract_files, omp_issue, capsys):
    files = dict(contract_files)
    assert "contract.json" in files
    del files["contract.json"]
    _assert_refused(gateway, tmp_path, ["approve", "--issue", omp_issue], files, capsys)


def test_missing_manifest(gateway, tmp_path, contract_files, omp_issue, capsys):
    files = dict(contract_files)
    del files["manifest.json"]
    _assert_refused(gateway, tmp_path, ["approve", "--issue", omp_issue], files, capsys)


def test_extra_file(gateway, tmp_path, contract_files, omp_issue, capsys):
    files = dict(contract_files)
    files["extra.json"] = base64.b64encode(b"extra").decode("ascii")
    _assert_refused(gateway, tmp_path, ["approve", "--issue", omp_issue], files, capsys)


def test_parent_segment_refused(gateway, tmp_path, schema_bytes, omp_issue, capsys):
    paths = ["../x", "manifest.json", "schema.json"]
    assert paths == sorted(set(paths))
    files = _encoded(paths, schema_bytes, {"../x": b"x"})
    _assert_refused(gateway, tmp_path, ["approve", "--issue", omp_issue], files, capsys)


def test_absolute_path_refused(gateway, tmp_path, schema_bytes, omp_issue, capsys):
    paths = ["/abs.json", "manifest.json", "schema.json"]
    assert paths == sorted(set(paths))
    files = _encoded(paths, schema_bytes, {"/abs.json": b"x"})
    _assert_refused(gateway, tmp_path, ["approve", "--issue", omp_issue], files, capsys)


def test_path_character_refused(gateway, tmp_path, schema_bytes, omp_issue, capsys):
    paths = ["a b.json", "manifest.json", "schema.json"]
    assert paths == sorted(set(paths))
    files = _encoded(paths, schema_bytes, {"a b.json": b"x"})
    _assert_refused(gateway, tmp_path, ["approve", "--issue", omp_issue], files, capsys)


def test_bad_base64(gateway, tmp_path, contract_files, omp_issue, capsys):
    files = dict(contract_files)
    files["contract.json"] = "!!!!"
    _assert_refused(gateway, tmp_path, ["approve", "--issue", omp_issue], files, capsys)


def test_approve_extra_argument(gateway, tmp_path, contract_files, capsys):
    _assert_refused(
        gateway,
        tmp_path,
        ["approve", "--issue", "OMP-1", "extra"],
        contract_files,
        capsys,
    )


def test_extra_argument_refuses_an_allowed_issue(
    gateway, tmp_path, contract_files, omp_issue, capsys
):
    fixed = _fixed(gateway, tmp_path)
    out = io.StringIO()
    rc = gateway.main(
        ["approve", "--issue", omp_issue, "extra"],
        fixed=fixed,
        stdin=io.BytesIO(_stdin(contract_files)),
        stdout=out,
    )
    assert rc == 2
    assert out.getvalue() == ""
    entry = _log_entry(fixed)
    assert entry["args"] == ["--issue", omp_issue, "extra"]
    assert entry["rc"] == 2
    captured = capsys.readouterr()
    assert "refused" in captured.err


def test_non_omp_enum_issue_refused(
    gateway, tmp_path, contract_files, issue_enum, capsys
):
    home = next(
        item for item in issue_enum if re.fullmatch(r"OMP-[0-9]+", item) is None
    )
    _assert_refused(
        gateway, tmp_path, ["approve", "--issue", home], contract_files, capsys
    )


def test_unsorted_manifest_paths(gateway, tmp_path, schema_bytes, omp_issue, capsys):
    paths = ["schema.json", "manifest.json"]
    assert paths != sorted(set(paths))
    files = _encoded(paths, schema_bytes, {})
    _assert_refused(gateway, tmp_path, ["approve", "--issue", omp_issue], files, capsys)


def test_duplicate_manifest_paths(gateway, tmp_path, schema_bytes, omp_issue, capsys):
    paths = ["manifest.json", "manifest.json", "schema.json"]
    assert paths != sorted(set(paths))
    files = _encoded(paths, schema_bytes, {})
    _assert_refused(gateway, tmp_path, ["approve", "--issue", omp_issue], files, capsys)


def test_approval_json_refused(gateway, tmp_path, schema_bytes, omp_issue, capsys):
    paths = ["approval.json", "manifest.json", "schema.json"]
    assert paths == sorted(set(paths))
    files = _encoded(paths, schema_bytes, {"approval.json": b"{}"})
    _assert_refused(gateway, tmp_path, ["approve", "--issue", omp_issue], files, capsys)


def test_approve_without_stdin(gateway, tmp_path, omp_issue, capsys):
    fixed = _fixed(gateway, tmp_path)
    out = io.StringIO()
    rc = gateway.main(["approve", "--issue", omp_issue], fixed=fixed, stdout=out)
    assert rc == 2
    assert out.getvalue() == ""
    entry = _log_entry(fixed)
    assert entry["action"] == "approve"
    assert entry["args"] == ["--issue", omp_issue]
    assert entry["rc"] == 2
    captured = capsys.readouterr()
    assert "refused" in captured.err


def test_approve_stdin_over_32mib(gateway, tmp_path, capsys):
    fixed = _fixed(gateway, tmp_path)
    out = io.BytesIO()

    class _Bytes:
        def __init__(self, total: int) -> None:
            self._left = total

        def read(self, n: int = -1) -> bytes:
            if self._left <= 0:
                return b""
            take = self._left if n is None or n < 0 else min(n, self._left)
            self._left -= take
            return b"x" * take

    rc = gateway.main(
        ["approve", "--issue", "OMP-514"],
        fixed=fixed,
        stdin=_Bytes(STDIN_LIMIT + 1),
        stdout=out,
    )
    assert rc == 2
    assert out.getvalue() == b""
    entry = _log_entry(fixed)
    assert entry["rc"] == 2
    assert entry["args"] == ["--issue", "OMP-514"]
    captured = capsys.readouterr()
    assert "32 MiB" in captured.err


def test_approve_stdin_of_32mib_is_read(gateway, tmp_path, capsys):
    fixed = _fixed(gateway, tmp_path)
    out = io.BytesIO()
    rc = gateway.main(
        ["approve", "--issue", "OMP-514"],
        fixed=fixed,
        stdin=b"x" * STDIN_LIMIT,
        stdout=out,
    )
    assert rc == 2
    assert out.getvalue() == b""
    captured = capsys.readouterr()
    assert "32 MiB" not in captured.err
    assert "refused" in captured.err
    entry = _log_entry(fixed)
    assert entry["rc"] == 2
