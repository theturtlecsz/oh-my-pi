"""OMP-606-s03: frozen mechanism tasks run through the managed evaluator in the real jail."""

from __future__ import annotations

import io
import json
import tarfile
import threading
import zlib
from dataclasses import replace
from pathlib import Path

import pytest
from omp_work.jobs.managed_trial import Evaluation, evaluate_candidate
from omp_work.jobs.runner import LocalJailBackend
from omp_work.operations.artifacts import bytes_sha256
from omp_work.research import mechanism_tasks as mt
from test_research_runner import _unshare_probe_fails

_needs_jail = pytest.mark.skipif(_unshare_probe_fails(), reason="unshare probe failed")

REPO_ROOT = Path(__file__).resolve().parents[3]


def _run(
    task: mt.MechanismTask,
    node_id: str,
    *,
    candidate_tar: bytes | None = None,
    confirmation: bytes | None = None,
) -> Evaluation:
    inputs = mt.managed_inputs(task, node_id)
    tar = inputs.candidate_tar if candidate_tar is None else candidate_tar
    return evaluate_candidate(
        LocalJailBackend(),
        inputs.protocol,
        candidate_tar=tar,
        candidate_digest=bytes_sha256(tar).hexdigest(),
        evaluator_script=inputs.evaluator_script,
        confirmation=inputs.confirmation if confirmation is None else confirmation,
        cancel=threading.Event(),
    )


def _custom_tar(task: mt.MechanismTask, node_id: str, candidate_py: str) -> bytes:
    """The task's candidate files with ``candidate.py`` replaced."""
    original = mt.managed_inputs(task, node_id).candidate_tar
    buffer = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(original)) as src, tarfile.open(
        fileobj=buffer, mode="w"
    ) as dst:
        for member in src.getmembers():
            data = src.extractfile(member).read()  # type: ignore[union-attr]
            if member.name == "candidate.py":
                data = candidate_py.encode()
                member.size = len(data)
            dst.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def _expected_score(task: mt.MechanismTask, node_id: str) -> float:
    spec = json.loads(task.sealed)
    params = task.manifest["candidate_parameters"][node_id]
    x = list(spec["start"])
    for _ in range(params["iters"]):
        x = [
            xi - params["lr"] * 2.0 * w * (xi - s)
            for xi, w, s in zip(x, spec["weights"], spec["shift"])
        ]
    return 1.0 / (1.0 + sum((v - s) ** 2 for v, s in zip(x, spec["shift"])))


def _expected_ratio(task: mt.MechanismTask, node_id: str) -> float:
    node = next(n for n in task.nodes if n.id == node_id)
    strategy = zlib.Z_FILTERED if node.strategy == "filtered" else zlib.Z_DEFAULT_STRATEGY
    comp = zlib.compressobj(node.level, zlib.DEFLATED, 15, node.mem_level, strategy)
    return len(task.payload) / len(comp.compress(task.payload) + comp.flush())


@pytest.fixture(scope="module")
def engineering() -> tuple[mt.MechanismTask, ...]:
    return mt.engineering_cohort(REPO_ROOT)


@_needs_jail
def test_simulation_score_is_evaluator_recomputed_not_reported() -> None:
    task = mt.development_cohort()[0]
    scores = {}
    for node_id in ("a", "b"):
        result = _run(task, node_id)
        assert result.verdict == "qualified", result.failure_reason
        assert result.negative_control == "rejected"
        # The candidate prints and writes a perfect 1.0; the evaluator ignores it.
        assert result.metrics["score"] == pytest.approx(_expected_score(task, node_id), rel=1e-9)
        assert result.metrics["score"] < 1.0
        scores[node_id] = result.metrics["score"]
    assert scores["b"] > scores["a"]


@_needs_jail
def test_engineering_ratio_is_evaluator_recomputed(engineering) -> None:
    task = engineering[0]
    ratios = {}
    for node_id in ("a", "c"):
        result = _run(task, node_id)
        assert result.verdict == "qualified", result.failure_reason
        assert result.metrics["ratio"] == pytest.approx(_expected_ratio(task, node_id), rel=1e-12)
        assert result.metrics["ratio"] < 1000.0  # candidate-reported ratio ignored
        ratios[node_id] = result.metrics["ratio"]
    assert ratios["c"] > ratios["a"]


