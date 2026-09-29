"""OMP-405: the owner capability gains work.stop; automation does not.

``ops capabilities init`` writes owner.json with six scopes, including
work.stop. ``ops capabilities automation`` writes automation.json with the
owner's original five scopes and no work.stop. No database.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from omp_work.__main__ import main

_AUTOMATION_SCOPES = (
    "work.read",
    "work.mutate",
    "work.approve",
    "work.close",
    "work.execute",
)


def _capability_path(xdg: Path, name: str) -> Path:
    return xdg / "omp" / "work-ledger" / "capabilities" / f"{name}.json"


def test_owner_capability_includes_work_stop_and_automation_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    workspace_id = uuid4()
    owner_id = uuid4()

    assert (
        main(
            [
                "ops",
                "capabilities",
                "init",
                "--workspace-id",
                str(workspace_id),
                "--owner-id",
                str(owner_id),
            ]
        )
        == 0
    )
    owner = json.loads(_capability_path(tmp_path, "owner").read_text())
    assert "work.stop" in owner["scopes"]
    assert owner["scopes"] == sorted([*_AUTOMATION_SCOPES, "work.stop"])

    assert main(["ops", "capabilities", "automation", "--workspace-id", str(workspace_id)]) == 0
    automation = json.loads(_capability_path(tmp_path, "automation").read_text())
    assert automation["scopes"] == sorted(_AUTOMATION_SCOPES)
    assert "work.stop" not in automation["scopes"]
