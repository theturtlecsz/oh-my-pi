from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from omp_work.v1.api_models import WorkItemsPage, WorkItemSummary
from omp_work.v1.canonical import canonical_json
from omp_work.v1.models import EvidenceReceipt
from support.stub_events import FakeEvents, make_event
from support.stub_generator import StubGenerator

from omp_knowledge.learning.capture import drain
from omp_knowledge.learning.cli import EXIT_OK, main as cli_main
from omp_knowledge.learning.generation import (
    GenerationResult,
    LocalChatGenerator,
    ensure_repository_precondition,
    extract_trace_repository,
)
from omp_knowledge.learning.models import (
    Claim,
    Lesson,
    Precondition,
    SourceIdentity,
    SourceSpan,
)
from omp_knowledge.learning.store import LearningStore
from omp_knowledge.learning.uses import supply

LOOPBACK_BASE_URL = "http://127.0.0.1:18080"
REPOSITORY = "ssh://git@github.com/org/repo"


class RepositoryRecords:
    """Mapping-backed workflow and reader-known supporting receipt."""

    def __init__(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "workflows" / "OMP-175.json"
        payload = json.loads(fixture.read_text())
        self.key = payload["key"]
        view = payload["workflow"]
        for attempt in view["close_attempts"]:
            attempt["repository"] = REPOSITORY
        self.view = MappingProxyType(view)
        self.evidence = EvidenceReceipt.model_validate(next(
            receipt for receipt in view["receipts"]
            if receipt["kind"] == "audit" and receipt["verdict"] == "PASS"
        ))

    def work_items(
        self,
        *,
        after: tuple[datetime, UUID] | None = None,
        limit: int | None = None,
    ) -> WorkItemsPage:
        del limit
        if after is not None:
            return WorkItemsPage(items=(), next_created_at=None, next_work_id=None)
        item = self.view["item"]
        return WorkItemsPage(
            items=(WorkItemSummary(
                work_id=item["work_id"],
                key=self.key,
                state=item["state"],
                created_at=item["revision"]["created_at"],
            ),),
            next_created_at=None,
            next_work_id=None,
        )

    def workflow(self, key: str) -> Mapping[str, Any]:
        assert key == self.key
        return self.view

    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        if str(receipt_id) != str(self.evidence.receipt_id):
            raise KeyError(receipt_id)
        return self.evidence


@pytest.fixture
def drained_store() -> Iterator[LearningStore]:
    records = RepositoryRecords()
    event = make_event(
        sequence=1,
        aggregate_id=UUID(records.view["item"]["work_id"]),
        payload={"work_id": records.view["item"]["work_id"]},
    )
    payload_text = canonical_json(records.evidence.payload)
    start, end = 0, 32
    claim_text = payload_text[start:end]
    source_span = SourceSpan(start=start, end=end)
    lesson = Lesson(
        title="Check candidate drift before closeout",
        steps=("compare candidate commit against HEAD",),
        claims=(Claim(
            text=claim_text,
            receipt_ids=(records.evidence.receipt_id,),
            source_span=source_span,
            evaluator=records.evidence.issuer,
            outcome="PASS",
        ),),
    )
    generator = StubGenerator([GenerationResult(
        model="stub-model",
        profile="stub-profile",
        request_sha256="0" * 64,
        response_sha256="0" * 64,
        lessons=(lesson,),
    )])
    with LearningStore(":memory:") as store:
        run = drain(
            store, FakeEvents([event]), records, generator,
            workspace_id=event.workspace_id, records=records,
        )
        assert run.proposals_accepted == 1

        unit_row = store.execute("SELECT trace_json, source_json FROM units").fetchone()
        assert unit_row is not None
        stored_work_record = json.loads(unit_row["trace_json"])["work_record"]
        assert stored_work_record["candidate"]["candidate_id"] == str(records.evidence.candidate_id)
        assert stored_work_record["revision"]["revision_id"] == str(records.evidence.revision_id)
        source = SourceIdentity.model_validate_json(unit_row["source_json"])
        assert source.candidate_id == str(records.evidence.candidate_id)
        assert source.revision_id == str(records.evidence.revision_id)

        proposal_row = store.execute("SELECT source_json, lesson_json FROM proposals WHERE status = 'accepted'").fetchone()
        assert proposal_row is not None
        prop_source = SourceIdentity.model_validate_json(proposal_row["source_json"])
        assert prop_source.candidate_id == str(records.evidence.candidate_id)
        assert prop_source.revision_id == str(records.evidence.revision_id)
        prop_lesson = Lesson.model_validate_json(proposal_row["lesson_json"])
        assert prop_lesson.claims[0].source_span == source_span
        assert canonical_json(records.evidence.payload)[prop_lesson.claims[0].source_span.start:prop_lesson.claims[0].source_span.end] == prop_lesson.claims[0].text
        assert prop_lesson.claims[0].evaluator == records.evidence.issuer
        assert prop_lesson.claims[0].outcome == "PASS"

        yield store


def test_drain_scopes_unscoped_generator_lesson_before_storage_and_supply(
    drained_store: LearningStore,
) -> None:
    """Drained lessons persist repository eq and supply only to that repository."""
    proposal = drained_store.execute("SELECT lesson_json FROM proposals").fetchone()
    version = drained_store.execute(
        "SELECT preconditions_json FROM procedure_versions"
    ).fetchone()
    expected = [{"key": "repository", "op": "eq", "value": REPOSITORY}]
    assert json.loads(proposal["lesson_json"])["preconditions"] == expected
    assert json.loads(version["preconditions_json"]) == expected

    unit_row = drained_store.execute("SELECT trace_json, source_json FROM units").fetchone()
    assert unit_row is not None
    stored_work_record = json.loads(unit_row["trace_json"])["work_record"]
    source = SourceIdentity.model_validate_json(unit_row["source_json"])
    assert stored_work_record["candidate"]["candidate_id"] == source.candidate_id
    assert stored_work_record["revision"]["revision_id"] == source.revision_id
    prop_source = SourceIdentity.model_validate_json(drained_store.execute("SELECT source_json FROM proposals").fetchone()["source_json"])
    assert prop_source.candidate_id == source.candidate_id
    assert prop_source.revision_id == source.revision_id

    for context, count in (
        ({"repository": REPOSITORY}, 1),
        ({"repository": "ssh://git@github.com/other/repo"}, 0),
        ({}, 0),
    ):
        result = supply(
            drained_store, workspace_id=uuid4(), work_key="next-work",
            context=context,
        )
        assert len(result.lines) == count


def test_cli_supply_cwd_excludes_drained_lesson_from_another_repository(
    drained_store: LearningStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI remote-derived context cannot supply a different repository's lesson."""
    git_dir = tmp_path / "repo"
    git_dir.mkdir()
    subprocess.run(["git", "init"], cwd=git_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:other/repo.git"],
        cwd=git_dir,
        check=True,
        capture_output=True,
    )
    rc = cli_main([
        "supply", "--state-dir", ":memory:", "--workspace", str(uuid4()),
        "--work-key", "next-work", "--cwd", str(git_dir), "--json",
    ], store=drained_store)

    assert rc == EXIT_OK
    assert json.loads(capsys.readouterr().out) == []


def _receipt(repository: str, *, truncated: bool = False, raw: str | None = None) -> dict[str, Any]:
    return {
        "payload_truncated": truncated,
        "payload_text": raw
        if raw is not None
        else json.dumps({"delivery": {"repository": repository}}),
    }


def _chat(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
        },
    )


def _generate(trace: dict[str, Any], content: str):
    generator = LocalChatGenerator(
        LOOPBACK_BASE_URL,
        model="qwen3.8-27b-q5",
        profile="local-qwen",
        transport=httpx.MockTransport(lambda request: _chat(content)),
    )
    try:
        return generator.generate(trace)
    finally:
        generator.close()


def _envelope(lesson: dict[str, Any]) -> str:
    return json.dumps({"lessons": [lesson], "no_lesson_reason": None})


@pytest.mark.parametrize(
    ("trace", "expected"),
    [
        (
            {"payload": {"evidence": {"delivery": {"repository": " org/payload "}}}},
            "org/payload",
        ),
        (
            {
                "work_record": {
                    "close_attempts": [
                        {"repository": " "},
                        {"repository": " org/attempt "},
                    ]
                }
            },
            "org/attempt",
        ),
        (
            {"work_record": {"receipts": [_receipt(" org/receipt ")]}},
            "org/receipt",
        ),
    ],
)
def test_extract_trace_repository_reads_each_shape(trace: dict[str, Any], expected: str) -> None:
    """Each naming shape yields its stripped repository."""
    assert extract_trace_repository(trace) == expected


def test_extract_trace_repository_first_match_wins() -> None:
    """Payload beats close attempts, which beat receipts; blanks are not matches."""
    trace: dict[str, Any] = {
        "repository": "org/top",
        "payload": {"evidence": {"delivery": {"repository": "org/payload"}}},
        "work_record": {
            "repository": "org/record",
            "close_attempts": [
                {"repository": "org/attempt-1"},
                {"repository": "org/attempt-2"},
            ],
            "receipts": [_receipt("org/receipt")],
        },
    }
    assert extract_trace_repository(trace) == "org/payload"

    trace["payload"]["evidence"]["delivery"]["repository"] = "   "
    assert extract_trace_repository(trace) == "org/attempt-1"

    trace["work_record"]["close_attempts"] = [
        None,
        {"repository": ""},
        {"repository": "org/attempt-2"},
    ]
    assert extract_trace_repository(trace) == "org/attempt-2"

    trace["work_record"]["close_attempts"] = [{"repository": None}, {"repository": "  "}]
    assert extract_trace_repository(trace) == "org/receipt"


def test_extract_trace_repository_skips_truncated_and_unparseable_receipts() -> None:
    """A later parseable receipt is used when earlier receipts cannot name a repository."""
    trace = {
        "work_record": {
            "receipts": [
                _receipt("org/truncated", truncated=True),
                _receipt("org/bad", raw="{not-json"),
                _receipt("org/array", raw=json.dumps(["org/array"])),
                {
                    "payload_truncated": False,
                    "payload_text": json.dumps({"delivery": {"repository": "   "}}),
                },
                _receipt("org/kept"),
            ]
        }
    }
    assert extract_trace_repository(trace) == "org/kept"


@pytest.mark.parametrize(
    "trace",
    [
        {},
        {"work_record": None},
        {"payload": {}, "work_record": {}},
        {"repository": "org/top"},
        {"work_record": {"repository": "org/record", "close_attempts": [], "receipts": []}},
        {"payload": {"evidence": {"delivery": {"repository": "  "}}}},
        {"work_record": {"close_attempts": [{"repository": ""}, {}]}},
        {
            "work_record": {
                "receipts": [
                    _receipt("org/truncated", truncated=True),
                    _receipt("org/bad", raw="not json"),
                ]
            }
        },
    ],
)
def test_extract_trace_repository_none_when_unnamed(trace: dict[str, Any]) -> None:
    """A trace that never names a repository through the three shapes yields None."""
    assert extract_trace_repository(trace) is None


def test_ensure_repository_precondition_replaces_repository_axis() -> None:
    """eq R is added, model repository eq and ne are dropped, and project_id stays."""
    lesson = Lesson(
        title="Restart the worker",
        steps=("stop the worker",),
        preconditions=(
            Precondition(key="project_id", op="eq", value="proj-1"),
            Precondition(key="repository", op="eq", value="org/wrong"),
            Precondition(key="project_id", op="ne", value="proj-other"),
            Precondition(key="repository", op="ne", value="org/also-wrong"),
        ),
    )
    updated = ensure_repository_precondition(lesson, "org/repo")

    assert updated.preconditions == (
        Precondition(key="project_id", op="eq", value="proj-1"),
        Precondition(key="project_id", op="ne", value="proj-other"),
        Precondition(key="repository", op="eq", value="org/repo"),
    )
    assert updated.title == lesson.title
    assert lesson.preconditions[1].value == "org/wrong"

    bare = Lesson(title="t", steps=("s",))
    assert ensure_repository_precondition(bare, "org/repo").preconditions == (
        Precondition(key="repository", op="eq", value="org/repo"),
    )


def test_generate_stamps_repository_from_close_attempt() -> None:
    """A model lesson with no preconditions gains eq for the trace repository."""
    trace = {"work_record": {"close_attempts": [{"repository": "org/repo"}]}}
    result = _generate(trace, _envelope({"title": "Restart the worker", "steps": ["stop the worker"]}))

    assert result.lessons[0].preconditions == (
        Precondition(key="repository", op="eq", value="org/repo"),
    )


def test_generate_replaces_wrong_model_repository() -> None:
    """A model repository eq is replaced by the repository the trace names."""
    trace = {"work_record": {"close_attempts": [{"repository": "org/repo"}]}}
    content = _envelope(
        {
            "title": "Restart the worker",
            "steps": ["stop the worker"],
            "preconditions": [{"key": "repository", "op": "eq", "value": "org/wrong"}],
        }
    )
    result = _generate(trace, content)

    assert result.lessons[0].preconditions == (
        Precondition(key="repository", op="eq", value="org/repo"),
    )


def test_generate_leaves_preconditions_when_trace_names_no_repository() -> None:
    """Lessons pass through unchanged when the trace names no repository."""
    content = _envelope(
        {
            "title": "Restart the worker",
            "steps": ["stop the worker"],
            "preconditions": [
                {"key": "project_id", "op": "eq", "value": "proj-1"},
                {"key": "repository", "op": "eq", "value": "org/model"},
            ],
        }
    )
    result = _generate({"unit_id": "u-1"}, content)

    assert result.lessons[0].preconditions == (
        Precondition(key="project_id", op="eq", value="proj-1"),
        Precondition(key="repository", op="eq", value="org/model"),
    )
