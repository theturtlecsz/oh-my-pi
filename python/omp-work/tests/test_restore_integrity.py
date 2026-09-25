"""Restore a fixture dump into a second temporary PostgreSQL, without object storage."""
import json
import os
import shutil

import pytest

from omp_work.operations import backup
from test_workflow_service import _create, _grant, service
from uuid import uuid4

pytestmark = pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
                               reason="set OMP_WORK_POSTGRES_INTEGRATION=1")


def test_restore_verifies_real_fixture_dump_in_disposable_cluster(service, tmp_path, monkeypatch):
    workspace = uuid4()
    _grant(service, workspace)
    _create(service, workspace, "Restore fixture", description="persisted fixture work")
    config = service.config
    dump = tmp_path / "fixture.dump"
    backup._run(["pg_dump", "--host", config.host, "--port", str(config.port),
                 "--username", "omp_work_backup", "--format=custom", "--no-owner",
                 "--no-privileges", "--file", str(dump), config.database], env=backup._postgres_env(config))
    prefix = "fixture/base/"
    evidence = []
    monkeypatch.setattr(backup, "validate_bundle", lambda **kwargs: None)
    monkeypatch.setattr(backup, "_latest_backup", lambda *args: (prefix, {}))
    monkeypatch.setattr(backup, "_record_evidence", lambda *args, **kwargs: evidence.append(kwargs) or "fixture-receipt")

    def download(_, key, destination, **kwargs):
        if key.endswith("manifest.json.gpg"):
            destination.write_text(json.dumps({
                "backup_id": "fixture", "contract_sha256": backup.contract_sha256(),
                "migration_set_sha256": backup.migration_set_sha256(),
                "objects": [{"key": prefix + "ledger.dump.gpg", "sha256": "0" * 64}],
            }))
        else:
            shutil.copyfile(dump, destination)
    monkeypatch.setattr(backup, "_download_decrypt", download)
    assert backup.restore_drill(config, reason="fixture", backup_id="fixture") == "fixture-receipt"
    assert evidence[-1]["outcome"] == "passed:logical_restore:fixture"
