"""Pinned-source drift error names changed paths and provides the reseal command."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from omp_work import nsi_eval, nsi_rollout
from omp_work.nsi_rollout import RESEAL_COMMAND

ROOT = Path(__file__).resolve().parents[3]
SHADOW_PATH = ROOT / "docs/nsi6/shadow.json"


def test_qualification_reports_drift_paths_and_reseal_command(monkeypatch):
    report = json.loads(SHADOW_PATH.read_bytes())
    stored_pins = report["pins"]
    route_key = "session-system/extensions/workflow/host.ts"
    impl_key = "omp_work.nsi_eval"
    added_key = "extra_pin"

    assert route_key in stored_pins["route_sources"]
    assert impl_key in stored_pins["implementation_sources"]
    assert added_key not in stored_pins

    def drifted():
        pins = deepcopy(stored_pins)
        pins["route_sources"][route_key] = "0" * 64
        pins["implementation_sources"][impl_key] = "0" * 64
        pins[added_key] = "0" * 64
        return pins

    monkeypatch.setattr(nsi_eval, "report_pins", drifted)

    with pytest.raises(ValueError) as excinfo:
        nsi_rollout.qualification(report)

    message = str(excinfo.value)
    assert message.startswith("implementation/schema drift")
    assert RESEAL_COMMAND in message
    assert "for an intended source change docs/nsi6 is resealed with exactly:" in message

    expected_paths = sorted([
        f"route_sources.{route_key}",
        f"implementation_sources.{impl_key}",
        added_key,
    ])

    for path in expected_paths:
        assert path in message

    prefix, _, _ = message.partition("; for an intended source change docs/nsi6 is resealed with exactly: ")
    assert prefix.startswith("implementation/schema drift: ")
    paths_part = prefix.removeprefix("implementation/schema drift: ")
    reported_paths = [p.strip() for p in paths_part.split(",")]
    assert reported_paths == expected_paths


def test_qualification_with_real_pins_reaches_critical_omission_rejection():
    report = json.loads(SHADOW_PATH.read_bytes())
    with pytest.raises(ValueError, match="^typed false-ready on critical-omission subset$"):
        nsi_rollout.qualification(report)


def test_differing_pins_subkeys_and_top_level(monkeypatch):
    report = json.loads(SHADOW_PATH.read_bytes())
    stored_pins = report["pins"]

    def drifted():
        pins = deepcopy(stored_pins)
        pins["helper_sources"] = "f" * 64
        pins["referenced_schemas"] = "f" * 64
        pins["route_sources"]["extra_route.ts"] = "1" * 64
        return pins

    monkeypatch.setattr(nsi_eval, "report_pins", drifted)

    with pytest.raises(ValueError) as excinfo:
        nsi_rollout.qualification(report)

    message = str(excinfo.value)
    expected_paths = sorted([
        "helper_sources",
        "referenced_schemas",
        "route_sources.extra_route.ts",
    ])
    prefix, _, _ = message.partition("; for an intended source change docs/nsi6 is resealed with exactly: ")
    paths_part = prefix.removeprefix("implementation/schema drift: ")
    assert [p.strip() for p in paths_part.split(",")] == expected_paths
