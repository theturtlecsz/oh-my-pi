"""Test cutover.sh: apply/rollback of flood's units between users, with refusals.

The script runs against fake `systemctl`/`sudo`/`docker` on PATH. The fake
systemctl keeps the units' is-enabled/is-active state in a JSON file and logs
every invocation, so the test can assert both the resulting unit state and
that a refusal mutated nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

if os.geteuid() == 0:
    pytest.skip(
        "Root user bypasses the cutover's permission assumptions",
        allow_module_level=True,
    )

REPO_ROOT = Path(__file__).resolve().parents[3]
CUTOVER = REPO_ROOT / "infra" / "automation-user" / "cutover.sh"

OWNER_UNITS = [
    "flood.service",
    "flood-dash-http.service",
    "flood-operator.timer",
    "flood-export.timer",
    "flood-dashboard.timer",
    "flood-tmp-watch.timer",
    "flood-home-pkg-watch.path",
]
AUTOMATION_UNITS = [*OWNER_UNITS, "robomp.service"]

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
# Fake systemctl for the cutover test: `-M <user>@` selects the automation
# user's manager, anything else is the owner's. State lives in
# $FAKE_SYSTEMD_STATE, invocations are appended to $FAKE_LOG.
#
# Exit codes follow the real tool: `is-enabled`/`is-active` print the state but
# exit non-zero for `disabled`/`inactive`, and an unreadable unit prints
# nothing and exits non-zero. $FAKE_SYSTEMD_FAIL, when set to `<side> <verb>`
# or `<side> <verb> <unit>`, makes every matching call fail without touching
# state.
set -u
printf 'systemctl %s\\n' "$*" >>"$FAKE_LOG"

side=owner
positional=()
i=1
while [ "$i" -le "$#" ]; do
	a=${!i}
	case "$a" in
		--user) i=$((i + 1)) ;;
		-M) side=automation; i=$((i + 2)) ;;
		*) positional+=("$a"); i=$((i + 1)) ;;
	esac
done

cmd=${positional[0]:-}
last=${positional[-1]:-}
if [ -n "${FAKE_SYSTEMD_FAIL:-}" ]; then
	case "$side $cmd $last" in
		"$FAKE_SYSTEMD_FAIL" | "$FAKE_SYSTEMD_FAIL "*) matched=1 ;;
		*) matched=0 ;;
	esac
	if [ "$matched" -eq 1 ]; then
		printf 'fake systemctl: injected failure: %s %s %s\\n' "$side" "$cmd" "$last" >&2
		exit 1
	fi
fi

python3 - "$FAKE_SYSTEMD_STATE" "$side" "$cmd" "${positional[@]:1}" <<'PY'
import json
import os
import sys

path, side, cmd = sys.argv[1], sys.argv[2], sys.argv[3]
args = sys.argv[4:]
with open(path, encoding="utf-8") as handle:
    state = json.load(handle)
section = state.setdefault(side, {})

if cmd in ("is-enabled", "is-active"):
    key = "is_enabled" if cmd == "is-enabled" else "is_active"
    value = section.get(args[-1], {}).get(key, "")
    if not value:
        print("Failed to get unit state: " + args[-1], file=sys.stderr)
        sys.exit(1)
    print(value)
    sys.exit(0 if value in ("enabled", "active") else 1)

if cmd in ("enable", "disable", "start", "stop", "restart"):
    name = args[-1]
    entry = section.setdefault(name, {})
    if cmd == "enable":
        entry["is_enabled"] = "enabled"
        if "--now" in args:
            entry["is_active"] = "failed" if name == os.environ.get("FAKE_SYSTEMD_STUCK") else "active"
    elif cmd == "disable":
        entry["is_enabled"] = "disabled"
        if "--now" in args:
            entry["is_active"] = "inactive"
    elif cmd == "start":
        entry["is_active"] = "active"
    elif cmd == "stop":
        entry["is_active"] = "inactive"
    elif cmd == "restart":
        entry["is_active"] = "active"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    sys.exit(0)

if cmd == "cat":
    if args[-1] in section:
        print("[Unit]")
        print("Description=fake " + args[-1])
        sys.exit(0)
    sys.exit(1)

sys.exit(0)
PY
"""

