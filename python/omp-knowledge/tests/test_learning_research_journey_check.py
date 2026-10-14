from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from omp_knowledge.learning.cli import EXIT_OK, EXIT_RUN_FAILED, main
from omp_knowledge.learning.models import Attribution, Claim, Lesson, SourceIdentity, SourceSpan
from omp_knowledge.learning.promotion import has_journey_pass
from omp_knowledge.learning.proposals import ProposalRecord, create_proposal
from omp_knowledge.learning.store import LearningStore
from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.models import EvidenceKind, EvidenceReceipt

WORK_ID = UUID(int=588)
WORKSPACE_ID = UUID(int=589)
CAMPAIGN_ID = str(UUID(int=590))
FOREIGN_ID = str(UUID(int=591))
WORK_KEY = "OMP-588"


@dataclass
class WorkItem:
    work_id: UUID = WORK_ID
    state: str = "DONE"


class ResearchClient:
    def __init__(self, campaigns: list[object], *, state: str = "DONE") -> None:
        self.campaigns = campaigns
        self.state = state
        self.paths: list[str] = []

    def work_item(self, key: str) -> WorkItem:
        assert key == WORK_KEY
        return WorkItem(state=self.state)

    def _get(self, path: str) -> dict[str, object]:
        self.paths.append(path)
        return {"work_id": str(WORK_ID), "campaigns": self.campaigns}


def campaign(campaign_id: str = CAMPAIGN_ID, state: str = "concluded") -> dict[str, object]:
    return {"campaign_id": campaign_id, "state": state, "outcome": "selected"}


@pytest.fixture
def store():
    with LearningStore(":memory:") as active_store:
        yield active_store


@pytest.fixture
def receipt() -> EvidenceReceipt:
    payload = {"exit_code": 0}
    return EvidenceReceipt(
        receipt_id=uuid4(), work_id=WORK_ID, revision_id=uuid4(), candidate_id=uuid4(),
        kind=EvidenceKind.VERIFICATION, payload=payload, payload_sha256=sha256(payload),
        issuer="test-runner", issued_at=datetime.now(timezone.utc), verdict="PASS", independent=True,
    )


class ReceiptReader:
    def __init__(self, receipt: EvidenceReceipt) -> None:
        self._receipt = receipt

    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        if str(receipt_id) != str(self._receipt.receipt_id):
            raise KeyError(receipt_id)
        return self._receipt


def propose(
    store: LearningStore, receipt: EvidenceReceipt, *,
    citations: tuple[str, ...] = (CAMPAIGN_ID,),
    aggregate_id: UUID | str = WORK_ID,
    workspace_id: UUID | str = WORKSPACE_ID,
    supported: bool = True,
) -> ProposalRecord:
    # Journey matching reads aggregate_id, including a caller-supplied FOREIGN_ID.
    claims: tuple[Claim, ...] = ()
    cited = receipt
    binding: dict[str, str] = {}
    if supported:
        text = "Verification passed"
        payload = {**receipt.payload, "text": text}
        encoded = canonical_json(payload)
        start = encoded.index(text)
        if receipt.candidate_id is None:
            raise AssertionError("verification receipt is missing candidate_id")
        cited = receipt.model_copy(
            update={
                "work_id": receipt.work_id,
                "candidate_id": receipt.candidate_id,
                "revision_id": receipt.revision_id,
                "payload": payload,
                "payload_sha256": sha256(payload),
            }
        )
        claims = (
            Claim(
                text=text,
                receipt_ids=(str(cited.receipt_id),),
                source_span=SourceSpan(start=start, end=start + len(text)),
                evaluator=cited.issuer,
                outcome="PASS",
            ),
        )
        binding = {
            "work_id": str(cited.work_id),
            "candidate_id": str(cited.candidate_id),
            "revision_id": str(cited.revision_id),
        }
    return create_proposal(
        store, unit_id=uuid4(),
        lesson=Lesson(
            title=f"Learned from campaign {CAMPAIGN_ID}",
            steps=(f"Apply research result from {CAMPAIGN_ID}",),
            claims=claims,
            research_campaign_ids=citations,
        ),
        attribution=Attribution(
            model="local-model", profile="local",
            source=SourceIdentity(
                workspace_id=workspace_id, event_id=uuid4(), event_sequence=1,
                aggregate_id=aggregate_id,
                **binding,
            ),
        ),
        reader=ReceiptReader(cited),
    )


def check(
    store: LearningStore, client: ResearchClient, capsys: pytest.CaptureFixture[str], *,
    passed: bool = False, json_flag: bool = False,
) -> None:
    rc = main(
        [
            "journey-check", "--state-dir", ":memory:", "--work-url", "http://fake",
            "--bearer-file", "/fake", "--workspace", str(WORKSPACE_ID),
            "--work-key", WORK_KEY,
        ] + (["--json"] if json_flag else []),
        store=store, client=client,
    )
    assert rc == (EXIT_OK if passed else EXIT_RUN_FAILED)
    output = capsys.readouterr()
    assert output.out == json.dumps({"capability": "research_to_learning", "passed": passed}) + "\n"
    assert output.err == ""
    assert has_journey_pass(store, str(WORKSPACE_ID)) is passed
    rows = store.execute("SELECT * FROM journey_passes").fetchall()
    assert len(rows) == int(passed)
    if passed:
        assert rows[0]["workspace_id"] == str(WORKSPACE_ID)
        assert rows[0]["work_id"] == str(WORK_ID)
        assert rows[0]["work_key"] == WORK_KEY
        assert rows[0]["capability"] == "research_to_learning"
    assert client.paths == ([f"/v1/work-items/{WORK_KEY}/research"] if client.state == "DONE" else [])


