from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any, Protocol
from uuid import NAMESPACE_OID, UUID, uuid5

import psycopg

from omp_work.knowledge_contracts import (
    CONTEXT_BUNDLE_IDENTITY_ENCODING,
    BudgetActual,
    BudgetSpec,
    ContextBundle,
    ProcedureProposal,
)
from omp_work.v1.canonical import canonical_json, sha256

from ..config import KnowledgeConfig
from ..engine.protocol import (
    EngineError,
    EngineTimeoutError,
    EngineUnavailableError,
    KnowledgeEngine,
)
from ..learning.corrections import (
    build_withdrawn_validity,
    exclude_withdrawn_facts,
    load_correction_rows,
)
from ..models import ContextCompileRequest
from ..native.consumer import NativeSourceUnavailableError, native_readonly_session

logger = logging.getLogger(__name__)


class BudgetInsufficientError(Exception):
    def __init__(self, message: str, mandatory_used: int, limit: int) -> None:
        super().__init__(message)
        self.mandatory_used = mandatory_used
        self.limit = limit


class BudgetMethodUnavailable(Exception):
    def __init__(self, message: str = "budget method unavailable", owner: str = "FLEET-5") -> None:
        super().__init__(message)
        self.owner = owner


class WorkRevisionUnverifiableError(Exception):
    def __init__(
        self,
        message: str = "mandatory work revision unverifiable in native ledger",
        revision_id: UUID | None = None,
    ) -> None:
        super().__init__(message)
        self.revision_id = revision_id


_BUNDLE_COLUMNS = """
    bundle_id, bundle_sha256, workspace_id, repository_id,
    work_id, revision_id, candidate_id, stage,
    snapshot_id, proposal_lineage, receipt_lineage,
    budget_spec, budget_actual, enrichment_status,
    excluded_proposal_ids, content, compiled_at,
    identity_encoding, identity_canonical_json, content_canonical_json
"""


def _json_column(value: Any) -> Any:
    return value if isinstance(value, (dict, list)) else json.loads(value)


def bundle_from_row(row: dict[str, Any]) -> ContextBundle:
    """Rebuild a ContextBundle from a persisted context_bundles row, bytes and metadata verbatim.
    Legacy rows keep NULL identity/content bytes: they are historical and unverifiable, never rebuilt.
    """
    content = _json_column(row["content"])
    return ContextBundle(
        bundle_id=row["bundle_id"],
        bundle_sha256=row["bundle_sha256"],
        workspace_id=row["workspace_id"],
        repository_id=row["repository_id"],
        work_id=row["work_id"],
        revision_id=row["revision_id"],
        candidate_id=row["candidate_id"],
        stage=row["stage"],
        snapshot_id=row["snapshot_id"],
        mandatory=content.get("mandatory", {}),
        optional=content.get("optional", {}),
        budget=BudgetActual.model_validate(_json_column(row["budget_actual"])),
        enrichment_status=row["enrichment_status"],
        proposal_lineage=tuple(UUID(p) for p in _json_column(row["proposal_lineage"])),
        receipt_lineage=tuple(UUID(r) for r in _json_column(row["receipt_lineage"])),
        excluded_proposal_ids=tuple(UUID(p) for p in _json_column(row["excluded_proposal_ids"])),
        compiled_at=row["compiled_at"],
        identity_encoding=row.get("identity_encoding"),
        identity_canonical_json=row.get("identity_canonical_json"),
        content_canonical_json=row.get("content_canonical_json"),
    )


def select_bundle_row(conn: psycopg.Connection, bundle_id: UUID) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_BUNDLE_COLUMNS} FROM omp_knowledge.context_bundles WHERE bundle_id = %s",
            (bundle_id,),
        )
        return cur.fetchone()


class ContextCompiler(Protocol):
    async def compile(
        self,
        request: ContextCompileRequest,
        actor_id: UUID,
        engine: KnowledgeEngine,
    ) -> ContextBundle: ...


