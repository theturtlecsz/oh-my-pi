"""Test verify-restrictions.py security checks for the automation user."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

if os.geteuid() == 0:
    pytest.skip("Root user bypasses filesystem mode restrictions", allow_module_level=True)


def make_writable_recursive(path: Path) -> None:
    try:
        path.chmod(0o700)
    except OSError:
        pass
    try:
        for entry in path.iterdir():
            if entry.is_dir():
                make_writable_recursive(entry)
            else:
                with contextlib.suppress(OSError):
                    entry.chmod(0o700)
    except OSError:
        pass
    with contextlib.suppress(OSError):
        path.chmod(0o700)


@pytest.fixture(autouse=True)
def cleanup_permissions(tmp_path: Path):
    yield
    make_writable_recursive(tmp_path)


@pytest.fixture
def setup_env(tmp_path: Path) -> dict[str, Any]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_home = tmp_path / "user_home"
    fake_home.mkdir(mode=0o700)
    owner_home = tmp_path / "owner_home"
    owner_home.mkdir(mode=0o700)

    admin_cmds_file = tmp_path / "admin-commands.txt"
    allowed_cmds = [
        "/usr/local/libexec/ompbot/robomp-ctl up",
        "/usr/local/libexec/ompbot/robomp-ctl down",
        "/usr/local/libexec/ompbot/robomp-ctl restart",
        "/usr/local/libexec/ompbot/robomp-ctl ps",
    ]
    admin_cmds_file.write_text("\n".join(allowed_cmds) + "\n", encoding="utf-8")

    docker_socket = tmp_path / "docker.sock"
    docker_socket.touch()
    docker_socket.chmod(0o000)

    bearer_file = fake_home / "capabilities" / "automation.json"
    bearer_file.parent.mkdir(parents=True, exist_ok=True)
    bearer_file.write_text(
        json.dumps({"actor_kind": "automation", "token": "auto-token-123"}),
        encoding="utf-8",
    )
    bearer_file.chmod(0o600)

    client_config_file = fake_home / "client.json"
    client_config_file.write_text(
        json.dumps(
            {
                "base_url": "http://127.0.0.1:54322",
                "workspace_id": "00000000-0000-0000-0000-000000000001",
                "owner_id": "00000000-0000-0000-0000-000000000002",
                "bearer_file": str(bearer_file),
            }
        ),
        encoding="utf-8",
    )

    sensitive_paths = [
        ".config/omp/work-ledger/capabilities",
        ".config/omp-work",
        ".config/gh",
        ".git-credentials",
        ".netrc",
        ".ssh",
        ".aws",
        ".azure",
        ".kube",
        ".config/gcloud",
        ".omp/agent",
        ".claude",
        ".docker/config.json",
    ]
    for p in sensitive_paths:
        target = owner_home / p
        if target.name.endswith(".json") or target.name.startswith(".git-") or target.name == ".netrc":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("secret", encoding="utf-8")
            target.chmod(0o000)
        else:
            target.mkdir(parents=True, exist_ok=True)
            dummy = target / "dummy"
            dummy.write_text("secret", encoding="utf-8")
            dummy.chmod(0o000)
            target.chmod(0o000)

    owner_home.chmod(0o111)

    fake_id = bin_dir / "id"
    fake_id.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-Gn" ]; then\n'
        '    printf "%s\\n" "${FAKE_GROUPS:-ompbot}"\n'
        "    exit 0\n"
        "fi\n"
        'exec /usr/bin/id "$@"\n',
        encoding="utf-8",
    )
    fake_id.chmod(0o755)

    fake_sudo = bin_dir / "sudo"
    fake_sudo.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-n" ] && [ "$2" = "true" ]; then\n'
        '    if [ -n "$FAKE_SUDO_ALLOW_TRUE" ]; then\n'
        "        exit 0\n"
        "    fi\n"
        "    exit 1\n"
        "fi\n"
        'if [ "$1" = "-n" ] && [ "$2" = "-l" ]; then\n'
        '    if [ -n "$FAKE_SUDO_OUTPUT" ]; then\n'
        '        printf "%s\\n" "$FAKE_SUDO_OUTPUT"\n'
        "        exit 0\n"
        "    fi\n"
        '    printf "User ompbot may run the following commands on host:\\n    (root) NOPASSWD: /usr/local/libexec/ompbot/robomp-ctl up, /usr/local/libexec/ompbot/robomp-ctl down, /usr/local/libexec/ompbot/robomp-ctl restart, /usr/local/libexec/ompbot/robomp-ctl ps\\n"\n'
        "    exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_sudo.chmod(0o755)

    env = {}
    for k, v in os.environ.items():
        if not k.startswith("AWS_") and not k.startswith("AZURE_") and k not in {
            "ARM_CLIENT_SECRET",
            "KUBECONFIG",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "HOME",
            "PATH",
            "FAKE_GROUPS",
            "FAKE_SUDO_OUTPUT",
            "FAKE_SUDO_ALLOW_TRUE",
        }:
            env[k] = v
    env["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
    env["HOME"] = str(fake_home)
    env["FAKE_GROUPS"] = "ompbot"

    return {
        "fake_home": fake_home,
        "owner_home": owner_home,
        "admin_cmds_file": admin_cmds_file,
        "docker_socket": docker_socket,
        "client_config_file": client_config_file,
        "bearer_file": bearer_file,
        "env": env,
    }


def run_verify(
    setup: dict[str, Any],
    extra_env: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(setup["env"])
    if extra_env:
        env.update(extra_env)

    script_path = (
        Path(__file__).resolve().parents[3]
        / "infra"
        / "automation-user"
        / "verify-restrictions.py"
    )

    cmd = [
        sys.executable,
        str(script_path),
        "--owner",
        "root",
        "--owner-home",
        str(setup["owner_home"]),
        "--admin-commands",
        str(setup["admin_cmds_file"]),
        "--client-config",
        str(setup["client_config_file"]),
        "--docker-socket",
        str(setup["docker_socket"]),
    ]
    if extra_args:
        cmd.extend(extra_args)

    return subprocess.run(cmd, env=env, capture_output=True, text=True)


def test_restricted_setup_exits_0(setup_env: dict[str, Any]) -> None:
    proc = run_verify(setup_env)
    assert proc.returncode == 0, f"Expected returncode 0, got {proc.returncode}:\n{proc.stdout}\n{proc.stderr}"
    data = json.loads(proc.stdout)
    assert len(data) == 7
    for check, res in data.items():
        assert res["ok"] is True, f"Check '{check}' failed: {res['detail']}"


def test_fail_readable_capabilities_owner_json(setup_env: dict[str, Any]) -> None:
    owner_home = setup_env["owner_home"]
    cap_dir = owner_home / ".config" / "omp" / "work-ledger" / "capabilities"
    cap_dir.chmod(0o755)
    owner_json = cap_dir / "owner.json"
    owner_json.write_text('{"token": "owner-secret"}', encoding="utf-8")
    owner_json.chmod(0o644)

    proc = run_verify(setup_env)
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["owner_credentials_unreadable"]["ok"] is False
    for k, v in data.items():
        if k != "owner_credentials_unreadable":
            assert v["ok"] is True, f"Unexpected failure in '{k}': {v['detail']}"


def test_fail_sudo_all_all(setup_env: dict[str, Any]) -> None:
    extra_env = {
        "FAKE_SUDO_OUTPUT": "User ompbot may run the following commands on host:\n    (ALL) ALL"
    }
    proc = run_verify(setup_env, extra_env=extra_env)
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["sudo_named_only"]["ok"] is False
    for k, v in data.items():
        if k != "sudo_named_only":
            assert v["ok"] is True, f"Unexpected failure in '{k}': {v['detail']}"


def test_fail_sudo_extra_command(setup_env: dict[str, Any]) -> None:
    extra_env = {
        "FAKE_SUDO_OUTPUT": (
            "User ompbot may run the following commands on host:\n"
            "    (root) NOPASSWD: /usr/local/libexec/ompbot/robomp-ctl up, "
            "/usr/local/libexec/ompbot/robomp-ctl down, "
            "/usr/local/libexec/ompbot/robomp-ctl restart, "
            "/usr/local/libexec/ompbot/robomp-ctl ps, /bin/sh"
        )
    }
    proc = run_verify(setup_env, extra_env=extra_env)
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["sudo_named_only"]["ok"] is False
    for k, v in data.items():
        if k != "sudo_named_only":
            assert v["ok"] is True, f"Unexpected failure in '{k}': {v['detail']}"


def test_fail_docker_in_groups(setup_env: dict[str, Any]) -> None:
    extra_env = {"FAKE_GROUPS": "ompbot docker"}
    proc = run_verify(setup_env, extra_env=extra_env)
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["groups"]["ok"] is False
    for k, v in data.items():
        if k != "groups":
            assert v["ok"] is True, f"Unexpected failure in '{k}': {v['detail']}"


def test_fail_aws_dir_present(setup_env: dict[str, Any]) -> None:
    fake_home = setup_env["fake_home"]
    (fake_home / ".aws").mkdir()

    proc = run_verify(setup_env)
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["no_cloud_credentials"]["ok"] is False
    for k, v in data.items():
        if k != "no_cloud_credentials":
            assert v["ok"] is True, f"Unexpected failure in '{k}': {v['detail']}"


def test_fail_aws_access_key_set(setup_env: dict[str, Any]) -> None:
    extra_env = {"AWS_ACCESS_KEY_ID": "AKIAEXAMPLE123"}
    proc = run_verify(setup_env, extra_env=extra_env)
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["no_cloud_credentials"]["ok"] is False
    for k, v in data.items():
        if k != "no_cloud_credentials":
            assert v["ok"] is True, f"Unexpected failure in '{k}': {v['detail']}"


def test_fail_bearer_actor_kind_owner(setup_env: dict[str, Any]) -> None:
    bearer_file = setup_env["bearer_file"]
    bearer_file.write_text(
        json.dumps({"actor_kind": "owner", "token": "owner-token-123"}),
        encoding="utf-8",
    )

    proc = run_verify(setup_env)
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["ledger_principal"]["ok"] is False
    for k, v in data.items():
        if k != "ledger_principal":
            assert v["ok"] is True, f"Unexpected failure in '{k}': {v['detail']}"


def test_fail_sudo_true_succeeds(setup_env: dict[str, Any]) -> None:
    extra_env = {"FAKE_SUDO_ALLOW_TRUE": "1"}
    proc = run_verify(setup_env, extra_env=extra_env)
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["sudo_named_only"]["ok"] is False
    for k, v in data.items():
        if k != "sudo_named_only":
            assert v["ok"] is True, f"Unexpected failure in '{k}': {v['detail']}"


def test_fail_docker_socket_writable(setup_env: dict[str, Any]) -> None:
    docker_socket = setup_env["docker_socket"]
    docker_socket.chmod(0o666)

    proc = run_verify(setup_env)
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["docker_socket"]["ok"] is False
    for k, v in data.items():
        if k != "docker_socket":
            assert v["ok"] is True, f"Unexpected failure in '{k}': {v['detail']}"


def test_fail_bearer_in_owner_home(setup_env: dict[str, Any]) -> None:
    owner_home = setup_env["owner_home"]
    owner_home.chmod(0o700)
    owner_bearer = owner_home / "automation.json"
    owner_bearer.write_text(
        json.dumps({"actor_kind": "automation", "token": "auto-tok"}),
        encoding="utf-8",
    )
    owner_bearer.chmod(0o600)
    owner_home.chmod(0o111)

    client_config_file = setup_env["client_config_file"]
    client_config_file.write_text(
        json.dumps(
            {
                "base_url": "http://127.0.0.1:54322",
                "workspace_id": "00000000-0000-0000-0000-000000000001",
                "owner_id": "00000000-0000-0000-0000-000000000002",
                "bearer_file": str(owner_bearer),
            }
        ),
        encoding="utf-8",
    )

    proc = run_verify(setup_env)
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["ledger_principal"]["ok"] is False
    for k, v in data.items():
        if k != "ledger_principal":
            assert v["ok"] is True, f"Unexpected failure in '{k}': {v['detail']}"


def test_fail_owner_home_listable(setup_env: dict[str, Any]) -> None:
    owner_home = setup_env["owner_home"]
    # Change owner_home from 0o111 to 0o755 so it becomes listable
    owner_home.chmod(0o755)

    proc = run_verify(setup_env)
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["owner_credentials_unreadable"]["ok"] is False
    for k, v in data.items():
        if k != "owner_credentials_unreadable":
            assert v["ok"] is True, f"Unexpected failure in '{k}': {v['detail']}"

