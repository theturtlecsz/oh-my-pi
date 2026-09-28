"""parallel-test.sh proves the automation user can run flood, robomp, and agents."""

from __future__ import annotations

import json
import os
import pwd
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra" / "automation-user" / "parallel-test.sh"
NEEDS_NON_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="euid is 0")


def write_exec(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def argv_rows(log: Path) -> list[tuple[str, list[str]]]:
    rows: list[tuple[str, list[str]]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        rows.append((Path(parts[0]).name, parts[1:]))
    return rows


def stage(tmp_path: Path) -> dict[str, Path | dict[str, str]]:
    script = tmp_path / "parallel-test.sh"
    script.write_bytes(SCRIPT.read_bytes())
    script.chmod(0o755)
    write_exec(
        tmp_path / "verify-restrictions.py",
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

Path(os.environ["ARGV_LOG"]).open("a", encoding="utf-8").write(
    "verify\\t" + "\\t".join(sys.argv[1:]) + "\\n"
)
raise SystemExit(int(os.environ.get("VERIFY_RC", "0")))
""",
    )
    write_exec(
        tmp_path / "github-probe.py",
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

token = os.environ.get("GH_TOKEN", "")
if token != "test-token":
    raise SystemExit(f"unexpected GH_TOKEN {token!r}")
Path(os.environ["ARGV_LOG"]).open("a", encoding="utf-8").write(
    "github\\t" + "\\t".join(sys.argv[1:]) + "\\n"
)
raise SystemExit(0)
""",
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    argv_log.write_text("", encoding="utf-8")
    log_header = """#!/bin/sh
printf '%s' "$0" >> "$ARGV_LOG"
for arg in "$@"; do
  printf '\\t%s' "$arg" >> "$ARGV_LOG"
done
printf '\\n' >> "$ARGV_LOG"
"""
    write_exec(
        bin_dir / "gh",
        log_header
        + """if [ "$1" = "auth" ] && [ "$2" = "token" ]; then
  printf '%s\\n' 'test-token'
fi
exit 0
""",
    )
    write_exec(
        bin_dir / "sudo",
        log_header
        + """last=
for arg in "$@"; do
  last=$arg
done
if [ "$last" = "ps" ]; then
  printf '%s\\n' 'NAME STATUS' 'robomp-1 Up'
fi
exit 0
""",
    )
    agent = (
        log_header
        + """reply=${AGENT_REPLY-OK}
if [ -n "$reply" ]; then
  printf '%s\\n' "$reply"
fi
exit 0
"""
    )
    write_exec(bin_dir / "omp", agent)
    write_exec(bin_dir / "claude", agent)
    write_exec(bin_dir / "timeout", log_header + "exit 0\n")

    user = pwd.getpwuid(os.geteuid()).pw_name
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["ARGV_LOG"] = str(argv_log)
    env["USER"] = user
    env["LOGNAME"] = user
    env["VERIFY_RC"] = "0"
    env.pop("AGENT_REPLY", None)
    return {"script": script, "argv_log": argv_log, "env": env, "user": user}


def run_probe(staged: dict, tmp_path: Path, owner: str, evidence: Path | None = None):
    evidence = evidence or (tmp_path / "evidence")
    evidence.mkdir(exist_ok=True)
    admin = tmp_path / "admin-commands"
    admin.write_text("robomp-ctl\n", encoding="utf-8")
    flood_dir = tmp_path / "flood"
    flood_dir.mkdir(exist_ok=True)
    config = tmp_path / "flood-config.json"
    config.write_text("{}\n", encoding="utf-8")
    repo = "acme/widgets"
    result = subprocess.run(
        [
            "bash",
            str(staged["script"]),
            "--owner",
            owner,
            "--admin-commands",
            str(admin),
            "--repo",
            repo,
            "--flood-dir",
            str(flood_dir),
            "--flood-config",
            str(config),
            "--evidence-dir",
            str(evidence),
        ],
        cwd=tmp_path / "cwd" if (tmp_path / "cwd").is_dir() else tmp_path,
        env=staged["env"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result, evidence, admin, flood_dir, config, repo


@NEEDS_NON_ROOT
def test_all_stubs_pass(tmp_path: Path) -> None:
    (tmp_path / "cwd").mkdir()
    staged = stage(tmp_path)
    result, evidence, admin, flood_dir, config, repo = run_probe(staged, tmp_path, "root")
    assert result.returncode == 0, result.stderr
    summary = json.loads((evidence / "summary.json").read_text(encoding="utf-8"))
    assert summary == {
        "verify": 0,
        "github": 0,
        "robomp": 0,
        "omp": 0,
        "claude": 0,
        "flood": 0,
    }
    user = staged["user"]
    ctl = f"/usr/local/libexec/{user}/robomp-ctl"
    assert argv_rows(staged["argv_log"]) == [
        ("verify", ["--owner", "root", "--admin-commands", str(admin)]),
        ("gh", ["auth", "token"]),
        ("github", ["--repo", repo, "--branch", "main"]),
        ("sudo", ["-n", ctl, "up"]),
        ("sudo", ["-n", ctl, "ps"]),
        ("omp", ["-p", "Reply with exactly: OK"]),
        ("claude", ["-p", "Reply with exactly: OK"]),
        ("timeout", ["3600", "python3", str(flood_dir / "flood.py"), "run", "--config", str(config)]),
        ("sudo", ["-n", ctl, "down"]),
    ]
    for step in ("verify", "github", "robomp", "omp", "claude", "flood"):
        assert (evidence / f"{step}.log").is_file()


@NEEDS_NON_ROOT
def test_verify_failure_continues_and_stops_robomp(tmp_path: Path) -> None:
    staged = stage(tmp_path)
    staged["env"]["VERIFY_RC"] = "1"
    result, evidence, _, _, _, _ = run_probe(staged, tmp_path, "root")
    assert result.returncode == 1, result.stderr
    summary = json.loads((evidence / "summary.json").read_text(encoding="utf-8"))
    assert summary["verify"] != 0
    names = [name for name, _args in argv_rows(staged["argv_log"])]
    assert names.index("verify") < names.index("github")
    assert "gh" in names
    assert "omp" in names
    assert "claude" in names
    assert "timeout" in names
    assert names.count("sudo") >= 3
    assert argv_rows(staged["argv_log"])[-1][1][-1] == "down"
    assert any(args[-1] == "down" and "robomp-ctl" in args[-2] for _name, args in argv_rows(staged["argv_log"]))


def test_refuses_when_run_as_owner(tmp_path: Path) -> None:
    staged = stage(tmp_path)
    evidence = tmp_path / "evidence"
    owner = pwd.getpwuid(os.geteuid()).pw_name
    result, evidence, _, _, _, _ = run_probe(staged, tmp_path, owner, evidence)
    assert result.returncode == 2, result.stderr
    assert list(evidence.iterdir()) == []
    assert staged["argv_log"].read_text(encoding="utf-8") == ""


@NEEDS_NON_ROOT
def test_refuses_when_checkout_is_under_owner_home(tmp_path: Path) -> None:
    owner_home = tmp_path / "owner-home"
    checkout = owner_home / "checkout"
    script_dir = checkout / "infra" / "automation-user"
    script_dir.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True, capture_output=True)
    script = script_dir / "parallel-test.sh"
    script.write_bytes(SCRIPT.read_bytes())
    script.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_exec(
        bin_dir / "getent",
        """#!/bin/sh
if [ "$1" = "passwd" ]; then
  printf 'root:x:0:0:root:%s:/bin/sh\\n' "$FAKE_OWNER_HOME"
  exit 0
fi
exec /usr/bin/getent "$@"
""",
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_OWNER_HOME"] = str(owner_home.resolve())
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--owner",
            "root",
            "--admin-commands",
            str(tmp_path / "admin-commands"),
            "--repo",
            "acme/widgets",
            "--flood-dir",
            str(tmp_path / "flood"),
            "--flood-config",
            str(tmp_path / "flood-config.json"),
            "--evidence-dir",
            str(evidence),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert list(evidence.iterdir()) == []


@NEEDS_NON_ROOT
def test_agent_reply_without_ok_fails_that_step_only(tmp_path: Path) -> None:
    staged = stage(tmp_path)
    staged["env"]["AGENT_REPLY"] = "nope"
    result, evidence, _, _, _, _ = run_probe(staged, tmp_path, "root")
    assert result.returncode == 1, result.stderr
    summary = json.loads((evidence / "summary.json").read_text(encoding="utf-8"))
    assert summary["verify"] == 0
    assert summary["github"] == 0
    assert summary["robomp"] == 0
    assert summary["omp"] != 0
    assert summary["claude"] != 0
    assert summary["flood"] == 0
    assert argv_rows(staged["argv_log"])[-1][1][-1] == "down"
