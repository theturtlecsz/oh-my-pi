"""Existing acceptance/research evidence must not masquerade as O9 freeze."""
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest

from omp_work.qualification_records import (
    ScopeIdentity, decode_evidence, scope_boundary, unavailable_report,
)
from omp_work.v1.canonical import sha256


@pytest.fixture
def accepted_evidence():
    subject = {
        "work_id": str(uuid4()), "revision_id": str(uuid4()),
        "candidate_id": str(uuid4()), "candidate_sha256": "a" * 64,
        "candidate_commit": "b" * 40,
    }
    receipts = []
    refs = []
    for kind in ("verification", "audit", "push"):
        receipt = {
            **subject, "receipt_id": str(uuid4()), "kind": kind,
            "payload": {"result": "PASS"}, "payload_sha256": sha256({"result": "PASS"}),
            "artifact_sha256": "c" * 64, "issuer": "test-service",
            "issued_at": "2026-10-13T00:00:00Z", "verdict": "PASS",
        }
        receipts.append(receipt)
        refs.append({key: receipt[key] for key in (
            "receipt_id", "kind", "payload_sha256", "artifact_sha256",
        )})
    completion = {
        "subject": subject, "runner": {
            "launch_id": str(uuid4()), "tool_call_id": "call", "task_sha256": "d" * 64,
        }, "check": {"manifest_id": str(uuid4()), "task_sha256": "d" * 64},
        "artifacts": refs, "result": "PASS", "delivery": {
            "repository": "oh-my-pi", "remote_url": "https://example.invalid/repo",
            "remote_ref": "main", "candidate_commit": "b" * 40, "remote_commit": "b" * 40,
        },
    }
    return receipts, completion


@pytest.mark.parametrize("gate", [
    "20-consecutive-accepted", "72h/30-distinct-task", "within-run/100-execution-attempt",
])
def test_sound_linked_acceptance_cannot_supply_missing_o9_freeze(accepted_evidence, gate):
    receipts, completion = accepted_evidence
    snapshot = decode_evidence(receipts=receipts, completions=[("completed", completion)])
    assert snapshot.acceptance_record_ids == ("completed",)
    assert snapshot.exclusions == ()
    assert snapshot.protocol is None
    report = unavailable_report(snapshot, gate)
    assert report.status == "not started"
    assert report.count is None
    assert report.contributing_record_ids == ()
    assert report.strata_status == "not preregistered"
    assert report.allocations == ()
    assert {e.record_id for e in report.exclusions} == {
        "completed", *(r["receipt_id"] for r in receipts),
    }


@pytest.mark.parametrize("damage,reason", [
    ("malformed", "invalid acceptance evidence"),
    ("dangling", "dangling receipt link"),
    ("hash", "payload hash mismatch"),
    ("reference_hash", "receipt binding mismatch"),
    ("subject", "receipt binding mismatch"),
    ("artifact", "receipt binding mismatch"),
    ("delivery", "delivery binding mismatch"),
    ("duplicate", "duplicate receipt id"),
])
def test_invalid_acceptance_proof_is_excluded(accepted_evidence, damage, reason):
    receipts, completion = accepted_evidence
    if damage == "malformed":
        completion = {"result": "PASS"}
    elif damage == "dangling":
        receipts.pop()
    elif damage == "hash":
        receipts[0]["payload"]["result"] = "forged"
    elif damage == "reference_hash":
        completion["artifacts"][0]["payload_sha256"] = "f" * 64
    elif damage == "subject":
        receipts[0]["work_id"] = str(uuid4())
    elif damage == "artifact":
        completion["artifacts"][0]["artifact_sha256"] = "f" * 64
    elif damage == "delivery":
        completion["delivery"]["remote_commit"] = "e" * 40
    elif damage == "duplicate":
        receipts.append(receipts[0].copy())
    snapshot = decode_evidence(receipts=receipts, completions=[("completed", completion)])
    assert snapshot.acceptance_record_ids == ()
    assert any(reason in e.reason for e in snapshot.exclusions)
    assert unavailable_report(snapshot, "20-consecutive-accepted").count is None


def observation(payload):
    return {
        "observation_id": str(uuid4()), "workspace_id": str(uuid4()),
        "campaign_id": str(uuid4()), "trial_id": str(uuid4()),
        "issuer_kind": "legacy_autoresearch", "source_ref": "job:managed-trial:test",
        "execution_status": "completed", "payload": payload,
        "payload_sha256": sha256(payload), "observed_at": "2026-10-13T00:00:00Z",
        "recorded_at": "2026-10-13T00:00:00Z",
    }


def test_campaign_hash_current_release_and_payload_allocations_are_not_freeze():
    row = observation({
        "protocol_sha256": "a" * 64, "release": "current", "verdict": "qualified",
        "freeze": True, "allocations": {"cpu": 100}, "metrics": {"loss": 0.1},
    })
    snapshot = decode_evidence(observations=[row])
    row["payload"]["metrics"]["loss"] = 999
    assert '"loss":0.1' in snapshot.observations[0].payload_json
    with pytest.raises(FrozenInstanceError):
        snapshot.protocol = "forged"
    report = unavailable_report(snapshot, "within-run/100-execution-attempt")
    assert report.count is None
    assert report.allocations == ()
    assert report.strata_status == "not preregistered"


@pytest.mark.parametrize("damage", ["hash", "malformed", "duplicate"])
def test_observation_boundary_rejects_bad_or_ambiguous_records(damage):
    row = observation({"receipt_sha256": "a" * 64})
    rows = [row]
    if damage == "hash":
        row["payload_sha256"] = "b" * 64
    elif damage == "malformed":
        row["observed_at"] = "yesterday"
    else:
        rows.append(row.copy())
    snapshot = decode_evidence(observations=rows)
    assert snapshot.observations == ()
    assert snapshot.exclusions


@pytest.mark.parametrize("field", ["release", "configuration", "recipe", "evaluator", "workload", "criteria"])
def test_any_frozen_identity_change_ends_shared_scope(field):
    scope = ScopeIdentity("r", "c", "recipe", "e", "w", "criteria")
    assert scope_boundary(scope, replace(scope, **{field: "changed"})) == (field,)
    assert scope_boundary(scope, scope) == ()


def test_empty_records_do_not_start_any_gate():
    report = unavailable_report(decode_evidence(), "72h/30-distinct-task")
    assert (report.status, report.count) == ("not started", None)
    assert "release baseline" in report.missing_proof
    assert report.allocations == ()
