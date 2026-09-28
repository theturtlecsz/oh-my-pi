"""Vector projection store for Fleet Knowledge (FK-3).

Projects structural snapshot facts into float32 BLOB vectors with stable
namespace isolation, deterministic projection and vector digests, and exact
rollback/rebuild support.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import struct
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from omp_work.knowledge_namespace import snapshot_namespace, validate_snapshot_id
from omp_work.knowledge_publication import SnapshotInvisibleError
from omp_work.v1.models import StrictModel

from omp_knowledge.errors import KnowledgeError, SnapshotNotPublishedError
from omp_knowledge.inference.embedding import Embedder


class VectorHit(StrictModel):
    """One search hit from vector cosine similarity matching."""

    fact_id: str
    text: str
    score: float

    def __init__(
        self,
        fact_id: str | None = None,
        text: str | None = None,
        score: float | None = None,
        **kwargs: Any,
    ) -> None:
        if fact_id is not None and "fact_id" not in kwargs:
            kwargs["fact_id"] = fact_id
        if text is not None and "text" not in kwargs:
            kwargs["text"] = text
        if score is not None and "score" not in kwargs:
            kwargs["score"] = score
        super().__init__(**kwargs)


class VectorProjection(StrictModel):
    """Summary and digests of a built vector projection generation."""

    namespace: str
    generation_id: str
    vector_count: int
    projection_sha256: str
    vectors_sha256: str

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)


def _cosine_similarity(u: Sequence[float], v: Sequence[float]) -> float:
    if len(u) != len(v):
        raise ValueError(
            f"vector dimension mismatch: query={len(u)}, stored={len(v)}"
        )
    dot = 0.0
    norm_u = 0.0
    norm_v = 0.0
    for a, b in zip(u, v):
        dot += a * b
        norm_u += a * a
        norm_v += b * b
    if norm_u <= 0.0 or norm_v <= 0.0:
        return 0.0
    sim = dot / (math.sqrt(norm_u) * math.sqrt(norm_v))
    return max(-1.0, min(1.0, sim))


class VectorProjectionStore:
    """SQLite store maintaining vector projections and float32 rows under vectors.sqlite."""

    def __init__(self, state_dir: Path | str) -> None:
        self._persistent: sqlite3.Connection | None = None
        if isinstance(state_dir, str) and state_dir == ":memory:":
            self._db_path = ":memory:"
            self._persistent = sqlite3.connect(":memory:")
            self._persistent.row_factory = sqlite3.Row
        else:
            path = Path(state_dir)
            if path.is_file() or path.suffix == ".sqlite":
                self._db_path = str(path)
                path.parent.mkdir(parents=True, exist_ok=True)
            else:
                path.mkdir(parents=True, exist_ok=True)
                self._db_path = str(path / "vectors.sqlite")
        self._init_db()

    def close(self) -> None:
        if self._persistent is not None:
            self._persistent.close()
            self._persistent = None

    def _get_connection(self) -> sqlite3.Connection:
        if self._persistent is not None:
            return self._persistent
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._get_connection()
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
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vector_projections (
                    namespace TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    vector_count INTEGER NOT NULL,
                    projection_sha256 TEXT NOT NULL,
                    vectors_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (namespace, generation_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vector_rows (
                    namespace TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    fact_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    text_sha256 TEXT NOT NULL,
                    vector BLOB NOT NULL,
                    PRIMARY KEY (namespace, generation_id, fact_id)
                )
                """
            )

    def build(
        self,
        publications: Any,
        workspace_id: UUID | str,
        repository_id: UUID | str,
        snapshot_id: str,
        embedder: Embedder | Any,
    ) -> VectorProjection:
        """Build vector projection for a published snapshot.

        Replaces any prior rows for (namespace, generation_id) in one transaction.
        Raises SnapshotNotPublishedError if the snapshot is not published.
        """
        validate_snapshot_id(snapshot_id)
        ws_id = UUID(str(workspace_id))
        repo_id = UUID(str(repository_id))

        if hasattr(publications, "is_published"):
            if not publications.is_published(
                workspace_id=ws_id, repository_id=repo_id, snapshot_id=snapshot_id
            ):
                raise SnapshotNotPublishedError(snapshot_id)

        try:
            projection, _ = publications.load(
                workspace_id=ws_id, repository_id=repo_id, snapshot_id=snapshot_id
            )
        except SnapshotInvisibleError as exc:
            raise SnapshotNotPublishedError(snapshot_id) from exc

        namespace = snapshot_namespace(
            workspace_id=ws_id, repository_id=repo_id, snapshot_id=snapshot_id
        )

        generation_id = getattr(
            getattr(embedder, "profile", None),
            "generation_id",
            getattr(embedder, "generation_id", None),
        )
        if not generation_id or not isinstance(generation_id, str):
            raise ValueError(
                "embedder must have a profile with generation_id or a generation_id attribute"
            )

        sorted_nodes = sorted(projection.nodes, key=lambda n: n.structural_fact_id)
        texts = [f"{n.file}:{n.line or 0} {n.kind} {n.name}" for n in sorted_nodes]

        vectors = embedder.embed_documents(texts) if texts else []
        if len(vectors) != len(texts):
            raise ValueError(
                f"embedder returned {len(vectors)} vectors for {len(texts)} texts"
            )

        text_hashes: list[str] = []
        blobs: list[bytes] = []
        for text, vec in zip(texts, vectors):
            text_hashes.append(hashlib.sha256(text.encode("utf-8")).hexdigest())
            blobs.append(struct.pack(f"<{len(vec)}f", *vec))

        projection_sha_hasher = hashlib.sha256()
        projection_sha_hasher.update(generation_id.encode("utf-8"))
        for node, t_sha in zip(sorted_nodes, text_hashes):
            projection_sha_hasher.update(node.structural_fact_id.encode("utf-8"))
            projection_sha_hasher.update(t_sha.encode("utf-8"))
        projection_sha256 = projection_sha_hasher.hexdigest()

        vectors_sha_hasher = hashlib.sha256()
        for blob in blobs:
            vectors_sha_hasher.update(blob)
        vectors_sha256 = vectors_sha_hasher.hexdigest()

        vector_count = len(sorted_nodes)
        now = datetime.now(timezone.utc).isoformat()

        with self._connection() as conn:
            conn.execute(
                "DELETE FROM vector_rows WHERE namespace = ? AND generation_id = ?",
                (namespace, generation_id),
            )
            conn.execute(
                "DELETE FROM vector_projections WHERE namespace = ? AND generation_id = ?",
                (namespace, generation_id),
            )
            if sorted_nodes:
                conn.executemany(
                    "INSERT INTO vector_rows (namespace, generation_id, fact_id, text, text_sha256, vector) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (namespace, generation_id, node.structural_fact_id, text, t_sha, blob)
                        for node, text, t_sha, blob in zip(sorted_nodes, texts, text_hashes, blobs)
                    ],
                )
            conn.execute(
                "INSERT INTO vector_projections (namespace, generation_id, vector_count, projection_sha256, vectors_sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (namespace, generation_id, vector_count, projection_sha256, vectors_sha256, now),
            )

        return VectorProjection(
            namespace=namespace,
            generation_id=generation_id,
            vector_count=vector_count,
            projection_sha256=projection_sha256,
            vectors_sha256=vectors_sha256,
        )

    def search(
        self,
        publications: Any,
        workspace_id: Any,
        repository_id: Any = None,
        snapshot_id: str | None = None,
        generation_id: str | None = None,
        query_vector: Sequence[float] | None = None,
        k: int = 10,
    ) -> list[VectorHit]:
        """Search built vector rows using cosine similarity. Ties broken by fact id."""
        # Handle positional parameter flexibility
        if repository_id is not None and snapshot_id is not None and generation_id is not None and query_vector is not None:
            # 6 or 7 arguments: publications is provided as first argument
            actual_publications = publications
            actual_ws = UUID(str(workspace_id))
            actual_repo = UUID(str(repository_id))
            actual_snap = str(snapshot_id)
            actual_gen = str(generation_id)
            actual_query = query_vector
            actual_k = k
        elif snapshot_id is not None and generation_id is not None:
            # 6 arguments without publications: publications is actually workspace_id
            actual_publications = None
            actual_ws = UUID(str(publications))
            actual_repo = UUID(str(workspace_id))
            actual_snap = str(repository_id)
            actual_gen = str(snapshot_id)
            actual_query = generation_id  # type: ignore
            actual_k = query_vector if isinstance(query_vector, int) else 10
        else:
            raise ValueError("insufficient arguments to search()")

        validate_snapshot_id(actual_snap)

        if actual_publications is not None and hasattr(actual_publications, "is_published"):
            if not actual_publications.is_published(
                workspace_id=actual_ws, repository_id=actual_repo, snapshot_id=actual_snap
            ):
                raise SnapshotNotPublishedError(actual_snap)

        namespace = snapshot_namespace(
            workspace_id=actual_ws, repository_id=actual_repo, snapshot_id=actual_snap
        )

        with self._connection() as conn:
            proj = conn.execute(
                "SELECT vector_count FROM vector_projections WHERE namespace = ? AND generation_id = ?",
                (namespace, actual_gen),
            ).fetchone()
            if proj is None:
                raise KnowledgeError(
                    f"vector_generation_mismatch: generation {actual_gen} not built for {namespace}",
                    code="vector_generation_mismatch",
                    status_code=409,
                )
            rows = conn.execute(
                "SELECT fact_id, text, vector FROM vector_rows WHERE namespace = ? AND generation_id = ? ORDER BY fact_id",
                (namespace, actual_gen),
            ).fetchall()

        hits: list[VectorHit] = []
        for row in rows:
            fact_id = row["fact_id"]
            text = row["text"]
            blob = row["vector"]
            vec = struct.unpack(f"<{len(blob)//4}f", blob)
            score = _cosine_similarity(actual_query, vec)
            hits.append(VectorHit(fact_id=fact_id, text=text, score=score))

        hits.sort(key=lambda h: (-h.score, h.fact_id))
        if actual_k is not None and actual_k >= 0:
            return hits[:actual_k]
        return hits

    def get_projection(
        self,
        *,
        workspace_id: UUID | str,
        repository_id: UUID | str,
        snapshot_id: str,
        generation_id: str,
    ) -> VectorProjection | None:
        """Get summary metadata for a built projection, or None."""
        namespace = snapshot_namespace(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
        )
        with self._connection() as conn:
            row = conn.execute(
                "SELECT namespace, generation_id, vector_count, projection_sha256, vectors_sha256 "
                "FROM vector_projections WHERE namespace = ? AND generation_id = ?",
                (namespace, generation_id),
            ).fetchone()
            if row is None:
                return None
            return VectorProjection(
                namespace=row["namespace"],
                generation_id=row["generation_id"],
                vector_count=row["vector_count"],
                projection_sha256=row["projection_sha256"],
                vectors_sha256=row["vectors_sha256"],
            )


__all__ = [
    "VectorHit",
    "VectorProjection",
    "VectorProjectionStore",
]
