"""Integrated two-repository journey on one state root (OMP-312-s05)."""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from omp_work.knowledge_publication import (
    SnapshotInvisibleError,
    StructuralPublicationStore,
)
from omp_work.v1.models import EvidenceKind, EvidenceReceipt
from support.stub_events import FakeEvents, make_event
from support.stub_generator import StubGenerator

from omp_knowledge.inspection import build_report
from omp_knowledge.learning.capture import drain
from omp_knowledge.learning.cleanup import NativeCommittedTarget, drain_cleanup
from omp_knowledge.learning.corrections import correct
from omp_knowledge.learning.generation import GenerationResult
from omp_knowledge.learning.models import (
    Attribution,
    Claim,
    Lesson,
    Precondition,
    SourceIdentity,
)
from omp_knowledge.learning.policy import REASON_INVALID_CITATION, NativeReceipts
from omp_knowledge.learning.store import LearningStore
from omp_knowledge.learning.uses import procedure_history, record_outcome, record_use, supply
from omp_knowledge.source_import import import_source

REPO_OH_MY_PI = "ssh://git@github.com/theturtlecsz/oh-my-pi"
REPO_MEDIA_DISCOVERY = "ssh://git@github.com/theturtlecsz/media-discovery"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
HEX = "0" * 64
PAYLOAD_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
SNAP_A = "a" * 64
SNAP_B = "b" * 64


class DictReceiptReader:
    """Fake reader conforming to NativeReceipts."""

    def __init__(self, receipts: dict[str | UUID, EvidenceReceipt] | None = None) -> None:
        self._receipts: dict[str, EvidenceReceipt] = {}
        if receipts:
            for key, value in receipts.items():
                self._receipts[str(key)] = value

    def add(self, receipt: EvidenceReceipt) -> None:
        self._receipts[str(receipt.receipt_id)] = receipt

    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        key = str(receipt_id)
        if key not in self._receipts:
            raise KeyError(f"Receipt {receipt_id} not found in reader")
        return self._receipts[key]


def make_receipt(
    *,
    receipt_id: UUID | None = None,
    candidate_id: UUID | None = None,
    kind: EvidenceKind = EvidenceKind.VERIFICATION,
    verdict: str | None = "PASS",
) -> EvidenceReceipt:
    return EvidenceReceipt(
        receipt_id=receipt_id or uuid4(),
        work_id=uuid4(),
        revision_id=uuid4(),
        candidate_id=candidate_id or uuid4(),
        kind=kind,
        payload={"exit_code": 0 if verdict == "PASS" else 1},
        payload_sha256=PAYLOAD_SHA256,
        issuer="test-runner",
        issued_at=datetime.now(timezone.utc),
        verdict=verdict,
        independent=True,
    )


@dataclass(frozen=True)
class Repo:
    slug: str
    url: str
    title: str
    rejected_title: str
    steps: tuple[str, ...]
    fixture: str
    snapshot_id: str
    marker: str
    other_marker: str
    repository_id: UUID
    excluded_project: str


@dataclass
class Journey:
    repo: Repo
    unit_id: str
    procedure_id: str
    support_receipt_id: str
    use_receipt_ids: tuple[str, ...]
    outcome_receipt_id: str
    outcome_verdict: str
    correction_receipt_id: str


def _git_repo(path: Path, origin: str, filename: str, content: str) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)
    (path / filename).write_text(content)
    subprocess.run(["git", "add", filename], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        cwd=path,
        check=True,
    )
    return path


def _supplied(line: str) -> tuple[str, int]:
    head = line.split(" supply=", 1)[0]
    procedure_id, version = head.removeprefix("PROCEDURE ").split("@v", 1)
    return procedure_id, int(version)


