"""Owner settlements preserve exact language and immutable intake history."""
from copy import deepcopy
import hashlib
import os
from uuid import uuid4

import pytest

from omp_work.intake_repository import append_revision, lock_revision, read_revision
from omp_work.v1.intake_commands import execute_intake, read_intake_view
from omp_work.v1.models import CommandEnvelope
from omp_work.v1.store_shared import WorkStoreError
from omp_work.v1.store import PostgresWorkStore
from test_owner_relay_store import _approved_mission
from test_nsi2_claims import evidence_command, http_intake  # noqa: F401
from test_nsi2_repository import connection, revision_body
from test_nsi_schemas import CRITERION, DECISION, REQUIREMENT
from test_workflow_service import OWNER, _command, service  # noqa: F401

pytestmark = pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
                              reason="set OMP_WORK_POSTGRES_INTEGRATION=1")

TEXT = "  Keep café ≥ 99.9%.\r\nDo NOT trim.\t🙂  "
INSTRUCTION = dict(text=TEXT, provenance=dict(channel="owner-chat", message_ref="msg:exact",
                                            received_at="2026-10-11T00:00:00Z"))


@pytest.fixture
def seeded(service, http_intake):
    ws, mission = http_intake
    body = revision_body(ws)
    body.update(decisions=[], instructions=[], criteria=[], assessments=[], derivations=[], supersessions={})
    for claim in body["snapshot"]["claims"]:
        claim.update(state="admitted", truth="true", admitted_by=dict(id=str(OWNER), kind="owner"))
    with connection(service, ws) as conn, conn.cursor() as cur:
        append_revision(cur, lock_revision(cur, workspace_id=ws, run_id="run-1", expected_revision=-1,
                                          mission_id=mission, operation_id=uuid4()), body, operation_id=uuid4())
    return ws, mission, body


def decision(revision=1, **changes):
    payload = dict(run_id="run-1", expected_revision=revision,
                   decision={**DECISION, "actor": dict(id=str(OWNER), kind="owner"), "revision": revision,
                             "reason": "  exact reason\n", "answer": dict(type="string", value="  exact answer\t")},
                   claim_ids=["claim-1", "prior"], instruction=deepcopy(INSTRUCTION))
    payload.update(changes)
    return dict(type="record_intake_decision", payload=payload)


def envelope(ws, command):
    return CommandEnvelope.model_validate(dict(api_version="work.omp.dev/v1", workspace_id=ws,
                                               operation_id=uuid4(), request_id=uuid4(), correlation_id=uuid4(),
                                               command=command))


def view(service, ws, revision=None):
    with connection(service, ws) as conn, conn.cursor() as cur:
        return read_intake_view(cur, ws, "run-1" if revision is None else f"run-1@{revision}")


def criterion(revision=1):
    return dict(type="link_intake_criterion", payload=dict(
        run_id="run-1", expected_revision=revision, criterion=CRITERION,
        requirements=[{**REQUIREMENT, "text": "  Must preserve ≥ and\nnewlines.  "}],
    ))


def issue(revision=1):
    return dict(type="record_intake_issue", payload=dict(
        run_id="run-1", expected_revision=revision, gaps=["gap-1"],
        issue=dict(id="question-added", question="  Exact question?\n", gap_ids=["gap-1"],
                   conflict_ids=["conflict-1"], category="non_functional_qualities"),
    ))


def supersede(revision=2):
    return dict(type="supersede_intake_claim", payload=dict(
        run_id="run-1", expected_revision=revision, old_claim_id="claim-1",
        replacement_claim_id="prior", decision_id="decision-1",
    ))


