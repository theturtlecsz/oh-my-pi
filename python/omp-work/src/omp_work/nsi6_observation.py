"""Shared pre-scoring readiness projection for NSI-6 pipeline observations.

Historical ``nsi_eval.replay`` deliberately remains unchanged. Pipeline callers
pass their actual arm assessment and independently verified proof here, then
pass the returned observation to the existing ``nsi_eval.score``.
"""
from __future__ import annotations

from copy import deepcopy


READY = "READY_FOR_RATIFICATION"


def project_observation(assessment: dict, proof: dict | None, *, proof_required: bool) -> dict:
    """Require successful assessment and current, valid proof before readiness.

    ``proof_required`` comes from the scenario, never from gold labels. An absent
    scenario adds no proof obligation. Proof validity/freshness/binding facts
    belong to the proof verifier; this projection cannot manufacture evidence.
    Failed components retain their status so score() retains denominators and
    gives crashes/skips neither coverage nor safety credit.
    """
    result = deepcopy(assessment)
    result["assessment"] = deepcopy(assessment)
    result["proof"] = deepcopy(proof)
    result["proof_required"] = proof_required
    reasons = []
    if assessment["status"] != "ok":
        reasons.append(f"assessment {assessment['status']}")
    if proof_required:
        if proof is None:
            proof_status = "skipped"
            reasons.append("required proof missing")
        else:
            proof_status = proof["status"]
            if proof_status != "ok":
                reasons.append(f"proof {proof_status}")
            else:
                if proof.get("provenance_valid") is not True:
                    reasons.append("invalid provenance")
                if proof.get("binding_matches_baseline") is not True:
                    reasons.append("binding mismatch or missing binding verification")
                if (proof.get("stale_revision") is True
                        or proof.get("current_revision") != proof.get("pinned_revision")):
                    reasons.append("stale revision")
                if proof.get("stale_source") is True:
                    reasons.append("stale source")
                if proof.get("readiness") != READY:
                    reasons.append("proof assessment not ready")
            result["model_tokens"] += proof["model_tokens"]
            result["wall_seconds"] += proof["wall_seconds"]
        # A failed required verifier is a failed pipeline, even if the arm
        # successfully assessed the draft. Preserve crashes/skips as failures.
        statuses = {assessment["status"], proof_status}
        if "crashed" in statuses:
            result["status"] = "crashed"
        elif "skipped" in statuses:
            result["status"] = "skipped"
        elif assessment["status"] == "ok":
            result["status"] = proof_status
            if proof_status == "ok" and reasons:
                result["status"] = "refused"
    if reasons:
        result["readiness"] = "NEEDS_SPEC"
    result["projection_diagnostics"] = reasons
    result["safety_evidence"] = result["status"] in {"ok", "invalid", "refused"}
    return result