def _evidence_ids(evidence: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for key in (
        "supporting_receipt_ids",
        "use_receipt_ids",
        "outcome_receipt_ids",
        "correction_receipt_ids",
    ):
        found.update(str(item) for item in evidence[key])
    return found


def _proposals_for_unit(report: Any, unit_id: str) -> list[dict[str, Any]]:
    return [row for row in report.acceptance["proposals"] if row["unit_id"] == unit_id]


def test_two_repository_journey(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    store = LearningStore(state_root)
    reader = DictReceiptReader()
    assert isinstance(reader, NativeReceipts)
    workspace_id = uuid4()
    repos = (
        Repo(
            slug="oh-my-pi",
            url=REPO_OH_MY_PI,
            title="Pin oh-my-pi release checks",
            rejected_title="Unknown receipt for oh-my-pi",
            steps=("read the native receipt", "rerun the release gate"),
            fixture="A",
            snapshot_id=SNAP_A,
            marker="ts/src.gammaOnlyA",
            other_marker="py/pkg/gamma.gamma_only_b",
            repository_id=uuid4(),
            excluded_project="oh-my-pi-regressed",
        ),
        Repo(
            slug="media-discovery",
            url=REPO_MEDIA_DISCOVERY,
            title="Pin media-discovery catalog checks",
            rejected_title="Unknown receipt for media-discovery",
            steps=("read the native receipt", "rerun the catalog extract"),
            fixture="B",
            snapshot_id=SNAP_B,
            marker="py/pkg/gamma.gamma_only_b",
            other_marker="ts/src.gammaOnlyA",
            repository_id=uuid4(),
            excluded_project="media-discovery-regressed",
        ),
    )
    journeys: list[Journey] = []
    sequence = 1

    for repo in repos:
        support_receipt = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="PASS")
        reader.add(support_receipt)
        unknown_receipt_id = uuid4()
        accepted_lesson = Lesson(
            title=repo.title,
            steps=repo.steps,
            preconditions=(Precondition(key="repository", op="eq", value=repo.url),),
            claims=(
                Claim(
                    text=f"{repo.slug} verification passed",
                    receipt_ids=(support_receipt.receipt_id,),
                ),
            ),
        )
        rejected_lesson = Lesson(
            title=repo.rejected_title,
            steps=(f"skip native evidence for {repo.slug}",),
            claims=(
                Claim(
                    text=f"{repo.slug} cites a receipt the ledger does not have",
                    receipt_ids=(unknown_receipt_id,),
                ),
            ),
        )
        event = make_event(
            sequence=sequence,
            event_type="complete_work",
            workspace_id=workspace_id,
        )
        sequence += 1
        generator = StubGenerator(
            [
                GenerationResult(
                    model=f"model-{repo.slug}",
                    profile=f"profile-{repo.slug}",
                    request_sha256=HEX,
                    response_sha256=HEX,
                    lessons=(accepted_lesson, rejected_lesson),
                )
            ],
            model=f"model-{repo.slug}",
            profile=f"profile-{repo.slug}",
        )

        # 1. capture
        run = drain(
            store,
            FakeEvents([event]),
            reader,
            generator,
            workspace_id=workspace_id,
        )
        assert run.status == "succeeded"
        assert run.units_failed == 0
        assert run.units_no_lesson == 0
        assert len(run.units) == 1
        unit = run.units[0]
        assert unit.state == "succeeded"
        assert unit.retryable is False
        assert unit.error_code is None

        # 2. proposal + native acceptance
        assert run.proposals_accepted == 1
        assert run.proposals_rejected == 1
        assert len(unit.proposal_ids) == 2
        report = build_report(state_root)
        proposals = _proposals_for_unit(report, unit.unit_id)
        assert {row["proposal_id"] for row in proposals} == set(unit.proposal_ids)
        accepted = next(row for row in proposals if row["status"] == "accepted")
        rejected = next(row for row in proposals if row["status"] == "rejected")
        assert accepted["reason"] is None
        assert accepted["procedure_id"]
        assert rejected["status"] == "rejected"
        assert rejected["reason"] == REASON_INVALID_CITATION
        assert rejected["procedure_id"] is None
        procedure_id = str(accepted["procedure_id"])
        procedures = {row["procedure_id"]: row for row in report.procedures}
        assert set(procedures) == {journey.procedure_id for journey in journeys} | {procedure_id}
        procedure = procedures[procedure_id]
        assert procedure["title"] == repo.title
        assert procedure["status"] == "active"
        assert procedure["current_version"] == 1
        version_one = next(row for row in procedure["versions"] if row["version"] == 1)
        assert version_one["steps"] == list(repo.steps)
        assert version_one["preconditions"] == [
            {"key": "repository", "op": "eq", "value": repo.url}
        ]
        assert repo.rejected_title not in {row["title"] for row in report.procedures}
        assert str(unknown_receipt_id) not in report.evidence[procedure_id]["supporting_receipt_ids"]
        assert str(support_receipt.receipt_id) in report.evidence[procedure_id]["supporting_receipt_ids"]

        # 3. fresh-task reuse
        fresh = supply(
            store,
            workspace_id=workspace_id,
            work_key=f"fresh-{repo.slug}",
            context={"repository": repo.url},
        )
        assert len(fresh.lines) == 1
        assert len(fresh.supply_ids) == 1
        fresh_procedure, fresh_version = _supplied(fresh.lines[0])
        assert fresh_procedure == procedure_id
        assert fresh_version == 1
        assert repo.title in fresh.lines[0]
        assert "; ".join(repo.steps) in fresh.lines[0]
        for earlier in journeys:
            assert earlier.procedure_id not in fresh.lines[0]
            assert earlier.repo.title not in fresh.lines[0]
        foreign_url = next(item.url for item in repos if item.url != repo.url)
        foreign = supply(
            store,
            workspace_id=workspace_id,
            work_key=f"foreign-{repo.slug}",
            context={"repository": foreign_url},
        )
        foreign_ids = [_supplied(line)[0] for line in foreign.lines]
        assert procedure_id not in foreign_ids
        if journeys:
            assert foreign_ids == [journeys[0].procedure_id]
            assert _supplied(foreign.lines[0])[1] == 2
        else:
            assert foreign.lines == ()
            assert foreign.supply_ids == ()

        candidate_id = uuid4()
        use_receipts = (
            make_receipt(candidate_id=candidate_id, kind=EvidenceKind.VERIFICATION, verdict="PASS"),
            make_receipt(candidate_id=candidate_id, kind=EvidenceKind.AUDIT, verdict="PASS"),
        )
        for receipt in use_receipts:
            reader.add(receipt)
        use = record_use(
            store,
            reader,
            supply_id=fresh.supply_ids[0],
            candidate_id=candidate_id,
            receipt_ids=tuple(receipt.receipt_id for receipt in use_receipts),
        )
        assert use.supply_id == fresh.supply_ids[0]
        assert use.candidate_id == str(candidate_id)
        assert use.receipt_ids == tuple(str(receipt.receipt_id) for receipt in use_receipts)

        # 4. observed outcome
        outcome_verdict = "PASS"
        outcome_receipt = make_receipt(
            candidate_id=candidate_id,
            kind=EvidenceKind.VERIFICATION,
            verdict=outcome_verdict,
        )
        reader.add(outcome_receipt)
        outcome = record_outcome(
            store,
            reader,
            use_id=use.use_id,
            receipt_id=outcome_receipt.receipt_id,
        )
        assert outcome.use_id == use.use_id
        assert outcome.candidate_id == str(candidate_id)
        assert outcome.receipt_id == str(outcome_receipt.receipt_id)
        assert outcome.verdict == outcome_verdict
        history = procedure_history(store, procedure_id=procedure_id)
        entry = next(row for row in history.entries if row.supply_id == fresh.supply_ids[0])
        assert entry.use_id == use.use_id
        assert entry.candidate_id == str(candidate_id)
        assert entry.receipt_ids == use.receipt_ids
        assert entry.outcome_id == outcome.outcome_id
        assert entry.outcome_receipt_id == outcome.receipt_id
        assert entry.verdict == outcome_verdict
        assert any(
            row.verdict == outcome_verdict and row.receipt_id == outcome.receipt_id
            for row in history.outcomes
        )

        # The excluded project still matches version 1. Narrowing is what removes it.
        still_eligible = supply(
            store,
            workspace_id=workspace_id,
            work_key=f"eligible-{repo.slug}",
            context={"repository": repo.url, "project_id": repo.excluded_project},
        )
        assert len(still_eligible.lines) == 1
        assert _supplied(still_eligible.lines[0]) == (procedure_id, 1)

        # 5. correction, cleanup, and the narrowed supply
        correction_receipt = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="NEEDS_FIX")
        reader.add(correction_receipt)
        added = Precondition(key="project_id", op="ne", value=repo.excluded_project)
        corrected = correct(
            store,
            reader,
            procedure_id=procedure_id,
            receipt_id=correction_receipt.receipt_id,
            action="narrow",
            preconditions=[added],
            attribution=Attribution(
                model=f"model-{repo.slug}",
                profile=f"profile-{repo.slug}",
                source=SourceIdentity(
                    workspace_id=str(workspace_id),
                    event_id=str(event.event_id),
                    event_sequence=event.sequence,
                    aggregate_id=str(event.aggregate_id),
                ),
            ),
        )
        assert corrected.status == "accepted"
        assert corrected.action == "narrow"
        assert corrected.reason is None
        assert corrected.from_version == 1
        assert corrected.to_version == 2
        assert corrected.preconditions == (added,)
        assert corrected.receipt_id == str(correction_receipt.receipt_id)

        cleanup = drain_cleanup(store, NativeCommittedTarget(store))
        done = [item for item in cleanup.items if item.procedure_id == procedure_id]
        assert cleanup.failed == 0
        assert len(done) == 1
        assert done[0].action == "narrow"
        assert done[0].done is True
        assert done[0].last_error is None
        follow_up = drain_cleanup(store, NativeCommittedTarget(store))
        assert all(item.procedure_id != procedure_id for item in follow_up.items)

        later = supply(
            store,
            workspace_id=workspace_id,
            work_key=f"later-{repo.slug}",
            context={"repository": repo.url},
        )
        assert len(later.lines) == 1
        assert _supplied(later.lines[0]) == (procedure_id, 2)
        assert repo.title in later.lines[0]
        for earlier in journeys:
            assert earlier.procedure_id not in later.lines[0]

        blocked = supply(
            store,
            workspace_id=workspace_id,
            work_key=f"blocked-{repo.slug}",
            context={"repository": repo.url, "project_id": repo.excluded_project},
        )
        assert blocked.lines == ()
        assert blocked.supply_ids == ()

        journeys.append(
            Journey(
                repo=repo,
                unit_id=unit.unit_id,
                procedure_id=procedure_id,
                support_receipt_id=str(support_receipt.receipt_id),
                use_receipt_ids=use.receipt_ids,
                outcome_receipt_id=outcome.receipt_id,
                outcome_verdict=outcome.verdict,
                correction_receipt_id=str(correction_receipt.receipt_id),
            )
        )

    # Supply isolation still holds once both procedures are at version 2.
    for journey in journeys:
        isolated = supply(
            store,
            workspace_id=workspace_id,
            work_key=f"isolated-{journey.repo.slug}",
            context={"repository": journey.repo.url},
        )
        assert len(isolated.lines) == 1
        assert _supplied(isolated.lines[0]) == (journey.procedure_id, 2)
        for other in journeys:
            if other.procedure_id != journey.procedure_id:
                assert other.procedure_id not in isolated.lines[0]
                assert other.repo.title not in isolated.lines[0]

    checkouts = {
        repo.slug: _git_repo(
            tmp_path / repo.slug,
            repo.url,
            f"{repo.slug}.txt",
            f"{repo.slug}\n",
        )
        for repo in repos
    }

    def _import(repo: Repo):
        return import_source(
            state_root,
            workspace_id=workspace_id,
            repository_id=repo.repository_id,
            snapshot_id=repo.snapshot_id,
            checkout=checkouts[repo.slug],
            enola_dir=FIXTURES / repo.fixture,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        imported = [future.result() for future in (pool.submit(_import, repo) for repo in repos)]

    structural = StructuralPublicationStore(state_root)
    for repo, result in zip(repos, imported, strict=True):
        assert result.publication.state == "published"
        assert result.publication.workspace_id == workspace_id
        assert result.publication.repository_id == repo.repository_id
        assert result.publication.snapshot_id == repo.snapshot_id
        assert result.publication.published_at
        own = structural.query(
            workspace_id=workspace_id,
            repository_id=repo.repository_id,
            snapshot_id=repo.snapshot_id,
            text=repo.marker,
            limit=20,
        )
        assert own.total_matched >= 1
        assert own.hits
        assert all(hit.repository_id == repo.repository_id for hit in own.hits)
        assert all(hit.snapshot_id == repo.snapshot_id for hit in own.hits)
        assert all(hit.workspace_id == workspace_id for hit in own.hits)
        assert all(repo.marker.lower() in hit.name.lower() for hit in own.hits)
        other = structural.query(
            workspace_id=workspace_id,
            repository_id=repo.repository_id,
            snapshot_id=repo.snapshot_id,
            text=repo.other_marker,
            limit=20,
        )
        assert other.total_matched == 0
        assert other.hits == ()

    with pytest.raises(SnapshotInvisibleError):
        structural.query(
            workspace_id=workspace_id,
            repository_id=repos[0].repository_id,
            snapshot_id=repos[1].snapshot_id,
            text="*",
        )
    with pytest.raises(SnapshotInvisibleError):
        structural.query(
            workspace_id=workspace_id,
            repository_id=repos[1].repository_id,
            snapshot_id=repos[0].snapshot_id,
            text="*",
        )

    report = build_report(state_root)
    snapshots = report.sources["structural_snapshots"]
    by_repository = {row["repository_id"]: row for row in snapshots}
    assert set(by_repository) == {str(repo.repository_id) for repo in repos}
    for repo in repos:
        row = by_repository[str(repo.repository_id)]
        assert row["workspace_id"] == str(workspace_id)
        assert row["snapshot_id"] == repo.snapshot_id
        assert row["state"] == "published"
        assert row["published_at"]

    units = {row["unit_id"]: row for row in report.sources["capture_units"]}
    assert set(units) == {journey.unit_id for journey in journeys}
    assert all(row["state"] == "succeeded" for row in units.values())

    procedures = {row["procedure_id"]: row for row in report.procedures}
    assert set(procedures) == {journey.procedure_id for journey in journeys}
    evidence_by_procedure = {journey.procedure_id: _evidence_ids(report.evidence[journey.procedure_id]) for journey in journeys}
    for journey in journeys:
        procedure = procedures[journey.procedure_id]
        assert procedure["current_version"] == 2
        assert procedure["status"] == "active"
        assert procedure["title"] == journey.repo.title
        versions = {row["version"]: row for row in procedure["versions"]}
        assert set(versions) == {1, 2}
        assert versions[1]["preconditions"] == [
            {"key": "repository", "op": "eq", "value": journey.repo.url}
        ]
        assert versions[1]["steps"] == list(journey.repo.steps)
        assert {"key": "repository", "op": "eq", "value": journey.repo.url} in versions[2]["preconditions"]
        assert {
            "key": "project_id",
            "op": "ne",
            "value": journey.repo.excluded_project,
        } in versions[2]["preconditions"]
        assert {
            "key": "project_id",
            "op": "ne",
            "value": journey.repo.excluded_project,
        } not in versions[1]["preconditions"]

        evidence = report.evidence[journey.procedure_id]
        assert set(evidence["supporting_receipt_ids"]) == {journey.support_receipt_id}
        assert set(evidence["use_receipt_ids"]) == set(journey.use_receipt_ids)
        assert set(evidence["outcome_receipt_ids"]) == {journey.outcome_receipt_id}
        assert set(evidence["correction_receipt_ids"]) == {journey.correction_receipt_id}
        assert any(
            row["receipt_id"] == journey.outcome_receipt_id and row["verdict"] == journey.outcome_verdict
            for row in evidence["outcomes"]
        )
        assert any(
            row["receipt_id"] == journey.correction_receipt_id
            and row["action"] == "narrow"
            and row["status"] == "accepted"
            for row in evidence["corrections"]
        )

    assert evidence_by_procedure[journeys[0].procedure_id].isdisjoint(
        evidence_by_procedure[journeys[1].procedure_id]
    )
    pinned = []
    for journey in journeys:
        current = next(
            row
            for row in procedures[journey.procedure_id]["versions"]
            if row["version"] == 2
        )
        urls = [
            item["value"]
            for item in current["preconditions"]
            if item["key"] == "repository" and item["op"] == "eq"
        ]
        assert urls == [journey.repo.url]
        pinned.append(journey.repo.url)
    assert pinned == [REPO_OH_MY_PI, REPO_MEDIA_DISCOVERY]

    accepted_rows = report.acceptance["accepted_proposals"]
    rejected_rows = report.acceptance["rejected_proposals"]
    assert {row["procedure_id"] for row in accepted_rows} == {journey.procedure_id for journey in journeys}
    assert {row["unit_id"] for row in accepted_rows} == {journey.unit_id for journey in journeys}
    assert {row["unit_id"] for row in rejected_rows} == {journey.unit_id for journey in journeys}
    assert all(row["reason"] is None for row in accepted_rows)
    assert all(row["reason"] == REASON_INVALID_CITATION for row in rejected_rows)
    assert all(row["procedure_id"] is None for row in rejected_rows)
    done_ids = {row["procedure_id"] for row in report.acceptance["cleanup_queue"]["done"]}
    pending_ids = {row["procedure_id"] for row in report.acceptance["cleanup_queue"]["pending"]}
    assert {journey.procedure_id for journey in journeys} <= done_ids
    assert pending_ids.isdisjoint({journey.procedure_id for journey in journeys})