FAKE_SUDO = """#!/usr/bin/env bash
set -u
printf 'sudo %s\\n' "$*" >>"$FAKE_LOG"
exec "$@"
"""

FAKE_DOCKER = """#!/usr/bin/env bash
set -u
printf 'docker %s\\n' "$*" >>"$FAKE_LOG"
if [ -n "${FAKE_DOCKER_OUTPUT:-}" ]; then
	printf '%s\\n' "$FAKE_DOCKER_OUTPUT"
fi
exit 0
"""


def write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def setup(tmp_path: Path) -> dict[str, Any]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "systemctl", FAKE_SYSTEMCTL)
    write_executable(bin_dir / "sudo", FAKE_SUDO)
    write_executable(bin_dir / "docker", FAKE_DOCKER)

    units_tsv = tmp_path / "cutover-units.tsv"
    lines = [f"owner\t{u}" for u in OWNER_UNITS] + [
        f"automation\t{u}" for u in AUTOMATION_UNITS
    ]
    units_tsv.write_text("\n".join(lines) + "\n", encoding="utf-8")

    systemd_state = tmp_path / "systemd-state.json"
    owner_state = {
        "flood.service": {"is_enabled": "enabled", "is_active": "active"},
        "flood-dash-http.service": {"is_enabled": "disabled", "is_active": "active"},
        "flood-operator.timer": {"is_enabled": "enabled", "is_active": "inactive"},
        "flood-export.timer": {"is_enabled": "enabled", "is_active": "active"},
        "flood-dashboard.timer": {"is_enabled": "enabled", "is_active": "active"},
        "flood-tmp-watch.timer": {"is_enabled": "enabled", "is_active": "active"},
        "flood-home-pkg-watch.path": {"is_enabled": "enabled", "is_active": "active"},
    }
    automation_state = {
        unit: {"is_enabled": "enabled", "is_active": "inactive"}
        for unit in AUTOMATION_UNITS
    }
    systemd_state.write_text(
        json.dumps({"owner": owner_state, "automation": automation_state}),
        encoding="utf-8",
    )

    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_LOG"] = str(log)
    env["FAKE_SYSTEMD_STATE"] = str(systemd_state)
    env.pop("FAKE_SYSTEMD_STUCK", None)
    env.pop("FAKE_SYSTEMD_FAIL", None)
    env.pop("FAKE_DOCKER_OUTPUT", None)

    return {
        "units_tsv": units_tsv,
        "state_file": tmp_path / "cutover-state.json",
        "systemd_state": systemd_state,
        "log": log,
        "env": env,
        "owner_state": owner_state,
    }


def read_state(setup: dict[str, Any]) -> dict[str, Any]:
    return json.loads(setup["systemd_state"].read_text(encoding="utf-8"))


def read_calls(setup: dict[str, Any]) -> list[str]:
    return [
        line for line in setup["log"].read_text(encoding="utf-8").splitlines() if line
    ]


def parse_call(line: str) -> tuple[str, str, str]:
    """Return (side, verb, unit) for one logged systemctl invocation."""
    tokens = line.split()
    if not tokens or tokens[0] != "systemctl":
        return ("other", tokens[0] if tokens else "", "")
    side = "owner"
    rest: list[str] = []
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token == "--user":
            i += 1
        elif token == "-M":
            side = "automation"
            i += 2
        else:
            rest.append(token)
            i += 1
    verb = rest[0] if rest else ""
    unit = rest[-1] if len(rest) > 1 else ""
    return (side, verb, unit)


def run_cutover(
    setup: dict[str, Any],
    action: str,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(setup["env"])
    if extra_env:
        env.update(extra_env)
    cmd = [
        "bash",
        str(CUTOVER),
        action,
        "--user",
        "ompbot",
        "--state-file",
        str(setup["state_file"]),
        "--units",
        str(setup["units_tsv"]),
    ]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)


