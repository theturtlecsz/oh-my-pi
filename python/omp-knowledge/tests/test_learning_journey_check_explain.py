from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast
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
    def __init__(
        self,
        campaigns: object = None,
        *,
        state: str = "DONE",
        include_campaigns: bool = True,
    ) -> None:
        self.campaigns = campaigns
        self.state = state
        self.include_campaigns = include_campaigns
        self.paths: list[str] = []

    def work_item(self, key: str) -> WorkItem:
        assert key == WORK_KEY
        return WorkItem(state=self.state)

    def _get(self, path: str) -> dict[str, object]:
        self.paths.append(path)
        data: dict[str, object] = {"work_id": str(WORK_ID)}
        if self.include_campaigns:
            data["campaigns"] = self.campaigns
        return data


def campaign(campaign_id: object = CAMPAIGN_ID, state: object = "concluded") -> dict[str, object]:
    res: dict[str, object] = {"outcome": "selected"}
    if campaign_id is not ...:
        res["campaign_id"] = campaign_id
    if state is not ...:
        res["state"] = state
    return res


@pytest.fixture
def store():
    with LearningStore(":memory:") as active_store:
        yield active_store


@pytest.fixture
def receipt() -> EvidenceReceipt:
    payload = {"exit_code": 0}
    return EvidenceReceipt(
        receipt_id=uuid4(),
        work_id=WORK_ID,
        revision_id=uuid4(),
        candidate_id=uuid4(),
        kind=EvidenceKind.VERIFICATION,
        payload=payload,
        payload_sha256=sha256(payload),
        issuer="test-runner",
        issued_at=datetime.now(timezone.utc),
        verdict="PASS",
        independent=True,
    )


class ReceiptReader:
    def __init__(self, receipt: EvidenceReceipt) -> None:
        self._receipt = receipt

    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        if str(receipt_id) != str(self._receipt.receipt_id):
            raise KeyError(receipt_id)
        return self._receipt


def propose(
    store: LearningStore,
    receipt: EvidenceReceipt,
    *,
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
        store,
        unit_id=uuid4(),
        lesson=Lesson(
            title=f"Learned from campaign {CAMPAIGN_ID}",
            steps=(f"Apply research result from {CAMPAIGN_ID}",),
            claims=claims,
            research_campaign_ids=citations,
        ),
        attribution=Attribution(
            model="local-model",
            profile="local",
            source=SourceIdentity(
                workspace_id=str(workspace_id),
                event_id=str(uuid4()),
                event_sequence=1,
                aggregate_id=str(aggregate_id),
                **binding,
            ),
        ),
        reader=ReceiptReader(cited),
    )


def run_and_assert(
    store: LearningStore,
    client: ResearchClient,
    capsys: pytest.CaptureFixture[str],
    *,
    passed: bool,
    explain: bool,
    json_flag: bool = False,
    expected_reason_substr: str | None = None,
) -> None:
    argv = [
        "journey-check",
        "--state-dir",
        ":memory:",
        "--work-url",
        "http://fake",
        "--bearer-file",
        "/fake",
        "--workspace",
        str(WORKSPACE_ID),
        "--work-key",
        WORK_KEY,
    ]
    if explain:
        argv.append("--explain")
    if json_flag:
        argv.append("--json")

    rc = main(argv, store=store, client=cast(Any, client))
    out, err = capsys.readouterr()

    expected_rc = EXIT_OK if passed else EXIT_RUN_FAILED
    assert rc == expected_rc
    assert out == json.dumps({"capability": "research_to_learning", "passed": passed}) + "\n"

    if passed:
        assert err == ""
        assert has_journey_pass(store, str(WORKSPACE_ID)) is True
        rows = store.execute("SELECT * FROM journey_passes").fetchall()
        assert len(rows) == 1
        assert rows[0]["workspace_id"] == str(WORKSPACE_ID)
        assert rows[0]["work_id"] == str(WORK_ID)
        assert rows[0]["work_key"] == WORK_KEY
        assert rows[0]["capability"] == "research_to_learning"
    else:
        assert has_journey_pass(store, str(WORKSPACE_ID)) is False
        assert len(store.execute("SELECT * FROM journey_passes").fetchall()) == 0
        if explain:
            assert err != ""
            assert err.endswith("\n")
            assert err.count("\n") == 1
            if expected_reason_substr is not None:
                assert expected_reason_substr in err
        else:
            assert err == ""