def test_exact_decision_instruction_and_multi_claim_settlement(service, seeded):
    ws, _, original = seeded
    question = issue()
    assert _command(service, ws, question)[0] == 200
    assert {i.issue.id for i in view(service, ws).issues} == {"question-added", "question-1", "conflict-1"}
    command = decision(2)
    command["payload"]["decision"]["question_id"] = "question-added"
    status, result = _command(service, ws, command)
    assert status == 200, result
    current = view(service, ws)
    record = current.decisions[0]
    assert record.instruction.model_dump(mode="json") == INSTRUCTION
    assert record.decision.answer.value == "  exact answer\t"
    assert record.decision.reason == "  exact reason\n"
    assert set(record.claim_ids) == {"claim-1", "prior"}
    assert {i.issue.id for i in current.issues} == {"question-1"}
    assert view(service, ws, 2).issues[0].issue.question == "  Exact question?\n"
    with connection(service, ws) as conn, conn.cursor() as cur:
        stored = read_revision(cur, workspace_id=ws, run_id="run-1", revision=2)
        assert stored.artifact_bytes["instruction:decision-1"] == TEXT.encode()
        assert stored.body.history.instruction_hashes["decision-1"] == hashlib.sha256(TEXT.encode()).hexdigest()
        assert set(stored.body.decisions[0].settled_claim_ids) == {"claim-1", "prior"}
        assert {c.id for c in stored.body.conflicts} == {"conflict-1"}
        assert stored.body.questions[-1].gap_ids == ["gap-1"]
        assert stored.snapshot.claims[0].value.model_dump() == original["snapshot"]["claims"][0]["value"]


@pytest.mark.parametrize("failure", ["model", "other_owner", "forged_relay", "missing_claim", "partial_settlement", "cross_run"])
def test_decision_refusals_leave_no_effects(service, seeded, failure):
    ws, _, _ = seeded
    command = decision()
    actor, kind = OWNER, "owner"
    if failure == "model":
        kind = "agent"
        command["payload"]["verified_relay_operation_id"] = str(uuid4())
    elif failure == "other_owner":
        actor = uuid4()
    elif failure == "forged_relay":
        command["payload"]["verified_relay_operation_id"] = str(uuid4())
    elif failure == "missing_claim":
        command["payload"]["claim_ids"].append("foreign-run-claim")
    elif failure == "partial_settlement":
        command["payload"]["claim_ids"] = ["claim-1"]
    else:
        command["payload"]["decision"]["run_id"] = "other-run"
    with connection(service, ws) as conn, conn.cursor() as cur:
        with pytest.raises(WorkStoreError), conn.transaction():
            execute_intake(cur, envelope(ws, command), actor, kind)
        assert read_revision(cur, workspace_id=ws, run_id="run-1", revision=0).body.decisions == []
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads WHERE workspace_id=%s", (ws,)).fetchone() == (0,)


def test_claim_and_decision_supersession_retains_trace_and_rejects_cycles(service, seeded):
    ws, _, original = seeded
    assert _command(service, ws, decision())[0] == 200
    assert _command(service, ws, supersede())[0] == 200
    current = view(service, ws)
    old = next(c for c in current.snapshot.claims if c.id == "claim-1")
    assert old.state == "superseded"
    assert old.value.model_dump() == original["snapshot"]["claims"][0]["value"]
    assert old.evidence[0].quote == "hello"
    assert view(service, ws, 1).snapshot.claims[0].state == "admitted"
    assert current.decisions[0].decision.revision == 1
    with connection(service, ws) as conn, conn.cursor() as cur:
        body = read_revision(cur, workspace_id=ws, run_id="run-1", revision=2).body
        assert body.history.claim_supersession_decisions == {"claim-1": "decision-1"}
        assert body.supersessions == {"prior": ["claim-1"]}
    for old_id, new_id in (("prior", "prior"), ("prior", "claim-1")):
        command = supersede(3)
        command["payload"].update(old_claim_id=old_id, replacement_claim_id=new_id)
        assert _command(service, ws, command)[0] == 400
    updated = decision(3, superseded_decision_id="decision-1")
    updated["payload"]["decision"]["id"] = "decision-2"
    updated["payload"]["instruction"]["text"] = "New instruction"
    assert _command(service, ws, updated)[0] == 200
    history = view(service, ws).decisions
    assert len(history) == 2
    assert history[0].instruction.text == TEXT
    assert history[1].superseded_decision_id == "decision-1"
    assert _command(service, ws, supersede(4))[0] == 400


