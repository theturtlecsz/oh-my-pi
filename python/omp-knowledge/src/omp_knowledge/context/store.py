"""Persistent bundle storage and deterministic reproduction (OMP-311 / FK-5).

- :class:`ContextBundleStore` manages persistent SQLite records of compiled
  fleet context bundles and their exclusions at ``<state_dir>/context-bundles.sqlite``.
- Handles atomic bundle persistence, idempotent no-op duplicate admissions,
  conflict rejection, and offline reproduction via :class:`RecordedCounter`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omp_work.v1.canonical import canonical_json, sha256

from .compiler import CompiledBundle, _sort_key, compile_bundle
from .models import (
    CompileRequest,
    Exclusion,
    Stage,
    StageIdentity,
)
from .tokens import RecordedCounter, ReproductionError


class BundleConflictError(Exception):
    """Raised when persisting a bundle with an existing ID but conflicting content."""


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS context_bundles (
    bundle_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL,
    work_key TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    counter_profile_json TEXT NOT NULL,
    counts_json TEXT NOT NULL,
    bundle_text TEXT NOT NULL,
    bundle_sha256 TEXT NOT NULL,
    section_sha256_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_exclusions (
    bundle_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    section TEXT NOT NULL,
    source TEXT NOT NULL,
    ref TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (bundle_id, ordinal)
);
"""


def compute_bundle_id(
    identity: StageIdentity | dict[str, Any], bundle_sha256: str
) -> str:
    """Compute the deterministic bundle ID from identity and bundle sha256."""
    identity_dict = (
        identity.model_dump(mode="json")
        if isinstance(identity, StageIdentity)
        else dict(identity)
    )
    return sha256({"identity": identity_dict, "bundle_sha256": bundle_sha256})


class BundleRecord:
    """A persisted bundle record loaded from SQLite."""

    def __init__(
        self,
        *,
        bundle_id: str,
        work_id: str,
        work_key: str,
        revision_id: str,
        stage: Stage,
        attempt_id: str,
        candidate_id: str,
        request_json: str,
        counter_profile_json: str,
        counts_json: str,
        bundle_text: str,
        bundle_sha256: str,
        section_sha256_json: str,
        created_at: str,
        exclusions: tuple[Exclusion, ...] = (),
    ) -> None:
        self.bundle_id = bundle_id
        self.work_id = work_id
        self.work_key = work_key
        self.revision_id = revision_id
        self.stage = stage
        self.attempt_id = attempt_id
        self.candidate_id = candidate_id
        self.request_json = request_json
        self.counter_profile_json = counter_profile_json
        self.counts_json = counts_json
        self.bundle_text = bundle_text
        self.bundle_sha256 = bundle_sha256
        self.section_sha256_json = section_sha256_json
        self.created_at = created_at
        self.exclusions = tuple(exclusions)

    @property
    def text(self) -> str:
        return self.bundle_text

    @property
    def sha256(self) -> str:
        return self.bundle_sha256

    @property
    def identity(self) -> StageIdentity:
        return StageIdentity(
            work_id=self.work_id,
            work_key=self.work_key,
            revision_id=self.revision_id,
            stage=self.stage,
            attempt_id=self.attempt_id,
            candidate_id=self.candidate_id,
        )

    @property
    def request(self) -> CompileRequest:
        return CompileRequest.model_validate_json(self.request_json)

    @property
    def counter_profile(self) -> str:
        try:
            val = json.loads(self.counter_profile_json)
            if isinstance(val, dict):
                return canonical_json(val)
            return str(val)
        except Exception:
            return self.counter_profile_json

    @property
    def counts(self) -> dict[str, int]:
        return json.loads(self.counts_json)

    @property
    def section_sha256(self) -> dict[str, str]:
        return json.loads(self.section_sha256_json)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BundleRecord):
            return False
        return (
            self.bundle_id == other.bundle_id
            and self.bundle_text == other.bundle_text
            and self.bundle_sha256 == other.bundle_sha256
            and self.identity == other.identity
            and self.exclusions == other.exclusions
            and self.request_json == other.request_json
            and self.counts_json == other.counts_json
            and self.counter_profile_json == other.counter_profile_json
            and self.section_sha256_json == other.section_sha256_json
        )


