"""Staged publication and retained active snapshots for structural code graphs.

Publication is a two-phase operation. Ingest (``stage``) writes the projected
snapshot and its coverage manifest into durable state with ``state='staged'``;
``publish`` switches that one row to ``state='published'``. A query reads only
``published`` snapshots, so a staged, aborted or crashed ingest is never
visible and the previous published snapshot keeps answering queries unchanged.
Because the namespace includes the snapshot id, two published candidates of one
repository coexist without either rewriting the other's query results.

State lives in one SQLite file under the state directory. There is no engine,
database server or model dependency here: an alternative engine consumes the
same staged payload and can use this store for the candidate-isolation
contract.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import Field

from .knowledge_contracts import ManifestFile
from .knowledge_structural import (
    CoverageManifest,
    StructuralProjection,
    build_coverage_manifest,
    parse_enola_receipt,
    project_structural_snapshot,
    validate_projection_version,
)
from .v1.canonical import canonical_json, sha256
from .v1.models import StrictModel

_STATE_STAGED = "staged"
_STATE_PUBLISHED = "published"
_STATE_ABORTED = "aborted"
_STATE_RETIRED = "retired"


class PublicationStoreError(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


class SnapshotInvisibleError(PublicationStoreError):
    """A query named a snapshot that is not currently published."""

    def __init__(self, snapshot_id: str) -> None:
        super().__init__(
            "snapshot_not_visible",
            f"snapshot_not_visible: {snapshot_id} is not a published snapshot",
        )
        self.snapshot_id = snapshot_id


class SnapshotConflictError(PublicationStoreError):
    """The same snapshot id was staged twice with different content."""

    def __init__(self, snapshot_id: str, message: str) -> None:
        super().__init__("snapshot_conflict", message)
        self.snapshot_id = snapshot_id


class SnapshotStateTransitionError(PublicationStoreError):
    def __init__(self, snapshot_id: str, current: str, action: str) -> None:
        super().__init__(
            "invalid_transition",
            f"cannot {action} snapshot {snapshot_id} in state {current!r}",
        )
        self.snapshot_id = snapshot_id
        self.current = current
        self.action = action


class SnapshotPublication(StrictModel):
    workspace_id: UUID
    repository_id: UUID
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    namespace: str = Field(min_length=1)
    state: str = Field(pattern=r"^(staged|published|aborted|retired)$")
    graph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    staged_at: str = Field(min_length=1)
    published_at: str | None = None


class StructuralQueryHit(StrictModel):
    """One structural fact returned to a consumer, with its full provenance."""

    structural_fact_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    node_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    file: str
    line: int | None = Field(default=None, ge=1)
    properties: dict[str, Any] = Field(default_factory=dict)
    workspace_id: UUID
    repository_id: UUID
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_version: str = Field(min_length=1)
    config_version: str | None = None
    processing_version: str = Field(min_length=1)


class StructuralQueryResult(StrictModel):
    snapshot_id: str
    namespace: str
    query: str
    hits: tuple[StructuralQueryHit, ...]
    total_matched: int = Field(ge=0)
    graph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def result_sha256(self) -> str:
        """Canonical hash of the returned hits: identical hits ⇒ identical id."""
        return sha256(
            {
                "graph_sha256": self.graph_sha256,
                "hits": [h.model_dump(mode="json") for h in self.hits],
                "namespace": self.namespace,
                "query": self.query,
                "snapshot_id": self.snapshot_id,
            }
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StructuralPublicationStore:
    """Durable staged publication state with retained active snapshots."""

    def __init__(self, state_dir: Path | str) -> None:
        self._persistent: sqlite3.Connection | None = None
        if isinstance(state_dir, str) and state_dir == ":memory:":
            # A new connection would open a fresh database, so one connection is
            # kept open for the lifetime of the store.
            self._db_path = ":memory:"
            self._persistent = sqlite3.connect(":memory:")
            self._persistent.row_factory = sqlite3.Row
        else:
            path = Path(state_dir)
            path.mkdir(parents=True, exist_ok=True)
            self._db_path = str(path / "structural-publications.sqlite")
        self._init_db()

    def _connection(self) -> sqlite3.Connection:
        if self._persistent is not None:
            return self._persistent
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = self._connection()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            if conn is not self._persistent:
                conn.close()

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS structural_snapshots (
                    workspace_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    state TEXT NOT NULL,
                    projection_sha256 TEXT NOT NULL,
                    coverage_sha256 TEXT NOT NULL,
                    graph_sha256 TEXT NOT NULL,
                    projection_json TEXT NOT NULL,
                    coverage_json TEXT NOT NULL,
                    staged_at TEXT NOT NULL,
                    published_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, repository_id, snapshot_id)
                )
                """
            )

    def _row(
        self,
        conn: sqlite3.Connection,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT * FROM structural_snapshots
            WHERE workspace_id = ? AND repository_id = ? AND snapshot_id = ?
            """,
            (str(workspace_id), str(repository_id), snapshot_id),
        ).fetchone()

    def stage(
        self,
        *,
        projection: StructuralProjection,
        coverage: CoverageManifest,
    ) -> SnapshotPublication:
        """Record a projected snapshot as staged (invisible to queries)."""
        if coverage.snapshot_id != projection.snapshot_id:
            raise SnapshotConflictError(
                projection.snapshot_id,
                "coverage manifest snapshot id does not match the projection",
            )
        # The applied projector version is engine-derived bookkeeping that a
        # later reader trusts, so an unsupported version is refused at the
        # publication boundary rather than stored and re-served.
        validate_projection_version(projection.processing_version)
        validate_projection_version(coverage.processing_version)
        projection_json = canonical_json(projection.model_dump(mode="json"))
        coverage_json = canonical_json(coverage.model_dump(mode="json"))
        projection_sha = sha256(projection.model_dump(mode="json"))
        coverage_sha = sha256(coverage.model_dump(mode="json"))
        now = _now()
        with self._db() as conn:
            existing = self._row(
                conn,
                projection.workspace_id,
                projection.repository_id,
                projection.snapshot_id,
            )
            if existing is not None:
                if (
                    existing["projection_sha256"] != projection_sha
                    or existing["coverage_sha256"] != coverage_sha
                ):
                    raise SnapshotConflictError(
                        projection.snapshot_id,
                        "snapshot was already recorded with different content",
                    )
                if existing["state"] == _STATE_ABORTED:
                    conn.execute(
                        """
                        UPDATE structural_snapshots
                        SET state = 'staged', updated_at = ?
                        WHERE workspace_id = ? AND repository_id = ?
                          AND snapshot_id = ? AND state = 'aborted'
                        """,
                        (
                            now,
                            str(projection.workspace_id),
                            str(projection.repository_id),
                            projection.snapshot_id,
                        ),
                    )
                row = self._row(
                    conn,
                    projection.workspace_id,
                    projection.repository_id,
                    projection.snapshot_id,
                )
                assert row is not None
                return self._publication(row)
            conn.execute(
                """
                INSERT INTO structural_snapshots (
                    workspace_id, repository_id, snapshot_id, namespace, state,
                    projection_sha256, coverage_sha256, graph_sha256,
                    projection_json, coverage_json, staged_at, published_at, updated_at
                ) VALUES (?, ?, ?, ?, 'staged', ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    str(projection.workspace_id),
                    str(projection.repository_id),
                    projection.snapshot_id,
                    projection.namespace,
                    projection_sha,
                    coverage_sha,
                    projection.graph_sha256,
                    projection_json,
                    coverage_json,
                    now,
                    now,
                ),
            )
            row = self._row(
                conn,
                projection.workspace_id,
                projection.repository_id,
                projection.snapshot_id,
            )
            assert row is not None
            return self._publication(row)

    def stage_enola_snapshot(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
        facts_bytes: bytes,
        receipt_bytes: bytes | None,
        manifest_files: Iterable[ManifestFile],
    ) -> SnapshotPublication:
        """Project Enola artifacts and stage them in one call."""
        projection = project_structural_snapshot(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            facts_bytes=facts_bytes,
            receipt_bytes=receipt_bytes,
        )
        receipt = parse_enola_receipt(receipt_bytes)
        coverage = build_coverage_manifest(
            projection, manifest_files=manifest_files, quality=receipt.quality
        )
        return self.stage(projection=projection, coverage=coverage)

    def publish(
        self, *, workspace_id: UUID, repository_id: UUID, snapshot_id: str
    ) -> SnapshotPublication:
        """Make one staged snapshot visible without touching the others."""
        now = _now()
        with self._db() as conn:
            row = self._row(conn, workspace_id, repository_id, snapshot_id)
            if row is None:
                raise SnapshotInvisibleError(snapshot_id)
            if row["state"] == _STATE_PUBLISHED:
                return self._publication(row)
            if row["state"] != _STATE_STAGED:
                raise SnapshotStateTransitionError(snapshot_id, row["state"], "publish")
            conn.execute(
                """
                UPDATE structural_snapshots
                SET state = 'published', published_at = ?, updated_at = ?
                WHERE workspace_id = ? AND repository_id = ? AND snapshot_id = ?
                """,
                (now, now, str(workspace_id), str(repository_id), snapshot_id),
            )
            updated = self._row(conn, workspace_id, repository_id, snapshot_id)
            assert updated is not None
            return self._publication(updated)

    def abort(
        self, *, workspace_id: UUID, repository_id: UUID, snapshot_id: str
    ) -> SnapshotPublication:
        """Mark a staged snapshot aborted. It stays invisible to queries."""
        now = _now()
        with self._db() as conn:
            row = self._row(conn, workspace_id, repository_id, snapshot_id)
            if row is None:
                raise SnapshotInvisibleError(snapshot_id)
            if row["state"] == _STATE_PUBLISHED:
                raise SnapshotStateTransitionError(snapshot_id, row["state"], "abort")
            conn.execute(
                """
                UPDATE structural_snapshots
                SET state = 'aborted', updated_at = ?
                WHERE workspace_id = ? AND repository_id = ? AND snapshot_id = ?
                """,
                (now, str(workspace_id), str(repository_id), snapshot_id),
            )
            updated = self._row(conn, workspace_id, repository_id, snapshot_id)
            assert updated is not None
            return self._publication(updated)

    def retire(
        self, *, workspace_id: UUID, repository_id: UUID, snapshot_id: str
    ) -> SnapshotPublication:
        """Withdraw a published snapshot from query visibility."""
        now = _now()
        with self._db() as conn:
            row = self._row(conn, workspace_id, repository_id, snapshot_id)
            if row is None:
                raise SnapshotInvisibleError(snapshot_id)
            if row["state"] != _STATE_PUBLISHED:
                raise SnapshotStateTransitionError(snapshot_id, row["state"], "retire")
            conn.execute(
                """
                UPDATE structural_snapshots
                SET state = 'retired', updated_at = ?
                WHERE workspace_id = ? AND repository_id = ? AND snapshot_id = ?
                """,
                (now, str(workspace_id), str(repository_id), snapshot_id),
            )
            updated = self._row(conn, workspace_id, repository_id, snapshot_id)
            assert updated is not None
            return self._publication(updated)

    def is_published(
        self, *, workspace_id: UUID, repository_id: UUID, snapshot_id: str
    ) -> bool:
        with self._db() as conn:
            row = self._row(conn, workspace_id, repository_id, snapshot_id)
            return bool(row is not None and row["state"] == _STATE_PUBLISHED)

    def active_snapshots(
        self, *, workspace_id: UUID, repository_id: UUID
    ) -> tuple[SnapshotPublication, ...]:
        """Every published (retained active) snapshot of one repository."""
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM structural_snapshots
                WHERE workspace_id = ? AND repository_id = ? AND state = 'published'
                ORDER BY published_at, snapshot_id
                """,
                (str(workspace_id), str(repository_id)),
            ).fetchall()
            return tuple(self._publication(row) for row in rows)

    def load(
        self, *, workspace_id: UUID, repository_id: UUID, snapshot_id: str
    ) -> tuple[StructuralProjection, CoverageManifest]:
        """Load a published snapshot's projection and coverage manifest."""
        with self._db() as conn:
            row = self._row(conn, workspace_id, repository_id, snapshot_id)
            if row is None or row["state"] != _STATE_PUBLISHED:
                raise SnapshotInvisibleError(snapshot_id)
            projection = StructuralProjection.model_validate_json(
                row["projection_json"]
            )
            coverage = CoverageManifest.model_validate_json(row["coverage_json"])
            # Re-validate on the read path too: a retained row written by an
            # older projector build must not be served as a current projection.
            validate_projection_version(projection.processing_version)
            validate_projection_version(coverage.processing_version)
            return projection, coverage

    def query(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
        text: str = "*",
        limit: int = 20,
    ) -> StructuralQueryResult:
        """Structural query scoped to one published snapshot namespace."""
        if limit < 1:
            raise ValueError("limit must be >= 1")
        projection, _coverage = self.load(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
        )
        needle = text.lower()
        hits: list[StructuralQueryHit] = []
        matched = 0
        for node in projection.nodes:
            if (
                text != "*"
                and needle not in node.name.lower()
                and needle not in node.kind.lower()
            ):
                continue
            matched += 1
            if len(hits) >= limit:
                continue
            provenance = node.provenance
            hits.append(
                StructuralQueryHit(
                    structural_fact_id=node.structural_fact_id,
                    node_id=node.node_id,
                    kind=node.kind,
                    name=node.name,
                    file=node.file,
                    line=node.line,
                    properties=node.properties,
                    workspace_id=provenance.workspace_id,
                    repository_id=provenance.repository_id,
                    snapshot_id=provenance.snapshot_id,
                    source_version=provenance.source_version,
                    config_version=provenance.config_version,
                    processing_version=provenance.processing_version,
                )
            )
        return StructuralQueryResult(
            snapshot_id=snapshot_id,
            namespace=projection.namespace,
            query=text,
            hits=tuple(hits),
            total_matched=matched,
            graph_sha256=projection.graph_sha256,
        )

    @staticmethod
    def _publication(row: sqlite3.Row) -> SnapshotPublication:
        return SnapshotPublication(
            workspace_id=UUID(row["workspace_id"]),
            repository_id=UUID(row["repository_id"]),
            snapshot_id=row["snapshot_id"],
            namespace=row["namespace"],
            state=row["state"],
            graph_sha256=row["graph_sha256"],
            projection_sha256=row["projection_sha256"],
            coverage_sha256=row["coverage_sha256"],
            staged_at=row["staged_at"],
            published_at=row["published_at"],
        )


__all__ = [
    "PublicationStoreError",
    "SnapshotConflictError",
    "SnapshotInvisibleError",
    "SnapshotPublication",
    "SnapshotStateTransitionError",
    "StructuralPublicationStore",
    "StructuralQueryHit",
    "StructuralQueryResult",
]
