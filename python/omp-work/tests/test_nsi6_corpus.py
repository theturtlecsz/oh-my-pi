"""NSI-6 frozen replay rejects missing coverage, label drift and altered mutations."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

import pytest
from pydantic import ValidationError

from omp_work.nsi6_corpus import (
    REQUIRED_CLASSES, evaluator_input, freeze_corpus, load_corpus, validate_cases,
)
from omp_work.v1.models import BoundedIntakeDraft

CORPUS = Path(__file__).resolve().parents[3] / "evals/nsi6/v1"


@pytest.fixture
def copied(tmp_path):
    root = tmp_path / "inputs"
    shutil.copytree(CORPUS / "inputs", root)
    manifest = tmp_path / "nsi6-manifest.json"
    shutil.copyfile(CORPUS / "nsi6-manifest.json", manifest)
    return root, manifest


def rewrite(root, document):
    (root / "cases.json").write_text(json.dumps(document, ensure_ascii=False) + "\n")


def test_tracked_corpus_replays_without_gold_or_input_repairs(copied):
    root, manifest = copied
    data = load_corpus(root, manifest)
    assert {label for row in data["cases"] for label in row["classes"]} >= REQUIRED_CLASSES
    for row in data["cases"]:
        raw = evaluator_input(row)
        assert set(raw) == {"bounded", "nsi2_proof_scenario"}
        assert raw == row["input"]
        raw["bounded"]["draft"]["goal"]["statement"] = "caller edit"
        assert raw != row["input"]  # A replay arm cannot poison later arms.
        BoundedIntakeDraft.model_validate(row["gold"]["corrected_draft"])
    # Re-freeze identical bytes reuses seal, with no evaluator execution.
    assert freeze_corpus(root, manifest).manifest_sha256 == json.loads(manifest.read_bytes())["manifest_sha256"]


@pytest.mark.parametrize("label", sorted(REQUIRED_CLASSES))
def test_missing_seeded_class_cannot_be_frozen(copied, label):
    root, _ = copied
    data = validate_cases(root)
    for row in data["cases"]:
        row["classes"] = [item for item in row["classes"] if item != label]
    rewrite(root, data)
    with pytest.raises(ValueError, match="missing classes"):
        freeze_corpus(root, root.parent / "new-manifest.json")


def test_duplicate_case_id_cannot_hide_inside_one_json_file(copied):
    root, _ = copied
    data = validate_cases(root)
    data["cases"].append(deepcopy(data["cases"][0]))
    rewrite(root, data)
    with pytest.raises(ValueError, match="duplicate case ids"):
        freeze_corpus(root, root.parent / "new-manifest.json")


@pytest.mark.parametrize("target", ["bytes", "source", "span", "labels", "attribution", "mutation"])
def test_frozen_digest_binds_every_replay_surface(copied, target):
    root, manifest = copied
    data = validate_cases(root)
    row = next(r for r in data["cases"] if r["id"] == "omitted-rollback")
    if target == "bytes":
        (root / "cases.json").write_bytes((root / "cases.json").read_bytes() + b" ")
    else:
        if target == "source": row["input"]["bounded"]["draft"]["source"]["text"] += "changed"
        elif target == "span": row["input"]["bounded"]["draft"]["source"]["spans"][0]["start"] += 1
        elif target == "labels": row["gold"]["readiness"] = "READY_FOR_RATIFICATION"
        elif target == "attribution": row["adjudication"]["attribution"] = "forged owner"
        else: row["origin"]["mutations"][0]["after"] = []
        rewrite(root, data)
    with pytest.raises(ValueError, match="file byte drift"):
        load_corpus(root, manifest)


@pytest.mark.parametrize("surface", ["input", "preimage", "result"])
def test_declared_mutations_must_reproduce_exact_raw_input(copied, surface):
    root, _ = copied
    data = validate_cases(root)
    row = next(r for r in data["cases"] if r["id"] == "tampered-source-hash")
    if surface == "input": row["input"]["bounded"]["draft"]["source"]["sha256"] = "1" * 64
    else: row["origin"]["mutations"][0]["before" if surface == "preimage" else "after"] = {}
    rewrite(root, data)
    with pytest.raises(ValueError, match="mutation .* drift"):
        freeze_corpus(root, root.parent / "new-manifest.json")


def test_omissions_remove_real_claims_and_oracles_without_unknown_blockers(copied):
    root, manifest = copied
    data = load_corpus(root, manifest)
    for behavior in ("rollback", "security", "failure"):
        for prefix in ("omitted-", "silent-omitted-"):
            row = next(r for r in data["cases"] if r["id"] == prefix + behavior)
            draft = row["input"]["bounded"]["draft"]
            BoundedIntakeDraft.model_validate(draft)
            assert draft["unknowns"] == []
            assert "c-" + behavior not in {c["id"] for c in draft["constraints"]}
            assert "ac-" + behavior not in {c["id"] for c in draft["acceptance_criteria"]}
            fixed = row["gold"]["corrected_draft"]
            assert "c-" + behavior in {c["id"] for c in fixed["constraints"]}
            assert "ac-" + behavior in row["gold"]["criterion_ids"]
            assert row["gold"]["material_question_targets"] == [behavior]
            assert row["gold"]["readiness"] == "NEEDS_SPEC"
            if prefix == "silent-omitted-":
                assert "span-" + behavior not in {s["id"] for s in draft["source"]["spans"]}
                assert fixed["source"]["text"] != draft["source"]["text"]


def test_intentionally_wrong_hashes_survive_replay(copied):
    root, manifest = copied
    data = load_corpus(root, manifest)
    for id in ("tampered-source-hash", "tampered-span-hash", "history-09-tampered-span"):
        row = next(r for r in data["cases"] if r["id"] == id)
        raw = evaluator_input(row)["bounded"]["draft"]
        with pytest.raises(ValidationError):
            BoundedIntakeDraft.model_validate(raw)
        assert raw != row["gold"]["corrected_draft"]
    row = next(r for r in data["cases"] if r["id"] == "tampered-provenance")
    scenario = evaluator_input(row)["nsi2_proof_scenario"]
    assert scenario["recompute_hashes"] is False
    wrong = scenario["mutations"][0]["after"]
    assert wrong != hashlib.sha256(scenario["source_bytes_utf8"].encode()).hexdigest()
    assert scenario["mutations"][0]["table"] == "omp_work.intake_artifacts"
    assert "assessment" not in scenario


def test_proof_revision_mutations_remain_persisted_and_pinned(copied):
    root, manifest = copied
    rows = load_corpus(root, manifest)["cases"]
    for id, table, field, old, new in (
        ("stale-source", "omp_work.intake_artifacts", "source_revision", "source-v1", "source-v2"),
        ("stale-revision", "omp_work.intake_heads", "current_revision", 0, 1),
    ):
        row = next(r for r in rows if r["id"] == id)
        proof = evaluator_input(row)["nsi2_proof_scenario"]
        mutation = proof["mutations"][0]
        assert (mutation["table"], mutation["field_path"], mutation["before"], mutation["after"]) == (table, [field], old, new)
        assert proof["revision"] == 0
        assert proof["recompute_hashes"] is False
        assert row["gold"]["readiness"] == "NEEDS_SPEC"


def test_paraphrase_rewording_preserves_positive_and_negative_gold(copied):
    root, manifest = copied
    rows = load_corpus(root, manifest)["cases"]
    for group, readiness in (("complete-export", "READY_FOR_RATIFICATION"), ("missing-security", "NEEDS_SPEC")):
        pair = [r for r in rows if r["paraphrase_group"] == group]
        assert len(pair) == 2
        assert pair[0]["input"]["bounded"]["draft"]["source"]["text"] != pair[1]["input"]["bounded"]["draft"]["source"]["text"]
        for key in ("readiness", "critical_required_behaviors", "criterion_ids", "material_question_targets"):
            assert pair[0]["gold"][key] == pair[1]["gold"][key]
        assert pair[0]["gold"]["readiness"] == readiness


def test_paraphrase_label_change_refused_even_after_reseal(copied):
    root, _ = copied
    data = validate_cases(root)
    next(r for r in data["cases"] if r["id"] == "ready-paraphrase-b")["gold"]["material_question_targets"] = ["security"]
    rewrite(root, data)
    with pytest.raises(ValueError, match="paraphrase gold semantics drift"):
        freeze_corpus(root, root.parent / "new-manifest.json")


def test_historical_snapshot_is_exact_tracked_fixture_with_original_attribution(copied):
    root, manifest = copied
    data = load_corpus(root, manifest)
    tracked = CORPUS.parents[2] / data["history"]["path"]
    assert (root / "bounded_intake_golden.json").read_bytes() == tracked.read_bytes()
    originals = {row["id"]: row for row in json.loads(tracked.read_bytes())}
    for row in data["cases"]:
        if row["origin"]["kind"] == "historical":
            original = originals[row["origin"]["case_id"]]
            assert evaluator_input(row)["bounded"] == original["input"]
            assert row["origin"]["adjudication"] == original["adjudication"]


def test_injected_authority_cannot_change_required_security_semantics(copied):
    root, manifest = copied
    rows = load_corpus(root, manifest)["cases"]
    injected = next(r for r in rows if r["id"] == "injected-omitted-security")
    omitted = next(r for r in rows if r["id"] == "omitted-security")
    assert injected["gold"] == omitted["gold"]
    assert injected["input"]["bounded"]["draft"]["source"]["text"] != omitted["input"]["bounded"]["draft"]["source"]["text"]
    BoundedIntakeDraft.model_validate(injected["input"]["bounded"]["draft"])
    control = next(r for r in rows if r["id"] == "ready-current-proof")
    assert control["gold"]["readiness"] == "READY_FOR_RATIFICATION"
    assert control["input"]["nsi2_proof_scenario"]["mutations"] == []
