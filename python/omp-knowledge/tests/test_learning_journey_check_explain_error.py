from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest

from omp_knowledge.learning.cli import EXIT_RUN_FAILED, main
from omp_knowledge.learning.models import Attribution, Claim, Lesson, SourceIdentity
from omp_knowledge.learning.promotion import has_journey_pass
from omp_knowledge.learning.proposals import ProposalRecord, create_proposal
from omp_knowledge.learning.store import LearningStore
from omp_work.v1.canonical import sha256
from omp_work.v1.client import WorkClient
from omp_work.v1.models import EvidenceKind, EvidenceReceipt

WORK_ID = UUID(int=588)
WORKSPACE_ID = UUID(int=589)
CAMPAIGN_ID = str(UUID(int=590))
WORK_KEY = "OMP-588"


@dataclass
class WorkItem:
    work_id: UUID = WORK_ID
    state: str = "DONE"


class FailingResearchClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def work_item(self, key: str) -> WorkItem:
        assert key == WORK_KEY
        return WorkItem(state="DONE")

    def _get(self, path: str) -> dict[str, object]:
        raise self._exc


class ConcludedResearchClient:
    def work_item(self, key: str) -> WorkItem:
        assert key == WORK_KEY
        return WorkItem(state="DONE")

    def _get(self, path: str) -> dict[str, object]:
        return {
            "work_id": str(WORK_ID),
            "campaigns": [{"campaign_id": CAMPAIGN_ID, "state": "concluded"}],
        }


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
) -> ProposalRecord:
    return create_proposal(
        store,
        unit_id=uuid4(),
        lesson=Lesson(
            title=f"Learned from campaign {CAMPAIGN_ID}",
            steps=(f"Apply research result from {CAMPAIGN_ID}",),
            claims=(Claim(text="Verification passed", receipt_ids=(str(receipt.receipt_id),)),),
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
            ),
        ),
        reader=ReceiptReader(receipt),
    )


def assert_error_run(
    store: LearningStore,
    client: Any,
    capsys: pytest.CaptureFixture[str],
    *,
    explain: bool,
    json_flag: bool,
    expected_error_type: str | None = None,
    expected_msg_fragments: list[str] | None = None,
    expected_stderr_exact: str | None = None,
    bearer_path: str = "/fake",
    post_run_cleanup: Any = None,
) -> None:
    argv = [
        "journey-check",
        "--state-dir",
        ":memory:",
        "--work-url",
        "http://fake",
        "--bearer-file",
        bearer_path,
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
    if post_run_cleanup is not None:
        post_run_cleanup()
    out, err = capsys.readouterr()

    # Exact failure JSON and EXIT_RUN_FAILED
    assert rc == EXIT_RUN_FAILED
    assert out == json.dumps({"capability": "research_to_learning", "passed": False}) + "\n"

    # No journey pass recorded
    assert has_journey_pass(store, str(WORKSPACE_ID)) is False
    assert len(store.execute("SELECT * FROM journey_passes").fetchall()) == 0

    # No traceback escaping
    assert "Traceback" not in err
    assert "Traceback" not in out

    if explain:
        # Exactly one newline-terminated stderr line
        assert err != ""
        assert err.endswith("\n")
        assert err.count("\n") == 1
        line = err[:-1]

        if expected_stderr_exact is not None:
            assert err == expected_stderr_exact

        if expected_error_type is not None:
            assert line.startswith(f"{expected_error_type}:")

        if expected_msg_fragments:
            for frag in expected_msg_fragments:
                assert frag in line
    else:
        # Same failure without --explain stays silent
        assert err == ""


@pytest.mark.parametrize("json_flag", [False, True])
@pytest.mark.parametrize("explain", [False, True])
def test_journey_check_actual_work_client_mock_transport_error(
    store, tmp_path, capsys, json_flag, explain
):
    bearer_file = tmp_path / "bearer.json"
    bearer_file.write_text(json.dumps({"token": "test-token"}))
    bearer_file.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=500,
            json={"error": {"code": "upstream_service_unavailable", "diagnostics": ["timeout"]}},
        )

    transport = httpx.MockTransport(handler)
    work_client = WorkClient(
        "http://testserver",
        WORKSPACE_ID,
        bearer_file,
        transport=transport,
    )
    try:
        assert_error_run(
            store,
            work_client,
            capsys,
            explain=explain,
            json_flag=json_flag,
            bearer_path=str(bearer_file),
            expected_error_type="WorkError",
            expected_msg_fragments=["upstream_service_unavailable"],
        )
    finally:
        work_client.close()


@pytest.mark.parametrize("json_flag", [False, True])
@pytest.mark.parametrize("explain", [False, True])
@pytest.mark.parametrize(
    "exc,expected_type,expected_fragments",
    [
        (
            ValueError("invalid research payload format"),
            "ValueError",
            ["invalid research payload format"],
        ),
        (
            KeyError("campaigns"),
            "KeyError",
            ["campaigns"],
        ),
        (
            OSError("connection reset during research fetch"),
            "OSError",
            ["connection reset during research fetch"],
        ),
        (
            RuntimeError("research subsystem terminated abnormally"),
            "RuntimeError",
            ["research subsystem terminated abnormally"],
        ),
    ],
    ids=["value_error", "key_error", "os_error", "runtime_error"],
)
def test_journey_check_injected_research_read_failures(
    store, capsys, exc, expected_type, expected_fragments, json_flag, explain
):
    client = FailingResearchClient(exc)
    assert_error_run(
        store,
        client,
        capsys,
        explain=explain,
        json_flag=json_flag,
        expected_error_type=expected_type,
        expected_msg_fragments=expected_fragments,
    )


@pytest.mark.parametrize("json_flag", [False, True])
@pytest.mark.parametrize("explain", [False, True])
def test_journey_check_multiline_value_error(store, capsys, json_flag, explain):
    client = FailingResearchClient(ValueError("first\nsecond\rthird\r\nfourth\u2028fifth"))
    assert_error_run(
        store,
        client,
        capsys,
        explain=explain,
        json_flag=json_flag,
        expected_error_type="ValueError",
        expected_msg_fragments=["first", "second", "third", "fourth", "fifth"],
        expected_stderr_exact="ValueError: first second third fourth fifth\n" if explain else None,
    )


@pytest.mark.parametrize("json_flag", [False, True])
@pytest.mark.parametrize("explain", [False, True])
def test_journey_check_ledger_read_oserror_diagnostic(
    store, receipt, monkeypatch, capsys, json_flag, explain
):
    propose(store, receipt)
    client = ConcludedResearchClient()

    @contextmanager
    def failing_transaction():
        raise OSError("ledger database disk I/O failure")
        yield  # pragma: no cover

    monkeypatch.setattr(store, "transaction", failing_transaction)

    assert_error_run(
        store,
        client,
        capsys,
        explain=explain,
        json_flag=json_flag,
        expected_error_type="OSError",
        expected_msg_fragments=["ledger database disk I/O failure"],
        expected_stderr_exact="OSError: ledger database disk I/O failure\n" if explain else None,
        post_run_cleanup=monkeypatch.undo,
    )