class ContextBundleStore:
    """SQLite-backed store for compiled fleet context bundles."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "context-bundles.sqlite"
        self._init_db()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)

    def persist(self, request: CompileRequest, compiled: CompiledBundle) -> str:
        """Persist a request and its compiled bundle in a single transaction.

        If a record with the same ``bundle_id`` and identical byte content already
        exists, this is a no-op returning the ID. If the content differs,
        :class:`BundleConflictError` is raised.
        """
        bundle_id = compute_bundle_id(request.identity, compiled.sha256)
        request_json = canonical_json(request.model_dump(mode="json"))

        try:
            profile_val = json.loads(compiled.counter_profile)
            counter_profile_json = (
                canonical_json(profile_val)
                if isinstance(profile_val, dict)
                else canonical_json(compiled.counter_profile)
            )
        except Exception:
            counter_profile_json = canonical_json(compiled.counter_profile)

        counts = dict(compiled.counts)
        counts[compiled.sha256] = compiled.tokens
        counts_json = canonical_json(counts)
        section_sha256_json = canonical_json(compiled.section_sha256)
        created_at = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT work_id, work_key, revision_id, stage, attempt_id, candidate_id, "
                "request_json, counter_profile_json, counts_json, bundle_text, bundle_sha256, section_sha256_json "
                "FROM context_bundles WHERE bundle_id = ?",
                (bundle_id,),
            )
            row = cursor.fetchone()
            if row is not None:
                if (
                    row["bundle_text"] == compiled.text
                    and row["bundle_sha256"] == compiled.sha256
                    and row["work_id"] == request.identity.work_id
                    and row["work_key"] == request.identity.work_key
                    and row["revision_id"] == request.identity.revision_id
                    and row["stage"] == request.identity.stage
                    and row["attempt_id"] == request.identity.attempt_id
                    and row["candidate_id"] == request.identity.candidate_id
                    and row["request_json"] == request_json
                    and row["counter_profile_json"] == counter_profile_json
                    and row["counts_json"] == counts_json
                    and row["section_sha256_json"] == section_sha256_json
                ):
                    excl_cursor = conn.execute(
                        "SELECT section, source, ref, reason, detail FROM context_exclusions "
                        "WHERE bundle_id = ? ORDER BY ordinal",
                        (bundle_id,),
                    )
                    excl_rows = excl_cursor.fetchall()
                    if len(excl_rows) == len(compiled.exclusions) and all(
                        r["section"] == e.section
                        and r["source"] == e.source
                        and r["ref"] == e.ref
                        and r["reason"] == e.reason
                        and r["detail"] == e.detail
                        for r, e in zip(excl_rows, compiled.exclusions)
                    ):
                        return bundle_id

                raise BundleConflictError(
                    f"bundle conflict for bundle_id {bundle_id}: existing record differs from new bundle"
                )

            conn.execute(
                "INSERT INTO context_bundles ("
                "bundle_id, work_id, work_key, revision_id, stage, attempt_id, candidate_id, "
                "request_json, counter_profile_json, counts_json, bundle_text, bundle_sha256, section_sha256_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bundle_id,
                    request.identity.work_id,
                    request.identity.work_key,
                    request.identity.revision_id,
                    request.identity.stage,
                    request.identity.attempt_id,
                    request.identity.candidate_id,
                    request_json,
                    counter_profile_json,
                    counts_json,
                    compiled.text,
                    compiled.sha256,
                    section_sha256_json,
                    created_at,
                ),
            )
            for ordinal, excl in enumerate(compiled.exclusions):
                conn.execute(
                    "INSERT INTO context_exclusions ("
                    "bundle_id, ordinal, section, source, ref, reason, detail"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        bundle_id,
                        ordinal,
                        excl.section,
                        excl.source,
                        excl.ref,
                        excl.reason,
                        excl.detail,
                    ),
                )

        return bundle_id

    def load(self, bundle_id: str) -> BundleRecord | None:
        """Load a persisted bundle record by ID, or None if not found."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT bundle_id, work_id, work_key, revision_id, stage, attempt_id, candidate_id, "
                "request_json, counter_profile_json, counts_json, bundle_text, bundle_sha256, section_sha256_json, created_at "
                "FROM context_bundles WHERE bundle_id = ?",
                (bundle_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None

            excl_cursor = conn.execute(
                "SELECT section, source, ref, reason, detail FROM context_exclusions "
                "WHERE bundle_id = ? ORDER BY ordinal",
                (bundle_id,),
            )
            exclusions = tuple(
                Exclusion(
                    section=r["section"],
                    source=r["source"],
                    ref=r["ref"],
                    reason=r["reason"],
                    detail=r["detail"],
                )
                for r in excl_cursor.fetchall()
            )
            return BundleRecord(
                bundle_id=row["bundle_id"],
                work_id=row["work_id"],
                work_key=row["work_key"],
                revision_id=row["revision_id"],
                stage=row["stage"],
                attempt_id=row["attempt_id"],
                candidate_id=row["candidate_id"],
                request_json=row["request_json"],
                counter_profile_json=row["counter_profile_json"],
                counts_json=row["counts_json"],
                bundle_text=row["bundle_text"],
                bundle_sha256=row["bundle_sha256"],
                section_sha256_json=row["section_sha256_json"],
                created_at=row["created_at"],
                exclusions=exclusions,
            )

    def reproduce(self, bundle_id: str) -> str:
        """Recompile the stored request using RecordedCounter and return the bundle text.

        The compiler counts the full render before each budget drop, but the
        record only preserves the final render and the per-item counts, so the
        request is first narrowed to the items the compiler actually admitted.
        Replaying the recorded budget exclusions leaves a request that compiles
        to the stored bytes without touching an unrecorded intermediate render.

        Raises :class:`ReproductionError` if the bundle is not found, the request
        cannot be recompiled, or the reproduced sha256/text does not match the stored record.
        """
        record = self.load(bundle_id)
        if record is None:
            raise ReproductionError(f"bundle not found: {bundle_id}")

        try:
            request = CompileRequest.model_validate_json(record.request_json)
        except Exception as exc:
            raise ReproductionError(f"invalid stored request JSON: {exc}") from exc

        try:
            counts = json.loads(record.counts_json)
        except Exception as exc:
            raise ReproductionError(f"invalid stored counts JSON: {exc}") from exc

        try:
            profile_val = json.loads(record.counter_profile_json)
            profile_str = (
                record.counter_profile_json
                if isinstance(profile_val, dict)
                else str(profile_val)
            )
        except Exception:
            profile_str = record.counter_profile_json

        effective = self._admitted_request(request, record)
        counter = RecordedCounter(profile=profile_str, counts=counts)
        try:
            recompiled = compile_bundle(effective, counter)
        except ReproductionError:
            raise
        except Exception as exc:
            raise ReproductionError(
                f"bundle compilation failed during reproduction: {exc}"
            ) from exc

        if (
            recompiled.sha256 != record.bundle_sha256
            or recompiled.text != record.bundle_text
        ):
            raise ReproductionError(
                f"reproduction mismatch: expected sha256={record.bundle_sha256}, "
                f"got {recompiled.sha256}"
            )

        return recompiled.text

    @staticmethod
    def _admitted_request(
        request: CompileRequest, record: BundleRecord
    ) -> CompileRequest:
        """Narrow a stored request to the current items the compiler admitted.

        Uses the compiler's own admission order to remove exactly the optional
        items it recorded as ``budget`` exclusions, so the recovered request
        compiles in one pass instead of replaying the budget loop against
        renders that were never recorded.
        """
        drops = sum(
            1 for exclusion in record.exclusions if exclusion.reason == "budget"
        )
        if drops == 0:
            return request

        candidates = [
            item for item in sorted(request.items, key=_sort_key) if item.status == "current"
        ]
        optional = [index for index, item in enumerate(candidates) if not item.mandatory]
        if drops > len(optional):
            raise ReproductionError(
                f"stored exclusions drop {drops} optional items but only "
                f"{len(optional)} are present"
            )
        dropped = set(optional[len(optional) - drops :])
        survivors = tuple(
            item for index, item in enumerate(candidates) if index not in dropped
        )
        return request.model_copy(update={"items": survivors})


__all__ = [
    "BundleConflictError",
    "BundleRecord",
    "ContextBundleStore",
    "ReproductionError",
    "compute_bundle_id",
]
