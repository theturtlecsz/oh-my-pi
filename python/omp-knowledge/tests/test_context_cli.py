"""Contract tests for the fleet context compile/reproduce CLI (OMP-311-s06).

A fake ``omp tokens count`` process and a NullEngine passed as ``engine=``
drive one compile. The bundle text keeps the revision title, the current
receipt, a symbol-map line from the published fixture snapshot, and an
applicable procedure, within the token budget. The stale receipt, the
unpermitted snapshot, the unpublished snapshot, and the withdrawn procedure
are absent from the text and present in the printed exclusions and in
``context_exclusions``. The bundle row is committed before the first stdout
write. A second identical compile returns the same id and the same bytes.
``reproduce`` prints that text; a failing counter exits 2 with no stdout and
no bundle row.
"""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from omp_knowledge.context.cli import main
from omp_knowledge.learning.store import LearningStore
from omp_work.knowledge_publication import StructuralPublicationStore
from omp_work.knowledge_source import normalize_remote_url
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
from support.null_engine import NullEngine

_PAYLOAD_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_SNAP_PUBLISHED = "5dc87df1316f9afab59d47c42eed60f6a3d78286d9dc852d5b9ff827b66d4aee"
_SNAP_UNPUBLISHED = "b" * 64
_SNAP_UNPERMITTED = "c" * 64
_ORIGIN = "git@github.com:acme/widgets.git"
_TITLE = "Ship the context bundle"
_APPLICABLE_TITLE = "Apply the fixture fix"
_WITHDRAWN_TITLE = "Withdrawn guidance"
_TOKEN_BUDGET = 100_000


class RowCheckingStdout(io.TextIOBase):
    """Records stdout and reads the bundle row during the first write.

    The check is recorded rather than raised: ``main`` turns an exception from
    ``write`` into exit 2, which would hide a missing row behind the exit code.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.parts: list[str] = []
        self.writes = 0
        self.error: str | None = None
        self.bundle_id_at_first_write: str | None = None
        self.text_at_first_write: str | None = None

    def write(self, data: str) -> int:
        if data:
            self.writes += 1
            if self.writes == 1:
                self._read_row()
            self.parts.append(data)
        return len(data)

    def _read_row(self) -> None:
        if not self.db_path.exists():
            self.error = "context bundle database missing at first stdout write"
            return
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT bundle_id, bundle_text FROM context_bundles"
            ).fetchone()
        except sqlite3.Error as exc:
            self.error = str(exc)
            return
        finally:
            connection.close()
        if row is None:
            self.error = "bundle row missing at first stdout write"
            return
        self.bundle_id_at_first_write = row[0]
        self.text_at_first_write = row[1]

    def getvalue(self) -> str:
        return "".join(self.parts)


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


def _compile_argv(
    *,
    state_dir: Path,
    structural_dir: Path,
    learning_db: Path,
    counter: Path,
    cwd: Path,
    workspace_id: UUID,
    repository_id: UUID,
    other_repository_id: UUID,
) -> list[str]:
    return [
        "compile",
        "--state-dir",
        str(state_dir),
        "--token-cmd",
        json.dumps([sys.executable, str(counter)]),
        "--encoding",
        "test-words",
        "--token-budget",
        str(_TOKEN_BUDGET),
        "--structural-state-dir",
        str(structural_dir),
        "--snapshot",
        f"{workspace_id}:{repository_id}:{_SNAP_PUBLISHED}",
        "--snapshot",
        f"{workspace_id}:{repository_id}:{_SNAP_UNPUBLISHED}",
        "--snapshot",
        f"{workspace_id}:{other_repository_id}:{_SNAP_UNPERMITTED}",
        "--permit-repository",
        str(repository_id),
        "--learning-db",
        str(learning_db),
        "--engine",
        "none",
        "--json",
    ]


def _stdin(cwd: Path, view: WorkflowView) -> io.StringIO:
    payload = {
        "stage": "implement",
        "attempt_id": "attempt-1",
        "cwd": str(cwd),
        "workflow": json.loads(view.model_dump_json()),
    }
    return io.StringIO(json.dumps(payload))


def _exclusion_rows(db_path: Path, bundle_id: str) -> list[dict[str, str]]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT section, source, ref, reason, detail
            FROM context_exclusions
            WHERE bundle_id = ?
            ORDER BY ordinal
            """,
            (bundle_id,),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _has_exclusion(exclusions: list[dict[str, str]], *, reason: str, ref: str) -> bool:
    return any(row["reason"] == reason and row["ref"] == ref for row in exclusions)


