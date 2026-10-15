"""Candidate provenance and label isolation must survive benchmark preparation."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from omp_work.intake_behaviour_candidates import (
    CHALLENGE_SOURCE,
    completion_blockers,
    model_input,
    validate_challenge_references,
)

ROOT = Path(__file__).resolve().parents[3]
CANDIDATES = ROOT / "evals/intake_behaviour/candidates/cases.json"
NSI6 = ROOT / "evals/nsi6/v1/inputs/cases.json"


@pytest.fixture
def document() -> dict:
    return json.loads(CANDIDATES.read_text())


def test_seed_requests_and_mutations_preserve_stated_versus_silent(document) -> None:
    source = {case["id"]: case for case in json.loads(NSI6.read_text())["cases"]}
    cases = {case["id"]: case for case in document["cases"]}
    assert set(cases) == {
        "ready-complete", "ambiguous-scope",
        *[prefix + target for prefix in ("omitted-", "silent-omitted-")
          for target in ("rollback", "security", "failure")],
    }
    original = source["ready-complete"]["input"]["bounded"]["draft"]["source"]["text"]
    for case in cases.values():
        upstream = source[case["id"]]
        assert case["provenance"]["kind"] == "synthetic"
        assert case["provenance"]["original_request"] == original
        assert case["provenance"]["source_origin"] == upstream["origin"]
        assert model_input(case)["evidence"]["draft"] == upstream["input"]["bounded"]["draft"]
        assert model_input(case)["request"] == upstream["input"]["bounded"]["draft"]["source"]["text"]
        # Mutation records must actually reconstruct the model-visible draft.
        replay = deepcopy(source["ready-complete"]["input"])
        for mutation in upstream["origin"].get("mutations", []):
            node = replay
            for key in mutation["path"][:-1]:
                node = node[key]
            key = mutation["path"][-1]
            assert node[key] == mutation["before"]
            node[key] = mutation["after"]
        assert replay == upstream["input"]
    for target in ("rollback", "security", "failure"):
        stated, silent = cases[f"omitted-{target}"], cases[f"silent-omitted-{target}"]
        requirement = next(b["statement"] for b in stated["gold"]["expected_draft_inclusion"] if b["id"] == target)
        assert requirement in model_input(stated)["request"]
        assert requirement not in model_input(silent)["request"]
        for case in (stated, silent):
            draft = model_input(case)["evidence"]["draft"]
            assert all(c["key"] != target for c in draft["constraints"])
            assert all(c["id"] != f"ac-{target}" for c in draft["acceptance_criteria"])
            assert case["gold"]["expected_question_targets"] == [target]
            assert case["gold"]["readiness"] == "NEEDS_SPEC"
        assert silent["gold"]["silent_adjudication"] == "pending_chris"
    assert cases["ready-complete"]["gold"]["missing_target"] is None
    assert cases["ready-complete"]["gold"]["readiness"] == "READY_FOR_RATIFICATION"
    assert cases["ambiguous-scope"]["gold"]["expected_question_targets"] == ["scope"]
    assert cases["ambiguous-scope"]["gold"]["readiness"] == "NEEDS_SPEC"


def test_model_projection_withholds_original_silent_requirement_and_gold(document) -> None:
    case = next(c for c in document["cases"] if c["id"] == "silent-omitted-security")
    visible = model_input(case)
    assert set(visible) == {"request", "evidence"}
    assert "Unauthenticated requests" not in json.dumps(visible)
    changed = deepcopy(case)
    changed["gold"] = {"secret": "changed"}
    changed["provenance"] = {"secret": "changed"}
    assert model_input(changed) == visible
    visible["evidence"]["draft"]["goal"]["statement"] = "modified by consumer"
    assert model_input(case) != visible
    case["input"]["gold"] = case["gold"]
    with pytest.raises(ValueError, match="only request and evidence"):
        model_input(case)


def test_challenges_resolve_only_against_v2_or_remain_blocked(document) -> None:
    pairs_path = ROOT / CHALLENGE_SOURCE
    pairs = json.loads(pairs_path.read_text()) if pairs_path.exists() else None
    for case in document["cases"]:
        assert validate_challenge_references(case, pairs)
    case = deepcopy(document["cases"][0])
    case["gold"]["claim_support_challenges"]["ids"] = ["nonexistent-challenge"]
    case["gold"]["claim_support_challenges"]["status"] = "linked"
    with pytest.raises(ValueError, match="without OMP-631|unknown OMP-631"):
        validate_challenge_references(case, pairs)
    # Exercise referential validation without adding fabricated corpus IDs.
    with pytest.raises(ValueError, match="unknown OMP-631"):
        validate_challenge_references(case, {"pairs": []})
    case["gold"]["claim_support_challenges"]["source_path"] = "evals/intake_support/v1/inputs/pairs.json"
    with pytest.raises(ValueError, match="must use OMP-631 v2"):
        validate_challenge_references(case, pairs)


def test_unfilled_real_history_cannot_qualify_even_if_status_is_changed(document) -> None:
    assert len(document["real_history_slots"]) >= 5
    assert all(slot["input"] is None and slot["gold"] is None for slot in document["real_history_slots"])
    assert completion_blockers(document, None)
    document["status"] = "completed"
    blockers = completion_blockers(document, None)
    for slot in document["real_history_slots"]:
        assert f"{slot['id']}: incomplete authentic history import" in blockers
    assert "at least five authentic past requests supplied by Chris required" in blockers
    assert any("Chris must adjudicate silent omission" in reason for reason in blockers)
    assert any("challenge references unresolved" in reason for reason in blockers)


@pytest.mark.parametrize("kind", ["unfilled_real_history", "synthetic"])
def test_synthetic_or_unfilled_imports_cannot_replace_authentic_requests(document, kind) -> None:
    for slot in document["real_history_slots"]:
        slot.update(status="imported", input=document["cases"][0]["input"], gold=document["cases"][0]["gold"])
        slot["provenance"].update(
            kind=kind, source_record="fixture-reference", request_author="fixture-author",
            requested_at="2026-10-15", supplied_by="Chris", original_request="fixture request",
        )
    blockers = completion_blockers(document, None)
    assert "at least five authentic past requests supplied by Chris required" in blockers
    assert sum("incomplete authentic history import" in reason for reason in blockers) == 5


def test_resolved_challenge_link_must_exist_in_supplied_pairs(document) -> None:
    case = deepcopy(document["cases"][0])
    refs = case["gold"]["claim_support_challenges"]
    # Test-only pair, never a proposed OMP-631 ID in the candidate data.
    refs.update(ids=["test-only-pair"], status="linked")
    assert validate_challenge_references(case, {"pairs": [{"id": "test-only-pair"}]}) == []
