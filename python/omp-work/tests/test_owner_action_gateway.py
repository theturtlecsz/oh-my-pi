"""Tests for the omp-owner-action gateway (OMP-546-s01)."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import pwd
import sys
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY_PATH = REPO_ROOT / "infra/automation-user/omp-owner-action"


@pytest.fixture(scope="module")
def gateway():
    loader = importlib.machinery.SourceFileLoader("omp_owner_action", str(GATEWAY_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def harness(tmp_path):
    log_file = tmp_path / "action.log"
    log_file.touch(mode=0o600)

    flood_dir = tmp_path / "flood"
    flood_dir.mkdir()
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()

    record_file = tmp_path / "recorded_invocations.jsonl"

    # Fake ledger_admin.py
    ledger_admin = flood_dir / "ledger_admin.py"
    ledger_admin.write_text(
        f"""#!/usr/bin/env python3
from pathlib import Path
import json, os, sys
record_file = Path({repr(str(record_file))})
exit_code_file = Path({repr(str(tmp_path / "exit_code.txt"))})
exit_code = int(exit_code_file.read_text().strip()) if exit_code_file.exists() else 0
with open(record_file, "a", encoding="utf-8") as f:
    f.write(json.dumps({{
        "script": "ledger_admin.py",
        "argv": sys.argv[1:],
        "env": dict(os.environ),
        "cwd": os.getcwd(),
        "stdin": sys.stdin.read(),
    }}) + "\\n")
sys.exit(exit_code)
""",
        encoding="utf-8",
    )
    ledger_admin.chmod(0o755)

    # Fake deploy-omp.sh
    deploy_sh = flood_dir / "deploy-omp.sh"
    deploy_sh.write_text(
        f"""#!/bin/bash
exit_code=0
if [ -f "{tmp_path}/exit_code.txt" ]; then
    exit_code=$(cat "{tmp_path}/exit_code.txt")
fi
python3 -c '
import json, os, sys
record_file = "{record_file}"
with open(record_file, "a", encoding="utf-8") as f:
    f.write(json.dumps({{
        "script": "deploy-omp.sh",
        "argv": sys.argv[1:],
        "env": dict(os.environ),
        "cwd": os.getcwd(),
        "stdin": sys.stdin.read(),
    }}) + "\\n")
