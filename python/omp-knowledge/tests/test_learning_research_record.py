"""Only concluded own-work research is exposed, with stable bounded references."""

import json
from datetime import datetime, timezone
from uuid import UUID

from omp_knowledge.learning.record import NativeResearchRecords, research_record_block
from omp_work.v1.api_models import ResearchView
from omp_work.v1.client import WorkClient
from omp_work.v1.models import ResearchCampaign, ResearchDeliverableBinding

WORK = UUID(int=1)
FOREIGN = UUID(int=2)
NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def campaign(number: int, *, work_id: UUID = WORK, state: str = "concluded",
             reason: str | None = "Measured improvement") -> ResearchCampaign:
    return ResearchCampaign.model_validate({
        "campaign_id": UUID(int=number), "workspace_id": UUID(int=3),
        "work_id": work_id, "revision_id": UUID(int=4),
        "domain": "engineering", "state": state, "created_at": NOW,
        "outcome": "supported" if state == "concluded" else None,
        "outcome_reason": reason, "spec_sha256": "a" * 64,
        "spec": {
            "objective": "Compare candidates", "evaluation_protocol_id": "eval",
            "evaluation_protocol_sha256": "b" * 64,
            "resource_policy_ref": "bounded", "candidate_mapping_policy": "native",
        },
    })


def binding(campaign_number: int, trial_number: int, *, work_id: UUID = WORK
            ) -> ResearchDeliverableBinding:
    return ResearchDeliverableBinding(
        campaign_id=UUID(int=campaign_number), trial_id=UUID(int=trial_number),
        workspace_id=UUID(int=3), work_id=work_id, revision_id=UUID(int=4),
        native_candidate_id=UUID(int=trial_number + 1000),
        candidate_digest=f"{trial_number:064x}", binding_sha256="c" * 64,
        bound_at=NOW,
    )


def test_filters_and_associates_refs_stably_after_reordering() -> None:
    """Running/foreign evidence never leaks; each campaign gets its own sorted refs."""
    campaigns = (campaign(12), campaign(10), campaign(11, state="running"),
                 campaign(13, work_id=FOREIGN))
    bindings = (binding(12, 104), binding(10, 103), binding(10, 101),
                binding(10, 100, work_id=FOREIGN), binding(11, 105), binding(13, 106))
    view = ResearchView(work_id=WORK, campaigns=campaigns, deliverable_bindings=bindings)
    block = research_record_block(view)
    reordered = view.model_copy(update={
        "campaigns": tuple(reversed(campaigns)),
        "deliverable_bindings": tuple(reversed(bindings)),
    })
    assert research_record_block(reordered) == block
    assert block["work_id"] == str(WORK)
    assert [row["campaign_id"] for row in block["campaigns"]] == [
        str(UUID(int=10)), str(UUID(int=12)),
    ]
    assert block["campaigns"][0]["deliverable_refs"] == [
        {
            "trial_id": str(UUID(int=n)), "native_candidate_id": str(UUID(int=n + 1000)),
            "candidate_digest": f"{n:064x}", "binding_sha256": "c" * 64,
        } for n in (101, 103)
    ]
    assert [ref["trial_id"] for ref in block["campaigns"][1]["deliverable_refs"]] == [
        str(UUID(int=104)),
    ]
    assert block["campaigns"][0]["outcome"] == "supported"
    assert block["campaigns"][0]["outcome_reason"] == "Measured improvement"
    assert block["truncated"] == {"campaigns": 0}
    assert issubclass(WorkClient, NativeResearchRecords)


def test_oversize_caps_sorted_evidence_and_discloses_all_omissions() -> None:
    """Oversized projections retain first 32 campaigns/8 refs and mark clipped reasons."""
    view = ResearchView(
        work_id=WORK,
        campaigns=tuple(campaign(n, reason="x" * 6000) for n in range(50, 10, -1)),
        deliverable_bindings=tuple(
            binding(n, trial) for n in range(50, 10, -1) for trial in range(112, 100, -1)
        ),
    )
    block = research_record_block(view)
    assert len(block["campaigns"]) == 32
    assert block["truncated"] == {"campaigns": 8}
    assert [row["campaign_id"] for row in block["campaigns"]] == [
        str(UUID(int=n)) for n in range(11, 43)
    ]
    for row in block["campaigns"]:
        assert row["outcome_reason"] == "x" * 2000 + " ...[truncated]"
        assert row["truncated"] == {"deliverable_refs": 4}
        assert [ref["trial_id"] for ref in row["deliverable_refs"]] == [
            str(UUID(int=n)) for n in range(101, 109)
        ]
    assert len(json.dumps(block)) < 150_000
    assert research_record_block(view.model_copy(update={
        "campaigns": tuple(reversed(view.campaigns)),
        "deliverable_bindings": tuple(reversed(view.deliverable_bindings)),
    })) == block


def test_absent_refs_and_null_reason_remain_explicit() -> None:
    """A conclusion without bindings does not invent candidate evidence or a reason."""
    block = research_record_block(ResearchView(
        work_id=WORK, campaigns=(campaign(10, reason=None),),
    ))
    assert block["campaigns"][0]["deliverable_refs"] == []
    assert block["campaigns"][0]["outcome_reason"] is None
    assert block["campaigns"][0]["truncated"] == {"deliverable_refs": 0}