@pytest.mark.parametrize("json_flag", [False, True])
def test_journey_check_explain_success(store, receipt, capsys, json_flag):
    propose(store, receipt, citations=(CAMPAIGN_ID,))
    client = ResearchClient([campaign(CAMPAIGN_ID, "concluded")])
    run_and_assert(store, client, capsys, passed=True, explain=True, json_flag=json_flag)


@pytest.mark.parametrize("json_flag", [False, True])
def test_journey_check_no_flag_failure_is_silent(store, receipt, capsys, json_flag):
    client = ResearchClient([campaign(CAMPAIGN_ID, "running")])
    run_and_assert(store, client, capsys, passed=False, explain=False, json_flag=json_flag)


@pytest.mark.parametrize("json_flag", [False, True])
@pytest.mark.parametrize("state", ["IN_PROGRESS", "FAILED", "BLOCKED", "PENDING"])
def test_journey_check_explain_non_done(store, receipt, capsys, state, json_flag):
    propose(store, receipt)
    client = ResearchClient([campaign()], state=state)
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        json_flag=json_flag,
        expected_reason_substr=f"work item {WORK_KEY} is not DONE (state: {state})",
    )


@pytest.mark.parametrize("json_flag", [False, True])
def test_journey_check_explain_absent_campaigns(store, receipt, capsys, json_flag):
    propose(store, receipt)
    client = ResearchClient(include_campaigns=False)
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        json_flag=json_flag,
        expected_reason_substr=f"work item {WORK_KEY} has absent research campaigns",
    )


@pytest.mark.parametrize("json_flag", [False, True])
def test_journey_check_explain_empty_campaigns(store, receipt, capsys, json_flag):
    propose(store, receipt)
    client = ResearchClient([])
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        json_flag=json_flag,
        expected_reason_substr=f"work item {WORK_KEY} has empty research campaigns",
    )


@pytest.mark.parametrize("json_flag", [False, True])
@pytest.mark.parametrize("bad_val", ["not-a-list", 123, {"key": "val"}, None])
def test_journey_check_explain_non_list_campaigns(store, receipt, capsys, bad_val, json_flag):
    propose(store, receipt)
    client = ResearchClient(bad_val)
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        json_flag=json_flag,
        expected_reason_substr=f"campaigns for work item {WORK_KEY} is malformed (not a list)",
    )


@pytest.mark.parametrize("json_flag", [False, True])
def test_journey_check_explain_running_campaign(store, receipt, capsys, json_flag):
    propose(store, receipt)
    client = ResearchClient([campaign(CAMPAIGN_ID, "running")])
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        json_flag=json_flag,
        expected_reason_substr=f"campaign {CAMPAIGN_ID} is not concluded (state: running)",
    )


@pytest.mark.parametrize("json_flag", [False, True])
@pytest.mark.parametrize("entry", [None, 123, "not-a-dict"])
def test_journey_check_explain_malformed_entry(store, receipt, capsys, entry, json_flag):
    propose(store, receipt)
    client = ResearchClient([entry])
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        json_flag=json_flag,
        expected_reason_substr="campaign at index 0 is malformed (not a dict)",
    )


