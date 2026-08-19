from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Protocol
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from omp_work import CONTRACT_VERSION, contract_sha256
from omp_work.integration.importer import TRANSFORMATION_VERSION
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import migration_set_sha256
from omp_work.operations.fingerprints import code_fingerprint, config_fingerprint, transform_sha256
from .canonical import canonical_json, command_sha256, sha256
from .models import Candidate, CommandEnvelope, CompletionInput, EvidenceReceipt, OperationReceipt, OperationState, RelationEdge
from .semantics import completion_blockers, validate_cutover_manifest, would_create_cycle

_RECEIPT_FIELDS = "receipt_id,work_id,revision_id,candidate_id,kind,payload,payload_sha256,artifact_sha256,issuer,issued_at,candidate_sha256,candidate_commit,verdict,independent,remote_ref,remote_commit"

# /center recent-activity projection (OMP-25): applied domain events that mean
# "something moved" — receipts, close proposals, completions. Metadata only.
_ACTIVITY_EVENT_TYPES = ("append_evidence", "request_closeout", "complete_work")
_ACTIVITY_EVENT_KINDS = {"request_closeout": "close_proposed", "complete_work": "completed"}


class WorkStore(Protocol):
    def execute(self, envelope: CommandEnvelope, *, actor_id: UUID, actor_kind: str, required_scope: str) -> tuple[OperationReceipt, dict[str, object]]: ...
    def read(self, workspace_id: UUID, actor_id: UUID, kind: str, value: str, *, candidate_allowlist: frozenset[UUID] | None = None) -> dict[str, object]: ...
    def activity(self, workspace_id: UUID, actor_id: UUID, *, project_id: UUID | None = None, limit: int = 8) -> dict[str, object]: ...