def test_compile_persists_before_stdout_and_reproduces(tmp_path: Path) -> None:
    workspace_id = uuid4()
    repository_id = uuid4()
    other_repository_id = uuid4()
    project_id = uuid4()
    work_id = uuid4()
    revision_id = uuid4()
    candidate_id = uuid4()
    current = _receipt(
        work_id=work_id, revision_id=revision_id, candidate_id=candidate_id
    )
    stale = _receipt(
        work_id=work_id, revision_id=uuid4(), candidate_id=candidate_id
    )
    view = _view(
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        project_id=project_id,
        receipts=(current, stale),
    )

    repo = tmp_path / "repo"
    _git_repo(repo)
    repository = normalize_remote_url(_ORIGIN)
    structural_dir = tmp_path / "structural"
    _publish_fixture(structural_dir, workspace_id, repository_id)
    learning_db = tmp_path / "learning.sqlite"
    _seed_procedures(learning_db, project_id=project_id, repository=repository)
    state_dir = tmp_path / "state"
    counter = tmp_path / "counter.py"
    _counter_script(counter, fail=False)

    engine = NullEngine()
    engine.publish(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=_SNAP_PUBLISHED,
    )

    argv = _compile_argv(
        state_dir=state_dir,
        structural_dir=structural_dir,
        learning_db=learning_db,
        counter=counter,
        cwd=repo,
        workspace_id=workspace_id,
        repository_id=repository_id,
        other_repository_id=other_repository_id,
    )
    db_path = state_dir / "context-bundles.sqlite"
    stdout = RowCheckingStdout(db_path)
    code = main(argv, stdin=_stdin(repo, view), stdout=stdout, engine=engine)

    assert code == 0, stdout.getvalue()
    assert stdout.error is None
    assert stdout.writes == 1
    raw = stdout.getvalue()
    assert raw.endswith("\n")
    assert raw.count("\n") == 1
    first = json.loads(raw)
    assert first["bundle_id"] == stdout.bundle_id_at_first_write
    assert first["text"] == stdout.text_at_first_write
    assert first["stage"] == "implement"
    assert first["token_budget"] == _TOKEN_BUDGET
    assert isinstance(first["tokens"], int)
    assert 0 < first["tokens"] <= _TOKEN_BUDGET

    text = first["text"]
    assert _TITLE in text
    assert str(current.receipt_id) in text
    assert "symbol py/pkg/alpha.normalize" in text
    assert f"{_APPLICABLE_TITLE}: check the symbol; rerun" in text
    assert str(stale.receipt_id) not in text
    assert _SNAP_UNPUBLISHED not in text
    assert _SNAP_UNPERMITTED not in text
    assert str(other_repository_id) not in text
    assert _WITHDRAWN_TITLE not in text

    exclusions = first["exclusions"]
    assert _has_exclusion(exclusions, reason="stale", ref=str(stale.receipt_id))
    assert _has_exclusion(exclusions, reason="missing", ref=_SNAP_UNPUBLISHED)
    assert _has_exclusion(exclusions, reason="denied", ref=_SNAP_UNPERMITTED)
    assert _has_exclusion(exclusions, reason="withdrawn", ref="proc-old @v1")
    assert any(
        row["section"] == "semantic"
        and row["reason"] == "denied"
        and row["ref"] == str(other_repository_id)
        for row in exclusions
    )
    assert any(
        row["section"] == "semantic"
        and row["reason"] == "missing"
        and row["ref"] == _SNAP_UNPUBLISHED
        for row in exclusions
    )
    assert _exclusion_rows(db_path, first["bundle_id"]) == exclusions

    again = RowCheckingStdout(db_path)
    second_code = main(argv, stdin=_stdin(repo, view), stdout=again, engine=engine)
    assert second_code == 0
    second = json.loads(again.getvalue())
    assert second["bundle_id"] == first["bundle_id"]
    assert second["text"].encode() == text.encode()

    reproduced = io.StringIO()
    reproduce_code = main(
        [
            "reproduce",
            "--state-dir",
            str(state_dir),
            "--bundle-id",
            first["bundle_id"],
            "--json",
        ],
        stdout=reproduced,
    )
    assert reproduce_code == 0
    replay = json.loads(reproduced.getvalue())
    assert replay == {
        "bundle_id": first["bundle_id"],
        "bundle_sha256": first["bundle_sha256"],
        "text": text,
    }


def test_failing_counter_exits_2_without_stdout_or_row(tmp_path: Path, capsys) -> None:
    work_id = uuid4()
    revision_id = uuid4()
    candidate_id = uuid4()
    view = _view(
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        project_id=uuid4(),
        receipts=(),
    )
    repo = tmp_path / "repo"
    _git_repo(repo)
    state_dir = tmp_path / "state"
    counter = tmp_path / "counter.py"
    _counter_script(counter, fail=True)
    stdout = io.StringIO()
    code = main(
        [
            "compile",
            "--state-dir",
            str(state_dir),
            "--token-cmd",
            json.dumps([sys.executable, str(counter)]),
            "--encoding",
            "test-words",
            "--token-budget",
            str(_TOKEN_BUDGET),
            "--json",
        ],
        stdin=_stdin(repo, view),
        stdout=stdout,
        engine=NullEngine(),
    )
    captured = capsys.readouterr()

    assert code == 2
    assert stdout.getvalue() == ""
    assert captured.out == ""
    assert "counter failed" in captured.err
    db_path = state_dir / "context-bundles.sqlite"
    if db_path.exists():
        connection = sqlite3.connect(db_path)
        try:
            count = connection.execute("SELECT COUNT(*) FROM context_bundles").fetchone()[0]
        finally:
            connection.close()
        assert count == 0


def test_reproduce_missing_bundle_exits_3(tmp_path: Path, capsys) -> None:
    stdout = io.StringIO()
    code = main(
        [
            "reproduce",
            "--state-dir",
            str(tmp_path / "state"),
            "--bundle-id",
            "missing",
            "--json",
        ],
        stdout=stdout,
    )
    captured = capsys.readouterr()

    assert code == 3
    assert stdout.getvalue() == ""
    assert captured.out == ""
    assert "missing" in captured.err