@pytest.mark.parametrize("json_flag", [False, True])
@pytest.mark.parametrize(
    "raw_campaign",
    [
        {"state": "concluded"},
        {"campaign_id": None, "state": "concluded"},
        {"campaign_id": 123, "state": "concluded"},
        {"campaign_id": "", "state": "concluded"},
        {"campaign_id": "   ", "state": "concluded"},
    ],
    ids=["missing-id", "none-id", "numeric-id", "empty-id", "blank-id"],
)
def test_journey_check_explain_malformed_id(store, receipt, capsys, raw_campaign, json_flag):
    propose(store, receipt)
    client = ResearchClient([raw_campaign])
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        json_flag=json_flag,
        expected_reason_substr="campaign at index 0 has missing or blank campaign_id",
    )


@pytest.mark.parametrize("json_flag", [False, True])
@pytest.mark.parametrize("explain", [False, True])
@pytest.mark.parametrize(
    "raw_campaign,expected_substr",
    [
        ({"campaign_id": CAMPAIGN_ID}, f"campaign {CAMPAIGN_ID} has missing or invalid state"),
        (
            {"campaign_id": CAMPAIGN_ID, "state": "bogus"},
            f"campaign {CAMPAIGN_ID} has invalid state: 'bogus'",
        ),
        (
            {"campaign_id": CAMPAIGN_ID, "state": 42},
            f"campaign {CAMPAIGN_ID} has invalid state: 42",
        ),
        (
            {"campaign_id": CAMPAIGN_ID, "state": []},
            f"campaign {CAMPAIGN_ID} has invalid state: []",
        ),
        (
            {"campaign_id": CAMPAIGN_ID, "state": {}},
            f"campaign {CAMPAIGN_ID} has invalid state: {{}}",
        ),
    ],
    ids=["missing-state", "bogus-state", "numeric-state", "list-state", "dict-state"],
)
def test_journey_check_explain_malformed_state(
    store, receipt, capsys, raw_campaign, expected_substr, json_flag, explain
):
    propose(store, receipt)
    client = ResearchClient([raw_campaign])
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=explain,
        json_flag=json_flag,
        expected_reason_substr=expected_substr,
    )


@pytest.mark.parametrize("json_flag", [False, True])
@pytest.mark.parametrize(
    "mutate",
    [
        "no-proposals",
        "foreign-workspace",
        "foreign-work",
        "inactive-procedure",
        "rejected-proposal",
        "foreign-citation",
        "uncited",
    ],
)
def test_journey_check_explain_no_matching_lesson(store, receipt, capsys, mutate, json_flag):
    if mutate == "foreign-workspace":
        propose(store, receipt, workspace_id=FOREIGN_ID)
    elif mutate == "foreign-work":
        propose(store, receipt, aggregate_id=FOREIGN_ID)
    elif mutate == "inactive-procedure":
        record = propose(store, receipt)
        with store.transaction() as conn:
            conn.execute(
                "UPDATE procedures SET status = 'withdrawn' WHERE procedure_id = ?",
                (record.procedure_id,),
            )
    elif mutate == "rejected-proposal":
        propose(store, receipt, supported=False)
    elif mutate == "foreign-citation":
        propose(store, receipt, citations=(FOREIGN_ID,))
    elif mutate == "uncited":
        propose(store, receipt, citations=())
    # mutate == "no-proposals" does not insert any proposal

    client = ResearchClient([campaign(CAMPAIGN_ID, "concluded")])
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        json_flag=json_flag,
        expected_reason_substr="no matching accepted active lesson",
    )


@pytest.mark.parametrize("json_flag", [False, True])
@pytest.mark.parametrize("blank_citation", ["", "   "])
def test_journey_check_explain_matching_plus_blank_citation_rejection(
    store, receipt, capsys, blank_citation, json_flag
):
    record = propose(store, receipt)
    payload = json.loads(record.lesson_json)
    payload["research_campaign_ids"] = [CAMPAIGN_ID, blank_citation]
    with store.transaction() as conn:
        conn.execute(
            "UPDATE proposals SET lesson_json = ? WHERE proposal_id = ?",
            (json.dumps(payload), record.proposal_id),
        )
    client = ResearchClient([campaign(CAMPAIGN_ID, "concluded")])
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        json_flag=json_flag,
        expected_reason_substr="no matching accepted active lesson",
    )


