"""Stored comparison stays reproducible; rehearsal never grants real approval."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from omp_work import nsi_eval, nsi_rollout, nsi_route
from omp_work.frozen_corpus import verify
from omp_work.nsi6_corpus import load_corpus
from omp_work.v1.canonical import sha256
from test_nsi6_service_route import (
    calls, evidence, service_world,  # noqa: F401
    test_requests_restart_and_saved_assessment_replay as service_rehearsal,
)
import test_nsi6_service_route as service_tests

ROOT = Path(__file__).resolve().parents[3]
REPORT = ROOT / "docs/nsi6/shadow.json"
ROLLBACK = ROOT / "docs/nsi6/rollback.json"


def test_capture_real_service_rehearsal(evidence, monkeypatch, service_world, calls, tmp_path):
    """Capture real HTTP receipts/counts while existing assertions check writes/replay."""
    requests, counts, transitions = [], [], []
    request = service_tests._request
    row_counts = service_tests._row_counts
    promote, rollback = nsi_rollout.promote, nsi_rollout.rollback

    def observed_request(client, world, command, **kwargs):
        response = request(client, world, command, **kwargs)
        requests.append({"mode": nsi_route.route_mode(world.config),
                         "command": command["type"], "http_status": response.status_code,
                         "body": response.json()})
        return response

    def observed_counts(config, workspace):
        result = row_counts(config, workspace)
        counts.append({"mode": nsi_route.route_mode(config), "counts": result})
        return result

    def observed_promote(*args, **kwargs):
        result = promote(*args, **kwargs)
        transitions.append(result)
        return result

    def observed_rollback(*args, **kwargs):
        result = rollback(*args, **kwargs)
        transitions.append(result)
        return result

    monkeypatch.setattr(service_tests, "_request", observed_request)
    monkeypatch.setattr(service_tests, "_row_counts", observed_counts)
    monkeypatch.setattr(nsi_rollout, "promote", observed_promote)
    monkeypatch.setattr(nsi_rollout, "rollback", observed_rollback)
    service_rehearsal(evidence, monkeypatch, service_world, calls, tmp_path)
    assert [state["mode"] for state in transitions] == ["typed", "current"]
    assert len(requests) == 33
    assert len(counts) == 9
    for index, mode in enumerate(("current", "typed", "current")):
        before, rejected, published = counts[index * 3:index * 3 + 3]
        assert before["counts"] == rejected["counts"]
        assert published["mode"] == mode
    out = os.environ.get("NSI6_SERVICE_EVIDENCE_OUT")
    if out:
        Path(out).write_text(json.dumps({
            "verified": True, "synthetic_review": True, "owner_approval": False,
            "scope": "disposable PostgreSQL and temporary config; fresh TestClient per transition and publication replay",
            "transitions": transitions, "requests": requests, "row_counts": counts,
            "write_deltas": [{key: counts[i + 2]["counts"][key] - counts[i]["counts"][key]
                              for key in counts[i]["counts"]} for i in (0, 3, 6)],
        }, indent=2) + "\n")


def stored():
    return json.loads(REPORT.read_bytes())


def verify_stored(report):
    """Existing gate recomputes replay before rejecting actual critical omissions."""
    proof = report["rollback_proof"]
    data = bytes.fromhex(proof["bytes_hex"])
    assert hashlib.sha256(data).hexdigest() == proof["sha256"]
    assert json.loads(data) == proof["raw"]
    assert data == ROLLBACK.read_bytes()
    assert proof["raw"]["verified"] is True
    assert proof["raw"]["owner_approval"] is False
    for name, artifact in proof["raw"]["artifacts"].items():
        artifact_bytes = (ROOT / f"docs/nsi6/{name}.json").read_bytes()
        assert hashlib.sha256(artifact_bytes).hexdigest() == artifact["sha256"]
        assert json.loads(artifact_bytes) == artifact["raw"]
    original = proof["raw"]["original_receipts"]
    assert hashlib.sha256(bytes.fromhex(original["bytes_hex"])).hexdigest() == original["sha256"]
    for path, digest in proof["raw"]["source_sha256"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest
    with pytest.raises(ValueError, match="typed false-ready on critical-omission subset"):
        nsi_rollout.qualification(report)


def test_stored_report_recomputes_every_case_metric_and_total():
    report = stored()
    verify_stored(report)
    corpus = nsi_route.CORPUS
    assert report["corpus"] == verify(corpus / "inputs", corpus / "nsi6-manifest.json").model_dump(mode="json")
    document = load_corpus(corpus / "inputs", corpus / "nsi6-manifest.json")
    for case, row in zip(document["cases"], report["cases"], strict=True):
        for arm in ("current", "typed"):
            assert row["arms"][arm]["metrics"] == nsi_eval.score(row["arms"][arm]["observation"], case["gold"])
    for arm in ("current", "typed"):
        assert report["aggregate"][arm] == nsi_eval.aggregate(report["cases"], arm)
        assert report["aggregate"][arm]["false_ready"]["numerator"] > 0


@pytest.mark.parametrize("part", ["metric", "case", "observation", "provenance", "pin"])
def test_resealed_drift_cannot_pass_recomputation(part):
    report = deepcopy(stored())
    if part == "metric":
        report["aggregate"]["typed"]["false_ready"]["numerator"] = 0
    elif part == "case":
        report["cases"].pop()
    elif part == "observation":
        report["cases"][0]["arms"]["typed"]["observation"]["readiness"] = "FORGED"
    elif part == "provenance":
        next(r for r in report["cases"] if r["provenance"]["status"] != "skipped")["provenance"]["status"] = "crashed"
    else:
        report["pins"]["route_sources"]["session-system/extensions/workflow/host.ts"] = "0" * 64
    report.pop("report_sha256")
    report["report_sha256"] = sha256(report)
    with pytest.raises(ValueError) as failure:
        nsi_rollout.qualification(report)
    assert "critical-omission subset" not in str(failure.value)


def test_changed_report_bytes_break_seal():
    report = stored()
    report["wall_seconds"] += 1
    with pytest.raises(ValueError, match="report digest drift"):
        nsi_rollout.qualification(report)


def test_changed_corpus_bytes_fail_frozen_verification(tmp_path):
    corpus = tmp_path / "corpus"
    shutil.copytree(nsi_route.CORPUS, corpus)
    source = corpus / "inputs/cases.json"
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="file byte drift"):
        verify(corpus / "inputs", corpus / "nsi6-manifest.json")


def test_changed_rollback_bytes_fail_embedded_binding():
    report = stored()
    report["rollback_proof"]["bytes_hex"] += "0a"
    with pytest.raises(AssertionError):
        verify_stored(report)


def test_stored_rehearsal_proves_no_rejected_writes_and_restored_host_behavior():
    proof = stored()["rollback_proof"]["raw"]
    service = proof["artifacts"]["service"]["raw"]
    assert [state["mode"] for state in service["transitions"]] == ["typed", "current"]
    for index, mode in enumerate(("current", "typed", "current")):
        before, rejected, published = service["row_counts"][index * 3:index * 3 + 3]
        assert before["counts"] == rejected["counts"]
        assert published["mode"] == mode
        assert service["write_deltas"][index] == {"items": 1, "relations": 1, "candidates": 1, "publications": 1}
    replays = [r for r in service["requests"] if r["body"].get("receipt", {}).get("state") == "replayed"]
    assert len(replays) == 6
    assert all(r["http_status"] == 200 for r in replays)
    host = proof["artifacts"]["host"]["raw"]["observations"]
    assert [r["mode"] for r in host] == ["current", "typed", "current"]
    assert "intake_typed_required" in host[1]["proseResult"]
    assert all("CONFIRM REQUIRED" in r["proseResult"] for r in (host[0], host[2]))
    assert all(r["preview_write_delta"] == 0 and r["confirmed_write_delta"] == {"issues": 1, "now": 1} for r in host)


def test_exact_stored_evidence_bytes_verify_and_whitespace_drift_fails(tmp_path):
    """Frozen manifest catches even byte edits that preserve canonical report JSON."""
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for name in ("shadow", "rollback", "service", "host"):
        filename = f"{name}.json"
        (inputs / filename).write_bytes((ROOT / "docs/nsi6" / filename).read_bytes())
    manifest = ROOT / "docs/nsi6/evidence-manifest.json"
    verified = verify(inputs, manifest)
    assert {f.id for f in verified.files} == {"shadow", "rollback", "service", "host"}
    report = inputs / "shadow.json"
    report.write_bytes(report.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="file byte drift"):
        verify(inputs, manifest)