def test_apply_then_rollback_restores_each_snapshot(setup: dict[str, Any]) -> None:
    before = read_state(setup)

    applied = run_cutover(setup, "apply")
    assert applied.returncode == 0, f"{applied.stdout}\n{applied.stderr}"

    saved = json.loads(setup["state_file"].read_text(encoding="utf-8"))
    assert saved["units"] == setup["owner_state"]

    after_apply = read_state(setup)
    for unit in OWNER_UNITS:
        assert after_apply["owner"][unit] == {
            "is_enabled": "disabled",
            "is_active": "inactive",
        }, unit
    for unit in AUTOMATION_UNITS:
        assert after_apply["automation"][unit] == {
            "is_enabled": "enabled",
            "is_active": "active",
        }, unit

    # Every owner unit was stopped before any automation unit was started.
    calls = [parse_call(line) for line in read_calls(setup)]
    last_owner_disable = max(
        i
        for i, (side, verb, _) in enumerate(calls)
        if side == "owner" and verb == "disable"
    )
    first_automation_enable = min(
        i
        for i, (side, verb, _) in enumerate(calls)
        if side == "automation" and verb == "enable"
    )
    assert last_owner_disable < first_automation_enable

    rolled = run_cutover(setup, "rollback")
    assert rolled.returncode == 0, f"{rolled.stdout}\n{rolled.stderr}"

    after_rollback = read_state(setup)
    assert after_rollback["owner"] == before["owner"]
    for unit in AUTOMATION_UNITS:
        assert after_rollback["automation"][unit] == {
            "is_enabled": "disabled",
            "is_active": "inactive",
        }, unit

    final = json.loads(setup["state_file"].read_text(encoding="utf-8"))
    assert "rolled_back_at" in final
    assert final["units"] == setup["owner_state"]


def test_apply_restores_enabled_inactive_and_disabled_active(
    setup: dict[str, Any],
) -> None:
    # flood-dash-http.service is disabled+active and flood-operator.timer is
    # enabled+inactive: rolling back must reproduce both axes exactly.
    applied = run_cutover(setup, "apply")
    assert applied.returncode == 0, applied.stderr
    rolled = run_cutover(setup, "rollback")
    assert rolled.returncode == 0, rolled.stderr

    after = read_state(setup)["owner"]
    assert after["flood-dash-http.service"] == {
        "is_enabled": "disabled",
        "is_active": "active",
    }
    assert after["flood-operator.timer"] == {
        "is_enabled": "enabled",
        "is_active": "inactive",
    }


def test_apply_refuses_when_state_file_exists(setup: dict[str, Any]) -> None:
    existing = {
        "user": "ompbot",
        "version": 1,
        "captured_at": "x",
        "units": setup["owner_state"],
    }
    setup["state_file"].write_text(json.dumps(existing), encoding="utf-8")
    before = read_state(setup)

    proc = run_cutover(setup, "apply")
    assert proc.returncode != 0
    assert read_state(setup) == before
    assert json.loads(setup["state_file"].read_text(encoding="utf-8")) == existing


def test_apply_refuses_when_automation_unit_missing(setup: dict[str, Any]) -> None:
    state = read_state(setup)
    del state["automation"]["robomp.service"]
    setup["systemd_state"].write_text(json.dumps(state), encoding="utf-8")
    before = read_state(setup)

    proc = run_cutover(setup, "apply")
    assert proc.returncode == 1
    assert "robomp.service" in proc.stderr
    assert read_state(setup) == before
    assert not setup["state_file"].exists()


def test_apply_refuses_when_owner_robomp_container_runs(setup: dict[str, Any]) -> None:
    before = read_state(setup)

    proc = run_cutover(
        setup, "apply", extra_env={"FAKE_DOCKER_OUTPUT": "robomp-ompbot"}
    )
    assert proc.returncode == 1
    assert "robomp" in proc.stderr
    assert read_state(setup) == before
    assert not setup["state_file"].exists()


def test_failed_postcheck_exits_1_and_rollback_still_works(
    setup: dict[str, Any],
) -> None:
    before = read_state(setup)

    proc = run_cutover(
        setup, "apply", extra_env={"FAKE_SYSTEMD_STUCK": "flood-export.timer"}
    )
    assert proc.returncode == 1
    assert "run: cutover.sh rollback --state-file" in proc.stdout + proc.stderr
    assert setup["state_file"].exists()

    failed_state = read_state(setup)
    assert failed_state["automation"]["flood-export.timer"]["is_active"] == "failed"

    rolled = run_cutover(setup, "rollback")
    assert rolled.returncode == 0, rolled.stderr
    assert read_state(setup)["owner"] == before["owner"]