def test_simultaneous_work_not_done_beats_campaign_and_lesson(store, capsys):
    # Gate 1 (non-DONE) beats Gate 2 (empty/absent campaigns), Gate 3 (running campaign), Gate 4 (no lesson)
    client = ResearchClient([campaign(CAMPAIGN_ID, "running")], state="IN_PROGRESS")
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        expected_reason_substr=f"work item {WORK_KEY} is not DONE (state: IN_PROGRESS)",
    )


def test_simultaneous_campaigns_gate_beats_lesson(store, capsys):
    # Gate 2 (empty campaigns) beats Gate 4 (no lesson)
    client = ResearchClient([])
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        expected_reason_substr=f"work item {WORK_KEY} has empty research campaigns",
    )

    # Gate 2 (non-list campaigns) beats Gate 4 (no lesson)
    client = ResearchClient("malformed-list")
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        expected_reason_substr=f"campaigns for work item {WORK_KEY} is malformed (not a list)",
    )

    # Gate 2 (absent campaigns) beats Gate 4 (no lesson)
    client = ResearchClient(include_campaigns=False)
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        expected_reason_substr=f"work item {WORK_KEY} has absent research campaigns",
    )


def test_simultaneous_earliest_list_entry_wins(store, capsys):
    # Entry 0 running beats Entry 1 malformed
    client = ResearchClient([campaign(CAMPAIGN_ID, "running"), None])
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        expected_reason_substr=f"campaign {CAMPAIGN_ID} is not concluded (state: running)",
    )

    # Entry 0 malformed beats Entry 1 running
    client = ResearchClient([None, campaign(CAMPAIGN_ID, "running")])
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        expected_reason_substr="campaign at index 0 is malformed (not a dict)",
    )

    # Entry 0 missing id beats Entry 1 running
    client = ResearchClient([{"state": "concluded"}, campaign(CAMPAIGN_ID, "running")])
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        expected_reason_substr="campaign at index 0 has missing or blank campaign_id",
    )

    # Entry 0 concluded, Entry 1 running beats Entry 2 invalid state
    client = ResearchClient(
        [
            campaign(CAMPAIGN_ID, "concluded"),
            campaign(FOREIGN_ID, "running"),
            {"campaign_id": "other", "state": "bogus"},
        ]
    )
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        expected_reason_substr=f"campaign {FOREIGN_ID} is not concluded (state: running)",
    )


def test_simultaneous_shape_before_state_in_same_entry(store, capsys):
    # Missing id before running state
    client = ResearchClient([{"state": "running"}])
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        expected_reason_substr="campaign at index 0 has missing or blank campaign_id",
    )

    # Blank id before running state
    client = ResearchClient([{"campaign_id": "", "state": "running"}])
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        expected_reason_substr="campaign at index 0 has missing or blank campaign_id",
    )

    # Non-string id before running state
    client = ResearchClient([{"campaign_id": 999, "state": "running"}])
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        expected_reason_substr="campaign at index 0 has missing or blank campaign_id",
    )

    # Missing id before invalid state
    client = ResearchClient([{"state": "bogus"}])
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        expected_reason_substr="campaign at index 0 has missing or blank campaign_id",
    )


def test_simultaneous_campaign_failure_beats_lesson_failure(store, capsys):
    # Campaign 0 running beats no lesson in store
    client = ResearchClient([campaign(CAMPAIGN_ID, "running")])
    run_and_assert(
        store,
        client,
        capsys,
        passed=False,
        explain=True,
        expected_reason_substr=f"campaign {CAMPAIGN_ID} is not concluded (state: running)",
    )