@_needs_jail
def test_corrupt_and_empty_compression_fail(engineering) -> None:
    task = engineering[0]
    corrupt = _custom_tar(
        task,
        "b",
        "import hashlib, json, pathlib\n"
        "p = json.loads(pathlib.Path('params.json').read_text())\n"
        "pathlib.Path('out').mkdir(exist_ok=True)\n"
        "pathlib.Path('out/compressed.bin').write_bytes(b'\\x78\\x9cnot-a-zlib-stream')\n"
        "pathlib.Path('out/envelope.json').write_text(json.dumps({'binding': p['binding'],"
        " 'source_sha256': hashlib.sha256(pathlib.Path('source.bin').read_bytes()).hexdigest()}))\n",
    )
    empty = _custom_tar(
        task,
        "b",
        "import hashlib, json, pathlib\n"
        "p = json.loads(pathlib.Path('params.json').read_text())\n"
        "pathlib.Path('out').mkdir(exist_ok=True)\n"
        "pathlib.Path('out/compressed.bin').write_bytes(b'')\n"
        "pathlib.Path('out/envelope.json').write_text(json.dumps({'binding': p['binding'],"
        " 'source_sha256': hashlib.sha256(pathlib.Path('source.bin').read_bytes()).hexdigest()}))\n",
    )
    for tar in (corrupt, empty):
        result = _run(task, "b", candidate_tar=tar)
        assert result.verdict == "evaluator_failed"
        assert not result.metrics


@_needs_jail
def test_truncated_stream_that_is_not_the_original_fails(engineering) -> None:
    task = engineering[0]
    wrong = _custom_tar(
        task,
        "b",
        "import hashlib, json, pathlib, zlib\n"
        "p = json.loads(pathlib.Path('params.json').read_text())\n"
        "raw = pathlib.Path('source.bin').read_bytes()\n"
        "pathlib.Path('out').mkdir(exist_ok=True)\n"
        "pathlib.Path('out/compressed.bin').write_bytes(zlib.compress(raw[:-1], 9))\n"
        "pathlib.Path('out/envelope.json').write_text(json.dumps({'binding': p['binding'],"
        " 'source_sha256': hashlib.sha256(raw).hexdigest()}))\n",
    )
    assert _run(task, "b", candidate_tar=wrong).verdict == "evaluator_failed"


@_needs_jail
def test_missing_output_fails_candidate() -> None:
    task = mt.development_cohort()[1]
    silent = _custom_tar(task, "a", "print('METRIC score=1.0')\n")
    result = _run(task, "a", candidate_tar=silent)
    assert result.verdict == "candidate_failed"
    assert not result.metrics


@_needs_jail
def test_binding_mismatch_is_refused() -> None:
    first, second = mt.development_cohort()[:2]
    # Candidate bytes of one task checked against another task's sealed bytes.
    result = _run(first, "a1x", confirmation=second.sealed)
    assert result.verdict == "evaluator_failed"
    assert not result.metrics


@_needs_jail
def test_non_finite_coordinates_fail() -> None:
    task = mt.development_cohort()[0]
    nan = _custom_tar(
        task,
        "a",
        "import json, pathlib\n"
        "p = json.loads(pathlib.Path('params.json').read_text())\n"
        "pathlib.Path('out').mkdir(exist_ok=True)\n"
        "pathlib.Path('out/envelope.json').write_text("
        "'{\"binding\": ' + json.dumps(p['binding']) + ', \"x\": [NaN, 0, 0, 0]}')\n",
    )
    assert _run(task, "a", candidate_tar=nan).verdict == "evaluator_failed"


def test_manifests_freeze_search_shape_and_follow_task_bytes(engineering) -> None:
    dev = mt.development_cohort()
    manifest = dev[0].manifest
    assert manifest["search"] == {"budget": 5, "width": 1, "seed": 0}
    assert all(node["cost"] == 1 for node in manifest["tree"])
    assert {n["id"]: n["parent"] for n in manifest["tree"]} == {
        "a": None, "b": None, "c": None, "a1": "a", "a2": "a", "a1x": "a1", "a1y": "a1",
    }
    for key in ("evaluator_sha256", "protocol_sha256", "candidate_parameters", "candidate_tar_sha256"):
        assert manifest[key]

    changed = mt.engineering_task(0, engineering[0].payload + b"\n", dict(engineering[0].manifest["provenance"]))
    assert changed.manifest_sha256 != engineering[0].manifest_sha256
    assert changed.task_id != engineering[0].task_id
    assert changed.manifest["candidate_tar_sha256"]["a"] != engineering[0].manifest["candidate_tar_sha256"]["a"]
    assert mt.development_cohort()[0].manifest_sha256 == dev[0].manifest_sha256


