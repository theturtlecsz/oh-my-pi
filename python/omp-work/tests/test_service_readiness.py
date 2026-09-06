"""Readiness must expose the same stale-runtime fence as command admission."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omp_work.operations import database, fingerprints
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import HealthReport
from omp_work.v1 import server


@pytest.mark.parametrize("changed_component", ["source", "migrations"])
def test_runtime_edit_makes_service_unready_until_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_component: str
) -> None:
    source = tmp_path / "runtime" / "service.py"
    source.parent.mkdir()
    source.write_text("version = 1\n")
    migration = tmp_path / "migrations.sha"
    migration.write_text("a" * 64)
    monkeypatch.setattr(fingerprints, "files", lambda _package: source.parent)
    monkeypatch.setattr(database, "migration_set_sha256", migration.read_text)
    monkeypatch.setattr(server, "migration_set_sha256", migration.read_text)
    monkeypatch.setattr(
        server, "collect_health", lambda *_args, **_kwargs: HealthReport(live=True, ready=True)
    )
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    capabilities = tmp_path / "capabilities"
    with TestClient(server.create_app(config, capabilities_dir=capabilities)) as client:
        before = client.get("/v1/health/ready").json()
        assert before["ready"] is True
        if changed_component == "source":
            source.write_text("version = 2\n")
        else:
            migration.write_text("b" * 64)
        stale = client.get("/v1/health/ready").json()
        assert stale["live"] is True
        assert stale["ready"] is False
        assert any("service_stale" in alert for alert in stale["alerts"])
        # Refresh still needs the prospective fingerprint before restarting.
        assert stale["service_fingerprint"] != before["service_fingerprint"]

    with TestClient(server.create_app(config, capabilities_dir=capabilities)) as restarted:
        after = restarted.get("/v1/health/ready").json()
        assert after["ready"] is True
        assert not any("service_stale" in alert for alert in after["alerts"])
        assert after["service_fingerprint"] == stale["service_fingerprint"]


def test_current_runtime_does_not_hide_database_readiness_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = HealthReport(live=True, ready=False, alerts=["pending migrations"])
    monkeypatch.setattr(server, "collect_health", lambda *_args, **_kwargs: report)
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    with TestClient(
        server.create_app(config, capabilities_dir=tmp_path / "capabilities")
    ) as client:
        body = client.get("/v1/health/ready").json()
    assert body["ready"] is False
    assert "pending migrations" in body["alerts"]
