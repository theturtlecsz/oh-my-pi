"""Fixture ledger seeds are visible to the lookup `/execute` uses."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from omp_harbor_eval.ledger_seed import main as seed_main
from omp_work import contract_sha256
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.api_models import WorkItemView
from omp_work.v1.server import create_app

HARBOR = Path(__file__).resolve().parents[1]
REPO = HARBOR.parents[1]
F1_SEED = HARBOR / "fixtures" / "f1" / "ledger-seed.json"
F2_SEED = HARBOR / "fixtures" / "f2" / "ledger-seed.json"
WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000aa")
ACTOR_ID = UUID("00000000-0000-7000-8000-0000000000bb")
F1_REVISION = "00000000-0000-7000-8000-000000000010"


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def _credentials(config: OperationsConfig) -> Path:
    for name in (
        "postgres",
        "omp_work_migrator",
        "omp_work_app",
        "omp_work_importer",
        "omp_work_readonly",
        "omp_work_backup",
        "gpg-passphrase",
    ):
        _write_secret(config.credentials_dir / name, f"{name}-secret")
    _write_secret(config.credentials_dir / "workspace-id", str(WORKSPACE_ID))
    _write_secret(config.credentials_dir / "operator-actor-id", str(ACTOR_ID))
    capabilities = config.config_dir / "capabilities"
    capabilities.mkdir(mode=0o700)
    capabilities.chmod(0o700)
    owner = capabilities / "owner.json"
    _write_secret(
        owner,
        json.dumps(
            {
                "actor_id": str(ACTOR_ID),
                "actor_kind": "owner",
                "scopes": ["work.approve", "work.close", "work.execute", "work.mutate", "work.read"],
                "token": "owner-token",
                "workspaces": [str(WORKSPACE_ID)],
            }
        ),
    )
    return owner


def _headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer owner-token",
        "X-OMP-Workspace-ID": str(WORKSPACE_ID),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }


def _issue_error(http: TestClient, key: str) -> str | None:
    """The `/execute` lookup: the work item, then the tree, else `issue <key> not found`."""

    item = http.get(f"/v1/work-items/{key}", headers=_headers())
    if item.status_code == 200:
        return None
    tree = http.get(f"/v1/workspaces/{WORKSPACE_ID}/tree", headers=_headers())
    if tree.status_code == 200:
        for entry in tree.json().get("items", []):
            alias = entry.get("alias") if isinstance(entry.get("alias"), dict) else {}
            if entry.get("work_id") == key or alias.get("key") == key:
                return None
    return f"issue {key} not found"


def test_fixture_compose_selects_its_ledger_seed() -> None:
    dockerfile = (HARBOR / "docker" / "Dockerfile.workservice").read_text(encoding="utf-8")
    assert "COPY evals/harbor/fixtures/f1/ledger-seed.json /opt/harbor/seeds/f1/ledger-seed.json" in dockerfile
    assert "COPY evals/harbor/fixtures/f2/ledger-seed.json /opt/harbor/seeds/f2/ledger-seed.json" in dockerfile
    assert "COPY evals/harbor/src/omp_harbor_eval/ledger_seed.py /opt/harbor/ledger_seed.py" in dockerfile
    entrypoint = (HARBOR / "docker" / "workservice-entrypoint.sh").read_text(encoding="utf-8")
    after_bootstrap = entrypoint.split("python -m omp_work ops bootstrap", 1)[1]
    assert after_bootstrap.index("ledger_seed.py") < after_bootstrap.index("python -m omp_work serve")
    f1 = yaml.safe_load((HARBOR / "fixtures" / "f1" / "environment" / "docker-compose.yaml").read_text(encoding="utf-8"))
    f2_path = HARBOR / "fixtures" / "f2" / "environment" / "docker-compose.yaml"
    topology = HARBOR / "topology" / "compose.yaml"
    assert f2_path.read_bytes() == topology.read_bytes()
    f2 = yaml.safe_load(f2_path.read_text(encoding="utf-8"))
    assert f1["services"]["workservice"]["environment"]["OMP_HARBOR_LEDGER_SEED"] == "/opt/harbor/seeds/f1/ledger-seed.json"
    assert f2["services"]["workservice"]["environment"]["OMP_HARBOR_LEDGER_SEED"] == "/opt/harbor/seeds/f2/ledger-seed.json"


def test_seeded_service_execute_lookup_finds_the_fixture_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sys.path.insert(0, str(REPO / "python" / "omp-work" / "tests"))
    from installed_runtime_support import ReservedPort
    from pg_native import native_postgres

    reserve = ReservedPort()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("OMP_WORK_POSTGRES_PORT", str(reserve.port))
    config = OperationsConfig.defaults()
    owner = _credentials(config)
    with native_postgres(tmp_path / "pg", reserve.port, reserve=reserve):
        bootstrap(config)
        assert seed_main([str(F1_SEED)]) == 0
        assert seed_main([str(F2_SEED)]) == 0
        assert seed_main([str(F1_SEED)]) == 0
        app = create_app(config, capabilities_dir=owner.parent)
        with TestClient(app) as http:
            assert _issue_error(http, "OMP-1") is None
            assert _issue_error(http, "OMP-246") is None
            assert _issue_error(http, "OMP-999") == "issue OMP-999 not found"
            first = WorkItemView.model_validate(http.get("/v1/work-items/OMP-1", headers=_headers()).json())
            second = WorkItemView.model_validate(http.get("/v1/work-items/OMP-246", headers=_headers()).json())
        assert str(first.revision.revision_id) == F1_REVISION
        assert first.revision.revision_number == 1
        assert first.revision.scope == "initial/scope"
        assert list(first.revision.acceptance_criteria) == ["Initial AC 1"]
        assert first.revision.title == "Initial Item"
        assert first.state == "IN_PROGRESS"
        assert first.archived is False
        assert second.alias.key == "OMP-246"
        assert second.state == "IN_PROGRESS"
        assert second.archived is False
        with psycopg.connect(**config.connection_kwargs("omp_work_app")) as conn:
            conn.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(WORKSPACE_ID), str(ACTOR_ID)),
            )
            row = conn.execute(
                "SELECT next_alias FROM omp_control.workspaces WHERE workspace_id = %s",
                (WORKSPACE_ID,),
            ).fetchone()
        assert row is not None
        assert row[0] >= 247
