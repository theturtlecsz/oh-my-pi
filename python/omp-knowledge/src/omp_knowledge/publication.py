from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from .errors import SnapshotNotPublishedError


class PublicationManager:
    """Manages snapshot publication states (staged, published, aborted, retired)
    persisted locally in SQLite under state_dir.
    """

    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir) if isinstance(state_dir, str) and state_dir != ":memory:" else state_dir
        if isinstance(self.state_dir, Path):
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self._db_path = str(self.state_dir / "publications.sqlite")
        else:
            self._db_path = ":memory:"
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshot_publications (
                    workspace_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    published_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, repository_id, snapshot_id)
                )
                """
            )

    def publish(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> bool:
        """Mark snapshot as published."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO snapshot_publications (
                    workspace_id, repository_id, snapshot_id, status, published_at, updated_at
                ) VALUES (?, ?, ?, 'published', ?, ?)
                ON CONFLICT (workspace_id, repository_id, snapshot_id) DO UPDATE SET
                    status = 'published',
                    published_at = COALESCE(snapshot_publications.published_at, excluded.published_at),
                    updated_at = excluded.updated_at
                """,
                (str(workspace_id), str(repository_id), snapshot_id, now, now),
            )
        return True

    def abort(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> bool:
        """Mark snapshot as aborted (leaves it unpublished)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO snapshot_publications (
                    workspace_id, repository_id, snapshot_id, status, published_at, updated_at
                ) VALUES (?, ?, ?, 'aborted', NULL, ?)
                ON CONFLICT (workspace_id, repository_id, snapshot_id) DO UPDATE SET
                    status = 'aborted',
                    updated_at = excluded.updated_at
                """,
                (str(workspace_id), str(repository_id), snapshot_id, now),
            )
        return True

    def is_published(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> bool:
        """Check if snapshot has status 'published'."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT status FROM snapshot_publications
                WHERE workspace_id = ? AND repository_id = ? AND snapshot_id = ?
                """,
                (str(workspace_id), str(repository_id), snapshot_id),
            )
            row = cursor.fetchone()
            return bool(row and row["status"] == "published")

    def retire(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> bool:
        """Mark snapshot as retired."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO snapshot_publications (
                    workspace_id, repository_id, snapshot_id, status, published_at, updated_at
                ) VALUES (?, ?, ?, 'retired', NULL, ?)
                ON CONFLICT (workspace_id, repository_id, snapshot_id) DO UPDATE SET
                    status = 'retired',
                    updated_at = excluded.updated_at
                """,
                (str(workspace_id), str(repository_id), snapshot_id, now),
            )
        return True

    def verify_published(
        self,
        *,
        workspace_id: UUID,
        repository_id: UUID,
        snapshot_id: str,
    ) -> None:
        """Raise SnapshotNotPublishedError if snapshot is not published."""
        if not self.is_published(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
        ):
            raise SnapshotNotPublishedError(snapshot_id)


__all__ = ["PublicationManager"]
