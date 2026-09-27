"""Contract tests for Fleet Knowledge state-root maintenance (OMP-312).

Each test defends one externally observable recovery contract:

- a backup taken while a writer commits to the learning store yields an
  ``exact_records.json`` whose every table digest equals the SQLite copy's,
  because both are read from one snapshot;
- a rebuild into an empty directory reproduces the recorded per-table digests
  and copies ``sources/``; a tampered record or a tampered checksum is refused
  and leaves the target untouched;
- a rollback restores the pre-change rows, keeps the replaced root aside, and
  leaves no ``-wal``/``-shm`` behind; a checksum mismatch refuses before the
  live root is touched.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from omp_knowledge.context.store import ContextBundleStore
from omp_knowledge.learning.store import LearningStore
from omp_knowledge.maintenance import (
    MaintenanceError,
    create_backup,
    rebuild_from_records,
    rollback,
)
from omp_knowledge.maintenance.cli import EXIT_OK, EXIT_REFUSED, main
from omp_knowledge.maintenance.records import (
    extract_tables,
    load_manifest,
    load_records,
)
from omp_knowledge.publication import PublicationManager
from omp_work.knowledge_publication import StructuralPublicationStore


def _seed_state_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)

    learning = LearningStore(root)
    with learning.transaction() as conn:
        conn.execute(
            "INSERT INTO capture_cursor (workspace_id, last_sequence, updated_at) "
            "VALUES ('ws-1', 7, '2026-09-27T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO procedures (procedure_id, fingerprint, status, current_version, "
            "title, created_at, updated_at) VALUES ('proc-1', 'fp-1', 'active', 1, "
            "'Fix the flake', '2026-09-27T00:00:00Z', '2026-09-27T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO procedure_versions (procedure_id, version, title, steps_json, "
            "preconditions_json, model, profile, source_json, created_at) VALUES "
            "('proc-1', 1, 'Fix the flake', '[\"run tests\"]', '[]', 'm', 'p', '{}', "
            "'2026-09-27T00:00:00Z')"
        )
    learning.close()

    ContextBundleStore(root)
    bundle = sqlite3.connect(str(root / "context-bundles.sqlite"))
    try:
        bundle.execute(
            "INSERT INTO context_bundles (bundle_id, work_id, work_key, revision_id, "
            "stage, attempt_id, candidate_id, request_json, counter_profile_json, "
            "counts_json, bundle_text, bundle_sha256, section_sha256_json, created_at) "
            "VALUES ('bundle-1', 'w-1', 'OMP-1', 'r-1', 'implement', 'a-1', 'c-1', "
            "'{}', '{}', '{}', 'text', 'sha', '{}', '2026-09-27T00:00:00Z')"
        )
        bundle.execute(
            "INSERT INTO context_exclusions (bundle_id, ordinal, section, source, ref, "
            "reason, detail) VALUES ('bundle-1', 0, 'exact', 'receipt', 'x', 'stale', 'd')"
        )
        bundle.commit()
    finally:
        bundle.close()

    StructuralPublicationStore(root)
    structural = sqlite3.connect(str(root / "structural-publications.sqlite"))
    try:
        structural.execute(
            "INSERT INTO structural_snapshots (workspace_id, repository_id, snapshot_id, "
            "namespace, state, projection_sha256, coverage_sha256, graph_sha256, "
            "projection_json, coverage_json, staged_at, published_at, updated_at) VALUES "
            "('ws', 'repo', 'snap', 'ns', 'published', 'p', 'c', 'g', '{}', '{}', "
            "'2026-09-27T00:00:00Z', '2026-09-27T00:00:00Z', '2026-09-27T00:00:00Z')"
        )
        structural.commit()
    finally:
        structural.close()

    PublicationManager(root)
    publications = sqlite3.connect(str(root / "publications.sqlite"))
    try:
        publications.execute(
            "INSERT INTO snapshot_publications (workspace_id, repository_id, snapshot_id, "
            "status, published_at, updated_at) VALUES ('ws', 'repo', 'snap', 'published', "
            "'2026-09-27T00:00:00Z', '2026-09-27T00:00:00Z')"
        )
        publications.commit()
    finally:
        publications.close()

    sources = root / "sources"
    (sources / "nested").mkdir(parents=True, exist_ok=True)
    (sources / "notes.txt").write_text("retained source notes\n", encoding="utf-8")
    (sources / "nested" / "deep.txt").write_text("nested source\n", encoding="utf-8")


def _store_digests(db_path: Path) -> dict[str, str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {name: info["sha256"] for name, info in extract_tables(conn).items()}
    finally:
        conn.close()


def _tree_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return hashes


def test_backup_while_writer_active_records_match_copy(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _seed_state_root(root)
    backup = tmp_path / "backup"

    db_path = root / "learning.sqlite"
    stop = threading.Event()
    committed = threading.Event()
    commits = [0]
    failure: list[BaseException] = []

    def writer() -> None:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            while not stop.is_set():
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE capture_cursor SET last_sequence = last_sequence + 1, "
                    "updated_at = 'write' WHERE workspace_id = 'ws-1'"
                )
                conn.execute(
                    "INSERT INTO cleanup_queue (procedure_id, action, created_at) "
                    "VALUES ('proc-1', 'cleanup', 'write')"
                )
                conn.execute("COMMIT")
                commits[0] += 1
                committed.set()
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion
            failure.append(exc)
        finally:
            conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert committed.wait(timeout=10), "the writer never committed"
        commits_before_backup = commits[0]
        create_backup(root, backup)
    finally:
        stop.set()
        thread.join()

    assert not failure, failure
    assert commits[0] > commits_before_backup, (
        "the writer must keep committing while the backup runs"
    )

    records = load_records(backup)
    manifest = load_manifest(backup)
    learning = records["stores"]["learning"]
    recorded = {name: table["sha256"] for name, table in learning["tables"].items()}

    assert recorded == _store_digests(backup / "learning.sqlite")
    assert recorded == {
        name: table["sha256"]
        for name, table in manifest["stores"]["learning"]["tables"].items()
    }
    assert learning["tables"]["capture_cursor"]["rows"]
    assert learning["tables"]["cleanup_queue"]["rows"]


def test_rebuild_from_records_reproduces_digests_and_sources(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _seed_state_root(root)
    backup = tmp_path / "backup"
    create_backup(root, backup)

    manifest = load_manifest(backup)
    target = tmp_path / "restored"
    result = rebuild_from_records(backup, target)

    assert result["target_root"] == str(target)
    assert set(result["stores"]) == {
        "learning",
        "context_bundles",
        "structural_publications",
        "publications",
    }
    for name, file_name in (
        ("learning", "learning.sqlite"),
        ("context_bundles", "context-bundles.sqlite"),
        ("structural_publications", "structural-publications.sqlite"),
        ("publications", "publications.sqlite"),
    ):
        assert _store_digests(target / file_name) == {
            table: info["sha256"]
            for table, info in manifest["stores"][name]["tables"].items()
        }

    assert (target / "sources" / "notes.txt").read_text(encoding="utf-8") == (
        "retained source notes\n"
    )
    assert (target / "sources" / "nested" / "deep.txt").read_text(encoding="utf-8") == (
        "nested source\n"
    )

    # The rebuilt learning store's rows are the exact rows, not a schema shell.
    conn = sqlite3.connect(str(target / "learning.sqlite"))
    try:
        cursor = conn.execute(
            "SELECT last_sequence FROM capture_cursor WHERE workspace_id = 'ws-1'"
        ).fetchone()
        procedures = conn.execute("SELECT COUNT(*) FROM procedures").fetchone()[0]
    finally:
        conn.close()
    assert cursor == (7,)
    assert procedures == 1


def test_rebuild_refuses_non_empty_target(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _seed_state_root(root)
    backup = tmp_path / "backup"
    create_backup(root, backup)

    target = tmp_path / "restored"
    target.mkdir()
    (target / "keep.txt").write_text("occupied", encoding="utf-8")

    with pytest.raises(MaintenanceError, match="not empty"):
        rebuild_from_records(backup, target)
    assert (target / "keep.txt").read_text(encoding="utf-8") == "occupied"


def test_rebuild_refuses_tampered_record_and_leaves_target_untouched(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    _seed_state_root(root)
    backup = tmp_path / "backup"
    create_backup(root, backup)

    records_path = backup / "exact_records.json"
    records = json.loads(records_path.read_text(encoding="utf-8"))
    rows = records["stores"]["learning"]["tables"]["capture_cursor"]["rows"]
    rows[0][1] = 999_999
    records_path.write_text(json.dumps(records), encoding="utf-8")

    # Keep the file checksum consistent with the edited records so the refusal
    # comes from the per-table digest contract, not from the file checksum.
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["exact_records.json"] = {
        "sha256": hashlib.sha256(records_path.read_bytes()).hexdigest(),
        "size": records_path.stat().st_size,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    target = tmp_path / "restored"
    with pytest.raises(MaintenanceError, match="digest mismatch"):
        rebuild_from_records(backup, target)
    assert not target.exists()


def test_rebuild_refuses_tampered_checksum(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _seed_state_root(root)
    backup = tmp_path / "backup"
    create_backup(root, backup)

    with open(backup / "learning.sqlite", "ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(MaintenanceError, match="checksum mismatch"):
        rebuild_from_records(backup, tmp_path / "restored")


def test_rollback_restores_rows_and_keeps_root_aside(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _seed_state_root(root)
    backup = tmp_path / "backup"
    create_backup(root, backup)
    original = _store_digests(root / "learning.sqlite")

    # Diverge the live root after the backup.
    conn = sqlite3.connect(str(root / "learning.sqlite"), isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO cleanup_queue (procedure_id, action, created_at) "
            "VALUES ('proc-1', 'extra', 'later')"
        )
    finally:
        conn.close()
    (root / "sources" / "notes.txt").write_text("mutated\n", encoding="utf-8")

    result = rollback(backup, root)

    assert _store_digests(root / "learning.sqlite") == original
    assert (root / "sources" / "notes.txt").read_text(encoding="utf-8") == (
        "retained source notes\n"
    )
    assert not list(root.glob("*-wal")) and not list(root.glob("*-shm"))
    conn = sqlite3.connect(str(root / "learning.sqlite"))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM cleanup_queue"
        ).fetchone()[0] == 0
    finally:
        conn.close()

    aside = Path(result["aside"])
    assert aside.exists()
    assert aside.name.startswith("state.pre-rollback-")
    aside_conn = sqlite3.connect(str(aside / "learning.sqlite"))
    try:
        assert aside_conn.execute(
            "SELECT COUNT(*) FROM cleanup_queue"
        ).fetchone()[0] == 1
    finally:
        aside_conn.close()


def test_rollback_refuses_tampered_checksum_and_leaves_state_untouched(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    _seed_state_root(root)
    backup = tmp_path / "backup"
    create_backup(root, backup)

    with open(backup / "context-bundles.sqlite", "ab") as handle:
        handle.write(b"tampered")
    before = _tree_hashes(root)

    with pytest.raises(MaintenanceError, match="checksum mismatch"):
        rollback(backup, root)

    assert _tree_hashes(root) == before
    assert not list(tmp_path.glob("state.pre-rollback-*"))


def test_cli_backup_exit_codes_and_json(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _seed_state_root(root)
    backup = tmp_path / "backup"
    out = io.StringIO()

    code = main(
        ["backup", "--state-root", str(root), "--backup-dir", str(backup), "--json"],
        stdout=out,
    )
    assert code == EXIT_OK
    payload = json.loads(out.getvalue())
    assert payload["backup_dir"] == str(backup.resolve())
    assert "learning" in payload["stores"]

    tampered = tmp_path / "broken"
    create_backup(root, tampered)
    with open(tampered / "learning.sqlite", "ab") as handle:
        handle.write(b"tampered")

    code = main(
        ["rebuild", "--backup-dir", str(tampered), "--target-root", str(tmp_path / "x")],
        stdout=io.StringIO(),
    )
    assert code == EXIT_REFUSED