def test_rollback_is_idempotent(setup: dict[str, Any]) -> None:
    assert run_cutover(setup, "apply").returncode == 0
    first = run_cutover(setup, "rollback")
    assert first.returncode == 0
    state_after_first = read_state(setup)

    second = run_cutover(setup, "rollback")
    assert second.returncode == 0, second.stderr
    assert read_state(setup) == state_after_first


def test_rollback_refuses_without_state_file(setup: dict[str, Any]) -> None:
    before = read_state(setup)

    proc = run_cutover(setup, "rollback")
    assert proc.returncode == 1
    assert read_state(setup) == before


def test_plan_mutates_nothing(setup: dict[str, Any]) -> None:
    before = read_state(setup)

    proc = run_cutover(setup, "plan")
    assert proc.returncode == 0, proc.stderr
    assert read_state(setup) == before
    assert not setup["state_file"].exists()

    mutating = {"enable", "disable", "start", "stop", "restart"}
    for line in read_calls(setup):
        _, verb, _ = parse_call(line)
        assert verb not in mutating, line


def test_plan_prints_disabled_and_inactive_states(setup: dict[str, Any]) -> None:
    # `is-enabled`/`is-active` exit non-zero for disabled/inactive while still
    # printing the state; plan must show the printed word, not `unknown`.
    proc = run_cutover(setup, "plan")
    assert proc.returncode == 0, proc.stderr

    lines = proc.stdout.splitlines()
    assert "  owner\tflood-dash-http.service\tdisabled\tactive\n" in [
        f"{line}\n" for line in lines
    ]
    assert "  owner\tflood-operator.timer\tenabled\tinactive\n" in [
        f"{line}\n" for line in lines
    ]
    assert "unknown" not in proc.stdout


def test_apply_refuses_when_owner_unit_state_is_unreadable(
    setup: dict[str, Any],
) -> None:
    before = read_state(setup)

    proc = run_cutover(setup, "apply", extra_env={"FAKE_SYSTEMD_FAIL": "owner is-active"})
    assert proc.returncode == 1
    assert "is-active" in proc.stderr
    assert read_state(setup) == before
    assert not setup["state_file"].exists()


def test_apply_fails_when_owner_unit_cannot_be_stopped(setup: dict[str, Any]) -> None:
    before = read_state(setup)

    proc = run_cutover(
        setup,
        "apply",
        extra_env={"FAKE_SYSTEMD_FAIL": "owner disable flood.service"},
    )
    assert proc.returncode == 1
    assert "run: cutover.sh rollback --state-file" in proc.stdout + proc.stderr
    assert setup["state_file"].exists()
    # The failed stop left flood.service enabled and active.
    failed_state = read_state(setup)
    assert failed_state["owner"]["flood.service"] == {
        "is_enabled": "enabled",
        "is_active": "active",
    }
    assert before["owner"]["flood.service"] == failed_state["owner"]["flood.service"]

    rolled = run_cutover(setup, "rollback")
    assert rolled.returncode == 0, rolled.stderr
    assert read_state(setup)["owner"] == before["owner"]


def test_rollback_refuses_corrupt_state_file_before_mutation(
    setup: dict[str, Any],
) -> None:
    assert run_cutover(setup, "apply").returncode == 0
    before = read_state(setup)

    setup["state_file"].write_text("{not json", encoding="utf-8")
    proc = run_cutover(setup, "rollback")
    assert proc.returncode == 1
    assert "unreadable" in proc.stderr
    # The snapshot was read before any unit was touched, so nothing changed.
    assert read_state(setup) == before
    assert setup["state_file"].read_text(encoding="utf-8") == "{not json"


def test_rollback_reports_failed_owner_restore(setup: dict[str, Any]) -> None:
    assert run_cutover(setup, "apply").returncode == 0
    setup["log"].write_text("", encoding="utf-8")

    proc = run_cutover(
        setup,
        "rollback",
        extra_env={"FAKE_SYSTEMD_FAIL": "owner enable flood.service"},
    )
    assert proc.returncode == 1
    assert "flood.service" in proc.stderr
    # A partial restore is not a rollback: F keeps no rolled_back_at stamp.
    final = json.loads(setup["state_file"].read_text(encoding="utf-8"))
    assert "rolled_back_at" not in final