' "$@"
exit "$exit_code"
""",
        encoding="utf-8",
    )
    deploy_sh.chmod(0o755)

    isolated_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "USER": "isolated_thetu",
        "HOME": str(tmp_path / "home"),
        "LANG": "C.UTF-8",
    }

    return {
        "log": log_file,
        "flood": flood_dir,
        "clone": clone_dir,
        "python": sys.executable,
        "bash": "/bin/bash",
        "ssh_keygen": "/usr/bin/ssh-keygen",
        "signing_key": tmp_path / "signing.key",
        "env": isolated_env,
        "record_file": record_file,
        "exit_code_file": tmp_path / "exit_code.txt",
    }


def read_log_records(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def read_script_records(record_path: Path) -> list[dict]:
    if not record_path.exists():
        return []
    lines = record_path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_fixed_dataclass_and_defaults(gateway):
    Fixed = gateway.Fixed
    FIXED = gateway.FIXED

    assert FIXED.log == Path("/var/log/omp-owner-action.log")
    assert FIXED.flood == Path("/home/thetu/flood")
    assert FIXED.clone == Path("/home/thetu/flood-repos/oh-my-pi")
    assert FIXED.python == Path("/usr/bin/python3")
    assert FIXED.bash == Path("/usr/bin/bash")
    assert FIXED.ssh_keygen == Path("/usr/bin/ssh-keygen")
    assert FIXED.signing_key == Path("/home/thetu/.config/omp/owner-signing/omp-owner-signing")
    assert FIXED.env["USER"] == "thetu"
    assert FIXED.env["HOME"] == "/home/thetu"

    # Fixed must be frozen
    with pytest.raises(FrozenInstanceError):
        FIXED.log = Path("/tmp/other.log")  # type: ignore[misc]


def test_ledger_close_success(gateway, harness):
    fixed = gateway.Fixed(
        log=harness["log"],
        flood=harness["flood"],
        clone=harness["clone"],
        python=harness["python"],
        bash=harness["bash"],
        ssh_keygen=harness["ssh_keygen"],
        signing_key=harness["signing_key"],
        env=harness["env"],
    )

    rc = gateway.main(["ledger-close", "OMP-546", "closing evidence proof"], fixed=fixed)
    assert rc == 0

    script_runs = read_script_records(harness["record_file"])
    assert len(script_runs) == 1
    run = script_runs[0]
    assert run["script"] == "ledger_admin.py"
    assert run["argv"] == ["close", "OMP-546", "closing evidence proof"]
    assert run["stdin"] == ""  # stdin is DEVNULL

    logs = read_log_records(harness["log"])
    assert len(logs) == 1
    log = logs[0]
    assert log["action"] == "ledger-close"
    assert log["args"] == ["OMP-546", "closing evidence proof"]
    assert log["rc"] == 0
    assert "at" in log
    assert "caller" in log


def test_close_riders_success(gateway, harness):
    fixed = gateway.Fixed(
        log=harness["log"],
        flood=harness["flood"],
        clone=harness["clone"],
        python=harness["python"],
        bash=harness["bash"],
        ssh_keygen=harness["ssh_keygen"],
        signing_key=harness["signing_key"],
        env=harness["env"],
    )

    rc = gateway.main(["close-riders"], fixed=fixed)
    assert rc == 0

    script_runs = read_script_records(harness["record_file"])
    assert len(script_runs) == 1
    run = script_runs[0]
    assert run["script"] == "ledger_admin.py"
    assert run["argv"] == ["close-riders"]
    assert run["stdin"] == ""

    logs = read_log_records(harness["log"])
    assert len(logs) == 1
    log = logs[0]
    assert log["action"] == "close-riders"
    assert log["args"] == []
    assert log["rc"] == 0


def test_deploy_success(gateway, harness):
    fixed = gateway.Fixed(
        log=harness["log"],
        flood=harness["flood"],
        clone=harness["clone"],
        python=harness["python"],
        bash=harness["bash"],
        ssh_keygen=harness["ssh_keygen"],
        signing_key=harness["signing_key"],
        env=harness["env"],
    )

    rc = gateway.main(["deploy"], fixed=fixed)
    assert rc == 0

    script_runs = read_script_records(harness["record_file"])
    assert len(script_runs) == 1
    run = script_runs[0]
    assert run["script"] == "deploy-omp.sh"
    assert run["cwd"] == str(harness["clone"])
    assert run["stdin"] == ""

    logs = read_log_records(harness["log"])
    assert len(logs) == 1
    log = logs[0]
    assert log["action"] == "deploy"
    assert log["args"] == []
    assert log["rc"] == 0


def test_child_exit_code_propagated_and_logged(gateway, harness):
    fixed = gateway.Fixed(
        log=harness["log"],
        flood=harness["flood"],
        clone=harness["clone"],
        python=harness["python"],
        bash=harness["bash"],
        ssh_keygen=harness["ssh_keygen"],
        signing_key=harness["signing_key"],
        env=harness["env"],
    )

    harness["exit_code_file"].write_text("42\n", encoding="utf-8")

    rc = gateway.main(["deploy"], fixed=fixed)
    assert rc == 42

    logs = read_log_records(harness["log"])
    assert len(logs) == 1
    assert logs[0]["rc"] == 42


@pytest.mark.parametrize(
    "bad_key",
    ["omp-1", "OMP-1x", "OMP-", "123", "OMP-12a3", "omp-546", "OMP-", "OMP-1 2", "$(id)", ";"],
)
def test_bad_key_refused(gateway, harness, bad_key, capsys):
    fixed = gateway.Fixed(
        log=harness["log"],
        flood=harness["flood"],
        clone=harness["clone"],
        python=harness["python"],
        bash=harness["bash"],
        ssh_keygen=harness["ssh_keygen"],
        signing_key=harness["signing_key"],
        env=harness["env"],
    )

    rc = gateway.main(["ledger-close", bad_key, "some evidence"], fixed=fixed)
    assert rc == 2

    captured = capsys.readouterr()
    assert "refused" in captured.err

    # Fake script was NOT run
    assert read_script_records(harness["record_file"]) == []

    logs = read_log_records(harness["log"])
    assert len(logs) == 1
    assert logs[0]["action"] == "ledger-close"
    assert logs[0]["args"] == [bad_key, "some evidence"]
    assert logs[0]["rc"] == 2


@pytest.mark.parametrize(
    "extra_argv",
    [
        ["deploy", "extra"],
        ["deploy", "--status"],
        ["close-riders", "extra"],
        ["close-riders", "--dry-run"],
        ["ledger-close", "OMP-123", "evidence", "extra"],
    ],
)
def test_extra_argument_refused(gateway, harness, extra_argv, capsys):
    fixed = gateway.Fixed(
        log=harness["log"],
        flood=harness["flood"],
        clone=harness["clone"],
        python=harness["python"],
        bash=harness["bash"],
        ssh_keygen=harness["ssh_keygen"],
        signing_key=harness["signing_key"],
        env=harness["env"],
    )

    rc = gateway.main(extra_argv, fixed=fixed)
    assert rc == 2

    captured = capsys.readouterr()
    assert "refused" in captured.err

    assert read_script_records(harness["record_file"]) == []

    logs = read_log_records(harness["log"])
    assert len(logs) == 1
    assert logs[0]["action"] == extra_argv[0]
    assert logs[0]["args"] == extra_argv[1:]
    assert logs[0]["rc"] == 2


@pytest.mark.parametrize(
    "shell_argv",
    [
        ["$(id)"],
        [";"],
        ["deploy; rm -rf /"],
        ["ledger-close", "$(id)", "evidence"],
        ["ledger-close", ";", "evidence"],
    ],
)
def test_shell_text_refused(gateway, harness, shell_argv, capsys):
    fixed = gateway.Fixed(
        log=harness["log"],
        flood=harness["flood"],
        clone=harness["clone"],
        python=harness["python"],
        bash=harness["bash"],
        ssh_keygen=harness["ssh_keygen"],
        signing_key=harness["signing_key"],
        env=harness["env"],
    )

    rc = gateway.main(shell_argv, fixed=fixed)
    assert rc == 2

    captured = capsys.readouterr()
    assert "refused" in captured.err

    assert read_script_records(harness["record_file"]) == []

    logs = read_log_records(harness["log"])
    assert len(logs) == 1
    assert logs[0]["action"] == shell_argv[0]
    assert logs[0]["args"] == shell_argv[1:]
    assert logs[0]["rc"] == 2


@pytest.mark.parametrize(
    "unknown_argv",
    [
        ["unknown-action"],
        ["unknown-action", "foo", "bar"],
        ["sign"],
        ["approve", "--issue", "OMP-123"],
        [],
    ],
)
def test_unknown_action_refused(gateway, harness, unknown_argv, capsys):
    fixed = gateway.Fixed(
        log=harness["log"],
        flood=harness["flood"],
        clone=harness["clone"],
        python=harness["python"],
        bash=harness["bash"],
        ssh_keygen=harness["ssh_keygen"],
        signing_key=harness["signing_key"],
        env=harness["env"],
    )

    rc = gateway.main(unknown_argv, fixed=fixed)
    assert rc == 2

    captured = capsys.readouterr()
    assert "refused" in captured.err

    assert read_script_records(harness["record_file"]) == []

    logs = read_log_records(harness["log"])
    assert len(logs) == 1
    expected_action = unknown_argv[0] if unknown_argv else ""
    expected_args = unknown_argv[1:] if unknown_argv else []
    assert logs[0]["action"] == expected_action
    assert logs[0]["args"] == expected_args
    assert logs[0]["rc"] == 2


def test_missing_log_refused(gateway, harness, capsys):
    missing_log = harness["flood"] / "nonexistent.log"
    assert not missing_log.exists()

    fixed = gateway.Fixed(
        log=missing_log,
        flood=harness["flood"],
        clone=harness["clone"],
        python=harness["python"],
        bash=harness["bash"],
        ssh_keygen=harness["ssh_keygen"],
        signing_key=harness["signing_key"],
        env=harness["env"],
    )

    rc = gateway.main(["deploy"], fixed=fixed)
    assert rc == 2

    captured = capsys.readouterr()
    assert "log open failed" in captured.err

    # Must run nothing
    assert read_script_records(harness["record_file"]) == []
    # Must not create the log file (no O_CREAT)
    assert not missing_log.exists()


def test_caller_env_var_does_not_reach_child(gateway, harness, monkeypatch):
    fixed = gateway.Fixed(
        log=harness["log"],
        flood=harness["flood"],
        clone=harness["clone"],
        python=harness["python"],
        bash=harness["bash"],
        ssh_keygen=harness["ssh_keygen"],
        signing_key=harness["signing_key"],
        env=harness["env"],
    )

    secret_key = "FORBIDDEN_CALLER_VAR_999"
    secret_val = "secret_value_xyz"
    monkeypatch.setenv(secret_key, secret_val)
    monkeypatch.setenv("USER", "attacker_user")

    rc = gateway.main(["ledger-close", "OMP-100", "test evidence"], fixed=fixed)
    assert rc == 0

    script_runs = read_script_records(harness["record_file"])
    assert len(script_runs) == 1
    child_env = script_runs[0]["env"]

    assert secret_key not in child_env
    assert child_env["USER"] == harness["env"]["USER"]
    assert child_env["HOME"] == harness["env"]["HOME"]

    # Gateway must never mutate outer os.environ
    assert os.environ[secret_key] == secret_val


def test_caller_identity_logged(gateway, harness, monkeypatch):
    fixed = gateway.Fixed(
        log=harness["log"],
        flood=harness["flood"],
        clone=harness["clone"],
        python=harness["python"],
        bash=harness["bash"],
        ssh_keygen=harness["ssh_keygen"],
        signing_key=harness["signing_key"],
        env=harness["env"],
    )

    # 1. With SUDO_USER set
    monkeypatch.setenv("SUDO_USER", "ompbot_automation")
    rc = gateway.main(["close-riders"], fixed=fixed)
    assert rc == 0

    logs = read_log_records(harness["log"])
    assert len(logs) == 1
    assert logs[0]["caller"] == "ompbot_automation"

    # 2. Without SUDO_USER (falls back to username)
    monkeypatch.delenv("SUDO_USER", raising=False)
    rc = gateway.main(["close-riders"], fixed=fixed)
    assert rc == 0

    logs = read_log_records(harness["log"])
    assert len(logs) == 2
    expected_caller = pwd.getpwuid(os.getuid()).pw_name
    assert logs[1]["caller"] == expected_caller


@pytest.mark.parametrize(
    "bad_evidence",
    [
        "",  # empty
        "has\nnewline",  # control character
        "has\rreturn",  # control character
        "has\x00null",  # control character
        "a" * 2001,  # too long (>2000)
    ],
)
def test_evidence_validation_refused(gateway, harness, bad_evidence, capsys):
    fixed = gateway.Fixed(
        log=harness["log"],
        flood=harness["flood"],
        clone=harness["clone"],
        python=harness["python"],
        bash=harness["bash"],
        ssh_keygen=harness["ssh_keygen"],
        signing_key=harness["signing_key"],
        env=harness["env"],
    )

    rc = gateway.main(["ledger-close", "OMP-546", bad_evidence], fixed=fixed)
    assert rc == 2

    captured = capsys.readouterr()
    assert "refused" in captured.err

    assert read_script_records(harness["record_file"]) == []

    logs = read_log_records(harness["log"])
    assert len(logs) == 1
    assert logs[0]["action"] == "ledger-close"
    assert logs[0]["args"] == ["OMP-546", bad_evidence]
    assert logs[0]["rc"] == 2


def test_evidence_max_length_accepted(gateway, harness):
    fixed = gateway.Fixed(
        log=harness["log"],
        flood=harness["flood"],
        clone=harness["clone"],
        python=harness["python"],
        bash=harness["bash"],
        ssh_keygen=harness["ssh_keygen"],
        signing_key=harness["signing_key"],
        env=harness["env"],
    )

    max_evidence = "x" * 2000
    rc = gateway.main(["ledger-close", "OMP-546", max_evidence], fixed=fixed)
    assert rc == 0

    script_runs = read_script_records(harness["record_file"])
    assert len(script_runs) == 1
    assert script_runs[0]["argv"] == ["close", "OMP-546", max_evidence]

    logs = read_log_records(harness["log"])
    assert len(logs) == 1
    assert logs[0]["args"] == ["OMP-546", max_evidence]
    assert logs[0]["rc"] == 0
    assert set(logs[0].keys()) == {"at", "caller", "action", "args", "rc"}


def test_os_environ_never_mutated(gateway, harness):
    fixed = gateway.Fixed(
        log=harness["log"],
        flood=harness["flood"],
        clone=harness["clone"],
        python=harness["python"],
        bash=harness["bash"],
        ssh_keygen=harness["ssh_keygen"],
        signing_key=harness["signing_key"],
        env=harness["env"],
    )

    before = dict(os.environ)
    gateway.main(["ledger-close", "OMP-123", "evidence"], fixed=fixed)
    gateway.main(["close-riders"], fixed=fixed)
    gateway.main(["deploy"], fixed=fixed)
    gateway.main(["unknown-action"], fixed=fixed)
    assert dict(os.environ) == before


def test_stdout_stringio_redirection(gateway, harness):
    import io

    fixed = gateway.Fixed(
        log=harness["log"],
        flood=harness["flood"],
        clone=harness["clone"],
        python=harness["python"],
        bash=harness["bash"],
        ssh_keygen=harness["ssh_keygen"],
        signing_key=harness["signing_key"],
        env=harness["env"],
    )

    buf = io.StringIO()
    rc = gateway.main(["deploy"], fixed=fixed, stdout=buf)
    assert rc == 0

