"""Pinned-source reseal updates shadow pins only and still rejects forged results."""
import json
from pathlib import Path
import shutil

import pytest

from omp_work import nsi_eval, nsi_rollout
from omp_work.frozen_corpus import verify
from omp_work.nsi6_reseal import reseal
from omp_work.v1.canonical import sha256
import test_nsi6_evidence

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "docs/nsi6"
EVIDENCE = ("shadow", "rollback", "service", "host")
ROUTE = "session-system/extensions/workflow/host.ts"
ORIGIN = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def real_evidence_unchanged():
    before = {path.name: path.read_bytes() for path in SOURCE.iterdir() if path.is_file()}
    yield
    after = {path.name: path.read_bytes() for path in SOURCE.iterdir() if path.is_file()}
    assert after == before


def copy_evidence(tmp_path: Path) -> Path:
    directory = tmp_path / "nsi6"
    directory.mkdir()
    for name in (*EVIDENCE, "evidence-manifest"):
        target = directory / f"{name}.json"
        shutil.copy(SOURCE / target.name, target)
    (directory / "evidence-manifest.json").chmod(0o400)
    return directory


def snapshot(directory: Path) -> dict[str, bytes]:
    named = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    hidden = {path.name: path.read_bytes() for path in directory.glob(".*") if path.is_file()}
    return {**named, **hidden}


def write_report(path: Path, report: dict) -> None:
    body = {key: value for key, value in report.items() if key != "report_sha256"}
    report["report_sha256"] = sha256(body)
    with path.open("w") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")


def test_reseal_after_pinned_source_change_keeps_stored_recomputation(monkeypatch, tmp_path):
    directory = copy_evidence(tmp_path)
    original_bytes = (directory / "shadow.json").read_bytes()
    original = json.loads(original_bytes)
    unchanged = {name: (directory / f"{name}.json").read_bytes() for name in EVIDENCE if name != "shadow"}
    manifest_before = (directory / "evidence-manifest.json").read_bytes()
    assert ROUTE in original["pins"]["route_sources"]
    current = nsi_eval.report_pins

    def drifted():
        pins = current()
        pins["route_sources"][ROUTE] = "0" * 64
        return pins

    monkeypatch.setattr(nsi_eval, "report_pins", drifted)
    with pytest.raises(ValueError, match="implementation/schema drift"):
        nsi_rollout.qualification(json.loads(original_bytes))

    reseal(directory=directory, origin=ORIGIN)

    updated_bytes = (directory / "shadow.json").read_bytes()
    updated = json.loads(updated_bytes)
    for key, value in original.items():
        if key not in {"pins", "report_sha256"}:
            assert updated[key] == value
    assert list(updated["pins"]) == list(original["pins"])
    assert list(updated["pins"]["route_sources"]) == list(original["pins"]["route_sources"])
    assert updated["pins"]["route_sources"][ROUTE] == "0" * 64
    for name, digest in original["pins"]["route_sources"].items():
        if name != ROUTE:
            assert updated["pins"]["route_sources"][name] == digest
    for key in ("helper_sources", "referenced_schemas", "implementation_sources"):
        assert updated["pins"][key] == original["pins"][key]
    assert updated["report_sha256"] != original["report_sha256"]
    body = {key: value for key, value in updated.items() if key != "report_sha256"}
    assert updated["report_sha256"] == sha256(body)
    before_lines = original_bytes.splitlines()
    after_lines = updated_bytes.splitlines()
    assert len(before_lines) == len(after_lines)
    changed = [after for before, after in zip(before_lines, after_lines, strict=True) if before != after]
    assert len(changed) == 2
    assert all(b"report_sha256" in line or ROUTE.encode() in line for line in changed)
    for name, data in unchanged.items():
        assert (directory / f"{name}.json").read_bytes() == data
    assert (directory / "evidence-manifest.json").read_bytes() != manifest_before
    assert list(directory.glob(".*")) == []

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for name in EVIDENCE:
        shutil.copy(directory / f"{name}.json", inputs / f"{name}.json")
    verified = verify(inputs, directory / "evidence-manifest.json")
    assert {item.id for item in verified.files} == set(EVIDENCE)
    assert verified.origin_commit == ORIGIN

    monkeypatch.setattr(test_nsi6_evidence, "REPORT", directory / "shadow.json")
    test_nsi6_evidence.test_stored_report_recomputes_every_case_metric_and_total()


@pytest.mark.parametrize("edit", ["aggregate-metric", "case-readiness"])
def test_resealed_metric_or_readiness_edit_is_refused(tmp_path, edit):
    directory = copy_evidence(tmp_path)
    report = json.loads((directory / "shadow.json").read_bytes())
    if edit == "aggregate-metric":
        metric = report["aggregate"]["typed"]["false_ready"]
        assert metric["numerator"] != 0
        metric["numerator"] = 0
    else:
        observation = report["cases"][0]["arms"]["typed"]["observation"]
        assert observation["readiness"] != "NEEDS_SPEC"
        observation["readiness"] = "NEEDS_SPEC"
    write_report(directory / "shadow.json", report)
    before = snapshot(directory)
    with pytest.raises(ValueError, match="modified observations/metrics/summary") as failure:
        reseal(directory=directory, origin=ORIGIN)
    assert str(failure.value) != "typed false-ready on critical-omission subset"
    assert snapshot(directory) == before