class WorkStoreError(Exception):
    def __init__(self, code: str, diagnostics: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostics = diagnostics


class PostgresWorkStore:
    def __init__(self, config: OperationsConfig) -> None:
        self._config = config

    @contextmanager
    def _transaction(self, workspace_id: UUID, actor_id: UUID, *, serializable: bool = False) -> Iterator[psycopg.Cursor[dict[str, object]]]:
        with psycopg.connect(**self._config.connection_kwargs("omp_work_app"), row_factory=dict_row) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    if serializable:
                        cur.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                    cur.execute("SET LOCAL search_path = pg_catalog")
                    cur.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    yield cur

    def execute(self, envelope: CommandEnvelope, *, actor_id: UUID, actor_kind: str, required_scope: str) -> tuple[OperationReceipt, dict[str, object]]:
        request_hash = command_sha256(envelope)
        for attempt in range(1, 4):
            try:
                return self._execute(envelope, actor_id, actor_kind, required_scope, request_hash, attempt)
            except psycopg.Error as error:
                if error.sqlstate not in {"40001", "40P01"}:
                    raise
                if attempt == 3:
                    raise WorkStoreError("unavailable", ("retry_exhausted",)) from error
        raise AssertionError("unreachable")

    def _execute(self, envelope: CommandEnvelope, actor_id: UUID, actor_kind: str, required_scope: str, request_hash: str, attempt: int) -> tuple[OperationReceipt, dict[str, object]]:
        command = envelope.command
        serializable = command.type in {"create_work_batch", "put_relation", "remove_relation", "finalize_candidate", "activate_cutover"}
        conflict = False
        with self._transaction(envelope.workspace_id, actor_id, serializable=serializable) as cur:
            cur.execute("SELECT request_sha256, response, result_sha256, diagnostics FROM omp_control.idempotent_commands WHERE workspace_id=%s AND operation_id=%s FOR UPDATE", (envelope.workspace_id, envelope.operation_id))
            stored = cur.fetchone()
            if stored:
                if stored["request_sha256"] != request_hash:
                    cur.execute("UPDATE omp_control.idempotent_commands SET conflict_count=conflict_count+1, updated_at=clock_timestamp() WHERE workspace_id=%s AND operation_id=%s", (envelope.workspace_id, envelope.operation_id))
                    self._record_event(cur, envelope, actor_id, actor_kind, {"code": "idempotency_conflict"}, event_type="idempotency_conflict")
                    conflict = True
                else:
                    result = dict(stored["response"] or {})
                    return OperationReceipt(operation_id=envelope.operation_id, request_id=envelope.request_id, state=OperationState.REPLAYED, request_sha256=request_hash, result_sha256=stored["result_sha256"], diagnostics=tuple(stored["diagnostics"])), result
            if not conflict:
                if command.type != "activate_cutover":
                    cur.execute("SELECT first_work_mutation_at, expected_first_request_id FROM omp_control.workspace_authority WHERE workspace_id=%s", (envelope.workspace_id,))
                    authority = cur.fetchone()
                    if authority is None:
                        raise WorkStoreError("cutover_invariant", ("authority_absent",))
                    expected = authority["expected_first_request_id"]
                    if authority["first_work_mutation_at"] is None and expected is not None and (envelope.request_id != expected or command.type != "attest_cutover_plan"):
                        raise WorkStoreError("cutover_invariant", ("awaiting_cutover_plan_attestation",))
                if command.type == "create_work_batch":
                    result = self._create_batch(cur, envelope)
                elif command.type == "revise_work":
                    result = self._revise(cur, envelope)
                elif command.type == "set_work_state":
                    result = self._set_state(cur, envelope)
                elif command.type == "put_relation":
                    result = self._put_relation(cur, envelope, False)
                elif command.type == "remove_relation":
                    result = self._put_relation(cur, envelope, True)
                elif command.type == "set_focus":
                    result = self._set_focus(cur, envelope, False)
                elif command.type == "clear_focus":
                    result = self._set_focus(cur, envelope, True)
                elif command.type == "record_project_health":
                    result = self._project_health(cur, envelope)
                elif command.type == "append_evidence":
                    result = self._append_evidence(cur, envelope)
                elif command.type == "finalize_candidate":
                    result = self._finalize_candidate(cur, envelope)
                elif command.type == "request_closeout":
                    result = self._request_closeout(cur, envelope)
                elif command.type == "complete_work":
                    result = self._complete_work(cur, envelope)
                elif command.type == "activate_cutover":
                    result = self._activate_cutover(cur, envelope)
                elif command.type == "attest_cutover_plan":
                    result = self._attest_cutover_plan(cur, envelope)
                else:
                    raise WorkStoreError("unavailable")
                result_hash = sha256(result)
                self._record_event(cur, envelope, actor_id, actor_kind, result)
                if command.type != "activate_cutover":
                    cur.execute("UPDATE omp_control.workspace_authority SET first_work_mutation_at=clock_timestamp(), first_work_mutation_request_id=%s WHERE workspace_id=%s AND first_work_mutation_at IS NULL", (envelope.request_id, envelope.workspace_id))
                cur.execute("INSERT INTO omp_control.idempotent_commands(workspace_id,operation_id,request_id,correlation_id,command_type,required_scope,request_sha256,state,response,result_sha256,attempt_count,diagnostics) VALUES(%s,%s,%s,%s,%s,%s,%s,'applied',%s,%s,%s,%s)", (envelope.workspace_id, envelope.operation_id, envelope.request_id, envelope.correlation_id, command.type, required_scope, request_hash, json.dumps(result), result_hash, attempt, []))
                return OperationReceipt(operation_id=envelope.operation_id, request_id=envelope.request_id, state=OperationState.APPLIED, request_sha256=request_hash, result_sha256=result_hash), result
        raise WorkStoreError("idempotency_conflict")

    def _record_event(self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope, actor_id: UUID, actor_kind: str, result: dict[str, object], *, event_type: str | None = None) -> None:
        payload = envelope.command.payload
        aggregate_id = getattr(payload, "work_id", envelope.workspace_id)
        if hasattr(payload, "relation"):
            aggregate_id = payload.relation.source_work_id
        elif hasattr(payload, "receipt"):
            aggregate_id = payload.receipt.work_id
        elif hasattr(payload, "input"):
            aggregate_id = payload.input.work_id
        elif hasattr(payload, "slot"):
            aggregate_id = payload.slot.work_id or envelope.workspace_id
        cur.execute("SELECT event_sha256 FROM omp_audit.domain_events WHERE workspace_id=%s AND aggregate_id=%s ORDER BY sequence DESC LIMIT 1", (envelope.workspace_id, aggregate_id))
        previous = cur.fetchone()
        previous_hash = previous["event_sha256"] if previous else None
        payload_hash = sha256(result)
        event_hash = sha256({"aggregate_id": str(aggregate_id), "operation_id": str(envelope.operation_id), "previous_event_sha256": previous_hash, "payload_sha256": payload_hash})
        cur.execute("INSERT INTO omp_audit.domain_events(event_id,workspace_id,aggregate_type,aggregate_id,aggregate_version,actor_id,actor_kind,capability_id,request_id,correlation_id,operation_id,causation_id,event_type,outcome,payload,payload_sha256,previous_event_sha256,event_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'applied',%s,%s,%s,%s)", (uuid4(), envelope.workspace_id, "work_item" if aggregate_id != envelope.workspace_id else "workspace", aggregate_id, int(result.get("row_version", 1)), actor_id, actor_kind, envelope.operation_id, envelope.request_id, envelope.correlation_id, envelope.operation_id, envelope.operation_id, event_type or envelope.command.type, json.dumps(result), payload_hash, previous_hash, event_hash))

    def _create_batch(self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope) -> dict[str, object]:
        payload = envelope.command.payload
        project_ids = {item.project_id for item in payload.items if item.project_id is not None}
        if project_ids:
            cur.execute("SELECT project_id FROM omp_work.projects WHERE workspace_id=%s AND project_id = ANY(%s)", (envelope.workspace_id, [str(project_id) for project_id in project_ids]))
            if {row["project_id"] for row in cur.fetchall()} != project_ids:
                raise WorkStoreError("invalid_request", ("unknown project reference",))
        parent_sources: set[str] = set()
        seen_edges: set[tuple[str, str, str]] = set()
        for relation in payload.relations:
            if relation.kind.value == "parent":
                if relation.source_ref in parent_sources:
                    raise WorkStoreError("invalid_request", ("child has multiple parents",))
                parent_sources.add(relation.source_ref)
            edge_key = (relation.source_ref, relation.target_ref, relation.kind.value)
            if edge_key in seen_edges:
                raise WorkStoreError("invalid_request", ("duplicate relation",))
            seen_edges.add(edge_key)
        cur.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING", (envelope.workspace_id,))
        cur.execute("SELECT next_alias FROM omp_control.workspaces WHERE workspace_id=%s FOR UPDATE", (envelope.workspace_id,))
        start = int(cur.fetchone()["next_alias"])
        items: list[dict[str, object]] = []
        now = datetime.now(timezone.utc)
        ref_to_work_id: dict[str, UUID] = {}
        for offset, item in enumerate(payload.items):
            work_id, revision_id = uuid4(), uuid4()
            title = item.title.strip()
            content_hash = sha256({"title": title, "description": item.description, "scope": item.scope, "acceptance_criteria": list(item.acceptance_criteria)})
            cur.execute("INSERT INTO omp_work.work_items(work_id,workspace_id,state,current_revision_id,project_id) VALUES(%s,%s,%s,%s,%s)", (work_id,envelope.workspace_id,item.state,revision_id,item.project_id))
            cur.execute("INSERT INTO omp_work.work_aliases(work_id,workspace_id,key,origin) VALUES(%s,%s,%s,'local')", (work_id,envelope.workspace_id,f"OMP-{start + offset}"))
            cur.execute("INSERT INTO omp_work.work_revisions(revision_id,work_id,workspace_id,revision_number,title,description,scope,content_sha256,created_by,supplied_at) VALUES(%s,%s,%s,1,%s,%s,%s,%s,%s,%s)", (revision_id,work_id,envelope.workspace_id,title,item.description,item.scope,content_hash,"service",now))
            for position, criterion in enumerate(item.acceptance_criteria):
                cur.execute("INSERT INTO omp_work.acceptance_criteria(revision_id,workspace_id,position,criterion) VALUES(%s,%s,%s,%s)", (revision_id,envelope.workspace_id,position,criterion))
            ref_to_work_id[item.client_ref] = work_id
            items.append({"client_ref": item.client_ref, "work_id": str(work_id), "revision_id": str(revision_id), "key": f"OMP-{start + offset}", "state": item.state, "row_version": 1})
        edges: list[RelationEdge] = []
        for relation in payload.relations:
            source, target = ref_to_work_id[relation.source_ref], ref_to_work_id[relation.target_ref]
            if relation.kind.value == "related" and str(source) > str(target):
                source, target = target, source
            edge = RelationEdge(workspace_id=envelope.workspace_id, source_work_id=source, target_work_id=target, kind=relation.kind)
            if would_create_cycle(tuple(edges), edge):
                raise WorkStoreError("relation_cycle")
            edges.append(edge)
            cur.execute("INSERT INTO omp_work.work_relations(relation_id,workspace_id,source_work_id,target_work_id,kind) VALUES(%s,%s,%s,%s,%s)", (uuid4(),envelope.workspace_id,source,target,relation.kind.value))
        cur.execute("UPDATE omp_control.workspaces SET next_alias=next_alias+%s WHERE workspace_id=%s", (len(items),envelope.workspace_id))
        return {"type": "create_work_batch", "items": items}
    def _revise(self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope) -> dict[str, object]:
        payload = envelope.command.payload
        revision = payload.revision
        if revision.work_id != payload.work_id:
            raise WorkStoreError("invalid_request")
        cur.execute("SELECT current_revision_id FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s FOR UPDATE", (envelope.workspace_id,payload.work_id))
        current = cur.fetchone()
        if not current or current["current_revision_id"] != payload.expected_revision_id:
            raise WorkStoreError("revision_conflict")
        cur.execute("SELECT content_sha256,revision_number FROM omp_work.work_revisions WHERE revision_id=%s", (payload.expected_revision_id,))
        previous = cur.fetchone()
        if previous["content_sha256"] == revision.content_sha256:
            return {"type": "revise_work", "revision_id": str(payload.expected_revision_id), "changed": False}
        if revision.revision_number != previous["revision_number"] + 1:
            raise WorkStoreError("revision_conflict")
        cur.execute("INSERT INTO omp_work.work_revisions(revision_id,work_id,workspace_id,revision_number,title,description,scope,content_sha256,created_by,supplied_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (revision.revision_id,revision.work_id,envelope.workspace_id,revision.revision_number,revision.title,revision.description,revision.scope,revision.content_sha256,revision.created_by,revision.created_at))
        for position, criterion in enumerate(revision.acceptance_criteria):
            cur.execute("INSERT INTO omp_work.acceptance_criteria(revision_id,workspace_id,position,criterion) VALUES(%s,%s,%s,%s)", (revision.revision_id,envelope.workspace_id,position,criterion))
        cur.execute("UPDATE omp_work.work_items SET current_revision_id=%s,current_candidate_id=NULL,row_version=row_version+1 WHERE work_id=%s", (revision.revision_id,payload.work_id))
        return {"type": "revise_work", "revision_id": str(revision.revision_id), "changed": True}

    def _put_relation(self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope, remove: bool) -> dict[str, object]:
        relation = envelope.command.payload.relation
        if relation.workspace_id != envelope.workspace_id or relation.source_work_id == relation.target_work_id:
            raise WorkStoreError("invalid_request")
        source, target = relation.source_work_id, relation.target_work_id
        if relation.kind.value == "related" and str(source) > str(target):
            source, target = target, source
        if remove:
            cur.execute("UPDATE omp_work.work_relations SET active=false,revoked_at=clock_timestamp() WHERE workspace_id=%s AND source_work_id=%s AND target_work_id=%s AND kind=%s AND active RETURNING relation_id", (envelope.workspace_id,source,target,relation.kind.value))
            if not cur.fetchone():
                raise WorkStoreError("revision_conflict")
            return {"type": "remove_relation", "source_work_id": str(source), "target_work_id": str(target), "kind": relation.kind.value, "active": False}
        cur.execute("WITH RECURSIVE path(id) AS (SELECT target_work_id FROM omp_work.work_relations WHERE workspace_id=%s AND source_work_id=%s AND kind=%s AND active UNION SELECT r.target_work_id FROM omp_work.work_relations r JOIN path p ON r.source_work_id=p.id WHERE r.workspace_id=%s AND r.kind=%s AND r.active) SELECT 1 FROM path WHERE id=%s", (envelope.workspace_id,target,relation.kind.value,envelope.workspace_id,relation.kind.value,source))
        if relation.kind.value != "related" and cur.fetchone():
            raise WorkStoreError("relation_cycle")
        cur.execute("INSERT INTO omp_work.work_relations(relation_id,workspace_id,source_work_id,target_work_id,kind) VALUES(%s,%s,%s,%s,%s)", (uuid4(),envelope.workspace_id,source,target,relation.kind.value))
        return {"type": "put_relation", "source_work_id": str(source), "target_work_id": str(target), "kind": relation.kind.value, "active": True}

    def _append_evidence(self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope) -> dict[str, object]:
        receipt = envelope.command.payload.receipt
        if sha256(receipt.payload) != receipt.payload_sha256:
            raise WorkStoreError("invalid_request", ("payload_sha256 does not match the canonical payload body",))
        cur.execute("SELECT current_revision_id,current_candidate_id FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s FOR UPDATE", (envelope.workspace_id, receipt.work_id))
        item = cur.fetchone()
        if not item or item["current_revision_id"] != receipt.revision_id:
            raise WorkStoreError("stale_evidence")
        if receipt.kind.value == "plan":
            if not receipt.candidate_sha256:
                raise WorkStoreError("stale_evidence")
            current_candidate_id = item["current_candidate_id"]
            if current_candidate_id is not None:
                cur.execute("SELECT kind FROM omp_work.candidates WHERE workspace_id=%s AND candidate_id=%s", (envelope.workspace_id, current_candidate_id))
                current = cur.fetchone()
                cur.execute("SELECT verdict FROM omp_evidence.receipts WHERE workspace_id=%s AND candidate_id=%s AND kind='audit' ORDER BY issued_at DESC, receipt_id DESC LIMIT 1", (envelope.workspace_id, current_candidate_id))
                latest_audit = cur.fetchone()
                if current is None or current["kind"] != "final" or latest_audit is None or latest_audit["verdict"] not in ("NEEDS_FIX", "BLOCKED"):
                    raise WorkStoreError("stale_evidence")
            cur.execute("INSERT INTO omp_work.candidates(candidate_id,workspace_id,work_id,revision_id,candidate_sha256,commit_sha,allocated_at) VALUES(%s,%s,%s,%s,%s,%s,%s)", (receipt.candidate_id, envelope.workspace_id, receipt.work_id, receipt.revision_id, receipt.candidate_sha256, receipt.candidate_commit, receipt.issued_at))
            cur.execute("UPDATE omp_work.work_items SET current_candidate_id=%s WHERE work_id=%s", (receipt.candidate_id, receipt.work_id))
        else:
            if item["current_candidate_id"] != receipt.candidate_id:
                raise WorkStoreError("stale_evidence")
            cur.execute("SELECT candidate_sha256,commit_sha FROM omp_work.candidates WHERE candidate_id=%s", (receipt.candidate_id,))
            candidate = cur.fetchone()
            if candidate is None or (
                receipt.kind.value in {"verification", "audit"}
                and (receipt.candidate_sha256 != candidate["candidate_sha256"] or receipt.candidate_commit != candidate["commit_sha"])
            ):
                raise WorkStoreError("stale_evidence")
        cur.execute("INSERT INTO omp_evidence.receipts(receipt_id,workspace_id,work_id,revision_id,candidate_id,kind,payload,payload_sha256,artifact_sha256,issuer,issued_at,candidate_sha256,candidate_commit,verdict,independent,remote_ref,remote_commit) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (receipt.receipt_id, envelope.workspace_id, receipt.work_id, receipt.revision_id, receipt.candidate_id, receipt.kind.value, canonical_json(receipt.payload), receipt.payload_sha256, receipt.artifact_sha256, receipt.issuer, receipt.issued_at, receipt.candidate_sha256, receipt.candidate_commit, receipt.verdict, receipt.independent, receipt.remote_ref, receipt.remote_commit))
        return {"type": "append_evidence", "receipt": receipt.model_dump(mode="json")}

    def _finalize_candidate(self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope) -> dict[str, object]:
        payload = envelope.command.payload
        cur.execute("SELECT current_revision_id,current_candidate_id FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s FOR UPDATE", (envelope.workspace_id, payload.work_id))
        item = cur.fetchone()
        if not item or item["current_revision_id"] != payload.revision_id or item["current_candidate_id"] != payload.planned_candidate_id:
            raise WorkStoreError("stale_evidence")
        cur.execute("SELECT candidate_id,work_id,revision_id,kind FROM omp_work.candidates WHERE workspace_id=%s AND candidate_id=%s", (envelope.workspace_id, payload.planned_candidate_id))
        planned = cur.fetchone()
        if planned is None or planned["work_id"] != payload.work_id or planned["revision_id"] != payload.revision_id or planned["kind"] != "planned":
            raise WorkStoreError("stale_evidence")
        cur.execute(f"SELECT {_RECEIPT_FIELDS} FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s AND revision_id=%s AND candidate_id=%s AND kind='plan' ORDER BY issued_at,receipt_id LIMIT 1", (envelope.workspace_id, payload.work_id, payload.revision_id, payload.planned_candidate_id))
        plan = cur.fetchone()
        if plan is None:
            raise WorkStoreError("stale_evidence")
        now = datetime.now(timezone.utc)
        cur.execute("INSERT INTO omp_work.candidates(candidate_id,workspace_id,work_id,revision_id,candidate_sha256,commit_sha,kind,allocated_at) VALUES(%s,%s,%s,%s,%s,%s,'final',%s)", (payload.candidate_id, envelope.workspace_id, payload.work_id, payload.revision_id, payload.candidate_sha256, payload.commit_sha, now))
        derived_receipt_id = uuid4()
        cur.execute("INSERT INTO omp_evidence.receipts(receipt_id,workspace_id,work_id,revision_id,candidate_id,kind,payload,payload_sha256,artifact_sha256,issuer,issued_at,candidate_sha256,candidate_commit,verdict,independent,remote_ref,remote_commit) VALUES(%s,%s,%s,%s,%s,'plan',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (derived_receipt_id, envelope.workspace_id, payload.work_id, payload.revision_id, payload.candidate_id, canonical_json(plan["payload"]), plan["payload_sha256"], plan["artifact_sha256"], plan["issuer"], now, payload.candidate_sha256, payload.commit_sha, plan["verdict"], plan["independent"], plan["remote_ref"], plan["remote_commit"]))
        cur.execute("UPDATE omp_work.work_items SET current_candidate_id=%s WHERE workspace_id=%s AND work_id=%s", (payload.candidate_id, envelope.workspace_id, payload.work_id))
        candidate = {"candidate_id": str(payload.candidate_id), "work_id": str(payload.work_id), "revision_id": str(payload.revision_id), "candidate_sha256": payload.candidate_sha256, "commit_sha": payload.commit_sha, "kind": "final", "allocated_at": now.isoformat()}
        return {"type": "finalize_candidate", "candidate": candidate}

    def _request_closeout(self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope) -> dict[str, object]:
        work_id = envelope.command.payload.work_id
        cur.execute("SELECT current_revision_id,current_candidate_id FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s FOR UPDATE", (envelope.workspace_id,work_id))
        item = cur.fetchone()
        if not item or item["current_candidate_id"] is None:
            raise WorkStoreError("completion_blocked")
        cur.execute("SELECT intent_id,work_id,revision_id,candidate_id,state,requested_at FROM omp_evidence.closeout_intents WHERE work_id=%s AND revision_id=%s AND candidate_id=%s AND state='pending'", (work_id,item["current_revision_id"],item["current_candidate_id"]))
        intent = cur.fetchone()
        if intent is None:
            intent_id = uuid4()
            cur.execute("INSERT INTO omp_evidence.closeout_intents(intent_id,workspace_id,work_id,revision_id,candidate_id) VALUES(%s,%s,%s,%s,%s) RETURNING intent_id,work_id,revision_id,candidate_id,state,requested_at", (intent_id,envelope.workspace_id,work_id,item["current_revision_id"],item["current_candidate_id"]))
            intent = cur.fetchone()
        return {"type": "request_closeout", "intent": {key: str(value) if isinstance(value, UUID) else value.isoformat() if isinstance(value, datetime) else value for key, value in intent.items()}}

    def _complete_work(self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope) -> dict[str, object]:

        submitted = envelope.command.payload.input
        cur.execute("SELECT current_revision_id,current_candidate_id FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s FOR UPDATE", (envelope.workspace_id, submitted.work_id))
        item = cur.fetchone()
        if not item or item["current_revision_id"] != submitted.current_revision_id or item["current_candidate_id"] is None:
            raise WorkStoreError("stale_evidence")
        cur.execute("SELECT candidate_id,work_id,revision_id,candidate_sha256,commit_sha,kind,allocated_at FROM omp_work.candidates WHERE workspace_id=%s AND candidate_id=%s", (envelope.workspace_id, item["current_candidate_id"]))
        row = cur.fetchone()
        if row is None:
            raise WorkStoreError("stale_evidence")
        candidate = Candidate.model_validate(row)
        cur.execute(f"SELECT {_RECEIPT_FIELDS} FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s AND revision_id=%s AND candidate_id=%s ORDER BY issued_at,receipt_id", (envelope.workspace_id, submitted.work_id, item["current_revision_id"], candidate.candidate_id))
        receipts = tuple(EvidenceReceipt.model_validate(receipt) for receipt in cur.fetchall())
        cur.execute("SELECT intent_id FROM omp_evidence.closeout_intents WHERE workspace_id=%s AND work_id=%s AND revision_id=%s AND candidate_id=%s AND state='pending' FOR UPDATE", (envelope.workspace_id, submitted.work_id, item["current_revision_id"], candidate.candidate_id))
        if cur.fetchone() is None:
            raise WorkStoreError("completion_blocked")
        persisted = CompletionInput(work_id=submitted.work_id, current_revision_id=item["current_revision_id"], candidate=candidate, receipts=receipts, closeout_requested=True)
        if not submitted.closeout_requested or submitted.candidate != candidate or submitted.receipts != receipts:
            raise WorkStoreError("stale_evidence")
        if completion_blockers(persisted):
            raise WorkStoreError("completion_blocked")
        cur.execute("UPDATE omp_evidence.closeout_intents SET state='completed',completed_at=clock_timestamp() WHERE workspace_id=%s AND work_id=%s AND revision_id=%s AND candidate_id=%s AND state='pending'", (envelope.workspace_id, submitted.work_id, item["current_revision_id"], candidate.candidate_id))
        cur.execute("UPDATE omp_work.work_items SET state='DONE',row_version=row_version+1 WHERE workspace_id=%s AND work_id=%s AND current_revision_id=%s AND current_candidate_id=%s AND state<>'DONE' RETURNING row_version", (envelope.workspace_id, submitted.work_id, item["current_revision_id"], candidate.candidate_id))
        result = cur.fetchone()
        if result is None:
            raise WorkStoreError("stale_evidence")
        return {"type": "complete_work", "work_id": str(submitted.work_id), "state": "DONE", "row_version": result["row_version"]}

    def _set_state(self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope) -> dict[str, object]:
        payload = envelope.command.payload
        if not payload.state.strip() or payload.state == "DONE":
            raise WorkStoreError("invalid_request")
        cur.execute("UPDATE omp_work.work_items SET state=%s,row_version=row_version+1 WHERE workspace_id=%s AND work_id=%s AND state<>'DONE' RETURNING row_version", (payload.state,envelope.workspace_id,payload.work_id))
        row = cur.fetchone()
        if not row:
            raise WorkStoreError("revision_conflict")
        return {"type": "set_work_state", "work_id": str(payload.work_id), "state": payload.state, "row_version": row["row_version"]}

    def _set_focus(self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope, clear: bool) -> dict[str, object]:
        payload = envelope.command.payload
        owner_id = payload.owner_id if clear else payload.slot.owner_id
        expected = payload.expected_version
        work_id = None if clear else payload.slot.work_id
        cur.execute("SELECT version FROM omp_work.focus_slots WHERE workspace_id=%s AND owner_id=%s FOR UPDATE", (envelope.workspace_id,owner_id))
        row = cur.fetchone()
        if row is None:
            if expected != 0:
                raise WorkStoreError("focus_conflict")
            cur.execute("INSERT INTO omp_work.focus_slots(workspace_id,owner_id,work_id,version) VALUES(%s,%s,%s,1)", (envelope.workspace_id,owner_id,work_id))
            version=1
        else:
            if row["version"] != expected:
                raise WorkStoreError("focus_conflict")
            cur.execute("UPDATE omp_work.focus_slots SET work_id=%s,version=version+1 WHERE workspace_id=%s AND owner_id=%s RETURNING version", (work_id,envelope.workspace_id,owner_id))
            version=cur.fetchone()["version"]
        return {"type": "clear_focus" if clear else "set_focus", "workspace_id": str(envelope.workspace_id), "owner_id": str(owner_id), "work_id": str(work_id) if work_id else None, "version": version}

    def _project_health(self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope) -> dict[str, object]:
        payload = envelope.command.payload
        cur.execute("INSERT INTO omp_work.project_health(workspace_id,project_id,health) VALUES(%s,%s,%s) ON CONFLICT(workspace_id,project_id) DO UPDATE SET health=EXCLUDED.health,updated_at=clock_timestamp() RETURNING updated_at", (envelope.workspace_id,payload.project_id,payload.health))
        return {"type": "record_project_health", "health": {"workspace_id": str(envelope.workspace_id), "project_id": str(payload.project_id), "health": payload.health, "updated_at": cur.fetchone()["updated_at"].isoformat()}}

    def _activate_cutover(self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope) -> dict[str, object]:
        manifest = envelope.command.payload.manifest
        workspace_id = envelope.workspace_id

        def reject(*diagnostics: str) -> None:
            raise WorkStoreError("cutover_invariant", diagnostics)

        cur.execute("SELECT epoch_id FROM omp_control.workspace_authority WHERE workspace_id=%s", (workspace_id,))
        if cur.fetchone() is not None:
            reject("authority_already_active")
        if manifest.contract_version != CONTRACT_VERSION or manifest.contract_sha256 != contract_sha256():
            reject("contract_fingerprint_mismatch")
        if manifest.schema_sha256 != migration_set_sha256():
            reject("schema_fingerprint_mismatch")
        if manifest.transform_version != TRANSFORMATION_VERSION or manifest.transform_sha256 != transform_sha256():
            reject("transform_fingerprint_mismatch")
        if manifest.code_fingerprint != code_fingerprint() or manifest.config_fingerprint != config_fingerprint(self._config):
            reject("code_config_fingerprint_mismatch")
        try:
            validate_cutover_manifest(manifest.anomalies, manifest.parity_differences)
        except ValueError:
            reject("manifest_invariants_failed")
        if not manifest.command_smoke_results or any(not smoke.passed for smoke in manifest.command_smoke_results):
            reject("command_smoke_failed")

        cur.execute("SELECT export_id, state, parity_hashes FROM omp_integration.import_batches WHERE workspace_id=%s AND batch_id=%s", (workspace_id, manifest.import_batch_id))
        batch = cur.fetchone()
        if batch is None or batch["state"] != "promoted":
            reject("import_batch_not_promoted")
        persisted = dict(batch["parity_hashes"] or {})
        if persisted.get("dimension_counts") != manifest.dimension_counts.model_dump() or persisted.get("dimension_hashes") != manifest.dimension_hashes.model_dump():
            reject("dimension_parity_mismatch")
        if persisted.get("parity_groups") != manifest.parity_groups:
            reject("parity_group_mismatch")
        cur.execute("SELECT source_boundary, source_watermark, raw_export_sha256, state FROM omp_integration.raw_exports WHERE workspace_id=%s AND export_id=%s", (workspace_id, batch["export_id"]))
        export = cur.fetchone()
        if export is None or export["state"] != "complete" or export["raw_export_sha256"] != manifest.raw_export_sha256 or export["source_boundary"].isoformat() != manifest.source_boundary:
            reject("source_boundary_mismatch")
        if export["source_watermark"] is None or export["source_watermark"].isoformat() != manifest.source_watermark:
            reject("source_watermark_mismatch")
        cur.execute("SELECT export_id FROM omp_integration.raw_exports WHERE workspace_id=%s AND state='complete' ORDER BY completed_at DESC, export_id DESC LIMIT 1", (workspace_id,))
        latest = cur.fetchone()
        if latest is None or latest["export_id"] != batch["export_id"]:
            reject("stale_import_batch")
        cur.execute("SELECT count(*) AS n FROM omp_integration.migration_anomalies WHERE workspace_id=%s AND batch_id=%s AND disposition='blocking'", (workspace_id, manifest.import_batch_id))
        if cur.fetchone()["n"]:
            reject("blocking_anomalies")
        for kind, outcome_pattern, receipt in (("backup", "passed", manifest.backup_receipt_sha256), ("restore_drill", "passed:%", manifest.restore_receipt_sha256)):
            cur.execute("SELECT 1 FROM omp_control.operations_evidence WHERE kind=%s AND outcome LIKE %s AND receipt_sha256=%s", (kind, outcome_pattern, receipt))
            if cur.fetchone() is None:
                reject(f"{kind}_receipt_mismatch")

        manifest_json = json.loads(manifest.model_dump_json())
        manifest_hash = sha256(manifest_json)
        cur.execute("INSERT INTO omp_control.cutover_epochs(epoch_id,workspace_id,state,candidate_manifest,candidate_manifest_sha256,linear_credential_sha256,activated_at) VALUES(%s,%s,'active',%s,%s,%s,clock_timestamp())", (manifest.epoch_id, workspace_id, json.dumps(manifest_json), manifest_hash, manifest.linear_credential_sha256))
        cur.execute("INSERT INTO omp_control.workspace_authority(workspace_id,epoch_id,activated_at,expected_first_request_id) VALUES(%s,%s,clock_timestamp(),%s) RETURNING activated_at", (workspace_id, manifest.epoch_id, manifest.first_mutation_request_id))
        activated_at = cur.fetchone()["activated_at"]
        return {"type": "activate_cutover", "epoch_id": str(manifest.epoch_id), "authority": "work", "candidate_manifest_sha256": manifest_hash, "activated_at": activated_at.isoformat()}

    def _attest_cutover_plan(self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope) -> dict[str, object]:
        """The anointed first mutation. Every field must reproduce the sealed manifest
        exactly; the command mutates no candidate state, so the gate-nominated request
        cannot be rejected by domain rules."""
        payload = envelope.command.payload
        cur.execute("SELECT epoch_id, first_work_mutation_at FROM omp_control.workspace_authority WHERE workspace_id=%s", (envelope.workspace_id,))
        authority = cur.fetchone()
        if authority is None or authority["epoch_id"] != payload.epoch_id:
            raise WorkStoreError("cutover_invariant", ("attestation_epoch_mismatch",))
        if authority["first_work_mutation_at"] is not None:
            raise WorkStoreError("cutover_invariant", ("attestation_must_be_first_mutation",))
        cur.execute("SELECT state, candidate_manifest FROM omp_control.cutover_epochs WHERE epoch_id=%s AND workspace_id=%s", (payload.epoch_id, envelope.workspace_id))
        epoch = cur.fetchone()
        if epoch is None or epoch["state"] != "active":
            raise WorkStoreError("cutover_invariant", ("attestation_epoch_not_active",))
        manifest = dict(epoch["candidate_manifest"] or {})
        if payload.plan_sha256 != manifest.get("plan_sha256") or payload.plan_name != manifest.get("plan_name") or str(payload.work_id) != str(manifest.get("plan_work_id")):
            raise WorkStoreError("cutover_invariant", ("attestation_manifest_mismatch",))
        cur.execute("SELECT 1 FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s", (envelope.workspace_id, payload.work_id))
        if cur.fetchone() is None:
            raise WorkStoreError("invalid_request", ("attestation work item absent",))
        # Transaction-side cutoff: a client timeout cannot stop a commit, so the
        # anointed mutation itself refuses past freeze_at + the plan's one-hour window.
        cur.execute("SELECT clock_timestamp() > (%s::timestamptz + interval '60 minutes') AS expired", (str(manifest.get("freeze_at")),))
        if cur.fetchone()["expired"]:
            raise WorkStoreError("cutover_invariant", ("attestation_window_expired",))
        cur.execute("INSERT INTO omp_control.cutover_plan_attestations(workspace_id,epoch_id,work_id,request_id,plan_name,plan_sha256,plan_artifact,issuer) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)", (envelope.workspace_id, payload.epoch_id, payload.work_id, envelope.request_id, payload.plan_name, payload.plan_sha256, payload.plan_artifact, str(manifest.get("actor", "owner"))))
        return {"type": "attest_cutover_plan", "epoch_id": str(payload.epoch_id), "work_id": str(payload.work_id), "plan_sha256": payload.plan_sha256}

    def _item_view(self, cur: psycopg.Cursor[dict[str, object]], workspace_id: UUID, *, key: str, candidate_allowlist: frozenset[UUID] | None = None) -> dict[str, object]:
        cur.execute("SELECT i.work_id,i.workspace_id,i.state,i.project_id,i.archived,i.current_candidate_id,a.key,a.origin,r.revision_id,r.revision_number,r.title,r.description,r.scope,r.content_sha256,r.created_by,r.supplied_at FROM omp_work.work_items i JOIN omp_work.work_aliases a ON a.work_id=i.work_id AND a.primary_alias JOIN omp_work.work_revisions r ON r.revision_id=i.current_revision_id WHERE i.workspace_id=%s AND a.key=%s", (workspace_id, key))
        row = cur.fetchone()
        if not row:
            raise WorkStoreError("invalid_request")
        if candidate_allowlist is not None and row["current_candidate_id"] not in candidate_allowlist:
            raise WorkStoreError("forbidden")
        cur.execute("SELECT criterion FROM omp_work.acceptance_criteria WHERE revision_id=%s ORDER BY position", (row["revision_id"],))
        criteria = [entry["criterion"] for entry in cur.fetchall()]
        candidate = None
        if row["current_candidate_id"] is not None:
            cur.execute("SELECT candidate_id,work_id,revision_id,candidate_sha256,commit_sha,kind,allocated_at FROM omp_work.candidates WHERE candidate_id=%s", (row["current_candidate_id"],))
            candidate_row = cur.fetchone()
            candidate = dict(candidate_row) if candidate_row else None
        return {
            "work_id": row["work_id"],
            "workspace_id": row["workspace_id"],
            "alias": {"work_id": row["work_id"], "key": row["key"], "primary": True, "origin": row["origin"]},
            "state": row["state"],
            "revision": {"revision_id": row["revision_id"], "work_id": row["work_id"], "revision_number": row["revision_number"], "title": row["title"], "description": row["description"], "scope": row["scope"], "acceptance_criteria": criteria, "content_sha256": row["content_sha256"], "created_by": row["created_by"], "created_at": row["supplied_at"]},
            "candidate": candidate,
            "project_id": row["project_id"],
            "archived": row["archived"],
        }

    def read(self, workspace_id: UUID, actor_id: UUID, kind: str, value: str, *, candidate_allowlist: frozenset[UUID] | None = None) -> dict[str, object]:
        with self._transaction(workspace_id, actor_id) as cur:
            if kind == "item":
                return self._item_view(cur, workspace_id, key=value, candidate_allowlist=candidate_allowlist)
            if kind == "workflow":
                item = self._item_view(cur, workspace_id, key=value, candidate_allowlist=candidate_allowlist)
                work_id = item["work_id"]
                cur.execute("SELECT workspace_id,source_work_id,target_work_id,kind,active FROM omp_work.work_relations WHERE workspace_id=%s AND (source_work_id=%s OR target_work_id=%s) ORDER BY created_at", (workspace_id, work_id, work_id))
                relations = [dict(row) for row in cur.fetchall()]
                cur.execute(f"SELECT {_RECEIPT_FIELDS} FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s ORDER BY issued_at,receipt_id", (workspace_id, work_id))
                receipts = [dict(row) for row in cur.fetchall()]
                cur.execute("SELECT intent_id,work_id,revision_id,candidate_id,state,requested_at FROM omp_evidence.closeout_intents WHERE workspace_id=%s AND work_id=%s ORDER BY requested_at,intent_id", (workspace_id, work_id))
                closeout = [dict(row) for row in cur.fetchall()]
                project = None
                if item["project_id"] is not None:
                    cur.execute("SELECT p.project_id,p.workspace_id,p.key,p.name,h.health,h.updated_at AS health_updated_at FROM omp_work.projects p LEFT JOIN omp_work.project_health h ON h.workspace_id=p.workspace_id AND h.project_id=p.project_id WHERE p.workspace_id=%s AND p.project_id=%s", (workspace_id, item["project_id"]))
                    project_row = cur.fetchone()
                    project = dict(project_row) if project_row else None
                return {"item": item, "relations": relations, "receipts": receipts, "closeout": closeout, "project": project}
            if kind == "tree":
                cur.execute("SELECT a.key FROM omp_work.work_items i JOIN omp_work.work_aliases a ON a.work_id=i.work_id AND a.primary_alias WHERE i.workspace_id=%s ORDER BY a.key LIMIT 1000", (workspace_id,))
                items = [self._item_view(cur, workspace_id, key=row["key"]) for row in cur.fetchall()]
                cur.execute("SELECT workspace_id,source_work_id,target_work_id,kind,active FROM omp_work.work_relations WHERE workspace_id=%s ORDER BY created_at LIMIT 5000", (workspace_id,))
                relations = [dict(row) for row in cur.fetchall()]
                cur.execute("SELECT p.project_id,p.workspace_id,p.key,p.name,h.health,h.updated_at AS health_updated_at FROM omp_work.projects p LEFT JOIN omp_work.project_health h ON h.workspace_id=p.workspace_id AND h.project_id=p.project_id WHERE p.workspace_id=%s ORDER BY p.name LIMIT 500", (workspace_id,))
                projects = [dict(row) for row in cur.fetchall()]
                return {"workspace_id": workspace_id, "items": items, "relations": relations, "projects": projects}
            if kind == "focus":
                cur.execute("SELECT workspace_id,owner_id,work_id,version FROM omp_work.focus_slots WHERE workspace_id=%s AND owner_id=%s", (workspace_id,UUID(value)))
                return dict(cur.fetchone() or {"workspace_id": str(workspace_id), "owner_id": value, "work_id": None, "version": 0})
            if kind == "operation":
                cur.execute("SELECT operation_id,request_id,correlation_id,command_type,state,response,result_sha256,request_sha256,diagnostics FROM omp_control.idempotent_commands WHERE workspace_id=%s AND operation_id=%s", (workspace_id,UUID(value)))
                row = cur.fetchone()
                if not row or not row["result_sha256"]:
                    raise WorkStoreError("invalid_request")
                return {"receipt": {"operation_id": row["operation_id"], "request_id": row["request_id"], "state": row["state"], "request_sha256": row["request_sha256"], "result_sha256": row["result_sha256"], "diagnostics": list(row["diagnostics"])}, "command_type": row["command_type"], "request_id": row["request_id"], "correlation_id": row["correlation_id"], "result": row["response"]}
            if kind == "authority":
                cur.execute("SELECT a.epoch_id,a.activated_at,a.first_work_mutation_at,e.state AS epoch_state FROM omp_control.workspace_authority a JOIN omp_control.cutover_epochs e ON e.epoch_id=a.epoch_id AND e.workspace_id=a.workspace_id WHERE a.workspace_id=%s", (workspace_id,))
                row = cur.fetchone()
                if not row:
                    return {"authority": "linear", "epoch_id": None, "epoch_state": None, "activated_at": None, "first_work_mutation_at": None}
                return {"authority": "work", "epoch_id": str(row["epoch_id"]), "epoch_state": row["epoch_state"], "activated_at": row["activated_at"].isoformat(), "first_work_mutation_at": row["first_work_mutation_at"].isoformat() if row["first_work_mutation_at"] else None}
            raise WorkStoreError("invalid_request")

    def activity(self, workspace_id: UUID, actor_id: UUID, *, project_id: UUID | None = None, limit: int = 8) -> dict[str, object]:
        if not 1 <= limit <= 20:
            raise WorkStoreError("invalid_request", ("limit must be between 1 and 20",))
        with self._transaction(workspace_id, actor_id) as cur:
            filters = "e.workspace_id=%s AND e.outcome='applied' AND e.event_type = ANY(%s)"
            params: list[object] = [workspace_id, list(_ACTIVITY_EVENT_TYPES)]
            if project_id is not None:
                filters += " AND i.project_id=%s"
                params.append(project_id)
            base = f"FROM omp_audit.domain_events e JOIN omp_work.work_items i ON i.work_id=e.aggregate_id JOIN omp_work.work_aliases a ON a.work_id=i.work_id AND a.primary_alias JOIN omp_work.work_revisions r ON r.revision_id=i.current_revision_id WHERE {filters}"
            cur.execute(f"SELECT count(*) AS total {base}", params)
            total = int(cur.fetchone()["total"])
            cur.execute(f"SELECT e.event_type,e.payload,e.occurred_at,i.work_id,i.project_id,a.key,r.title {base} ORDER BY e.sequence DESC LIMIT %s", [*params, limit])
            events: list[dict[str, object]] = []
            for row in cur.fetchall():
                # Normalized metadata ONLY — receipt bodies and audit payloads never leave here.
                payload = row["payload"] if isinstance(row["payload"], dict) else {}
                receipt = payload.get("receipt") if isinstance(payload.get("receipt"), dict) else {}
                kind = _ACTIVITY_EVENT_KINDS.get(row["event_type"]) or str(receipt.get("kind") or "evidence")
                events.append({"kind": kind, "work_id": str(row["work_id"]), "key": row["key"], "title": row["title"], "project_id": str(row["project_id"]) if row["project_id"] else None, "occurred_at": row["occurred_at"].isoformat()})
            return {"workspace_id": str(workspace_id), "total": total, "events": events}
