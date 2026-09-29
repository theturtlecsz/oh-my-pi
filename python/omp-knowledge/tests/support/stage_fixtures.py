"""Stage-compiler fixtures copied from test_context_cli.py.

The counter script, git origin, workflow view, published Enola snapshot and
learning-store seed match that file so a stage compile can reuse the same
inputs the CLI contract already drives.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from omp_knowledge.learning.store import LearningStore
from omp_work.knowledge_publication import StructuralPublicationStore
from omp_work.v1.api_models import WorkflowView, WorkItemView
from omp_work.v1.canonical import sha256
from omp_work.v1.models import (
    Candidate,
    EvidenceKind,
    EvidenceReceipt,
    WorkAlias,
    WorkRevision,
)
from support.fixtures import load_staged_fixture

_PAYLOAD_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_SNAP_PUBLISHED = "5dc87df1316f9afab59d47c42eed60f6a3d78286d9dc852d5b9ff827b66d4aee"
_SNAP_UNPUBLISHED = "b" * 64
_SNAP_UNPERMITTED = "c" * 64
_ORIGIN = "git@github.com:acme/widgets.git"
_TITLE = "Ship the context bundle"
_APPLICABLE_TITLE = "Apply the fixture fix"
_WITHDRAWN_TITLE = "Withdrawn guidance"
_TOKEN_BUDGET = 100_000


def _counter_script(path: Path, *, fail: bool) -> None:
    if fail:
        path.write_text(
            "import sys\nsys.stderr.write('counter failed\\n')\nraise SystemExit(1)\n",
            encoding="utf-8",
        )
        return
    path.write_text(
        """\
import json
import sys

args = sys.argv[1:]
if len(args) < 3 or args[0] != "count" or args[1] != "--encoding":
    sys.stderr.write("expected: count --encoding <name>\\n")
    raise SystemExit(1)
encoding = args[2]
sys.stdout.write(
    json.dumps({"api": "countTokens", "encoding": encoding}, separators=(",", ":")) + "\\n"
)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    item = json.loads(line)
    count = len(str(item["text"]).split())
    sys.stdout.write(
        json.dumps({"id": item["id"], "tokens": count}, separators=(",", ":")) + "\\n"
    )
""",
        encoding="utf-8",
    )


def _git_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", _ORIGIN], cwd=path, check=True)


def _revision(work_id: UUID, revision_id: UUID) -> WorkRevision:
    return WorkRevision(
        revision_id=revision_id,
        work_id=work_id,
        revision_number=1,
        title=_TITLE,
        description="compile the fleet context",
        scope="python/omp-knowledge",
        acceptance_criteria=("title is present", "budget holds"),
        content_sha256="a" * 64,
        created_by="tester",
        created_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
    )


def _receipt(
    *,
    work_id: UUID,
    revision_id: UUID,
    candidate_id: UUID | None,
) -> EvidenceReceipt:
    return EvidenceReceipt(
        receipt_id=uuid4(),
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        kind=EvidenceKind.VERIFICATION,
        payload={"exit_code": 0},
        payload_sha256=_PAYLOAD_SHA,
        issuer="tester",
        issued_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
        verdict="PASS",
    )


def _view(
    *,
    work_id: UUID,
    revision_id: UUID,
    candidate_id: UUID,
    project_id: UUID,
    receipts: tuple[EvidenceReceipt, ...],
) -> WorkflowView:
    item = WorkItemView(
        work_id=work_id,
        workspace_id=uuid4(),
        alias=WorkAlias(work_id=work_id, key="OMP-311", primary=True, origin="local"),
        state="in_progress",
        revision=_revision(work_id, revision_id),
        candidate=Candidate(
            candidate_id=candidate_id,
            work_id=work_id,
            revision_id=revision_id,
            candidate_sha256="b" * 64,
            allocated_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
        ),
        project_id=project_id,
    )
    return WorkflowView(item=item, receipts=receipts)


def _publish_fixture(state_dir: Path, workspace_id: UUID, repository_id: UUID) -> None:
    facts_bytes, receipt_bytes, _insights, _raw = load_staged_fixture("A")
    store = StructuralPublicationStore(state_dir)
    store.stage_enola_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=_SNAP_PUBLISHED,
        facts_bytes=facts_bytes,
        receipt_bytes=receipt_bytes,
        manifest_files=[],
    )
    store.publish(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=_SNAP_PUBLISHED,
    )


def _seed_procedures(db_path: Path, *, project_id: UUID, repository: str) -> None:
    preconditions = json.dumps(
        [
            {"key": "project_id", "op": "eq", "value": str(project_id)},
            {"key": "repository", "op": "eq", "value": repository},
        ]
    )
    store = LearningStore(db_path)
    with store.transaction() as conn:
        for procedure_id, title, steps, status in (
            ("proc-apply", _APPLICABLE_TITLE, ["check the symbol", "rerun"], "active"),
            ("proc-old", _WITHDRAWN_TITLE, ["do not follow"], "withdrawn"),
        ):
            conn.execute(
                """
                INSERT INTO procedures (
                    procedure_id, fingerprint, status, current_version, title, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, '2026-09-26T00:00:00Z', '2026-09-26T00:00:00Z')
                """,
                (procedure_id, sha256([procedure_id]), status, title),
            )
            conn.execute(
                """
                INSERT INTO procedure_versions (
                    procedure_id, version, title, steps_json, preconditions_json,
                    model, profile, source_json, created_at
                ) VALUES (?, 1, ?, ?, ?, 'm', 'p', '{}', '2026-09-26T00:00:00Z')
                """,
                (procedure_id, title, json.dumps(steps), preconditions),
            )
    store.close()