def test_criterion_to_requirement_claim_evidence_and_content_collision(service, seeded):
    ws, _, _ = seeded
    command = criterion()
    status, result = _command(service, ws, command)
    assert status == 200, result
    current = view(service, ws)
    assert current.criterion_links[0].requirements[0].text == command["payload"]["requirements"][0]["text"]
    trace = current.traces[0]
    assert trace.requirement_ids == ("requirement-1",)
    assert trace.claim_ids == ("claim-1",)
    claim = next(c for c in current.snapshot.claims if c.id == trace.claim_ids[0])
    assert claim.evidence[0].quote == "hello"
    for mutation in ("content", "claim", "requirement"):
        invalid = criterion(2)
        invalid["payload"]["criterion"] = {**CRITERION, "id": "criterion-2"}
        if mutation == "content":
            invalid["payload"]["requirements"][0]["text"] = "different"
        elif mutation == "claim":
            invalid["payload"]["requirements"][0]["claim_ids"] = ["foreign-run-claim"]
        else:
            invalid["payload"]["criterion"]["requirement_ids"] = ["missing"]
        assert _command(service, ws, invalid)[0] == 400
    assert view(service, ws).revision == 2


def test_rollback_after_append_removes_decision_artifact_and_links(service, seeded):
    ws, _, _ = seeded
    with connection(service, ws) as conn, conn.cursor() as cur:
        with pytest.raises(RuntimeError), conn.transaction():
            execute_intake(cur, envelope(ws, decision()), OWNER, "owner")
            raise RuntimeError("abort outer operation")
        assert conn.execute("SELECT count(*) FROM omp_work.intake_owner_decisions WHERE workspace_id=%s", (ws,)).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM omp_work.intake_settled_claims WHERE workspace_id=%s", (ws,)).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM omp_work.intake_artifacts WHERE workspace_id=%s AND artifact_id LIKE 'instruction:%%'", (ws,)).fetchone() == (0,)


@pytest.mark.parametrize("operation", ["decision", "supersede", "issue", "criterion"])
def test_http_stale_replay_collision_each_operation(service, seeded, operation):
    ws, mission, _ = seeded
    command = {"decision": decision, "supersede": supersede, "issue": issue, "criterion": criterion}[operation]()
    if operation == "supersede":
        assert _command(service, ws, decision())[0] == 200
    operation_id = uuid4()
    status, body = _command(service, ws, command, operation_id=operation_id)
    assert status == 200, body
    baseline = view(service, ws).model_dump(mode="json")
    assert _command(service, ws, command)[0] == 409
    status, replay = _command(service, ws, command, operation_id=operation_id)
    assert status == 200 and replay["receipt"]["state"] == "replayed"
    assert replay["result"] == body["result"]
    changed = deepcopy(command)
    changed["payload"]["expected_revision"] += 1
    status, collision = _command(service, ws, changed, operation_id=operation_id)
    assert status == 409 and collision["error"]["code"] == "idempotency_conflict"
    assert view(service, ws).model_dump(mode="json") == baseline
    with connection(service, ws) as conn:
        events = conn.execute("SELECT event_type,aggregate_id FROM omp_audit.domain_events "
                              "WHERE workspace_id=%s AND operation_id=%s ORDER BY sequence", (ws, operation_id)).fetchall()
        assert events == [(command["type"], mission), ("idempotency_conflict", mission)]


