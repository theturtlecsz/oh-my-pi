"""Persistent route storage for compiled context bundles (OMP-278-s04 / FK-3 / FK-5).

Tracks the inference routes (embedding, reranker) resolved during bundle
compilation at ``<state_dir>/context-routes.sqlite``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


class ContextBundleRoute(dict[str, Any]):
    """One route entry associated with a compiled context bundle.

    Behaves as both a dict and an object with attribute access.
    """

    def __init__(
        self,
        *,
        role: str,
        name: str,
        provider: str,
        model: str = "",
        accelerator: str = "cpu",
        used: str = "primary",
        reason: str = "",
        **kwargs: Any,
    ) -> None:
        data = {
            "role": role,
            "name": name,
            "provider": provider,
            "model": model,
            "accelerator": accelerator,
            "used": used,
            "reason": reason,
            **kwargs,
        }
        super().__init__(data)

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value

    @property
    def role(self) -> str:
        return self["role"]

    @property
    def name(self) -> str:
        return self["name"]

    @property
    def provider(self) -> str:
        return self["provider"]

    @property
    def model(self) -> str:
        return self["model"]

    @property
    def accelerator(self) -> str:
        return self["accelerator"]

    @property
    def used(self) -> str:
        return self["used"]

    @property
    def reason(self) -> str:
        return self["reason"]


class ContextRouteStore:
    """SQLite store maintaining routes under ``context-routes.sqlite``."""

    def __init__(self, state_dir: str | Path) -> None:
        path = Path(state_dir)
        if path.is_file() or path.suffix == ".sqlite":
            self.db_path = path
            self.state_dir = path.parent
        else:
            self.state_dir = path
            self.db_path = self.state_dir / "context-routes.sqlite"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS context_bundle_routes (
                    bundle_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    accelerator TEXT NOT NULL,
                    used TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY (bundle_id, role, name)
                );
                """
            )
            conn.commit()

    def persist(
        self,
        bundle_id: str,
        routes: Sequence[Mapping[str, Any] | ContextBundleRoute],
    ) -> None:
        """Record route rows for a compiled bundle. Ignores duplicates."""
        if not routes:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO context_bundle_routes (
                    bundle_id, role, name, provider, model, accelerator, used, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        bundle_id,
                        str(r["role"]),
                        str(r["name"]),
                        str(r["provider"]),
                        str(r.get("model") or ""),
                        str(r["accelerator"]),
                        str(r["used"]),
                        str(r.get("reason") or ""),
                    )
                    for r in routes
                ],
            )
            conn.commit()

    record = persist

    def load(self, bundle_id: str) -> list[ContextBundleRoute]:
        """Load all route records for a bundle_id."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT role, name, provider, model, accelerator, used, reason
                FROM context_bundle_routes
                WHERE bundle_id = ?
                ORDER BY role, name
                """,
                (bundle_id,),
            ).fetchall()
            return [
                ContextBundleRoute(
                    role=row["role"],
                    name=row["name"],
                    provider=row["provider"],
                    model=row["model"],
                    accelerator=row["accelerator"],
                    used=row["used"],
                    reason=row["reason"],
                )
                for row in rows
            ]

    def close(self) -> None:
        pass


__all__ = [
    "ContextBundleRoute",
    "ContextRouteStore",
]
