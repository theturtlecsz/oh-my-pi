from __future__ import annotations

import copy
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest
import yaml

from omp_harbor_eval.isolation import check_topology, is_loopback, load_compose, main

COMPOSE = Path(__file__).resolve().parent.parent / "topology" / "compose.yaml"


def shipped() -> dict[str, Any]:
    document = load_compose(COMPOSE)
    assert isinstance(document, dict)
    return document


def test_shipped_compose_is_qualified() -> None:
    assert COMPOSE.is_file()
    assert check_topology(shipped()) == []


def test_cli_exits_zero_and_prints_nothing_on_shipped_compose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([str(COMPOSE)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_cli_subprocess_exits_zero_on_shipped_compose() -> None:
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    src_dir = str(repo_root / "evals" / "harbor" / "src")
    env = {**os.environ, "PYTHONPATH": src_dir}
    result = subprocess.run(
        [sys.executable, "-m", "omp_harbor_eval.isolation", str(COMPOSE)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_cli_subprocess_exits_one_and_prints_violation_on_mutated_compose(tmp_path: Path) -> None:
    compose = shipped()
    compose["services"]["worker"]["network_mode"] = "bridge"
    target = tmp_path / "compose.yaml"
    target.write_text(yaml.safe_dump(compose), encoding="utf-8")

    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    src_dir = str(repo_root / "evals" / "harbor" / "src")
    env = {**os.environ, "PYTHONPATH": src_dir}
    result = subprocess.run(
        [sys.executable, "-m", "omp_harbor_eval.isolation", str(target)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "rule 1: worker network_mode must be 'service:workservice', got 'bridge'"
    ]


def test_topology_structural_errors() -> None:
    assert check_topology([]) == ["topology: compose document must be a mapping"]
    assert check_topology({"services": "invalid"}) == ["topology: services must be a mapping"]

    c1 = shipped()
    del c1["services"]["workservice"]
    assert check_topology(c1) == ["topology: missing service: workservice"]

    c2 = shipped()
    del c2["services"]["worker"]
    assert check_topology(c2) == ["topology: missing service: worker"]

    c3 = shipped()
    del c3["services"]["verifier"]
    assert check_topology(c3) == ["topology: missing service: verifier"]


def test_evidence_mount_ro_variations() -> None:
    # ro in comma-separated mode (Hole 3)
    c1 = shipped()
    c1["services"]["verifier"]["volumes"] = ["evidence:/evidence:ro,z"]
    assert check_topology(c1) == []

    # long syntax read_only: true
    c2 = shipped()
    c2["services"]["verifier"]["volumes"] = [
        {"type": "volume", "source": "evidence", "target": "/evidence", "read_only": True}
    ]
    assert check_topology(c2) == []

    # long syntax read_only: false is a violation
    c3 = shipped()
    c3["services"]["verifier"]["volumes"] = [
        {"type": "volume", "source": "evidence", "target": "/evidence", "read_only": False}
    ]
    assert check_topology(c3) == ["rule 5: verifier evidence mount is not read-only"]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        # Rule 1
        pytest.param(
            lambda c: c["services"]["worker"].__setitem__("network_mode", "bridge"),
            ("rule 1: worker network_mode must be 'service:workservice', got 'bridge'",),
            id="rule-1-netns",
        ),
        # Rule 2
        pytest.param(
            lambda c: c["services"]["worker"].__setitem__("privileged", True),
            ("rule 2: worker is privileged",),
            id="rule-2-privileged",
        ),
        pytest.param(
            lambda c: c["services"]["worker"]["volumes"].append(
                "/var/run/docker.sock:/var/run/docker.sock"
            ),
            ("rule 2: worker mounts a docker socket",),
            id="rule-2-docker-socket-short",
        ),
        pytest.param(
            lambda c: c["services"]["worker"]["volumes"].append(
                {"type": "bind", "source": "/var/run/docker.sock", "target": "/docker.sock"}
            ),
            ("rule 2: worker mounts a docker socket",),
            id="rule-2-docker-socket-long",
        ),
        # Rule 3
        pytest.param(
            lambda c: c["services"]["worker"]["volumes"].append("pg_data:/mnt/pg"),
            ("rule 3: worker shares volume 'pg_data' with workservice",),
            id="rule-3-service-volume",
        ),
        pytest.param(
            lambda c: c["services"]["worker"].__setitem__("volumes_from", ["workservice"]),
            ("rule 3: worker specifies volumes_from",),
            id="rule-3-volumes-from",
        ),
        pytest.param(
            lambda c: (
                c["services"]["workservice"]["volumes"].append("/run/omp/creds:/run/omp/creds"),
                c["services"]["worker"]["volumes"].append("/run/omp/creds:/run/omp/creds"),
            ),
            ("rule 3: worker shares volume '/run/omp/creds' with workservice",),
            id="rule-3-bind-path-equal",
        ),
        pytest.param(
            lambda c: (
                c["services"]["workservice"]["volumes"].append("/run/omp:/run/omp"),
                c["services"]["worker"]["volumes"].append("/run/omp/creds:/run/omp/creds"),
            ),
            ("rule 3: worker shares volume '/run/omp/creds' with workservice",),
            id="rule-3-bind-path-nested",
        ),
        # Rule 4
        pytest.param(
            lambda c: c["services"]["worker"]["environment"].__setitem__(
                "OMP_WORKSERVICE_URL", "http://workservice:8080"
            ),
            ("rule 4: worker env OMP_WORKSERVICE_URL is not loopback: 'http://workservice:8080'",),
            id="rule-4-non-loopback-url",
        ),
        pytest.param(
            lambda c: c["services"]["worker"]["environment"].__setitem__(
                "OMP_WORKSERVICE_URL", "[127.0.0.1]http://10.0.0.5"
            ),
            (
                "rule 4: worker env OMP_WORKSERVICE_URL is not loopback: '[127.0.0.1]http://10.0.0.5'",
            ),
            id="rule-4-bracket-bypass-url",
        ),
        pytest.param(
            lambda c: c["services"]["worker"]["environment"].__setitem__(
                "OMP_POSTGRES_DSN", "postgresql://service/db"
            ),
            ("rule 4: worker env OMP_POSTGRES_DSN contains POSTGRES",),
            id="rule-4-postgres-key",
        ),
        pytest.param(
            lambda c: c["services"]["worker"]["environment"].__setitem__(
                "OMP_MIGRATOR_PASS", "secret"
            ),
            ("rule 4: worker env OMP_MIGRATOR_PASS contains MIGRATOR",),
            id="rule-4-migrator-key",
        ),
        pytest.param(
            lambda c: c["services"]["worker"]["environment"].__setitem__(
                "PGPASSWORD", "secret"
            ),
            ("rule 4: worker env PGPASSWORD contains PGPASSWORD",),
            id="rule-4-pgpassword-key",
        ),
        pytest.param(
            lambda c: c["services"]["worker"]["environment"].__setitem__(
                "ADMIN_SECRET", "secret"
            ),
            ("rule 4: worker env ADMIN_SECRET contains ADMIN",),
            id="rule-4-admin-key",
        ),
        pytest.param(
            lambda c: c["services"]["worker"].__setitem__(
                "environment", ["OMP_ADMIN_KEY=1"]
            ),
            ("rule 4: worker env OMP_ADMIN_KEY contains ADMIN",),
            id="rule-4-kv-list-env",
        ),
        # Rule 5
        pytest.param(
            lambda c: c["services"]["verifier"]["volumes"].remove("evidence:/evidence:ro"),
            ("rule 5: verifier mounts no evidence volume",),
            id="rule-5-no-evidence",
        ),
        pytest.param(
            lambda c: c["services"]["verifier"]["volumes"].append("evidence:/evidence:z"),
            ("rule 5: verifier evidence mount is not read-only",),
            id="rule-5-evidence-writable-z",
        ),
        pytest.param(
            lambda c: c["services"]["verifier"]["volumes"].append("evidence:/evidence"),
            ("rule 5: verifier evidence mount is not read-only",),
            id="rule-5-evidence-writable-default",
        ),
        pytest.param(
            lambda c: c["services"]["verifier"].__setitem__("network_mode", "service:workservice"),
            ("rule 5: verifier shares worker network namespace 'service:workservice'",),
            id="rule-5-shared-workservice-netns",
        ),
        pytest.param(
            lambda c: c["services"]["verifier"].__setitem__("network_mode", "service:worker"),
            ("rule 5: verifier shares worker network namespace 'service:worker'",),
            id="rule-5-joins-worker-netns",
        ),
        pytest.param(
            lambda c: c["services"]["verifier"].__setitem__(
                "network_mode", "container:omp-harbor-zero-spend-worker-1"
            ),
            (
                "rule 5: verifier shares worker network namespace 'container:omp-harbor-zero-spend-worker-1'",
            ),
            id="rule-5-container-id-netns",
        ),
        pytest.param(
            lambda c: c["services"]["verifier"].__setitem__("volumes_from", ["worker"]),
            ("rule 5: verifier specifies volumes_from",),
            id="rule-5-volumes-from",
        ),
    ],
)
def test_each_mutation_yields_its_violation(mutate, expected: tuple[str, ...]) -> None:
    compose = copy.deepcopy(shipped())
    mutate(compose)
    violations = check_topology(compose)
    assert tuple(violations) == expected, violations


def test_work_url_keys_are_checked_but_other_urls_are_not() -> None:
    compose = shipped()
    worker_env = compose["services"]["worker"]["environment"]
    worker_env["OMP_SCRIPTED_MODEL_URL"] = "http://10.0.0.5:9090/v1"
    assert check_topology(compose) == []
    worker_env["OMP_WORK_SERVICE_URL"] = "http://workservice:8080"
    assert check_topology(compose) == [
        "rule 4: worker env OMP_WORK_SERVICE_URL is not loopback: 'http://workservice:8080'"
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://127.0.0.1:8080", True),
        ("https://127.0.0.1", True),
        ("http://localhost:8080", True),
        ("http://[::1]:8080", True),
        ("http://[::1]", True),
        ("http://workservice:8080", False),
        ("http://10.0.0.5", False),
        ("http://example.com", False),
        ("[127.0.0.1]http://10.0.0.5", False),
        ("127.0.0.1", False),
        ("0.0.0.0", False),
        ("http://0.0.0.0:8080", False),
        ("", False),
    ],
)
def test_is_loopback_contract(value: str, expected: bool) -> None:
    assert is_loopback(value) is expected