@pytest.mark.parametrize("json_flag", [False, True])
def test_accepted_active_lesson_with_matching_citation_records_pass(store, receipt, capsys, json_flag):
    record = propose(store, receipt, citations=(str(UUID(int=592)), CAMPAIGN_ID))
    assert record.status == "accepted"
    check(store, ResearchClient([campaign(FOREIGN_ID), campaign()]), capsys, passed=True, json_flag=json_flag)


@pytest.mark.parametrize(
    "changes",
    [
        {"citations": ()},
        {"citations": (FOREIGN_ID,)},
        {"citations": (f"prefix-{CAMPAIGN_ID}",)},
        {"citations": (f" {CAMPAIGN_ID} ",)},
        {"workspace_id": FOREIGN_ID},
        {"aggregate_id": FOREIGN_ID},
        {"supported": False},
    ],
    ids=["uncited-prose", "foreign-campaign", "substring", "padded-id", "foreign-workspace", "foreign-work", "rejected"],
)
def test_unrelated_or_unadmitted_lesson_cannot_record_pass(store, receipt, capsys, changes):
    propose(store, receipt, **changes)
    check(store, ResearchClient([campaign()]), capsys)


def test_source_and_citation_must_belong_to_same_proposal(store, receipt, capsys):
    uncited = propose(store, receipt, citations=())
    foreign = propose(store, receipt, workspace_id=FOREIGN_ID)
    assert uncited.procedure_id == foreign.procedure_id
    check(store, ResearchClient([campaign()]), capsys)


def test_inactive_procedure_cannot_record_pass(store, receipt, capsys):
    record = propose(store, receipt)
    with store.transaction() as conn:
        conn.execute("UPDATE procedures SET status = 'withdrawn' WHERE procedure_id = ?", (record.procedure_id,))
    check(store, ResearchClient([campaign()]), capsys)


def test_rejected_proposal_attached_to_active_procedure_cannot_record_pass(store, receipt, capsys):
    active = propose(store, receipt, citations=())
    rejected = propose(store, receipt, supported=False)
    assert rejected.status == "rejected"
    with store.transaction() as conn:
        conn.execute("UPDATE proposals SET procedure_id = ? WHERE proposal_id = ?", (active.procedure_id, rejected.proposal_id))
    check(store, ResearchClient([campaign()]), capsys)


@pytest.mark.parametrize(
    "campaigns",
    [
        [], [campaign(state="running")], [campaign(), campaign(FOREIGN_ID, "running")],
        [{"state": "concluded"}], [campaign(), {"state": "concluded"}],
        [campaign("")], [campaign("   ")], [campaign(None)], [campaign(588)], [None],
        [{"state": "concluded", "outcome_reason": CAMPAIGN_ID}],
    ],
    ids=["empty", "running", "mixed", "missing-id", "one-missing-id", "empty-id", "blank-id", "null-id", "numeric-id", "non-object", "prose-id"],
)
def test_invalid_or_unfinished_campaigns_cannot_record_pass(store, receipt, capsys, campaigns):
    propose(store, receipt)
    check(store, ResearchClient(campaigns), capsys)


def test_non_done_work_cannot_record_pass(store, receipt, capsys):
    propose(store, receipt)
    check(store, ResearchClient([campaign()], state="IN_PROGRESS"), capsys)


@pytest.mark.parametrize(
    "field,value",
    [
        ("lesson_json", "{"), ("lesson_json", "{}"), ("lesson_json", "null"),
        ("source_json", "{"), ("source_json", "[]"),
    ],
    ids=["broken-lesson-json", "missing-lesson-fields", "null-lesson", "broken-source-json", "invalid-source"],
)
def test_malformed_persisted_proposal_cannot_record_pass(store, receipt, capsys, field, value):
    record = propose(store, receipt)
    with store.transaction() as conn:
        conn.execute(f"UPDATE proposals SET {field} = ? WHERE proposal_id = ?", (value, record.proposal_id))
    check(store, ResearchClient([campaign()]), capsys)


@pytest.mark.parametrize("citations", [None, CAMPAIGN_ID, [588], [{}], [CAMPAIGN_ID, ""], [CAMPAIGN_ID, "  "]])
def test_malformed_structured_citations_cannot_record_pass(store, receipt, capsys, citations):
    record = propose(store, receipt)
    payload = json.loads(record.lesson_json)
    payload["research_campaign_ids"] = citations
    with store.transaction() as conn:
        conn.execute("UPDATE proposals SET lesson_json = ? WHERE proposal_id = ?", (json.dumps(payload), record.proposal_id))
    check(store, ResearchClient([campaign()]), capsys)


def test_legacy_lesson_cannot_infer_campaign_from_prose(store, receipt, capsys):
    record = propose(store, receipt)
    payload = json.loads(record.lesson_json)
    del payload["research_campaign_ids"]
    with store.transaction() as conn:
        conn.execute("UPDATE proposals SET lesson_json = ? WHERE proposal_id = ?", (json.dumps(payload), record.proposal_id))
    check(store, ResearchClient([campaign()]), capsys)
