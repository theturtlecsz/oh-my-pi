"""Wire the label-blind support judge into claim writes (OMP-604-s06-s02-s01, OMP-604-s06-s02-s02).

Echo claim writes require no budget, call no model completion, and record no
usage: the stored link retains the caller relation and an echo support receipt
binding the artifact byte span.

Live judge claim writes resolve budget admission, call model completion (or a
scripted hook), settle usage under O8 in its own transaction, and persist
support receipts binding the artifact byte span.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import hashlib
import os
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg.rows import dict_row
import pytest
from omp_work.contracts.nsi.v1 import models as nsi
from omp_work.intake_repository import append_revision, lock_revision, read_revision, revision_hashes
from omp_work.intake_support import SourceSpan, render_claim_text
from omp_work.intake_support_judge import Completion, JudgeHook, use_judge_hook
from omp_work.operations.jobs_migrate import apply_jobs_migrations
from omp_work.v1.api_models import IntakeRecordView
from omp_work.v1.canonical import text_sha256
from omp_work.v1.intake_commands import read_intake_view
from test_nsi2_claims import (
    claim_command,
    envelope,
    evidence_command,
    http_intake,  # noqa: F401
)
from test_nsi2_repository import connection, revision_body
from test_workflow_service import OWNER, _command, _owner_headers, service  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

RUN_ID = "run-1"


@pytest.fixture(scope="module", autouse=True)
def _jobs_schema(service):  # noqa: F811 - reuse the shared service fixture
    """The usage ledger lives in omp_jobs; ensure its schema exists once."""
    apply_jobs_migrations(service.config)


def _scripted(verdict: str, *, input_tokens: int | None = 7, output_tokens: int | None = 3):
    """A completion that always returns one verdict, counting its calls."""
    seen: list[tuple[str, str]] = []

    def complete(prompt: str, model_id: str) -> Completion:
        seen.append((prompt, model_id))
        return Completion(f'{{"verdict": "{verdict}"}}', input_tokens, output_tokens)

    complete.seen = seen  # type: ignore[attr-defined]
    return complete


def _start_and_evidence(service, workspace_id, mission_id) -> None:
    assert _command(
        service, workspace_id,
        envelope(workspace_id, mission_id).command.model_dump(mode="json"),
    )[0] == 200
    assert _command(service, workspace_id, evidence_command(OWNER, "owner"))[0] == 200


def _seed_budget(service, workspace_id: UUID, mission_id: UUID, amount: str = "100") -> None:
    """Seed an active mission budget so resolve_admission succeeds."""
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True,
    ) as conn:
        row = conn.execute(
            "SELECT project_id FROM omp_work.project_missions WHERE workspace_id=%s AND mission_id=%s",
            (workspace_id, mission_id),
        ).fetchone()
        project_id = row[0]
        conn.execute(
            "INSERT INTO omp_work.spend_budgets "
            "(workspace_id, record_id, project_id, budget_id, mission_id, ceiling_usd, threshold_usd, decision_id, active) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true)",
            (workspace_id, uuid4(), project_id, str(mission_id), mission_id, Decimal(amount), Decimal(amount), uuid4()),
        )


def _domain_events(service, workspace_id: UUID) -> list[tuple[str, dict[str, object]]]:
    with connection(service, workspace_id) as conn:
        return conn.execute(
            "SELECT event_type, payload FROM omp_audit.domain_events WHERE workspace_id=%s ORDER BY sequence",
            (workspace_id,),
        ).fetchall()


def _stored_links(service, workspace_id, revision):
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        return read_revision(
            cur, workspace_id=workspace_id, run_id=RUN_ID, revision=revision,
        ).body.evidence_links


def _usage_count(service, workspace_id) -> int:
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True,
    ) as conn:
        return conn.execute(
            "SELECT count(*) FROM omp_jobs.usage_events WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()[0]


def _usage_row(service, usage_id: str) -> dict[str, object] | None:
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True,
    ) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT * FROM omp_jobs.usage_events WHERE usage_id=%s",
                (usage_id,),
            ).fetchone()


def test_echo_write_needs_no_budget_completion_or_usage(service, http_intake):
    workspace_id, mission_id = http_intake
    _start_and_evidence(service, workspace_id, mission_id)
    complete = _scripted("contradicts")
    with use_judge_hook(JudgeHook(echo_caller=True, complete=complete)):
        assert _command(service, workspace_id, claim_command(OWNER, "owner"))[0] == 200
    assert complete.seen == []
    assert _usage_count(service, workspace_id) == 0
    links = _stored_links(service, workspace_id, 2)
    assert len(links) == 1
    link = links[0]
    assert link.relation == "supports"
    receipt = link.support_receipt
    assert receipt is not None
    assert receipt.verdict == "supports"
    assert receipt.usage_id is None
    assert receipt.source_span == SourceSpan(start=link.start_byte, end=link.end_byte)


@pytest.mark.parametrize(
    ("verdict", "expected_status"),
    [
        ("supports", "supported"),
        ("contradicts", "contested"),
        ("neither", "unsupported"),
        ("abstain", "unsupported"),
        ("timeout", "unsupported"),
        ("evidence-unavailable", "unsupported"),
    ],
)
def test_budget_scripted_verdict_derives_status_and_stores_receipt(
    service, http_intake, verdict: str, expected_status: str,
):
    """Budget + scripted verdict => status and stored receipt across all six verdicts.

    GET head and pinned revision verify every state/binding against DB receipt and computed inputs;
    only supports agrees. Includes nonzero byte span with Unicode context.
    """
    workspace_id, mission_id = http_intake
    assert _command(
        service, workspace_id,
        envelope(workspace_id, mission_id).command.model_dump(mode="json"),
    )[0] == 200
    context_text = "prefix: aé🙂 suffix"
    ev_cmd = evidence_command(OWNER, "owner", text=context_text)
    assert _command(service, workspace_id, ev_cmd)[0] == 200

    _seed_budget(service, workspace_id, mission_id)
    complete = _scripted(verdict)
    passage = "aé🙂"
    span = {
        "artifact_id": "artifact-added",
        "start_byte": 8,
        "end_byte": 15,
        "quote": passage,
    }
    cmd = claim_command(
        OWNER, "owner",
        evidence=[span],
        evidence_links=[{"span": span, "relation": "supports"}],
    )
    with use_judge_hook(JudgeHook(complete=complete)):
        status, body = _command(service, workspace_id, cmd)
    assert status == 200
    assert len(complete.seen) == 1
    assert _usage_count(service, workspace_id) == 1

    events = _domain_events(service, workspace_id)
    claim_event = events[-1][1]
    assert claim_event["type"] == "record_intake_claim"
    assert claim_event["evidence_status"] == expected_status

    links = _stored_links(service, workspace_id, 2)
    assert len(links) == 1
    link = links[0]
    assert link.relation == "supports"  # Caller relation
    receipt = link.support_receipt
    assert receipt is not None
    assert receipt.verdict == verdict
    assert receipt.measurement == "measured"
    assert receipt.input_tokens == 7
    assert receipt.output_tokens == 3
    assert receipt.usage_id is not None
    assert receipt.source_span == SourceSpan(start=8, end=15)
    assert receipt.source_span.start > 0

    usage = _usage_row(service, receipt.usage_id)
    assert usage is not None
    assert usage["role_derived"] == "intake_support_judge"
    assert usage["measurement"] == "measured"
    assert usage["input_tokens"] == 7
    assert usage["output_tokens"] == 3

    # Computed inputs
    claim_payload_data = cmd["payload"]["claim"]
    claim_text = render_claim_text({
        "subject_id": claim_payload_data["subject"]["id"],
        "predicate_id": claim_payload_data["predicate_id"],
        "predicate_title": "Depends on",
        "value": claim_payload_data["value"],
        "modality": claim_payload_data["modality"],
        "polarity": claim_payload_data["polarity"],
    })
    expected_claim_sha256 = text_sha256(claim_text)
    expected_passage_sha256 = text_sha256(passage)
    expected_context_sha256 = text_sha256(context_text)

    # GET head and pinned revision (both HTTP and read_intake_view)
    head_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/{RUN_ID}",
        headers=_owner_headers(workspace_id),
    )
    assert head_resp.status_code == 200
    head_view = IntakeRecordView.model_validate(head_resp.json())

    pinned_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/{RUN_ID}?revision=3",
        headers=_owner_headers(workspace_id),
    )
    assert pinned_resp.status_code == 200
    pinned_view = IntakeRecordView.model_validate(pinned_resp.json())

    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        direct_head = read_intake_view(cur, workspace_id, RUN_ID)
        direct_pinned = read_intake_view(cur, workspace_id, f"{RUN_ID}@3")

    for view in (head_view, pinned_view, direct_head, direct_pinned):
        assert len(view.evidence_links) == 1
        assert len(view.support_judgments) == 1
        judgment = view.support_judgments[0]

        # Verify against DB receipt
        assert judgment.claim_id == link.claim_id
        assert judgment.artifact_id == link.artifact_id
        assert judgment.model_id == receipt.model_id
        assert judgment.start_byte == receipt.source_span.start
        assert judgment.end_byte == receipt.source_span.end
        assert judgment.caller_relation == link.relation
        assert judgment.verdict == receipt.verdict
        assert judgment.source_revision == receipt.source_revision
        assert judgment.model_version == receipt.model_version
        assert judgment.prompt_version == receipt.prompt_version
        assert judgment.rule_version == receipt.rule_version
        assert judgment.passage_sha256 == receipt.passage_sha256
        assert judgment.claim_text_sha256 == receipt.claim_text_sha256
        assert judgment.context_sha256 == receipt.context_sha256

        # Verify against computed inputs
        assert judgment.start_byte == 8
        assert judgment.end_byte == 15
        assert judgment.start_byte > 0
        assert judgment.caller_relation == "supports"
        assert judgment.verdict == verdict
        assert judgment.claim_text_sha256 == expected_claim_sha256
        assert judgment.passage_sha256 == expected_passage_sha256
        assert judgment.context_sha256 == expected_context_sha256
        assert judgment.source_revision == expected_context_sha256

        # Disagreement: only supports agrees
        if verdict == "supports":
            assert not judgment.disagreement
        else:
            assert judgment.disagreement
        assert judgment.disagreement == (judgment.caller_relation != judgment.verdict)


def test_no_budget_refuses_claim_write(service, http_intake):
    """No budget => forbidden; no revision, usage, or completion."""
    workspace_id, mission_id = http_intake
    _start_and_evidence(service, workspace_id, mission_id)
    complete = _scripted("supports")
    with use_judge_hook(JudgeHook(complete=complete)):
        status, body = _command(service, workspace_id, claim_command(OWNER, "owner"))
    assert status == 403
    assert body["error"]["code"] == "forbidden"
    assert complete.seen == []
    assert _usage_count(service, workspace_id) == 0

    with connection(service, workspace_id) as conn:
        assert conn.execute(
            "SELECT current_revision FROM omp_work.intake_heads WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM omp_work.intake_revisions WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone() == (2,)


def test_admission_copies_stored_receipts_without_rejudging(service, http_intake):
    """Admission copies stored receipts without rejudging."""
    workspace_id, mission_id = http_intake
    _start_and_evidence(service, workspace_id, mission_id)
    _seed_budget(service, workspace_id, mission_id)
    complete = _scripted("supports")
    with use_judge_hook(JudgeHook(complete=complete)):
        assert _command(service, workspace_id, claim_command(OWNER, "owner"))[0] == 200
    assert len(complete.seen) == 1
    assert _usage_count(service, workspace_id) == 1
    proposed_receipt = _stored_links(service, workspace_id, 2)[0].support_receipt
    assert proposed_receipt is not None

    proposal = claim_command(OWNER, "owner")
    admission = deepcopy(proposal)
    admission["payload"]["expected_revision"] = 3
    admission["payload"]["claim"].update(
        state="admitted", truth="true", admitted_by=dict(id=str(OWNER), kind="owner"),
    )
    with use_judge_hook(JudgeHook(complete=complete)):
        status, body = _command(service, workspace_id, admission)
    assert status == 200
    assert len(complete.seen) == 1
    assert _usage_count(service, workspace_id) == 1

    events = _domain_events(service, workspace_id)
    assert events[-1][1]["evidence_status"] == "supported"

    admitted_links = _stored_links(service, workspace_id, 3)
    assert len(admitted_links) == 1
    assert admitted_links[0].support_receipt == proposed_receipt


def test_admission_with_missing_receipts_reports_unsupported(service, http_intake):
    """Missing receipts never report support on admission."""
    workspace_id, mission_id = http_intake
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        seeded = revision_body(workspace_id, revision=0)
        seeded["snapshot"]["state"] = "reasoning"
        for artifact in seeded["snapshot"]["artifacts"]:
            artifact["content_hash"] = hashlib.sha256(seeded["artifact_bytes"][artifact["id"]]).hexdigest()
        seeded["supersessions"] = {}
        seeded["snapshot"]["ontology"]["predicates"][0]["authority"].append("model")
        proposed_claim = seeded["snapshot"]["claims"][0]
        span = proposed_claim["evidence"][0]
        seeded["evidence_links"] = [
            dict(claim_id=proposed_claim["id"], relation="supports", **span),
        ]
        seeded["binding"]["rule_bundle_hash"] = "1" * 64
        seeded["binding"]["schema_hash"] = "2" * 64
        seeded["binding"]["contract_hash"] = "3" * 64
        seeded_snapshot = nsi.IntakeSnapshot.model_validate(seeded["snapshot"])
        c_hash, s_hash = revision_hashes(seeded_snapshot, seeded["artifact_bytes"])
        seeded["binding"]["claim_set_hash"] = c_hash
        seeded["binding"]["source_hash"] = s_hash

        locked = lock_revision(
            cur, workspace_id=workspace_id, run_id=RUN_ID, expected_revision=-1,
            mission_id=mission_id, operation_id=uuid4(),
        )
        append_revision(cur, locked, seeded, operation_id=uuid4())

    assert _stored_links(service, workspace_id, 0)[0].support_receipt is None

    _seed_budget(service, workspace_id, mission_id)
    complete = _scripted("supports")

    admitted_claim = deepcopy(proposed_claim)
    admitted_claim.update(
        state="admitted", truth="true", admitted_by=dict(id=str(OWNER), kind="owner"),
    )
    admit_cmd = {
        "type": "record_intake_claim",
        "payload": {
            "run_id": RUN_ID,
            "expected_revision": 1,
            "claim": admitted_claim,
            "evidence_links": [dict(span=span, relation="supports")],
        },
    }
    with use_judge_hook(JudgeHook(complete=complete)):
        status, body = _command(service, workspace_id, admit_cmd)
    assert status == 200
    assert complete.seen == []  # Did not re-judge

    events = _domain_events(service, workspace_id)
    assert events[-1][1]["evidence_status"] == "unsupported"

    admitted_links = _stored_links(service, workspace_id, 1)
    assert len(admitted_links) == 1
    assert admitted_links[0].support_receipt is None

    # Legacy link without receipt produces no judgments on GET head and pinned revision
    head_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/{RUN_ID}",
        headers=_owner_headers(workspace_id),
    )
    assert head_resp.status_code == 200
    head_view = IntakeRecordView.model_validate(head_resp.json())
    assert len(head_view.evidence_links) == 1
    assert head_view.support_judgments == ()

    pinned_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/{RUN_ID}?revision=1",
        headers=_owner_headers(workspace_id),
    )
    assert pinned_resp.status_code == 200
    pinned_view = IntakeRecordView.model_validate(pinned_resp.json())
    assert len(pinned_view.evidence_links) == 1
    assert pinned_view.support_judgments == ()

    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        direct_head = read_intake_view(cur, workspace_id, RUN_ID)
        direct_pinned = read_intake_view(cur, workspace_id, f"{RUN_ID}@1")
        assert len(direct_head.evidence_links) == 1
        assert direct_head.support_judgments == ()
        assert len(direct_pinned.evidence_links) == 1
        assert direct_pinned.support_judgments == ()


def test_forced_timeout_persists_timeout_receipt(service, http_intake):
    """Forced timeout persists timeout receipt with unknown usage (null tokens)."""
    workspace_id, mission_id = http_intake
    _start_and_evidence(service, workspace_id, mission_id)
    _seed_budget(service, workspace_id, mission_id)

    seen: list[tuple[str, str]] = []

    def complete_timeout(prompt: str, model_id: str) -> Completion:
        seen.append((prompt, model_id))
        raise httpx.ReadTimeout("simulated timeout")

    with use_judge_hook(JudgeHook(complete=complete_timeout)):
        status, body = _command(service, workspace_id, claim_command(OWNER, "owner"))
    assert status == 200
    assert len(seen) == 1

    events = _domain_events(service, workspace_id)
    assert events[-1][1]["evidence_status"] == "unsupported"

    links = _stored_links(service, workspace_id, 2)
    assert len(links) == 1
    link = links[0]
    assert link.relation == "supports"
    receipt = link.support_receipt
    assert receipt is not None
    assert receipt.verdict == "timeout"
    assert receipt.measurement == "unknown"
    assert receipt.input_tokens is None
    assert receipt.output_tokens is None
    assert receipt.usage_id is not None
    assert receipt.source_span == SourceSpan(start=link.start_byte, end=link.end_byte)

    assert _usage_count(service, workspace_id) == 1
    usage = _usage_row(service, receipt.usage_id)
    assert usage is not None
    assert usage["measurement"] == "unknown"
    assert usage["input_tokens"] is None
    assert usage["output_tokens"] is None
    assert usage["tokens"] is None
    assert usage["price_usd"] is None


def test_receipt_binds_nonzero_artifact_byte_span(service, http_intake):
    """Receipt binds nonzero artifact byte span."""
    workspace_id, mission_id = http_intake
    assert _command(
        service, workspace_id,
        envelope(workspace_id, mission_id).command.model_dump(mode="json"),
    )[0] == 200
    ev_cmd = evidence_command(OWNER, "owner", text="prefix: aé🙂 suffix")
    assert _command(service, workspace_id, ev_cmd)[0] == 200

    _seed_budget(service, workspace_id, mission_id)
    complete = _scripted("supports")

    span = {
        "artifact_id": "artifact-added",
        "start_byte": 8,
        "end_byte": 15,
        "quote": "aé🙂",
    }
    cmd = claim_command(
        OWNER, "owner",
        evidence=[span],
        evidence_links=[{"span": span, "relation": "supports"}],
    )
    with use_judge_hook(JudgeHook(complete=complete)):
        status, body = _command(service, workspace_id, cmd)
    assert status == 200

    links = _stored_links(service, workspace_id, 2)
    assert len(links) == 1
    link = links[0]
    assert link.start_byte == 8
    assert link.end_byte == 15
    assert link.support_receipt is not None
    assert link.support_receipt.source_span == SourceSpan(start=8, end=15)
    assert link.support_receipt.source_span.start > 0

    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        view = read_intake_view(cur, workspace_id, RUN_ID)
    assert len(view.support_judgments) == 1
    judgment = view.support_judgments[0]
    assert judgment.start_byte == 8
    assert judgment.end_byte == 15
    assert judgment.start_byte > 0


def test_receiptless_legacy_link_yields_no_invented_judgments(service, http_intake):
    """Receiptless legacy links yield no invented judgments while keeping evidence_links unchanged."""
    workspace_id, mission_id = http_intake
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        seeded = revision_body(workspace_id, revision=0)
        seeded["snapshot"]["state"] = "reasoning"
        for artifact in seeded["snapshot"]["artifacts"]:
            artifact["content_hash"] = hashlib.sha256(seeded["artifact_bytes"][artifact["id"]]).hexdigest()
        seeded["supersessions"] = {}
        seeded["snapshot"]["ontology"]["predicates"][0]["authority"].append("owner")
        claim = seeded["snapshot"]["claims"][0]
        span = claim["evidence"][0]
        # Legacy link with no support_receipt
        seeded["evidence_links"] = [
            dict(claim_id=claim["id"], relation="supports", **span),
        ]
        seeded["binding"]["rule_bundle_hash"] = "1" * 64
        seeded["binding"]["schema_hash"] = "2" * 64
        seeded["binding"]["contract_hash"] = "3" * 64
        seeded_snapshot = nsi.IntakeSnapshot.model_validate(seeded["snapshot"])
        c_hash, s_hash = revision_hashes(seeded_snapshot, seeded["artifact_bytes"])
        seeded["binding"]["claim_set_hash"] = c_hash
        seeded["binding"]["source_hash"] = s_hash

        locked = lock_revision(
            cur, workspace_id=workspace_id, run_id=RUN_ID, expected_revision=-1,
            mission_id=mission_id, operation_id=uuid4(),
        )
        append_revision(cur, locked, seeded, operation_id=uuid4())

    stored_link = _stored_links(service, workspace_id, 0)[0]
    assert stored_link.support_receipt is None

    # GET head
    head_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/{RUN_ID}",
        headers=_owner_headers(workspace_id),
    )
    assert head_resp.status_code == 200
    head_view = IntakeRecordView.model_validate(head_resp.json())
    assert len(head_view.evidence_links) == 1
    assert head_view.evidence_links[0].relation == "supports"
    assert head_view.support_judgments == ()

    # GET pinned revision
    pinned_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/{RUN_ID}?revision=1",
        headers=_owner_headers(workspace_id),
    )
    assert pinned_resp.status_code == 200
    pinned_view = IntakeRecordView.model_validate(pinned_resp.json())
    assert len(pinned_view.evidence_links) == 1
    assert pinned_view.evidence_links[0].relation == "supports"
    assert pinned_view.support_judgments == ()

    # read_intake_view direct
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        direct_head = read_intake_view(cur, workspace_id, RUN_ID)
        direct_pinned = read_intake_view(cur, workspace_id, f"{RUN_ID}@1")
        assert len(direct_head.evidence_links) == 1
        assert direct_head.support_judgments == ()
        assert len(direct_pinned.evidence_links) == 1
        assert direct_pinned.support_judgments == ()
