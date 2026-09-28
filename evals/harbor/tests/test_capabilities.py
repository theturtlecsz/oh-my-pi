from __future__ import annotations

import copy
from pathlib import Path

import pytest
from omp_harbor_eval.isolation import check_capabilities, load_compose

HARBOR_DIR = Path(__file__).resolve().parent.parent
COMPOSE_PATHS = [
    HARBOR_DIR / "fixtures" / "f1" / "environment" / "docker-compose.yaml",
    HARBOR_DIR / "fixtures" / "f2" / "environment" / "docker-compose.yaml",
    HARBOR_DIR / "topology" / "compose.yaml",
]


@pytest.mark.parametrize("compose_path", COMPOSE_PATHS, ids=lambda p: p.parent.name or p.name)
def test_shipped_compose_files_pass_capability_checks(compose_path: Path) -> None:
    assert compose_path.is_file(), f"missing compose file: {compose_path}"
    document = load_compose(compose_path)
    violations = check_capabilities(document)
    assert violations == [], f"violations in {compose_path}: {violations}"


@pytest.mark.parametrize("compose_path", COMPOSE_PATHS, ids=lambda p: p.parent.name or p.name)
def test_required_services_declared_with_proper_capabilities(compose_path: Path) -> None:
    document = load_compose(compose_path)
    services = document.get("services", {})
    expected_services = {"workservice", "worker", "verifier", "main"}
    assert expected_services.issubset(services.keys()), f"missing services in {compose_path}: {expected_services - set(services.keys())}"

    for name in expected_services:
        svc = services[name]
        assert svc.get("cap_drop") == ["ALL"], f"{name} in {compose_path} must drop ALL caps"
        assert not svc.get("privileged"), f"{name} in {compose_path} must not be privileged"
        assert isinstance(svc.get("cap_add"), list), f"{name} in {compose_path} must define cap_add list"

    # Worker, verifier, and main run as non-root / unprivileged and need no extra capabilities.
    for name in ("worker", "verifier", "main"):
        assert services[name].get("cap_add") == [], f"{name} in {compose_path} needs no added capabilities"

    # Workservice retains only what PostgreSQL init and file operations require.
    expected_workservice_caps = ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"]
    assert sorted(services["workservice"].get("cap_add", [])) == sorted(expected_workservice_caps)


@pytest.mark.parametrize("compose_path", COMPOSE_PATHS, ids=lambda p: p.parent.name or p.name)
def test_lacking_cap_drop_fails_for_any_service(compose_path: Path) -> None:
    base = load_compose(compose_path)
    services = base.get("services", {})
    for svc_name in services:
        mutated = copy.deepcopy(base)
        del mutated["services"][svc_name]["cap_drop"]
        violations = check_capabilities(mutated)
        assert any(f"service '{svc_name}' lacks cap_drop: [ALL]" in v for v in violations)


@pytest.mark.parametrize("compose_path", COMPOSE_PATHS, ids=lambda p: p.parent.name or p.name)
def test_cap_drop_without_all_fails(compose_path: Path) -> None:
    base = load_compose(compose_path)
    services = base.get("services", {})
    for svc_name in services:
        mutated = copy.deepcopy(base)
        mutated["services"][svc_name]["cap_drop"] = ["NET_RAW", "SYS_CHROOT"]
        violations = check_capabilities(mutated)
        assert any(f"service '{svc_name}' cap_drop does not contain 'ALL'" in v for v in violations)


@pytest.mark.parametrize("compose_path", COMPOSE_PATHS, ids=lambda p: p.parent.name or p.name)
def test_setting_privileged_fails_for_any_service(compose_path: Path) -> None:
    base = load_compose(compose_path)
    services = base.get("services", {})
    for svc_name in services:
        mutated = copy.deepcopy(base)
        mutated["services"][svc_name]["privileged"] = True
        violations = check_capabilities(mutated)
        assert any(f"service '{svc_name}' is privileged" in v for v in violations)


@pytest.mark.parametrize("compose_path", COMPOSE_PATHS, ids=lambda p: p.parent.name or p.name)
def test_missing_cap_add_list_fails_for_any_service(compose_path: Path) -> None:
    base = load_compose(compose_path)
    services = base.get("services", {})
    for svc_name in services:
        mutated = copy.deepcopy(base)
        del mutated["services"][svc_name]["cap_add"]
        violations = check_capabilities(mutated)
        assert any(f"service '{svc_name}' lacks explicit cap_add list" in v for v in violations)


def test_structural_errors() -> None:
    assert check_capabilities([]) == ["capabilities: compose document must be a mapping"]
    assert check_capabilities({"services": "not-a-dict"}) == ["capabilities: services must be a mapping"]
