import os
import pwd
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PROVISION = REPO / "infra" / "automation-user" / "provision.sh"
OWNER = pwd.getpwuid(os.getuid())
USER = "ompauto"
REPO_URL = "https://example.invalid/oh-my-pi.git"


def render(tmp_path: Path, *, user: str = USER, owner: str | None = None):
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
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result, out


def test_render_only_writes_plan_without_robomp(tmp_path: Path):
    result, out = render(tmp_path)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in out.iterdir()) == ["plan.sh"]
    plan = (out / "plan.sh").read_text()
    rendered = "\n".join(p.read_text() for p in out.iterdir())
    assert "robomp" not in rendered
    assert "robomp" not in result.stdout
    assert "robomp" not in result.stderr
    assert "sudoers" not in plan
    assert "admin-commands" not in plan
    assert "docker" not in plan
    assert 'useradd -m -U -s /bin/bash -- "$user"' in plan
    assert 'loginctl enable-linger "$user"' in plan
    assert 'runuser -u "$user" -- git clone -- "$repo_url" "$home/oh-my-pi"' in plan
    checked = subprocess.run(["bash", "-n", str(out / "plan.sh")], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


def test_user_equal_to_owner_writes_no_plan(tmp_path: Path):
    result, out = render(tmp_path, user=OWNER.pw_name)
    assert result.returncode != 0
    assert not (out / "plan.sh").exists()


def test_plan_avoids_owner_home_and_account_edits(tmp_path: Path):
    result, out = render(tmp_path)
    assert result.returncode == 0, result.stderr
    plan = (out / "plan.sh").read_text()
    assert OWNER.pw_dir not in plan
    assert "usermod" not in plan
    assert "gpasswd" not in plan
    assert "robomp" not in plan
    useradd = next(line for line in plan.splitlines() if line.startswith("useradd "))
    assert useradd == 'useradd -m -U -s /bin/bash -- "$user"'
    assert "-G" not in useradd
    checked = subprocess.run(["bash", "-n", str(out / "plan.sh")], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