@pytest.mark.parametrize("operation", [issue, criterion, supersede])
def test_model_cannot_supply_findings_trace_or_supersession(service, seeded, operation):
    ws, _, _ = seeded
    command = operation(1)
    with connection(service, ws) as conn, conn.cursor() as cur:
        with pytest.raises(WorkStoreError, match="forbidden"), conn.transaction():
            execute_intake(cur, envelope(ws, command), OWNER, "agent")
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads WHERE workspace_id=%s", (ws,)).fetchone() == (0,)


@pytest.fixture
def applied_relay(service):
    # Exercise OMP-416 itself; do not fabricate a relay journal entry.
    ws, mission = _approved_mission(service)
    relay_envelope = envelope(ws, dict(type="relay_owner_intent", payload=dict(
        intent="pause", mission_id=str(mission), instruction=dict(
            text=TEXT, source_message_ref=INSTRUCTION["provenance"]["message_ref"],
            received_at=INSTRUCTION["provenance"]["received_at"], owner_authored=True,
        ),
    )))
    relayer = uuid4()
    _, result = PostgresWorkStore(service.config).execute(
        relay_envelope, actor_id=relayer, actor_kind="client", required_scope="work.client",
    )
    body = revision_body(ws)
    body.update(decisions=[], instructions=[], criteria=[], assessments=[], derivations=[], supersessions={})
    with connection(service, ws) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO omp_work.project_missions "
                    "(workspace_id,mission_id,project_id,objective,status) "
                    "SELECT workspace_id,%s,project_id,'Intake','draft' FROM omp_work.projects "
                    "WHERE workspace_id=%s", (mission, ws))
        append_revision(cur, lock_revision(cur, workspace_id=ws, run_id="run-1", expected_revision=-1,
                                          mission_id=mission, operation_id=uuid4()), body, operation_id=uuid4())
    return ws, relay_envelope.operation_id, result["relay"]


def test_applied_relay_copies_verified_provenance_without_new_intent(service, applied_relay):
    ws, ref, relay = applied_relay
    instruction = deepcopy(INSTRUCTION)
    instruction["provenance"]["channel"] = "relay"
    command = decision(verified_relay_operation_id=str(ref), instruction=instruction)
    status, result = _command(service, ws, command)
    assert status == 200, result
    assert view(service, ws).decisions[0].verified_relay_operation_id == ref
    with connection(service, ws) as conn, conn.cursor() as cur:
        stored = read_revision(cur, workspace_id=ws, run_id="run-1", revision=1)
        assert stored.body.history.relays["decision-1"] == relay
        assert conn.execute("SELECT count(*) FROM omp_control.idempotent_commands WHERE workspace_id=%s "
                            "AND command_type='relay_owner_intent'", (ws,)).fetchone() == (1,)


@pytest.mark.parametrize("mismatch", ["text", "message", "timestamp", "channel", "mission", "non_owner"])
def test_applied_relay_mismatches_and_non_owner_refused(service, applied_relay, mismatch):
    ws, ref, _ = applied_relay
    instruction = deepcopy(INSTRUCTION)
    instruction["provenance"]["channel"] = "relay"
    command = decision(verified_relay_operation_id=str(ref), instruction=instruction)
    if mismatch == "text":
        instruction["text"] += "forged"
    elif mismatch == "message":
        instruction["provenance"]["message_ref"] = "other-message"
    elif mismatch == "timestamp":
        instruction["provenance"]["received_at"] = "2026-10-12T00:00:00Z"
    elif mismatch == "channel":
        instruction["provenance"]["channel"] = "owner-chat"
    elif mismatch == "mission":
        other_ws, other_mission = _approved_mission(service)
        other = envelope(other_ws, dict(type="relay_owner_intent", payload=dict(
            intent="pause", mission_id=str(other_mission), instruction=dict(
                text=TEXT, source_message_ref=instruction["provenance"]["message_ref"],
                received_at=instruction["provenance"]["received_at"], owner_authored=True,
            ),
        )))
        PostgresWorkStore(service.config).execute(other, actor_id=uuid4(), actor_kind="client",
                                                required_scope="work.client")
        command["payload"]["verified_relay_operation_id"] = str(other.operation_id)
    with connection(service, ws) as conn, conn.cursor() as cur:
        with pytest.raises(WorkStoreError, match="forbidden"), conn.transaction():
            execute_intake(cur, envelope(ws, command), OWNER, "agent" if mismatch == "non_owner" else "owner")
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads WHERE workspace_id=%s", (ws,)).fetchone() == (0,)


