"""Offline candidate inspection only; no model execution, approval or freezing."""

from copy import deepcopy


CHALLENGE_SOURCE = "evals/intake_support/v2/inputs/pairs.json"


def model_input(case: dict) -> dict:
    """Project only request and evidence; provenance and proposed labels stay private."""
    visible = case["input"]
    if not isinstance(visible, dict) or set(visible) != {"request", "evidence"}:
        raise ValueError("candidate input must contain only request and evidence")
    if not isinstance(visible["request"], str) or not visible["request"].strip():
        raise ValueError("candidate request must be supplied")
    if set(visible["evidence"]) != {"draft"}:
        raise ValueError("candidate evidence must contain only draft")
    return deepcopy(visible)


def validate_challenge_references(case: dict, pairs: dict | None) -> list[str]:
    """Missing OMP-631 inputs remain an explicit blocker, never invented IDs."""
    refs = case["gold"]["claim_support_challenges"]
    if refs["source_path"] != CHALLENGE_SOURCE:
        raise ValueError("challenge references must use OMP-631 v2")
    if pairs is None:
        if refs["ids"] or refs["status"] != "blocked_missing_omp631_v2":
            raise ValueError("cannot resolve challenge IDs without OMP-631 v2")
        return ["OMP-631 v2 challenge references unresolved"]
    by_id = {pair["id"]: pair for pair in pairs["pairs"]}
    if any(ref not in by_id for ref in refs["ids"]):
        raise ValueError("unknown OMP-631 v2 challenge ID")
    if not refs["ids"] or refs["status"] != "linked":
        return ["OMP-631 v2 challenge references unresolved"]
    return []


def completion_blockers(document: dict, pairs: dict | None) -> list[str]:
    """Readiness for a completed benchmark cannot be inferred from seed count."""
    blockers = []
    if document["status"] != "completed":
        blockers.append("candidate only: no approved frozen benchmark or execution evidence")
    authentic = 0
    for slot in document["real_history_slots"]:
        provenance = slot["provenance"]
        required = ("source_record", "request_author", "requested_at", "supplied_by", "original_request")
        if (slot["status"] != "imported" or provenance["kind"] != "authentic_past_request"
                or provenance["supplied_by"] != "Chris"
                or not all(provenance.get(key) for key in required)
                or not slot["input"] or not slot["gold"]):
            blockers.append(f"{slot['id']}: incomplete authentic history import")
            continue
        model_input(slot)
        authentic += 1
    if authentic < 5:
        blockers.append("at least five authentic past requests supplied by Chris required")
    for case in document["cases"]:
        model_input(case)
        blockers.extend(f"{case['id']}: {reason}" for reason in validate_challenge_references(case, pairs))
        if case["gold"]["silent_adjudication"] == "pending_chris":
            blockers.append(f"{case['id']}: Chris must adjudicate silent omission")
    return blockers
