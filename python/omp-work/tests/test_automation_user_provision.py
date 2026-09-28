import os
import pwd
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PROVISION = REPO / "infra" / "automation-user" / "provision.sh"
OWNER = pwd.getpwuid(os.getuid())
USER = "ompauto"
REPO_URL = "https://example.invalid/oh-my-pi.git"


def render(tmp_path: Path, *, user: str = USER, owner: str | None = None, commands: Path | None = None):
    out = tmp_path / "rendered"
    cmd = [
        "bash",
        str(PROVISION),
        "--user",
        user,
        "--owner",
        owner if owner is not None else OWNER.pw_name,
        "--repo-url",
        REPO_URL,
        "--render-only",
        "--out",
        str(out),
    ]
    if commands is not None:
        cmd.extend(["--admin-commands", str(commands)])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result, out


def sudo_commands(text: str, user: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    assert lines[0] == f"Defaults:{user} env_reset"
    prefix = f"{user} ALL=(root) NOPASSWD:"
    assert lines[1].startswith(prefix)
    payload = lines[1][len(prefix) :].strip()
    return [part.strip() for part in payload.split(",")]


def test_sudoers_lists_exactly_the_four_robomp_ctl_commands(tmp_path: Path):
    result, out = render(tmp_path)
    assert result.returncode == 0, result.stderr
    commands = [
        f"/usr/local/libexec/{USER}/robomp-ctl up",
        f"/usr/local/libexec/{USER}/robomp-ctl down",
        f"/usr/local/libexec/{USER}/robomp-ctl restart",
        f"/usr/local/libexec/{USER}/robomp-ctl ps",
    ]
    text = (out / "sudoers").read_text()
    assert sudo_commands(text, USER) == commands
    # ALL=(root) is the host/runas spec. The command list itself is not ALL.
    assert "ALL" not in sudo_commands(text, USER)
    assert (out / "admin-commands").read_text().splitlines() == commands
    service = (out / "robomp.service").read_text()
    assert "Type=oneshot" in service
    assert "RemainAfterExit=yes" in service
    assert f"ExecStart=/usr/bin/sudo -n /usr/local/libexec/{USER}/robomp-ctl up" in service
    assert f"ExecStop=/usr/bin/sudo -n /usr/local/libexec/{USER}/robomp-ctl down" in service
    visudo = shutil.which("visudo")
    if visudo:
        checked = subprocess.run([visudo, "-cf", str(out / "sudoers")], capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr


@pytest.mark.parametrize(
    "entry",
    [
        "/bin/bash",
        "/usr/local/libexec/ompauto/robomp-ctl *",
        "robomp-ctl up",
        "ALL",
        "/usr/bin/sudo",
        "/bin/su",
        "/usr/bin/env",
        "/usr/bin/systemctl",
        "/usr/bin/docker",
    ],
)
def test_unsafe_admin_command_writes_no_sudoers(tmp_path: Path, entry: str):
    commands = tmp_path / "commands"
    commands.write_text(entry + "\n")
    result, out = render(tmp_path / "case", commands=commands)
    assert result.returncode != 0
    assert not (out / "sudoers").exists()


def test_user_equal_to_owner_writes_no_sudoers(tmp_path: Path):
    result, out = render(tmp_path, user=OWNER.pw_name)
    assert result.returncode != 0
    assert not (out / "sudoers").exists()


def test_plan_avoids_owner_home_and_account_edits(tmp_path: Path):
    result, out = render(tmp_path)
    assert result.returncode == 0, result.stderr
    plan = (out / "plan.sh").read_text()
    assert OWNER.pw_dir not in plan
    assert "usermod" not in plan
    assert "gpasswd" not in plan
    useradd = next(line for line in plan.splitlines() if line.startswith("useradd "))
    assert useradd == 'useradd -m -U -s /bin/bash -- "$user"'
    assert "-G" not in useradd
    checked = subprocess.run(["bash", "-n", str(out / "plan.sh")], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


def test_robomp_ctl_ps_uses_fixed_compose_path_and_sh_skips_docker(tmp_path: Path):
    result, out = render(tmp_path)
    assert result.returncode == 0, result.stderr
    ctl = out / "robomp-ctl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    docker = bin_dir / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$DOCKER_LOG"\n')
    docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["DOCKER_LOG"] = str(log)
    ps = subprocess.run(["sh", str(ctl), "ps"], env=env, capture_output=True, text=True, timeout=30)
    assert ps.returncode == 0, ps.stderr
    assert log.read_text().splitlines() == [
        "compose",
        "-f",
        f"/etc/{USER}/robomp/docker-compose.yml",
        "--env-file",
        f"/etc/{USER}/robomp/.env",
        "-p",
        f"robomp-{USER}",
        "ps",
    ]
    log.unlink()
    up = subprocess.run(["sh", str(ctl), "up"], env=env, capture_output=True, text=True, timeout=30)
    assert up.returncode == 0, up.stderr
    assert log.read_text().splitlines()[-3:] == ["up", "-d", "--no-build"]
    assert f"/etc/{USER}/robomp/docker-compose.yml" in log.read_text().splitlines()
    log.unlink()
    rejected = subprocess.run(["sh", str(ctl), "sh"], env=env, capture_output=True, text=True, timeout=30)
    assert rejected.returncode == 2
    assert not log.exists()