def test_cohorts_are_content_disjoint_and_record_sources(engineering) -> None:
    dev, fresh = mt.development_cohort(), mt.confirmation_cohort()
    assert [t.seed for t in dev] + [t.seed for t in fresh] == [0, 1, 2, 3, 4]
    assert [t.seed for t in engineering] == [0, 1, 2, 3, 4]
    mt.assert_disjoint(dev, fresh, engineering)
    for task in engineering:
        files = task.manifest["provenance"]["files"]
        assert files and all(path.endswith(".py") and len(h) == 64 for path, h in files.items())
        for path, digest in files.items():
            assert bytes_sha256((REPO_ROOT / path).read_bytes()).hexdigest() == digest
        assert task.manifest["provenance"]["tree_sha256"]
        assert task.real_evidence
    with pytest.raises(ValueError, match="overlap"):
        mt.assert_disjoint(dev, dev[:1])
    with pytest.raises(ValueError):
        mt.simulation_task(3, "development")
    for cohort in (dev, fresh, engineering):
        assert mt.cohort_manifest(cohort)["manifest_sha256"]


def test_fixture_tasks_never_count_as_real_evidence() -> None:
    fixture = mt.engineering_task(0, b"synthetic bytes " * 64, {})
    named_only = mt.engineering_task(0, b"synthetic bytes " * 64, {"files": {"x.py": "nothash"}})
    assert not fixture.real_evidence and not named_only.real_evidence
    assert fixture.manifest["evidence_class"] == "fixture"
    with pytest.raises(ValueError, match="fixture"):
        mt.cohort_manifest([fixture])


def test_manifest_snapshot_cannot_drift_with_caller_edits() -> None:
    provenance = {"files": {"prod.py": "a" * 64}, "sampling": {"paths": ["prod.py"]}}
    task = mt.engineering_task(0, b"opaque production bytes", provenance)
    original = task.manifest
    provenance["files"]["prod.py"] = "b" * 64
    provenance["sampling"]["paths"].append("other.py")
    view = task.manifest
    view["search"]["budget"] = 500
    view["candidate_parameters"]["a"]["level"] = 0
    view["tree"][0]["cost"] = 99
    view["provenance"]["files"].clear()
    assert task.manifest == original
    assert bytes_sha256(task.manifest_bytes).hexdigest() == task.manifest_sha256


@pytest.mark.parametrize("domain", ["simulation", "engineering"])
def test_execution_uses_frozen_bytes_after_generator_changes(monkeypatch, domain) -> None:
    task = (
        mt.simulation_task(0, "development")
        if domain == "simulation"
        else mt.engineering_task(0, b"opaque production bytes", {})
    )
    before = mt.managed_inputs(task, "a")
    monkeypatch.setitem(mt._SIM_PARAMS["a"], "iters", 1)
    monkeypatch.setattr(mt, "_SIM_CANDIDATE", "raise SystemExit(1)")
    monkeypatch.setattr(mt, "_ZLIB_CANDIDATE", "raise SystemExit(1)")
    monkeypatch.setattr(mt, "_SIM_EVALUATOR", "raise SystemExit(1)")
    monkeypatch.setattr(mt, "_ZLIB_EVALUATOR", "raise SystemExit(1)")
    monkeypatch.setattr(mt, "TIMEOUT_SECONDS", 1)
    # Even mutation of a previously returned protocol must not affect later runs.
    before.protocol.tolerance[before.protocol.primary] = 0.5
    after = mt.managed_inputs(task, "a")
    manifest = task.manifest
    assert after.candidate_tar == before.candidate_tar
    assert after.evaluator_script == before.evaluator_script
    assert after.confirmation == before.confirmation
    assert after.candidate_digest == manifest["candidate_tar_sha256"]["a"]
    assert bytes_sha256(after.evaluator_script).hexdigest() == manifest["evaluator_sha256"]
    assert bytes_sha256(after.protocol.protocol_bytes()).hexdigest() == manifest["protocol_sha256"]
    assert after.protocol.tolerance == {}


def test_managed_inputs_refuse_bytes_that_do_not_match_manifest() -> None:
    task = mt.simulation_task(0, "development")
    for changes in (
        {"candidate_tars": (("a", b"different archive"),)},
        {"evaluator_script": b"different evaluator"},
        {"protocol_bytes": b"{}"},
        {"sealed": b"different confirmation"},
        {"manifest_bytes": b"{}"},
    ):
        with pytest.raises(ValueError, match="frozen task manifest"):
            mt.managed_inputs(replace(task, **changes), "a")
