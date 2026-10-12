"""Assessment commands persist pinned proof without semantic writes or replay effects."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from omp_work import contract_sha256, validate_bundle
from omp_work.contracts.nsi.v1.compat import bounded_to_nsi, nsi_bounded_evaluation
from omp_work.contracts.nsi.v1.models import TypedBooleanValue
from omp_work.intake_repository import AssessmentRecord, DecisionRecord, append_assessment, read_revision, revision_hashes
from omp_work.v1 import intake_provenance
from omp_work.v1.api_models import IntakeAssessmentView, IntakeDecisionRecord
from omp_work.v1.models import CommandEnvelope, RecordIntakeDecisionCommand
from omp_work.v1.server import create_app
from omp_work.v1.store import PostgresWorkStore
from omp_work.v1.store_shared import WorkStoreError
from test_nsi2_claims import evidence_command, intake_capability
from test_nsi2_provenance_closure import (
    RUN, TEXT, SOURCE_URI, SOURCE_ACTOR, _draft, _entities, _retarget, _replace, advance, append_body, make_body,
    insert_relay, relay_json, mutate_cycle, mutate_superseded, mutate_missing_trace,
)
from test_nsi2_decisions import decision
from test_nsi2_repository import connection, locked, write_repository  # noqa: F401
from test_nsi_schemas import ASSESSMENT
from test_workflow_service import OWNER, _command, _grant, _owner_headers, service  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@pytest.fixture
def intake(service, write_repository):
    workspace_id, _ = write_repository
    _grant(service, workspace_id)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, make_body(workspace_id, relay=False), uuid4(), -1)
    return write_repository


def command(revision=1, **extra):
    return dict(type="assess_intake", payload=dict(run_id=RUN, expected_revision=revision, **extra))


def envelope(workspace_id, revision=1):
    return CommandEnvelope.model_validate(dict(
        api_version="work.omp.dev/v1", workspace_id=workspace_id, operation_id=uuid4(),
        request_id=uuid4(), correlation_id=uuid4(), command=command(revision),
    ))


def get(service, workspace_id, suffix=""):
    response = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/{RUN}{suffix}",
        headers=_owner_headers(workspace_id),
    )
    assert response.status_code == 200
    return response.json()


def counts(service, workspace_id):
    with connection(service, workspace_id) as conn:
        return tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in (
            "omp_work.intake_revisions", "omp_work.intake_assessments",
            "omp_work.intake_derivations", "omp_work.intake_derivation_claims",
            "omp_audit.domain_events", "omp_control.idempotent_commands",
        ))


@pytest.mark.parametrize("kind", ["owner", "rule_engine"])
def test_http_assessment_serializes_full_proof_and_replay_does_not_compute(service, intake, monkeypatch, kind):
    workspace_id, mission_id = intake
    actor_id, token = intake_capability(service, workspace_id, kind=kind, scopes=("work.approve", "work.read"))
    operation_id = uuid4()
    status, result = _command(service, workspace_id, command(), token=token, operation_id=operation_id)
    assert status == 200
    assert result["result"] == dict(type="assess_intake", run_id=RUN, revision=1, assessed_revision=1)
    view = get(service, workspace_id)
    assessment = view["assessments"][0]
    assert view["revision"] == view["provenance"]["revision"] == 1
    assert assessment["assessment"]["revision"] == assessment["binding"]["revision"] == 0
    assert assessment["provenance"]["revision"] == 0
    assert assessment["assessment"]["status"] == "READY_FOR_RATIFICATION"
    assert assessment["assessment"]["assessed_by"] == dict(id="omp-work:nsi2-provenance", kind="rule_engine")
    assert {r["id"] for r in assessment["receipts"]} == set(assessment["assessment"]["derivation_receipt_ids"])
    for receipt in assessment["receipts"]:
        assert {name: receipt[name] for name in assessment["binding"]} == assessment["binding"]
    assert view["provenance"] == {**assessment["provenance"], "revision": 1}
    assert view["provenance"]["claims"][0]["ancestry_ids"]
    assert view["provenance"]["sources"][0]["operation"]["event_id"]
    assert {t["criterion_id"]: t for t in view["provenance"]["traces"]} == {
        t["criterion_id"]: t for t in view["traces"]
    }
    store = PostgresWorkStore(service.config)
    assert store.read(workspace_id, OWNER, "intake", RUN) == view
    before = counts(service, workspace_id)
    assert before[:3] == (1, 1, len(assessment["receipts"]))
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (0,)
        assert conn.execute(
            "SELECT aggregate_type,aggregate_id,actor_id,actor_kind,payload FROM omp_audit.domain_events "
            "WHERE operation_id=%s", (operation_id,),
        ).fetchone() == ("mission", mission_id, actor_id, kind, result["result"])

    def unexpected(*args):
        pytest.fail("replay must bypass assessment computation")

    monkeypatch.setattr(intake_provenance, "evaluate_provenance", unexpected)
    status, replay = _command(service, workspace_id, command(), token=token, operation_id=operation_id)
    assert status == 200
    assert replay["receipt"]["state"] == "replayed"
    assert replay["result"] == result["result"]
    assert counts(service, workspace_id) == before
    assert get(service, workspace_id) == view


def test_store_assessment_uses_caller_transaction_and_pinned_read(service, intake):
    workspace_id, _ = intake
    store = PostgresWorkStore(service.config)
    receipt, result = store.execute(envelope(workspace_id), actor_id=OWNER, actor_kind="owner", required_scope="work.approve")
    assert receipt.state == "applied"
    assert result["revision"] == result["assessed_revision"] == 1
    historical = get(service, workspace_id, "@1")
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        prior = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0)
        assert historical["assessments"][0] == prior.assessments[0].model_dump(mode="json")
        append_body(cur, intake, advance(prior.body, 1), uuid4(), 0)
    current = get(service, workspace_id)
    assert current["revision"] == 2
    assert current["assessments"] == []
    assert get(service, workspace_id, "@1") == historical


def test_stale_request_refused_before_computation(service, intake, monkeypatch):
    workspace_id, _ = intake
    before = counts(service, workspace_id)

    def unexpected(*args):
        pytest.fail("stale request must not read or compute proof")

    monkeypatch.setattr(intake_provenance, "evaluate_provenance", unexpected)
    status, result = _command(service, workspace_id, command(2))
    assert status == 409
    assert result["error"]["code"] == "stale_intake"
    assert counts(service, workspace_id) == before


@pytest.mark.parametrize("kind", ["agent", "task-agent", "model", "tool", "client", "automation", "service"])
def test_approve_scope_does_not_authorize_other_kinds(service, intake, kind):
    workspace_id, _ = intake
    _, token = intake_capability(service, workspace_id, kind=kind, scopes=("work.approve", "work.read"))
    before = counts(service, workspace_id)
    status, result = _command(service, workspace_id, command(), token=token)
    assert status == 403
    assert result["error"]["code"] == "forbidden"
    assert counts(service, workspace_id) == before
    with pytest.raises(WorkStoreError, match="forbidden"):
        PostgresWorkStore(service.config).execute(
            envelope(workspace_id), actor_id=OWNER, actor_kind=kind, required_scope="work.approve",
        )
    assert counts(service, workspace_id) == before


@pytest.mark.parametrize("extra", [dict(verdict="READY_FOR_RATIFICATION"), dict(issuer="owner"), dict(assessed_by=dict(id="owner", kind="owner"))])
def test_forged_payload_refused_without_rows(service, intake, extra):
    workspace_id, _ = intake
    before = counts(service, workspace_id)
    status, result = _command(service, workspace_id, command(**extra))
    assert status == 400
    assert result["error"]["code"] == "invalid_request"
    assert counts(service, workspace_id) == before


def test_refusal_after_persistence_rolls_back_proof_journal_and_event(service, intake, monkeypatch):
    workspace_id, _ = intake
    before = counts(service, workspace_id)

    def refuse(cur, head, view, operation_id):
        append_assessment(cur, head, view, operation_id)
        raise WorkStoreError("invalid_request")

    monkeypatch.setattr(intake_provenance, "append_assessment", refuse)
    status, result = _command(service, workspace_id, command())
    assert status == 400
    assert result["error"]["code"] == "invalid_request"
    assert counts(service, workspace_id) == before


def test_reads_settle_explicit_persisted_findings_without_question_id_inference(service, intake):
    workspace_id, _ = intake
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        body = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body
        evaluation = intake_provenance.evaluate_provenance(cur, body)
        gap = dict(id="opaque-gap", dimension_id="missing_verification_oracle", premise_id="missing", reason="Need proof", materiality="material", waiver_policy="owner_with_reason")
        question = dict(id="opaque-question", question="What proves it?", category="completion_signals", gap_ids=["opaque-gap"], conflict_ids=[])
        assessment = evaluation.assessment.model_dump()
        assessment.update(status="READY_FOR_RATIFICATION", gaps=[gap])
        view = IntakeAssessmentView.model_validate(dict(
            id="stored-proof", assessment=assessment, binding=evaluation.binding,
            receipts=evaluation.receipts, provenance=evaluation.closure.closure,
            unresolved_issues=[dict(issue=question, gaps=["opaque-gap"])],
        ))
        append_assessment(cur, locked(cur, intake, expected=0, operation_id=uuid4()), view, uuid4())
    result = get(service, workspace_id)
    proof = result["assessments"][0]
    assert proof["assessment"]["status"] == "NEEDS_SPEC"
    assert proof["assessment"]["gaps"] == [gap]
    assert proof["unresolved_issues"] == result["issues"] == [dict(issue=question, gaps=["opaque-gap"])]
    assert proof["binding"] == evaluation.binding.model_dump()
    assert proof["receipts"] == [receipt.model_dump(mode="json") for receipt in evaluation.receipts]


def test_http_computed_findings_remain_visible_without_read_recomputation(service, write_repository, monkeypatch):
    workspace_id, _ = write_repository
    _grant(service, workspace_id)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        body = make_body(workspace_id, relay=False)
        body.gaps.append(body.gaps[0].model_copy(update={"id": "open-gap"}))
        body.questions.append(body.questions[0].model_copy(update={
            "id": "open-question", "gap_ids": ["open-gap"],
        }))
        append_body(cur, write_repository, body, uuid4(), -1)
    status, result = _command(service, workspace_id, command())
    assert status == 200
    assert result["result"]["revision"] == 1

    def unexpected(*args):
        pytest.fail("GET must read persisted findings without computing")

    monkeypatch.setattr(intake_provenance, "evaluate_provenance", unexpected)
    view = get(service, workspace_id)
    proof = view["assessments"][0]
    assert proof["assessment"]["status"] == "NEEDS_SPEC"
    gaps = {gap["id"] for gap in proof["assessment"]["gaps"]}
    assert gaps
    assert any(set(record["gaps"]) & gaps for record in proof["unresolved_issues"])
    assert proof["unresolved_issues"] == view["issues"]


def test_ready_read_does_not_revive_obsolete_bounded_findings(service, write_repository):
    workspace_id, _ = write_repository
    _grant(service, workspace_id)
    body = make_body(workspace_id, relay=False)
    body.gaps.append(body.gaps[0].model_copy(update={"id": "gap:oracle:obsolete"}))
    body.questions.append(body.questions[0].model_copy(update={
        "id": "question:oracle:obsolete", "gap_ids": ["gap:oracle:obsolete"],
    }))
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
    assert _command(service, workspace_id, command())[0] == 200
    result = get(service, workspace_id)
    assert result["assessments"][0]["assessment"]["status"] == "READY_FOR_RATIFICATION"
    assert result["assessments"][0]["assessment"]["gaps"] == []
    assert result["issues"] == []


def test_current_read_keeps_carried_legacy_proof_but_not_prior_concrete_assessments(service, write_repository):
    workspace_id, _ = write_repository
    _grant(service, workspace_id)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        body = make_body(workspace_id, relay=False)
        body.assessments = [AssessmentRecord.model_validate(dict(
            id="legacy-proof", assessment={**ASSESSMENT, "revision": 0},
        ))]
        append_body(cur, write_repository, body, uuid4(), -1)
    assert _command(service, workspace_id, command())[0] == 200
    historical = get(service, workspace_id, "@1")
    legacy = next(view for view in historical["assessments"] if view["id"] == "legacy-proof")
    assert len(historical["assessments"]) == 2
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        prior = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0)
        later = advance(prior.body, 1)
        later.assessments[0].id = "carried-proof"
        append_body(cur, write_repository, later, uuid4(), 0)
    current = get(service, workspace_id)
    assert current["revision"] == 2
    assert current["assessments"] == [{**legacy, "id": "carried-proof"}]
    assert legacy["assessment"]["revision"] == legacy["binding"]["revision"] == 0
    assert legacy["provenance"]["revision"] == 1
    assert get(service, workspace_id, "@1") == historical
    assert PostgresWorkStore(service.config).read(workspace_id, OWNER, "intake", RUN) == current


@contextmanager
def restarted(service):
    """Fresh store and WorkService, retaining only DB and capability files."""
    root = Path(__file__).parents[1] / "src/omp_work/contracts/v1"
    approved = json.loads((root / "approval.json").read_text())["contract_sha256"]
    assert contract_sha256() == approved
    validate_bundle()  # Approved generated schema must close over the new wire fields.
    store = PostgresWorkStore(service.config)
    with TestClient(create_app(service.config, capabilities_dir=service.capabilities, store=store)) as client:
        # Use the approved digest on the actual HTTP boundary, not a generated replacement.
        client.headers["X-OMP-Contract-SHA256"] = approved
        yield SimpleNamespace(client=client, config=service.config, capabilities=service.capabilities)


def apply(service, workspace_id, request, **kwargs):
    status, result = _command(service, workspace_id, request, **kwargs)
    assert status == 200, result
    return result


def record_claim(claim, revision, *, relation="supports"):
    return dict(type="record_intake_claim", payload=dict(
        run_id=RUN, expected_revision=revision, claim=claim,
        evidence_links=[dict(span=span, relation=relation) for span in claim["evidence"]],
    ))


@pytest.fixture
def relayed_proof(service, write_repository):
    workspace_id, mission_id = write_repository
    _grant(service, workspace_id)
    body = make_body(workspace_id)
    _replace(body, "leaf", premise_ids=["late"])
    # Proposed assertion has evidence; admission must preserve it and the full ancestry.
    ancestor = next(c for c in body.snapshot.claims if c.id == "ancestor")
    _replace(body, "late", evidence=ancestor.evidence)
    operation_id = uuid4()
    relay_id = body.history.decisions[0].verified_relay_operation_id
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        relay_event = insert_relay(cur, workspace_id, mission_id, relay_id,
                                   relay_json(body.history.decisions[0].instruction))
        body, source_event = append_body(cur, write_repository, body, operation_id, -1)
    claim = next(c for c in body.snapshot.claims if c.id == "late").model_dump(mode="json")
    claim.update(state="admitted", truth="true", admitted_by=dict(id=str(OWNER), kind="owner"))
    admission_id = uuid4()
    apply(service, workspace_id, record_claim(claim, 1), operation_id=admission_id)
    with connection(service, workspace_id) as conn:
        admission_event = conn.execute(
            "SELECT event_id FROM omp_audit.domain_events WHERE workspace_id=%s AND operation_id=%s",
            (workspace_id, admission_id),
        ).fetchone()[0]
    return workspace_id, body, dict(
        source=dict(operation_id=str(operation_id), event_id=str(source_event)),
        relay=dict(operation_id=str(relay_id), event_id=str(relay_event)),
        admission=dict(operation_id=str(admission_id), event_id=str(admission_event)),
    )


def test_restart_http_retains_exact_multihop_proof_and_audit_refs(service, relayed_proof, monkeypatch):
    workspace_id, body, refs = relayed_proof
    operation_id = uuid4()
    applied = apply(service, workspace_id, command(2), operation_id=operation_id)
    pinned = get(service, workspace_id, "@2")
    proof = pinned["assessments"][0]
    closure = proof["provenance"]
    assert proof["assessment"]["status"] == "READY_FOR_RATIFICATION"
    claims = {item["claim"]["id"]: item for item in closure["claims"]}
    assert set(claims["leaf"]["ancestry_ids"]) == {"late", "mid", "ancestor", "span-export"}
    assert len(claims["leaf"]["ancestry_ids"]) == 4
    assert set(claims["sibling"]["ancestry_ids"]) == {"mid", "ancestor", "span-export"}
    for claim_id in ("leaf", "sibling"):
        link = claims[claim_id]["evidence_links"]["ancestor"][0]
        assert link == dict(relation="supports", span=dict(
            artifact_id="instruction:decision-1", start_byte=0, end_byte=len(TEXT.encode()), quote=TEXT,
        ))
    span = claims["span-export"]["evidence_links"]["span-export"][0]["span"]
    assert span["quote"] == body.artifact_bytes["bounded-source"][span["start_byte"]:span["end_byte"]].decode()
    sources = {source["artifact"]["id"]: source for source in closure["sources"]}
    for artifact in body.snapshot.artifacts:
        source = sources[artifact.id]
        assert source["artifact"] == artifact.model_dump(mode="json")
        assert source["source"]["text"] == body.artifact_bytes[artifact.id].decode()
        assert source["source"]["sha256"] == artifact.content_hash
        if artifact.id == "bounded-source":
            assert source["source"]["spans"] == [span.model_dump(mode="json") for span in _draft(drop_unknowns=True).source.spans]
        assert source["operation"] == refs["source"]
    assert sources["bounded-source"]["artifact"]["uri"] == SOURCE_URI
    assert sources["bounded-source"]["artifact"]["actor"] == SOURCE_ACTOR.model_dump()
    admissions = {item["claim_id"]: item for item in closure["admissions"]}
    assert admissions["late"]["admitted_by"] == dict(id=str(OWNER), kind="owner")
    assert admissions["late"]["operation"] == refs["admission"]
    owner = closure["decisions"][0]
    assert owner["record"]["instruction"] == body.history.decisions[0].instruction.model_dump(mode="json")
    assert owner["record"]["decision"] == body.history.decisions[0].decision.model_dump(mode="json")
    assert owner["operation"] == refs["source"]
    assert closure["relays"][0]["relay"] == relay_json(body.history.decisions[0].instruction)
    assert closure["relays"][0]["operation"] == refs["relay"]
    assert closure["supersessions"][0] == dict(
        old_claim_id="retired", replacement_claim_id="ancestor", decision_id="decision-1", operation=refs["source"],
    )
    assert pinned["criterion_links"] == [link.model_dump(mode="json") for link in body.history.criteria]
    assert {t["criterion_id"]: t for t in closure["traces"]} == {t["criterion_id"]: t for t in pinned["traces"]}
    assert {t["criterion_id"]: set(t["requirement_ids"]) for t in closure["traces"]} == {
        "ac-export": {"req-ac-export"}, "ac-table": {"req-ac-table"},
    }
    assert {r["id"] for r in proof["receipts"]} == set(proof["assessment"]["derivation_receipt_ids"])
    for receipt in proof["receipts"]:
        assert {key: receipt[key] for key in proof["binding"]} == proof["binding"]
    claim_receipts = {tuple(r["conclusion_ids"]): r["premise_ids"] for r in proof["receipts"]
                      if r["rule_id"] == "nsi2.provenance.claim"}
    assert claim_receipts[("leaf",)] == ["late"]
    assert claim_receipts[("late",)] == ["mid"]
    assert claim_receipts[("mid",)] == ["ancestor"]
    assert claim_receipts[("ancestor",)] == ["span-export"]
    before = counts(service, workspace_id)

    def unexpected(*args):
        pytest.fail("restart GET, replay and stale refusal must not recompute pinned proof")

    monkeypatch.setattr(intake_provenance, "evaluate_provenance", unexpected)
    with restarted(service) as fresh:
        assert get(fresh, workspace_id) == get(fresh, workspace_id, "@2") == pinned
        replay = apply(fresh, workspace_id, command(2), operation_id=operation_id)
        assert replay["receipt"]["state"] == "replayed"
        assert replay["result"] == applied["result"]
        status, refused = _command(fresh, workspace_id, command(1))
        assert status == 409 and refused["error"]["code"] == "stale_intake"
        assert counts(fresh, workspace_id) == before
        assert get(fresh, workspace_id) == pinned


def test_real_source_and_semantic_writes_invalidate_current_ready_but_pin_history(service, intake):
    workspace_id, _ = intake
    apply(service, workspace_id, command())
    ready = get(service, workspace_id, "@1")
    apply(service, workspace_id, evidence_command(OWNER, "owner", expected_revision=1))
    with restarted(service) as fresh:
        current = get(fresh, workspace_id)
        assert current["revision"] == 2 and current["assessments"] == []
        assert get(fresh, workspace_id, "@1") == ready
        apply(fresh, workspace_id, command(2))
        source_proof = get(fresh, workspace_id, "@2")
        assert source_proof["assessments"][0]["binding"]["source_hash"] != ready["assessments"][0]["binding"]["source_hash"]
        claim = next(c for c in current["snapshot"]["claims"] if c["id"] == "ancestor")
        claim = deepcopy(claim)
        claim.update(id="semantic-added", asserted_by=dict(id=str(OWNER), kind="owner"),
                     admitted_by=dict(id=str(OWNER), kind="owner"),
                     value=dict(type="string", value="owner changed semantic content"))
        apply(fresh, workspace_id, record_claim(claim, 2))
    with restarted(service) as fresh:
        assert get(fresh, workspace_id)["assessments"] == []
        assert get(fresh, workspace_id, "@1") == ready
        assert get(fresh, workspace_id, "@2") == source_proof
        apply(fresh, workspace_id, command(3))
        assert get(fresh, workspace_id)["assessments"][0]["binding"]["claim_set_hash"] != source_proof["assessments"][0]["binding"]["claim_set_hash"]


@pytest.mark.parametrize("blocker", ["source-span-unknown", "material-unknown", "open-issue"])
def test_restart_http_unknown_premises_and_open_material_findings_block_ready(service, write_repository, blocker):
    workspace_id, _ = write_repository
    _grant(service, workspace_id)
    body = make_body(workspace_id, relay=False, drop_unknowns=blocker != "material-unknown")
    body.snapshot.state = "needs_spec"
    if blocker == "source-span-unknown":
        _replace(body, "span-export", truth="unknown")
    elif blocker == "material-unknown":
        draft = _draft(drop_unknowns=False)
        draft = draft.model_copy(update={"unknowns": (
            draft.unknowns[0].model_copy(update={"material": True, "kind": "authority_or_dependency"}),
            *draft.unknowns[1:],
        )})
        projected = bounded_to_nsi(draft)
        extras = [c for c in body.snapshot.claims if c.id in {"ancestor", "mid", "sibling", "leaf", "late", "retired"}]
        body.snapshot.claims = _retarget(list(projected.claims)) + extras
        body.snapshot.entities = _entities(body.snapshot.claims)
    elif blocker == "open-issue":
        body.questions.append(body.questions[0].model_copy(update={"id": "still-open", "gap_ids": []}))
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
    apply(service, workspace_id, command())
    wire = get(service, workspace_id)
    proof = wire["assessments"][0]
    assert proof["assessment"]["status"] == "NEEDS_SPEC"
    if blocker == "source-span-unknown":
        claim = next(c["claim"] for c in proof["provenance"]["claims"] if c["claim"]["id"] == "span-export")
        assert claim["truth"] == "unknown"
    elif blocker == "material-unknown":
        assert proof["assessment"]["gaps"]
        assert proof["unresolved_issues"] == wire["issues"]
    else:
        assert any(i["issue"]["id"] == "still-open" for i in proof["unresolved_issues"])
    with restarted(service) as fresh:
        assert get(fresh, workspace_id) == get(fresh, workspace_id, "@1") == wire


@pytest.mark.parametrize("defect,diagnostic", [
    ("dangling", "dangling reference"), ("cycle", "cycle"),
    ("superseded", "superseded reference"), ("trace", "missing criterion trace"),
])
def test_http_legacy_invalid_ancestry_refuses_without_persisting_ready(service, write_repository, defect, diagnostic):
    workspace_id, _ = write_repository
    _grant(service, workspace_id)
    body = make_body(workspace_id, relay=False)
    if defect == "cycle":
        mutate_cycle(body)
    elif defect == "superseded":
        mutate_superseded(body)
    elif defect == "trace":
        mutate_missing_trace(body)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
    if defect == "dangling":
        # Impossible legacy FK violation: bypass only inside this workspace-scoped transaction.
        with connection(service, workspace_id, "postgres") as conn:
            conn.execute("SET LOCAL session_replication_role = replica")
            conn.execute(
                "UPDATE omp_work.intake_claim_premises SET premise_id='missing-premise' "
                "WHERE workspace_id=%s AND run_id=%s AND revision=0 AND claim_id='leaf'",
                (workspace_id, RUN),
            )
            corrupted = read_revision(conn.cursor(), workspace_id=workspace_id, run_id=RUN, revision=0)
            claim_hash, source_hash = revision_hashes(corrupted.snapshot, corrupted.artifact_bytes)
            conn.execute(
                "UPDATE omp_work.intake_revisions SET claim_set_hash=%s,source_hash=%s "
                "WHERE workspace_id=%s AND run_id=%s AND revision=0",
                (claim_hash, source_hash, workspace_id, RUN),
            )
    before = counts(service, workspace_id)
    with restarted(service) as fresh:
        status, refused = _command(fresh, workspace_id, command())
        assert status == 400 and refused["error"]["code"] == "invalid_request"
        assert diagnostic in refused["error"]["diagnostics"]
        assert counts(fresh, workspace_id) == before
        response = fresh.client.get(f"/v1/workspaces/{workspace_id}/intake/{RUN}", headers=_owner_headers(workspace_id))
        assert response.status_code == 200
        assert response.json()["assessments"] == []


def test_third_conflicting_claim_reopens_two_claim_settlement_after_restart(service, write_repository):
    workspace_id, _ = write_repository
    _grant(service, workspace_id)
    body = make_body(workspace_id, relay=False)
    draft = _draft(drop_unknowns=True)
    second = draft.constraints[0].model_copy(update={
        "id": "c-yaml", "statement": "The export encoding is YAML",
        "value": draft.constraints[0].value.model_copy(update={"value": "yaml"}),
    })
    draft = draft.model_copy(update={"constraints": (*draft.constraints, second)})
    projected = bounded_to_nsi(draft)
    extras = [c for c in body.snapshot.claims if c.id in {"ancestor", "mid", "sibling", "leaf", "late"}]
    body.snapshot.claims = _retarget(list(projected.claims)) + extras
    body.snapshot.entities = _entities(body.snapshot.claims)
    findings = nsi_bounded_evaluation(projected)
    body.gaps, body.conflicts, body.questions = list(findings.gaps), list(findings.conflicts), list(findings.questions)
    body.decisions = []
    body.instructions = []
    body.supersessions = {}
    body.history.decisions = []
    body.history.claim_supersession_decisions = {}
    body.history.instruction_hashes = {}
    body.history.relays = {}
    third = second.model_copy(update={"id": "c-xml", "value": second.value.model_copy(update={"value": "xml"})})
    third_snapshot = bounded_to_nsi(draft.model_copy(update={"constraints": (*draft.constraints, third)}))
    new_claims = [c for c in third_snapshot.claims if c.id in {"c-xml", "bounded.annotation.c-xml"}]
    body.snapshot.entities.extend(_entities(new_claims))
    question_id = body.questions[0].id
    request = decision(1, claim_ids=["c-json", "c-yaml"])
    request["payload"]["decision"].update(id="decision-1", question_id=question_id)
    owner = RecordIntakeDecisionCommand.model_validate(request).payload
    stored_decision = owner.decision.model_copy(update={"revision": 0})
    body.decisions = [DecisionRecord(decision=stored_decision, settled_claim_ids=list(owner.claim_ids))]
    body.history.decisions = [IntakeDecisionRecord(
        decision=stored_decision, claim_ids=owner.claim_ids, instruction=owner.instruction,
    )]
    # Initial owner settlement is fixture state, just like the bounded proof and criteria.
    # Successor below uses the real decision command after observing the renewed conflict.
    original = make_body(workspace_id, relay=False)
    body.instructions = original.instructions
    body.history.instruction_hashes = original.history.instruction_hashes
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
    apply(service, workspace_id, command())
    settled = get(service, workspace_id, "@1")
    assert settled["assessments"][0]["assessment"]["status"] == "READY_FOR_RATIFICATION"
    revision = 1
    for claim in new_claims:
        raw = claim.model_dump(mode="json")
        raw.update(asserted_by=dict(id=str(OWNER), kind="owner"), admitted_by=dict(id=str(OWNER), kind="owner"))
        # Annotation commands require their own evidence, anchored to the same source span.
        if not raw["evidence"]:
            raw["evidence"] = new_claims[0].model_dump(mode="json")["evidence"]
        apply(service, workspace_id, record_claim(raw, revision))
        revision += 1
    assert get(service, workspace_id)["assessments"] == []
    apply(service, workspace_id, command(revision))
    reopened = get(service, workspace_id)
    proof = reopened["assessments"][0]
    assert proof["assessment"]["status"] == "CONFLICTED"
    conflict = proof["assessment"]["conflicts"][0]
    assert set(conflict["claim_ids"]) == {"c-json", "c-yaml", "c-xml"}
    assert conflict["id"] == body.conflicts[0].id
    assert conflict["reason"] == body.conflicts[0].reason
    assert proof["assessment"]["gaps"] == [gap.model_dump(mode="json") for gap in body.gaps]
    question = next(i for i in proof["unresolved_issues"] if i["issue"]["id"] == question_id)
    assert question["issue"] == body.questions[0].model_dump(mode="json")
    assert question["gaps"] == [gap.id for gap in body.gaps]
    assert reopened["decisions"][0]["claim_ids"] == ["c-json", "c-yaml"]
    assert reopened["decisions"][0]["instruction"] == request["payload"]["instruction"]
    with restarted(service) as fresh:
        assert get(fresh, workspace_id) == get(fresh, workspace_id, f"@{revision}") == reopened
        assert get(fresh, workspace_id, "@1") == settled
        successor = decision(revision, claim_ids=["c-json", "c-yaml", "c-xml"],
                             superseded_decision_id="decision-1")
        successor["payload"]["decision"].update(id="settle-all", question_id=question_id)
        apply(fresh, workspace_id, successor)
    with restarted(service) as fresh:
        completed = get(fresh, workspace_id)
        assert completed["issues"] == []
        assert completed["assessments"] == []
        assert completed["decisions"][-1]["claim_ids"] == ["c-json", "c-yaml", "c-xml"]
        assert completed["decisions"][-1]["instruction"] == successor["payload"]["instruction"]
        assert get(fresh, workspace_id, f"@{revision}") == reopened
        assert get(fresh, workspace_id, "@1") == settled


@pytest.mark.parametrize("collision", ["source-actor", "source-revision", "relation", "typed-value"])
def test_http_collisions_cannot_reuse_pinned_binding_or_receipts(service, write_repository, collision):
    workspace_id, _ = write_repository
    _grant(service, workspace_id)
    body = make_body(workspace_id, relay=False)
    # Proposed annotation is outside consumed proof until owner admission.
    if collision == "source-revision":
        body.snapshot.artifacts[0].actor = body.snapshot.artifacts[0].actor.model_copy(update={"id": str(OWNER)})
    span = next(c for c in body.snapshot.claims if c.id == "ancestor").evidence
    _replace(body, "late", evidence=span, value=TypedBooleanValue(type="boolean", value=True))
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, body, uuid4(), -1)
    apply(service, workspace_id, command())
    pinned = get(service, workspace_id, "@1")
    before = counts(service, workspace_id)
    if collision.startswith("source"):
        artifact = body.snapshot.artifacts[0]
        request = evidence_command(OWNER, "owner", artifact_id=artifact.id,
                                   uri=artifact.uri, text=body.artifact_bytes[artifact.id].decode(),
                                   revision=artifact.revision)
        if collision == "source-revision":
            request["payload"]["artifact"]["revision"] = "source-rev-8"
        else:
            # Authenticating as OWNER cannot borrow another source author's identity.
            request["payload"]["artifact"]["actor"] = artifact.actor.model_dump(mode="json")
    else:
        raw = next(c for c in body.snapshot.claims if c.id == "late").model_dump(mode="json")
        raw.update(state="admitted", truth="true", admitted_by=dict(id=str(OWNER), kind="owner"))
        if collision == "typed-value":
            raw["value"] = dict(type="integer", value=1)
        request = record_claim(raw, 1, relation="contradicts" if collision == "relation" else "supports")
    with restarted(service) as fresh:
        status, refused = _command(fresh, workspace_id, request)
        expected_status, expected_code = (403, "forbidden") if collision == "source-actor" else (400, "invalid_request")
        assert (status, refused["error"]["code"]) == (expected_status, expected_code)
        assert counts(fresh, workspace_id) == before
        assert get(fresh, workspace_id) == get(fresh, workspace_id, "@1") == pinned