class ByteBudgetCompiler:
    """Stage-context compiler enforcing byte-level UTF-8 budgets on canonical JSON content.
    Mandatory inputs are never dropped. Optional items are dropped strictly by byte limits, not rows.
    Persists immutable context bundles.
    """

    def __init__(self, config: KnowledgeConfig, knowledge_conn: psycopg.Connection) -> None:
        self.config = config
        self.knowledge_conn = knowledge_conn

    async def compile(
        self,
        request: ContextCompileRequest,
        actor_id: UUID,
        engine: KnowledgeEngine,
    ) -> ContextBundle:
        # 1. Budget method check: tokens method routes to missing FLEET-5 owner
        if request.budget.method == "tokens":
            raise BudgetMethodUnavailable(
                "token budget method unavailable: routes to missing owner FLEET-5",
                owner="FLEET-5",
            )

        # 2. Mandatory inputs collection
        work_rev_data: dict[str, Any] = {}
        receipts_data: list[dict[str, Any]] = []
        try:
            with native_readonly_session(
                self.config, workspace_id=request.workspace_id, actor_id=actor_id
            ) as native_conn:
                with native_conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT revision_id, work_id, revision_number, title, description, scope, content_sha256
                        FROM omp_work.work_revisions
                        WHERE workspace_id = %s AND work_id = %s AND revision_id = %s
                        """,
                        (request.workspace_id, request.work_id, request.revision_id),
                    )
                    r_row = cur.fetchone()
                    if not r_row:
                        raise WorkRevisionUnverifiableError(
                            f"work revision {request.revision_id} not found in native ledger",
                            revision_id=request.revision_id,
                        )
                    work_rev_data = {
                        "revision_id": str(r_row["revision_id"]),
                        "work_id": str(r_row["work_id"]),
                        "revision_number": r_row["revision_number"],
                        "title": r_row["title"],
                        "description": r_row["description"],
                        "scope": r_row["scope"],
                        "content_sha256": r_row["content_sha256"],
                    }

                    cur.execute(
                        """
                        SELECT receipt_id, kind, payload_sha256, issued_at, verdict, issuer
                        FROM omp_evidence.receipts
                        WHERE workspace_id = %s AND work_id = %s AND revision_id = %s
                        ORDER BY issued_at ASC
                        """,
                        (request.workspace_id, request.work_id, request.revision_id),
                    )
                    for rec_row in cur.fetchall():
                        receipts_data.append(
                            {
                                "receipt_id": str(rec_row["receipt_id"]),
                                "kind": rec_row["kind"],
                                "payload_sha256": rec_row["payload_sha256"],
                                "verdict": rec_row.get("verdict"),
                                "issuer": rec_row.get("issuer"),
                            }
                        )
        except WorkRevisionUnverifiableError:
            raise
        except (NativeSourceUnavailableError, psycopg.OperationalError, psycopg.DatabaseError) as exc:
            logger.warning("Error reading mandatory inputs from native DB: %s", exc)
            if isinstance(exc, NativeSourceUnavailableError):
                raise
            raise NativeSourceUnavailableError(str(exc)) from exc

        manifest_data: dict[str, Any] = {}
        if request.snapshot_id:
            with self.knowledge_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT manifest
                    FROM omp_knowledge.snapshots
                    WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                    """,
                    (request.workspace_id, request.repository_id, request.snapshot_id),
                )
                s_row = cur.fetchone()
                if s_row:
                    manifest_data = s_row["manifest"]

        mandatory_content: dict[str, Any] = {
            "work_revision": work_rev_data,
            "receipts": receipts_data,
            "snapshot_manifest": manifest_data,
        }

        # Measure canonical UTF-8 bytes of mandatory inputs
        mandatory_canonical = canonical_json(mandatory_content)
        mandatory_bytes = len(mandatory_canonical.encode("utf-8"))

        if mandatory_bytes > request.budget.limit:
            raise BudgetInsufficientError(
                f"budget insufficient for mandatory inputs: mandatory={mandatory_bytes} bytes, limit={request.budget.limit} bytes",
                mandatory_used=mandatory_bytes,
                limit=request.budget.limit,
            )

        # 3. Optional enrichment: non-invalidated proposals & facts
        enrichment_status = "unavailable"
        engine_facts: list[dict[str, Any]] = []

        active_snapshot_id: str | None = None
        if request.snapshot_id:
            with self.knowledge_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT snapshot_id
                    FROM omp_knowledge.snapshots
                    WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                    """,
                    (request.workspace_id, request.repository_id, request.snapshot_id),
                )
                if cur.fetchone():
                    active_snapshot_id = request.snapshot_id
        else:
            with self.knowledge_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT snapshot_id
                    FROM omp_knowledge.snapshot_publications
                    WHERE workspace_id = %s AND repository_id = %s AND status = 'published'
                    ORDER BY published_at DESC NULLS LAST, staged_at DESC
                    LIMIT 1
                    """,
                    (request.workspace_id, request.repository_id),
                )
                s_row = cur.fetchone()
                if s_row:
                    active_snapshot_id = s_row["snapshot_id"]

        try:
            status = await engine.status()
            if not status.available:
                enrichment_status = "degraded"
            elif active_snapshot_id:
                q_res = await engine.query(
                    workspace_id=request.workspace_id,
                    repository_id=request.repository_id,
                    snapshot_id=active_snapshot_id,
                    query_text=request.stage,
                    limit=10,
                )
                rows = load_correction_rows(
                    self.knowledge_conn,
                    workspace_id=request.workspace_id,
                    repository_id=request.repository_id,
                )
                q_res = exclude_withdrawn_facts(build_withdrawn_validity(rows, active_snapshot_id), q_res)
                engine_facts = []
                for f in q_res.facts:
                    f_dict = f.model_dump(mode="json")
                    f_dict["node_id"] = str(uuid5(NAMESPACE_OID, f"omp-fact:{f.snapshot_id}:{f.fact_id}"))
                    engine_facts.append(f_dict)
                engine_facts.sort(key=lambda x: str(x.get("fact_id") or ""))
                enrichment_status = "applied"
            else:
                enrichment_status = "unavailable"
        except (EngineUnavailableError, EngineTimeoutError, EngineError, RuntimeError) as exc:
            logger.info("Optional enrichment query failed: %s", exc)
            enrichment_status = "degraded"

        # Read proposals in scope: separate active non-invalidated from invalidated
        available_proposals: list[dict[str, Any]] = []
        invalidated_proposal_ids: list[UUID] = []
        with self.knowledge_conn.cursor() as cur:
            cur.execute(
                """
                SELECT proposal_id, proposal_json, invalidated_at
                FROM omp_knowledge.procedure_proposals
                WHERE workspace_id = %s
                  AND repository_id = %s
                ORDER BY proposal_id ASC
                """,
                (request.workspace_id, request.repository_id),
            )
            for row in cur.fetchall():
                p_json = row["proposal_json"]
                if isinstance(p_json, str):
                    p_json = json.loads(p_json)
                if row["invalidated_at"] is None:
                    available_proposals.append(p_json)
                else:
                    invalidated_proposal_ids.append(UUID(str(row["proposal_id"])))

        # 4. Pack optional content respecting byte limit
        included_optional: dict[str, Any] = {}
        dropped_optional: list[str] = []
        included_proposal_ids: list[UUID] = []
        excluded_proposal_ids: list[UUID] = []

        # Pack proposals
        for prop in available_proposals:
            p_id_str = str(prop["proposal_id"])
            item_key = f"proposal:{p_id_str}"
            trial_optional = {**included_optional, item_key: prop}
            trial_content = {"mandatory": mandatory_content, "optional": trial_optional}
            trial_bytes = len(canonical_json(trial_content).encode("utf-8"))

            if trial_bytes <= request.budget.limit:
                included_optional[item_key] = prop
                included_proposal_ids.append(UUID(p_id_str))
            else:
                dropped_optional.append(item_key)
                excluded_proposal_ids.append(UUID(p_id_str))

        # Preserve invalidated proposal IDs encountered in scope in compiler output while excluding content
        for inv_id in invalidated_proposal_ids:
            if inv_id not in excluded_proposal_ids:
                excluded_proposal_ids.append(inv_id)
        excluded_proposal_ids.sort()

        # Pack engine facts
        for idx, fact in enumerate(engine_facts):
            item_key = f"fact:{idx}"
            trial_optional = {**included_optional, item_key: fact}
            trial_content = {"mandatory": mandatory_content, "optional": trial_optional}
            trial_bytes = len(canonical_json(trial_content).encode("utf-8"))

            if trial_bytes <= request.budget.limit:
                included_optional[item_key] = fact
            else:
                dropped_optional.append(item_key)

        # 5. Measure exact persisted bundle bytes
        final_content = {"mandatory": mandatory_content, "optional": included_optional}
        final_canonical = canonical_json(final_content)
        final_bytes = len(final_canonical.encode("utf-8"))

        budget_actual = BudgetActual(
            method="utf8_bytes",
            limit=request.budget.limit,
            used=final_bytes,
            mandatory_used=mandatory_bytes,
            dropped_optional=tuple(dropped_optional),
        )

        # The encoding tag is part of the hashed bytes: a representation change yields a new
        # bundle_id, so rows compiled under an older encoding stay untouched and historical.
        bundle_identity_payload = {
            "identity_encoding": CONTEXT_BUNDLE_IDENTITY_ENCODING,
            "workspace_id": str(request.workspace_id),
            "repository_id": str(request.repository_id),
            "work_id": str(request.work_id),
            "revision_id": str(request.revision_id),
            "candidate_id": str(request.candidate_id) if request.candidate_id is not None else None,
            "stage": request.stage,
            "snapshot_id": request.snapshot_id,
            "budget_spec": request.budget.model_dump(mode="json"),
            "content": final_content,
        }
        identity_encoding = CONTEXT_BUNDLE_IDENTITY_ENCODING
        identity_canonical_json = canonical_json(bundle_identity_payload)
        bundle_hash = hashlib.sha256(identity_canonical_json.encode("utf-8")).hexdigest()
        bundle_id = uuid5(NAMESPACE_OID, f"omp-context-bundle:{bundle_hash}")
        now = datetime.now(timezone.utc)

        receipt_lineage = tuple(UUID(r["receipt_id"]) for r in receipts_data if "receipt_id" in r)

        # Replay: an existing row is the authority; return its persisted bytes and metadata
        existing = select_bundle_row(self.knowledge_conn, bundle_id)
        if existing:
            return bundle_from_row(existing)

        bundle = ContextBundle(
            bundle_id=bundle_id,
            bundle_sha256=bundle_hash,
            workspace_id=request.workspace_id,
            repository_id=request.repository_id,
            work_id=request.work_id,
            revision_id=request.revision_id,
            candidate_id=request.candidate_id,
            stage=request.stage,
            snapshot_id=request.snapshot_id,
            mandatory=mandatory_content,
            optional=included_optional,
            budget=budget_actual,
            enrichment_status=enrichment_status,
            proposal_lineage=tuple(included_proposal_ids),
            receipt_lineage=receipt_lineage,
            excluded_proposal_ids=tuple(excluded_proposal_ids),
            compiled_at=now,
            identity_encoding=identity_encoding,
            identity_canonical_json=identity_canonical_json,
            content_canonical_json=final_canonical,
        )

        # 6. Persist immutable bundle in knowledge DB
        with self.knowledge_conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO omp_knowledge.context_bundles ({_BUNDLE_COLUMNS}) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT (bundle_id) DO NOTHING
                """,
                (
                    bundle_id,
                    bundle_hash,
                    request.workspace_id,
                    request.repository_id,
                    request.work_id,
                    request.revision_id,
                    request.candidate_id,
                    request.stage,
                    request.snapshot_id,
                    json.dumps([str(p) for p in included_proposal_ids]),
                    json.dumps([str(r) for r in receipt_lineage]),
                    json.dumps(request.budget.model_dump(mode="json")),
                    json.dumps(budget_actual.model_dump(mode="json")),
                    enrichment_status,
                    json.dumps([str(p) for p in excluded_proposal_ids]),
                    json.dumps(final_content),
                    now,
                    identity_encoding,
                    identity_canonical_json,
                    final_canonical,
                ),
            )
            lost_race = cur.rowcount == 0
        if lost_race:
            # A concurrent compile persisted this bundle_id first: the persisted row (its
            # compiled_at, enrichment_status, budget, bytes) is the answer, not our in-memory copy.
            winner = select_bundle_row(self.knowledge_conn, bundle_id)
            self.knowledge_conn.commit()
            if winner is None:
                raise RuntimeError(f"context bundle {bundle_id} conflicted but is not readable")
            return bundle_from_row(winner)
        self.knowledge_conn.commit()
        return bundle