def test_evidence_append_preserves_settlements_open_findings_and_requirement_trace(service, seeded):
    ws, _, _ = seeded
    assert _command(service, ws, decision())[0] == 200
    assert _command(service, ws, criterion(2))[0] == 200
    command = evidence_command(OWNER, "owner", text="new evidence")
    command["payload"]["expected_revision"] = 3
    status, result = _command(service, ws, command)
    assert status == 200, result
    current = view(service, ws)
    assert current.decisions[0].instruction.text == TEXT
    assert current.traces[0].claim_ids == ("claim-1",)
    assert current.issues == ()
    with connection(service, ws) as conn, conn.cursor() as cur:
        stored = read_revision(cur, workspace_id=ws, run_id="run-1", revision=3)
        assert set(stored.body.decisions[0].settled_claim_ids) == {"claim-1", "prior"}
        assert {i.claim_id for i in stored.body.instructions} == {"claim-1", "prior"}


def test_rule_engine_findings_stay_open_until_owner_settles_all_linked_claims(service, seeded):
    ws, _, original = seeded
    conflict = dict(type="record_intake_issue", payload=dict(
        run_id="run-1", expected_revision=1, gaps=["gap-1"], issue=dict(
            id="conflict-added", claim_ids=["claim-1", "prior"], predicate_id=original["snapshot"]["ontology"]["predicates"][0]["id"],
            reason="  Rule found contradiction.\n",
        ),
    ))
    with connection(service, ws) as conn, conn.cursor() as cur:
        execute_intake(cur, envelope(ws, conflict), uuid4(), "rule_engine")
    question = issue(2)
    question["payload"]["issue"]["conflict_ids"] = ["conflict-added"]
    assert _command(service, ws, question)[0] == 200
    records = {r.issue.id: r for r in view(service, ws).issues}
    assert records["conflict-added"].gaps == ("gap-1",)
    assert records["conflict-added"].issue.reason == "  Rule found contradiction.\n"
    command = decision(3)
    command["payload"]["decision"]["question_id"] = "question-added"
    bad = deepcopy(command)
    bad["payload"]["claim_ids"] = ["claim-1"]
    assert _command(service, ws, bad)[0] == 400
    assert "conflict-added" in {i.issue.id for i in view(service, ws).issues}
    assert _command(service, ws, command)[0] == 200
    assert "conflict-added" not in {i.issue.id for i in view(service, ws).issues}
    assert "conflict-1" in {i.issue.id for i in view(service, ws).issues}
    with connection(service, ws) as conn, conn.cursor() as cur:
        body = read_revision(cur, workspace_id=ws, run_id="run-1", revision=3).body
        assert {c.id for c in body.conflicts} == {"conflict-1", "conflict-added"}
        assert body.history.issues[0].gaps == ("gap-1",)
        assert next(q for q in body.questions if q.id == "question-added").conflict_ids == ["conflict-added"]


def test_shared_requirement_can_link_another_criterion_without_rewriting_text(service, seeded):
    ws, _, _ = seeded
    assert _command(service, ws, criterion())[0] == 200
    second = criterion(2)
    second["payload"]["criterion"] = {**CRITERION, "id": "criterion-2"}
    assert _command(service, ws, second)[0] == 200
    current = view(service, ws)
    assert len(current.traces) == 2
    assert all(trace.claim_ids == ("claim-1",) for trace in current.traces)
    assert current.criterion_links[0].requirements == current.criterion_links[1].requirements
