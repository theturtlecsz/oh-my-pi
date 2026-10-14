"""Integration tests proving research-to-lesson data flow into journey passes (OMP-588-s05)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from omp_knowledge.learning.capture import drain
from omp_knowledge.learning.cli import EXIT_OK, EXIT_RUN_FAILED, main as cli_main
from omp_knowledge.learning.generation import LocalChatGenerator
from omp_knowledge.learning.promotion import has_journey_pass
from omp_knowledge.learning.proposals import ProposalRecord
from omp_knowledge.learning.store import LearningStore
from omp_work.v1.api_models import (
    DomainEventsPage,
    DomainEventView,
    ResearchView,
    WorkflowView,
    WorkItemsPage,
    WorkItemSummary,
    WorkItemView,
)
from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.client import WorkClient
from omp_work.v1.models import (
    Candidate,
    EvidenceKind,
    EvidenceReceipt,
    ResearchCampaign,
    ResearchDeliverableBinding,
    WorkAlias,
    WorkRevision,
)


def _run_integration_variant(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    close_type: str,
    cite_campaign: bool,
) -> None:
    work_id = uuid4()
    workspace_id = uuid4()
    campaign_id = str(uuid4())
    revision_id = uuid4()
    candidate_id = uuid4()
    trial_id = uuid4()
    receipt_id = uuid4()
    work_key = "OMP-588"
    now = datetime.now(timezone.utc)

    # 1. Native domain projections and models
    claim_text = "Verification receipt confirms candidate quality"
    receipt_payload = {"exit_code": 0, "claim": claim_text}
    claim_start = canonical_json(receipt_payload).index(claim_text)
    receipt = EvidenceReceipt(
        receipt_id=receipt_id,
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        kind=EvidenceKind.VERIFICATION,
        payload=receipt_payload,
        payload_sha256=sha256(receipt_payload),
        issuer="test-runner",
        issued_at=now,
        verdict="PASS",
        independent=True,
    )

    campaign = ResearchCampaign.model_validate({
        "campaign_id": UUID(campaign_id),
        "workspace_id": workspace_id,
        "work_id": work_id,
        "revision_id": revision_id,
        "domain": "engineering",
        "state": "concluded",
        "created_at": now,
        "outcome": "supported",
        "outcome_reason": "Empirically verified candidate",
        "spec_sha256": "a" * 64,
        "spec": {
            "objective": "Compare candidates",
            "evaluation_protocol_id": "eval",
            "evaluation_protocol_sha256": "b" * 64,
            "resource_policy_ref": "bounded",
            "candidate_mapping_policy": "native",
        },
    })

    binding = ResearchDeliverableBinding(
        campaign_id=UUID(campaign_id),
        trial_id=trial_id,
        workspace_id=workspace_id,
        work_id=work_id,
        revision_id=revision_id,
        native_candidate_id=candidate_id,
        candidate_digest="0" * 64,
        binding_sha256="b" * 64,
        bound_at=now,
    )

    research_view = ResearchView(
        work_id=work_id,
        campaigns=(campaign,),
        deliverable_bindings=(binding,),
    )

    work_item_view = WorkItemView(
        work_id=work_id,
        workspace_id=workspace_id,
        alias=WorkAlias(work_id=work_id, key=work_key, primary=True, origin="local"),
        state="DONE",
        revision=WorkRevision(
            revision_id=revision_id,
            work_id=work_id,
            revision_number=1,
            title="Research-to-lesson integration item",
            description="Item description",
            scope="Item scope",
            acceptance_criteria=("criterion 1",),
            content_sha256="0" * 64,
            created_by="tester",
            created_at=now,
        ),
        candidate=Candidate(
            candidate_id=candidate_id,
            work_id=work_id,
            revision_id=revision_id,
            candidate_sha256="1" * 64,
            commit_sha="2" * 40,
            kind="planned",
            allocated_at=now,
        ),
    )

    workflow_view = WorkflowView(
        item=work_item_view,
        receipts=(receipt,),
    )

    # Native aggregate_id == work_id for both close types
    event_payload = {"work_id": str(work_id), "state": "DONE"}
    event_view = DomainEventView(
        event_id=uuid4(),
        sequence=1,
        workspace_id=workspace_id,
        aggregate_type="work_item" if close_type == "complete_work" else "execution_item",
        aggregate_id=work_id,
        aggregate_version=1,
        actor_id=uuid4(),
        actor_kind="worker",
        capability_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        operation_id=uuid4(),
        causation_id=uuid4(),
        event_type=close_type,
        outcome="succeeded",
        payload=event_payload,
        payload_sha256=sha256(event_payload),
        event_sha256="0" * 64,
        occurred_at=now,
    )

    # 2. Actual WorkClient with httpx.MockTransport native projections
    def work_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["X-OMP-Workspace-ID"] == str(workspace_id)
        path = request.url.path

        if path == f"/v1/workspaces/{workspace_id}/events":
            return httpx.Response(
                200,
                json=DomainEventsPage(
                    events=(event_view,),
                    watermark_sequence=1,
                    next_after_sequence=1,
                    has_more=False,
                ).model_dump(mode="json"),
            )
        elif path == f"/v1/workspaces/{workspace_id}/work-items":
            return httpx.Response(
                200,
                json=WorkItemsPage(
                    items=(
                        WorkItemSummary(
                            work_id=work_id,
                            key=work_key,
                            state="DONE",
                            created_at=now,
                        ),
                    ),
                    next_created_at=None,
                    next_work_id=None,
                ).model_dump(mode="json"),
            )
        elif path == f"/v1/work-items/{work_key}/workflow":
            return httpx.Response(200, json=workflow_view.model_dump(mode="json"))
        elif path == f"/v1/work-items/{work_key}/research":
            return httpx.Response(200, json=research_view.model_dump(mode="json"))
        elif path == f"/v1/work-items/{work_key}":
            return httpx.Response(200, json=work_item_view.model_dump(mode="json"))
        elif path == f"/v1/receipts/{receipt_id}":
            return httpx.Response(200, json=receipt.model_dump(mode="json"))
        else:
            return httpx.Response(404, json={"error": f"Unhandled path: {path}"})

    # 3. Generator MockTransport inspecting canonical research_record
    generator_inspections: list[dict[str, object]] = []

    def generator_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content.decode("utf-8"))
        messages = body["messages"]
        user_content = messages[1]["content"]
        trace = json.loads(user_content)

        # Generator transport inspects received canonical research_record outcomes/reasons/refs
        research_rec = trace.get("research_record")
        assert research_rec is not None
        assert research_rec["work_id"] == str(work_id)
        assert len(research_rec["campaigns"]) == 1
        camp = research_rec["campaigns"][0]
        assert camp["campaign_id"] == campaign_id
        assert camp["outcome"] == "supported"
        assert camp["outcome_reason"] == "Empirically verified candidate"
        assert camp["truncated"] == {"deliverable_refs": 0}
        assert research_rec["truncated"] == {"campaigns": 0}

        refs = camp["deliverable_refs"]
        assert len(refs) == 1
        assert refs[0]["trial_id"] == str(trial_id)
        assert refs[0]["native_candidate_id"] == str(candidate_id)
        assert refs[0]["candidate_digest"] == "0" * 64
        assert refs[0]["binding_sha256"] == "b" * 64

        # Inspect received work_record receipts
        work_rec = trace.get("work_record")
        assert work_rec is not None
        receipts = work_rec.get("receipts") or []
        assert len(receipts) == 1
        assert receipts[0]["receipt_id"] == str(receipt_id)
        assert receipts[0]["verdict"] == "PASS"

        generator_inspections.append(camp)

        # Produce lesson citing received campaign ID plus valid PASS receipt (or omit citation)
        citations = [camp["campaign_id"]] if cite_campaign else []
        lesson_data = {
            "title": "Empirically verified lesson",
            "steps": ["Apply research findings", "Execute candidate verification"],
            "claims": [
                {
                    "text": claim_text,
                    "receipt_ids": [receipts[0]["receipt_id"]],
                    "source_span": {"start": claim_start, "end": claim_start + len(claim_text)},
                    "evaluator": receipt.issuer,
                    "outcome": "PASS",
                }
            ],
            "research_campaign_ids": citations,
        }
        content = json.dumps({"lessons": [lesson_data], "no_lesson_reason": None})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    # Prepare bearer file with 0o600 permissions
    bearer_file = tmp_path / f"bearer_{close_type}_{cite_campaign}.json"
    bearer_file.write_text('{"token": "test-token"}')
    bearer_file.chmod(0o600)

    work_client = WorkClient(
        "http://work.test",
        workspace_id,
        bearer_file,
        transport=httpx.MockTransport(work_handler),
    )

    generator = LocalChatGenerator(
        "http://127.0.0.1:18080",
        model="local-model",
        profile="local-profile",
        transport=httpx.MockTransport(generator_handler),
    )

    # 4. Fresh persisted LearningStore per variant
    db_path = tmp_path / f"learning_{close_type}_{cite_campaign}.sqlite"
    store = LearningStore(str(db_path))
    try:
        run = drain(
            store,
            work_client,
            work_client,
            generator,
            workspace_id=str(workspace_id),
            records=work_client,
        )
        assert run.status == "succeeded"
        assert run.units_total == 1
        assert run.proposals_accepted == 1
        assert run.proposals_rejected == 0
        assert len(generator_inspections) == 1
    finally:
        store.close()

    # 5. Citation survives proposal persistence/reopen
    reopened = LearningStore(str(db_path))
    try:
        row = reopened.execute("SELECT * FROM proposals").fetchone()
        assert row is not None
        proposal = ProposalRecord.model_validate(dict(row))
        assert proposal.status == "accepted"
        assert proposal.attribution.source.aggregate_id == str(work_id)
        assert proposal.attribution.source.workspace_id == str(workspace_id)
        if cite_campaign:
            assert proposal.lesson.research_campaign_ids == (campaign_id,)
            assert json.loads(proposal.lesson_json)["research_campaign_ids"] == [campaign_id]
        else:
            assert proposal.lesson.research_campaign_ids == ()
            assert json.loads(proposal.lesson_json).get("research_campaign_ids", []) == []

        # Procedure is active
        proc = reopened.execute(
            "SELECT * FROM procedures WHERE procedure_id = ?",
            (proposal.procedure_id,),
        ).fetchone()
        assert proc is not None
        assert proc["status"] == "active"
    finally:
        reopened.close()

    # 6. CLI journey-check
    rc = cli_main(
        [
            "journey-check",
            "--state-dir", str(db_path),
            "--work-url", "http://work.test",
            "--bearer-file", str(bearer_file),
            "--workspace", str(workspace_id),
            "--work-key", work_key,
        ],
        client=work_client,
    )
    expected_passed = cite_campaign
    expected_rc = EXIT_OK if expected_passed else EXIT_RUN_FAILED
    assert rc == expected_rc

    # Journey-check emits exact two-key passed stdout and no stderr
    captured = capsys.readouterr()
    assert captured.out == json.dumps({"capability": "research_to_learning", "passed": expected_passed}) + "\n"
    assert captured.err == ""

    # Check recorded journey pass in store
    check_store = LearningStore(str(db_path))
    try:
        assert has_journey_pass(check_store, str(workspace_id)) is expected_passed
        rows = check_store.execute("SELECT * FROM journey_passes").fetchall()
        assert len(rows) == int(expected_passed)
        if expected_passed:
            assert rows[0]["workspace_id"] == str(workspace_id)
            assert rows[0]["work_id"] == str(work_id)
            assert rows[0]["work_key"] == work_key
            assert rows[0]["capability"] == "research_to_learning"
    finally:
        check_store.close()

    work_client.close()
    generator.close()


@pytest.mark.parametrize("close_type", ["complete_work", "complete_execution_item"])
def test_research_to_lesson_integration_records_journey_pass(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    close_type: str,
) -> None:
    """Full integration: research reaches generator request, citation survives proposal

    persistence and reopen, and journey-check emits exact two-key passed stdout and records pass.
    """
    _run_integration_variant(tmp_path, capsys, close_type=close_type, cite_campaign=True)


@pytest.mark.parametrize("close_type", ["complete_work", "complete_execution_item"])
def test_research_to_lesson_omitted_citation_fails_journey_check(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    close_type: str,
) -> None:
    """Negative contrast: same work/procedure/receipt evidence with omitted citation

    fails journey-check and records no pass, proving data flow over aggregate coincidence.
    """
    _run_integration_variant(tmp_path, capsys, close_type=close_type, cite_campaign=False)
