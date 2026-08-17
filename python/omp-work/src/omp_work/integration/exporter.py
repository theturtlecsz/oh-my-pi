from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256 as bytes_sha256
from pathlib import Path
import json
import re
import shutil
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from omp_work.operations.artifacts import decrypt_file, encrypt_file, read_json_artifact, resolve_artifact_path, write_json_artifact
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import _connect
from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.models import Anomaly, ReconciliationCounts, ReconciliationHashes, RelationEdge, RelationKind
from omp_work.v1.semantics import would_create_cycle
from .linear import LinearClient, LinearStream, load_credential, refresh_credential

DIMENSIONS = ("worlds", "surfaces", "promises", "work_items", "states", "labels", "relations", "comments", "attachments", "users")
WORKFLOW_PREFIXES = ("**Plan approved**", "**Execution handoff**", "**Session review**", "**Close proposed**", "**Owner verdict in session:")
UNFILTERABLE = {LinearStream.initiative_projects, LinearStream.relations, LinearStream.attachments}
SCHEMA_VERSION = "linear-export/v1"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourcePage(_Strict):
    schema_version: str = SCHEMA_VERSION
    export_id: UUID
    stream: str
    page_index: int = Field(ge=0)
    request_cursor: str | None = None
    end_cursor: str | None = None
    has_next_page: bool
    nodes: tuple[dict[str, Any], ...]


class ArtifactRecord(_Strict):
    path: str
    plaintext_sha256: str
    ciphertext_sha256: str
    variables_sha256: str | None = None
    stream: str | None = None
    page_index: int | None = None
    request_cursor: str | None = None
    end_cursor: str | None = None
    has_next_page: bool | None = None


class CursorRecord(_Strict):
    page_index: int
    request_cursor: str | None
    end_cursor: str | None
    has_next_page: bool
    scanned_count: int
    retained_count: int
    cumulative_count: int
    plaintext_sha256: str
    ciphertext_sha256: str
    artifact_path: str
    variables_sha256: str


class ExportRun(_Strict):
    export_id: UUID
    workspace_id: UUID
    mode: str
    base_export_id: UUID | None
    state: str
    source_started_at: datetime
    source_lower_bound: datetime | None
    source_boundary: datetime | None
    storage_root: str


class StreamSummary(_Strict):
    scanned: int = 0
    retained: int = 0
    excluded: int = 0


class AttachmentDisposition(_Strict):
    metadata_only: int = 0
    former_metadata: int = 0
    quarantined: int = 0


class ScopeReport(_Strict):
    streams: dict[str, StreamSummary]


class PrivacyReport(_Strict):
    credential_kind: str = "oauth"
    credential_scopes: tuple[str, ...] = ("read",)
    staging_cleanup: bool
    forbidden_fields_removed: bool
    body_fields_encrypted: bool = True


class SourceHashEntry(_Strict):
    id: str
    key: str | None = None
    owner_id: str | None = None
    updated_at: datetime | None = None
    source_sha256: str | None = None
    record_sha256: str
    artifact_ref: str


class SourceHashIndex(_Strict):
    worlds: dict[str, SourceHashEntry] = Field(default_factory=dict)
    surfaces: dict[str, SourceHashEntry] = Field(default_factory=dict)
    promises: dict[str, SourceHashEntry] = Field(default_factory=dict)
    work_items: dict[str, SourceHashEntry] = Field(default_factory=dict)
    states: dict[str, SourceHashEntry] = Field(default_factory=dict)
    labels: dict[str, SourceHashEntry] = Field(default_factory=dict)
    relations: dict[str, SourceHashEntry] = Field(default_factory=dict)
    comments: dict[str, SourceHashEntry] = Field(default_factory=dict)
    attachments: dict[str, SourceHashEntry] = Field(default_factory=dict)
    users: dict[str, SourceHashEntry] = Field(default_factory=dict)
    project_updates: dict[str, SourceHashEntry] = Field(default_factory=dict)


class ExportManifest(_Strict):
    export_id: UUID
    workspace_id: UUID
    mode: str
    base_export_id: UUID | None = None
    source_started_at: datetime
    source_lower_bound: datetime | None = None
    source_boundary: datetime
    source_watermark: datetime | None = None
    source_hashes: SourceHashIndex
    dimension_counts: ReconciliationCounts
    dimension_hashes: ReconciliationHashes
    raw_export_sha256: str
    manifest_sha256: str = ""
    artifacts: dict[str, ArtifactRecord]
    scope_report: ScopeReport
    privacy_report: PrivacyReport
    attachment_dispositions: AttachmentDisposition
    anomalies: tuple[Anomaly, ...] = ()


class ExportLedger:
    def __init__(self, config: OperationsConfig, workspace_id: UUID) -> None:
        self.config = config
        self.workspace_id = workspace_id

    def _connection(self):
        return _connect(self.config, "omp_work_importer")

    def _claims(self, cursor: Any) -> None:
        cursor.execute(
            "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
            (str(self.workspace_id), str(self.config.actor_id())),
        )

    def start(self, export_id: UUID, mode: str, base_export_id: UUID | None, root: str, started_at: datetime, lower: datetime | None) -> None:
        with self._connection() as connection, connection.transaction(), connection.cursor() as cursor:
            self._claims(cursor)
            cursor.execute(
                "INSERT INTO omp_integration.raw_exports (export_id,workspace_id,team_key,mode,base_export_id,source_started_at,source_lower_bound,state,storage_root) VALUES (%s,%s,'HOME',%s,%s,%s,%s,'running',%s)",
                (export_id, self.workspace_id, mode, base_export_id, started_at, lower, root),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("pagination_count_hash_gap")

    def run(self, export_id: UUID) -> ExportRun | None:
        with self._connection() as connection, connection.transaction(), connection.cursor() as cursor:
            self._claims(cursor)
            cursor.execute(
                "SELECT export_id,workspace_id,mode,base_export_id,state,source_started_at,source_lower_bound,source_boundary,storage_root FROM omp_integration.raw_exports WHERE export_id=%s AND workspace_id=%s",
                (export_id, self.workspace_id),
            )
            row = cursor.fetchone()
        return ExportRun.model_validate(dict(zip(ExportRun.model_fields, row, strict=True))) if row else None

    def latest_complete(self) -> ExportRun | None:
        with self._connection() as connection, connection.transaction(), connection.cursor() as cursor:
            self._claims(cursor)
            cursor.execute(
                "SELECT export_id,workspace_id,mode,base_export_id,state,source_started_at,source_lower_bound,source_boundary,storage_root FROM omp_integration.raw_exports WHERE workspace_id=%s AND team_key='HOME' AND state='complete' ORDER BY completed_at DESC LIMIT 1",
                (self.workspace_id,),
            )
            row = cursor.fetchone()
        return ExportRun.model_validate(dict(zip(ExportRun.model_fields, row, strict=True))) if row else None

    def committed(self, export_id: UUID, stream: str) -> list[CursorRecord]:
        with self._connection() as connection, connection.transaction(), connection.cursor() as cursor:
            self._claims(cursor)
            cursor.execute(
                "SELECT page_index,request_cursor,end_cursor,has_next_page,scanned_count,retained_count,cumulative_count,plaintext_sha256,ciphertext_sha256,artifact_path,variables_sha256 FROM omp_integration.extraction_cursors WHERE export_id=%s AND workspace_id=%s AND stream=%s ORDER BY page_index",
                (export_id, self.workspace_id, stream),
            )
            rows = cursor.fetchall()
        return [CursorRecord.model_validate(dict(zip(CursorRecord.model_fields, row, strict=True))) for row in rows]

    def commit(self, export_id: UUID, page: SourcePage, scanned_count: int, retained_count: int, cumulative_count: int, artifact: ArtifactRecord) -> None:
        if artifact.variables_sha256 is None:
            raise RuntimeError("pagination_count_hash_gap")
        with self._connection() as connection, connection.transaction(), connection.cursor() as cursor:
            self._claims(cursor)
            cursor.execute(
                "INSERT INTO omp_integration.extraction_cursors (export_id,workspace_id,stream,page_index,request_cursor,end_cursor,has_next_page,scanned_count,retained_count,cumulative_count,plaintext_sha256,ciphertext_sha256,artifact_path,variables_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    export_id,
                    self.workspace_id,
                    page.stream,
                    page.page_index,
                    page.request_cursor,
                    page.end_cursor,
                    page.has_next_page,
                    scanned_count,
                    retained_count,
                    cumulative_count,
                    artifact.plaintext_sha256,
                    artifact.ciphertext_sha256,
                    artifact.path,
                    artifact.variables_sha256,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("pagination_count_hash_gap")

    def set_boundary(self, export_id: UUID, boundary: datetime) -> None:
        with self._connection() as connection, connection.transaction(), connection.cursor() as cursor:
            self._claims(cursor)
            cursor.execute(
                "UPDATE omp_integration.raw_exports SET source_boundary=%s WHERE export_id=%s AND workspace_id=%s AND state='running' AND source_boundary IS NULL",
                (boundary, export_id, self.workspace_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("pagination_count_hash_gap")

    def finalize(self, export_id: UUID, boundary: datetime, source_watermark: datetime | None, raw_hash: str, manifest_hash: str, blocked: bool) -> None:
        with self._connection() as connection, connection.transaction(), connection.cursor() as cursor:
            self._claims(cursor)
            cursor.execute(
                "UPDATE omp_integration.raw_exports SET source_watermark=%s,raw_export_sha256=%s,manifest_sha256=%s,state=%s,completed_at=clock_timestamp() WHERE export_id=%s AND workspace_id=%s AND state='running' AND source_boundary=%s",
                (source_watermark, raw_hash, manifest_hash, "blocked" if blocked else "complete", export_id, self.workspace_id, boundary),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("pagination_count_hash_gap")


class LinearExporter:
    def __init__(self, config: OperationsConfig, *, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config
        self.transport = transport

    def full(self, workspace_id: UUID) -> ExportManifest:
        return self._run(workspace_id, "full", None, None)

    def delta(self, workspace_id: UUID) -> ExportManifest:
        base = ExportLedger(self.config, workspace_id).latest_complete()
        if base is None or base.source_boundary is None:
            raise ValueError("linear_delta_base_missing")
        self._load_base(base.export_id)
        return self._run(workspace_id, "delta", base.export_id, base.source_boundary)

    def resume(self, export_id: UUID) -> ExportManifest:
        roots = list((self.config.data_dir / "linear-exports").glob(f"*/{export_id}"))
        if len(roots) != 1:
            raise ValueError("linear_export_missing")
        workspace_id = UUID(roots[0].parent.name)
        run = ExportLedger(self.config, workspace_id).run(export_id)
        if run is None or run.state != "running":
            raise ValueError("linear_export_not_resumable")
        expected_root = self.config.data_dir / run.storage_root
        if expected_root.resolve() != roots[0].resolve():
            raise RuntimeError("pagination_count_hash_gap")
        return self._run(
            workspace_id,
            run.mode,
            run.base_export_id,
            run.source_lower_bound,
            export_id=export_id,
            resume=True,
            started=run.source_started_at,
            boundary=run.source_boundary,
        )

    def _run(
        self,
        workspace_id: UUID,
        mode: str,
        base_id: UUID | None,
        lower: datetime | None,
        *,
        export_id: UUID | None = None,
        resume: bool = False,
        started: datetime | None = None,
        boundary: datetime | None = None,
    ) -> ExportManifest:
        export_id = export_id or uuid4()
        started = started or datetime.now(timezone.utc)
        root = self.config.data_dir / "linear-exports" / str(workspace_id) / str(export_id)
        staging = self.config.state_dir / "staging" / str(export_id)
        if resume:
            if not root.is_dir() or root.stat().st_mode & 0o777 != 0o700:
                raise RuntimeError("pagination_count_hash_gap")
        else:
            root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            root.parent.chmod(0o700)
            root.mkdir(mode=0o700, exist_ok=False)
        ledger = ExportLedger(self.config, workspace_id)
        if not resume:
            ledger.start(export_id, mode, base_id, str(root.relative_to(self.config.data_dir)), started, lower)
        if mode == "delta" and boundary is None:
            boundary = datetime.now(timezone.utc)
            ledger.set_boundary(export_id, boundary)
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(mode=0o700, parents=True)
        artifacts: dict[str, ArtifactRecord] = {}
        summaries: dict[str, StreamSummary] = {}
        records: dict[LinearStream, list[dict[str, Any]]] = defaultdict(list)
        provenance: dict[tuple[LinearStream, str], str] = {}
        anomalies: list[Anomaly] = []
        try:
            credential_path = self.config.secret_path("linear-export.json")
            credential = load_credential(credential_path)
            if credential.kind == "oauth":
                credential = refresh_credential(credential_path, transport=self.transport)
            client = LinearClient(credential, transport=self.transport)
            try:
                if mode == "full":
                    self._pass(client, ledger, export_id, root, staging, "baseline", None, None, records, provenance, artifacts, summaries)
                    if boundary is None:
                        boundary = datetime.now(timezone.utc)
                        ledger.set_boundary(export_id, boundary)
                    self._pass(client, ledger, export_id, root, staging, "overlap", started, boundary, records, provenance, artifacts, summaries)
                else:
                    if boundary is None:
                        raise RuntimeError("pagination_count_hash_gap")
                    self._pass(client, ledger, export_id, root, staging, "delta", lower, boundary, records, provenance, artifacts, summaries)
            finally:
                client.close()
            source_hashes, _ = self._indexes(records, artifacts, provenance, anomalies, boundary)
            base_records: dict[LinearStream, list[dict[str, Any]]] = defaultdict(list)
            if base_id is not None:
                base_manifest, base_hashes, base_records = self._load_base(base_id)
                if base_manifest.source_boundary != lower:
                    raise RuntimeError("pagination_count_hash_gap")
                source_hashes = self._merge_indexes(base_hashes, source_hashes, boundary, anomalies)
            validation_records = self._merge_record_sets(base_records, records, boundary, anomalies)
            source_watermark = max(
                (
                    entry.updated_at
                    for dimension in DIMENSIONS
                    for entry in getattr(source_hashes, dimension).values()
                    if entry.updated_at is not None
                ),
                default=None,
            )
            anomalies.extend(self._anomalies(validation_records, source_hashes))
            anomalies = self._unique_anomalies(anomalies)
            dispositions = self._attachment_dispositions(validation_records[LinearStream.attachments], source_hashes.work_items)
            counts = ReconciliationCounts(**{name: len(getattr(source_hashes, name)) for name in DIMENSIONS})
            hashes = ReconciliationHashes(
                **{
                    name: sha256(
                        [{"id": entry.id, "record_sha256": entry.record_sha256} for entry in sorted(getattr(source_hashes, name).values(), key=lambda item: item.id)]
                    )
                    for name in DIMENSIONS
                }
            )
            raw_hash = sha256(
                {
                    "source_hashes": source_hashes.model_dump(mode="json"),
                    "pages": [artifact.plaintext_sha256 for _, artifact in sorted(self._page_artifacts(artifacts), key=self._page_sort_key)],
                }
            )
            scope = ScopeReport(streams=summaries)
            privacy = PrivacyReport(staging_cleanup=True, forbidden_fields_removed=True)
            self._write_report(root, staging, "source-hashes", source_hashes.model_dump(mode="json"), artifacts)
            self._write_report(root, staging, "scope-report", scope.model_dump(mode="json"), artifacts)
            self._write_report(root, staging, "privacy-report", privacy.model_dump(mode="json"), artifacts)
            self._write_report(root, staging, "anomaly-report", [item.model_dump(mode="json") for item in anomalies], artifacts)
            draft = ExportManifest(
                export_id=export_id,
                workspace_id=workspace_id,
                mode=mode,
                base_export_id=base_id,
                source_started_at=started,
                source_lower_bound=lower,
                source_boundary=boundary,
                source_watermark=source_watermark,
                source_hashes=source_hashes,
                dimension_counts=counts,
                dimension_hashes=hashes,
                raw_export_sha256=raw_hash,
                artifacts=artifacts,
                scope_report=scope,
                privacy_report=privacy,
                attachment_dispositions=dispositions,
                anomalies=tuple(anomalies),
            )
            manifest_hash = sha256(draft.model_dump(mode="json", exclude={"manifest_sha256"}))
            manifest = draft.model_copy(update={"manifest_sha256": manifest_hash})
            manifest_artifact = self._write_report(root, staging, "manifest", manifest.model_dump(mode="json"), artifacts)
            returned = manifest.model_copy(update={"artifacts": {**manifest.artifacts, "manifest": manifest_artifact}})
            ledger.finalize(export_id, boundary, source_watermark, raw_hash, manifest_hash, any(item.disposition == "blocking" for item in anomalies))
            return returned
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _pass(
        self,
        client: LinearClient,
        ledger: ExportLedger,
        export_id: UUID,
        root: Path,
        staging: Path,
        phase: str,
        lower: datetime | None,
        upper: datetime | None,
        records: dict[LinearStream, list[dict[str, Any]]],
        provenance: dict[tuple[LinearStream, str], str],
        artifacts: dict[str, ArtifactRecord],
        summaries: dict[str, StreamSummary],
    ) -> None:
        for stream in LinearStream:
            stream_key = f"{phase}:{stream.value}"
            filter_value = self._filter(stream, lower, upper)
            committed = ledger.committed(export_id, stream_key)
            after, complete, cumulative, restored = self._restore_chain(export_id, root, staging, stream, stream_key, filter_value, committed)
            summary = summaries.get(stream.value, StreamSummary())
            for cursor, page, artifact in restored:
                summary = StreamSummary(
                    scanned=summary.scanned + cursor.scanned_count,
                    retained=summary.retained + cursor.retained_count,
                    excluded=summary.excluded + cursor.scanned_count - cursor.retained_count,
                )
                self._accept_page(stream, page.nodes, artifact.path, records, provenance)
                artifacts[f"{stream_key}:{page.page_index}"] = artifact
            if not complete:
                for index, (request, nodes, has_next, end, variables) in enumerate(client.pages(stream, filter=filter_value, after=after), start=len(committed)):
                    scope = self._scope_ids(records)
                    kept: list[dict[str, Any]] = []
                    for raw in nodes:
                        normalized = self._normalize(stream, raw)
                        updated = self._date(normalized.get("updatedAt"))
                        if upper is not None and updated is not None and updated > upper:
                            continue
                        if self._in_scope(stream, normalized, scope):
                            kept.append(normalized)
                    page = SourcePage(
                        export_id=export_id,
                        stream=stream_key,
                        page_index=index,
                        request_cursor=request,
                        end_cursor=end,
                        has_next_page=has_next,
                        nodes=tuple(kept),
                    )
                    variables_hash = bytes_sha256(variables.encode()).hexdigest()
                    artifact = self._write_page(root, staging, phase, stream, page, variables_hash)
                    page_scanned = len(nodes)
                    page_retained = len(kept)
                    cumulative += page_retained
                    ledger.commit(export_id, page, page_scanned, page_retained, cumulative, artifact)
                    summary = StreamSummary(
                        scanned=summary.scanned + page_scanned,
                        retained=summary.retained + page_retained,
                        excluded=summary.excluded + page_scanned - page_retained,
                    )
                    self._accept_page(stream, page.nodes, artifact.path, records, provenance)
                    artifacts[f"{stream_key}:{index}"] = artifact
            summaries[stream.value] = summary

    def _restore_chain(
        self,
        export_id: UUID,
        root: Path,
        staging: Path,
        stream: LinearStream,
        stream_key: str,
        filter_value: dict[str, Any] | None,
        committed: list[CursorRecord],
    ) -> tuple[str | None, bool, int, list[tuple[CursorRecord, SourcePage, ArtifactRecord]]]:
        after: str | None = None
        cumulative = 0
        restored: list[tuple[CursorRecord, SourcePage, ArtifactRecord]] = []
        complete = False
        for expected_index, cursor in enumerate(committed):
            if cursor.page_index != expected_index or cursor.request_cursor != after or complete:
                raise RuntimeError("pagination_count_hash_gap")
            if cursor.has_next_page and (not cursor.end_cursor or cursor.end_cursor == cursor.request_cursor):
                raise RuntimeError("pagination_count_hash_gap")
            variables = json.dumps(
                {"first": 50, "after": cursor.request_cursor, **({"filter": filter_value} if filter_value is not None else {})},
                sort_keys=True,
                separators=(",", ":"),
            )
            if bytes_sha256(variables.encode()).hexdigest() != cursor.variables_sha256:
                raise RuntimeError("pagination_count_hash_gap")
            artifact = ArtifactRecord(
                path=cursor.artifact_path,
                plaintext_sha256=cursor.plaintext_sha256,
                ciphertext_sha256=cursor.ciphertext_sha256,
                variables_sha256=cursor.variables_sha256,
                stream=stream_key,
                page_index=cursor.page_index,
                request_cursor=cursor.request_cursor,
                end_cursor=cursor.end_cursor,
                has_next_page=cursor.has_next_page,
            )
            plain = staging / f"restore-{stream.value}-{cursor.page_index}.json"
            payload = self._read_artifact(artifact, plain, root)
            try:
                page = SourcePage.model_validate(payload)
            except Exception:
                raise RuntimeError("pagination_count_hash_gap") from None
            if (
                page.schema_version != SCHEMA_VERSION
                or page.export_id != export_id
                or page.stream != stream_key
                or page.page_index != cursor.page_index
                or page.request_cursor != cursor.request_cursor
                or page.end_cursor != cursor.end_cursor
                or page.has_next_page != cursor.has_next_page
                or cursor.retained_count != len(page.nodes)
                or cursor.scanned_count < cursor.retained_count
                or cursor.cumulative_count != cumulative + cursor.retained_count
            ):
                raise RuntimeError("pagination_count_hash_gap")
            cumulative = cursor.cumulative_count
            after = cursor.end_cursor
            complete = not cursor.has_next_page
            restored.append((cursor, page, artifact))
        return after, complete, cumulative, restored

    def _write_page(self, root: Path, staging: Path, phase: str, stream: LinearStream, page: SourcePage, variables_hash: str) -> ArtifactRecord:
        payload = page.model_dump(mode="json")
        plaintext_hash = sha256(payload)
        encrypted = root / f"{phase}-{stream.value}-{page.page_index}-{plaintext_hash}.json.gpg"
        siblings = list(root.glob(f"{phase}-{stream.value}-{page.page_index}-*.json.gpg"))
        if any(path != encrypted for path in siblings):
            raise RuntimeError("pagination_count_hash_gap")
        plain = staging / encrypted.name.removesuffix(".gpg")
        plain.write_text(canonical_json(payload), encoding="utf-8")
        plain.chmod(0o600)
        try:
            try:
                ciphertext_hash = encrypt_file(plain, encrypted, self.config.secret_path("gpg-passphrase"), mode=0o400)
            except FileExistsError:
                plain.unlink(missing_ok=True)
                reused = staging / f"reuse-{encrypted.name.removesuffix('.gpg')}"
                artifact = ArtifactRecord(path=str(encrypted.relative_to(self.config.data_dir)), plaintext_sha256=plaintext_hash, ciphertext_sha256=bytes_sha256(encrypted.read_bytes()).hexdigest())
                self._read_artifact(artifact, reused, root)
                ciphertext_hash = artifact.ciphertext_sha256
        finally:
            plain.unlink(missing_ok=True)
        return ArtifactRecord(
            path=str(encrypted.relative_to(self.config.data_dir)),
            plaintext_sha256=plaintext_hash,
            ciphertext_sha256=ciphertext_hash,
            variables_sha256=variables_hash,
            stream=page.stream,
            page_index=page.page_index,
            request_cursor=page.request_cursor,
            end_cursor=page.end_cursor,
            has_next_page=page.has_next_page,
        )

    def _read_artifact(self, artifact: ArtifactRecord, destination: Path, expected_root: Path | None = None) -> object:
        encrypted = self.config.data_dir / artifact.path
        return read_json_artifact(
            encrypted,
            destination,
            self.config.secret_path("gpg-passphrase"),
            expected_plaintext_sha256=artifact.plaintext_sha256,
            expected_ciphertext_sha256=artifact.ciphertext_sha256,
            data_dir=self.config.data_dir,
            expected_root=expected_root,
            decrypt_fn=decrypt_file,
        )

    def _artifact_path(self, relative: str, expected_root: Path | None = None) -> Path:
        return resolve_artifact_path(relative, self.config.data_dir, expected_root)
    @staticmethod
    def _accept_page(
        stream: LinearStream,
        nodes: tuple[dict[str, Any], ...],
        artifact_path: str,
        records: dict[LinearStream, list[dict[str, Any]]],
        provenance: dict[tuple[LinearStream, str], str],
    ) -> None:
        records[stream].extend(nodes)
        for node in nodes:
            if node.get("id"):
                provenance[(stream, str(node["id"]))] = artifact_path

    @staticmethod
    def _filter(stream: LinearStream, lower: datetime | None, upper: datetime | None) -> dict[str, Any] | None:
        if stream in UNFILTERABLE:
            return None
        if stream is LinearStream.teams:
            result: dict[str, Any] = {"key": {"eq": "HOME"}}
        elif stream is LinearStream.initiatives:
            result = {"teams": {"some": {"key": {"eq": "HOME"}}}}
        elif stream is LinearStream.projects:
            result = {"accessibleTeams": {"some": {"key": {"eq": "HOME"}}}}
        elif stream in {LinearStream.project_updates, LinearStream.milestones}:
            result = {"project": {"accessibleTeams": {"some": {"key": {"eq": "HOME"}}}}}
        elif stream is LinearStream.comments:
            result = {
                "issue": {"team": {"key": {"eq": "HOME"}}},
                "or": [{"body": {"startsWith": prefix}} for prefix in WORKFLOW_PREFIXES],
            }
        else:
            result = {"team": {"key": {"eq": "HOME"}}}
        if lower is not None or upper is not None:
            result["updatedAt"] = {name: value.astimezone(timezone.utc).isoformat() for name, value in (("gte", lower), ("lte", upper)) if value is not None}
        return result

    @staticmethod
    def _scope_ids(records: dict[LinearStream, list[dict[str, Any]]]) -> dict[str, set[str]]:
        return {
            "initiatives": {str(node["id"]) for node in records[LinearStream.initiatives] if node.get("id")},
            "projects": {str(node["id"]) for node in records[LinearStream.projects] if node.get("id")},
            "issues": {str(node["id"]) for node in records[LinearStream.issues] if node.get("id")},
        }

    @staticmethod
    def _in_scope(stream: LinearStream, node: dict[str, Any], scope: dict[str, set[str]] | None = None) -> bool:
        scope = scope or {}
        if stream is LinearStream.teams:
            return node.get("key") == "HOME"
        if stream is LinearStream.initiatives:
            teams = (node.get("teams") or {}).get("nodes", [])
            return not teams or any(team.get("key") == "HOME" for team in teams)
        if stream is LinearStream.projects:
            teams = (node.get("teams") or node.get("accessibleTeams") or {}).get("nodes", [])
            return any(team.get("key") == "HOME" for team in teams)
        if stream in {LinearStream.project_updates, LinearStream.milestones}:
            project = node.get("project") or {}
            return str(project.get("id", "")) in scope.get("projects", set()) or any(
                team.get("key") == "HOME" for team in (project.get("accessibleTeams") or {}).get("nodes", [])
            )
        if stream is LinearStream.initiative_projects:
            return str((node.get("initiative") or {}).get("id", "")) in scope.get("initiatives", set()) or str((node.get("project") or {}).get("id", "")) in scope.get("projects", set())
        if stream is LinearStream.relations:
            known = scope.get("issues", set())
            if known:
                return any(str((node.get(endpoint) or {}).get("id", "")) in known for endpoint in ("issue", "relatedIssue"))
            return any((node.get(endpoint) or {}).get("team", {}).get("key") == "HOME" for endpoint in ("issue", "relatedIssue"))
        if stream is LinearStream.comments:
            issue = node.get("issue") or {}
            return (
                str(issue.get("id", "")) in scope.get("issues", set()) or issue.get("team", {}).get("key") == "HOME"
            ) and any(str(node.get("body", "")).startswith(prefix) for prefix in WORKFLOW_PREFIXES)
        if stream is LinearStream.attachments:
            issue = node.get("issue") or {}
            return str(issue.get("id", "")) in scope.get("issues", set()) or issue.get("team", {}).get("key") == "HOME"
        return (node.get("team") or {}).get("key") == "HOME"

    @staticmethod
    def _user(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict) or not value.get("id"):
            return None
        return {key: value.get(key) for key in ("id", "name", "displayName", "active")}

    @classmethod
    def _normalize(cls, stream: LinearStream, node: dict[str, Any]) -> dict[str, Any]:
        simple_fields = {
            LinearStream.teams: ("id", "key", "name", "description", "createdAt", "updatedAt", "archivedAt"),
            LinearStream.initiatives: ("id", "name", "description", "status", "targetDate", "createdAt", "updatedAt", "archivedAt"),
            LinearStream.projects: ("id", "name", "description", "status", "health", "startDate", "targetDate", "createdAt", "updatedAt", "archivedAt"),
            LinearStream.project_updates: ("id", "body", "health", "createdAt", "updatedAt", "archivedAt"),
            LinearStream.milestones: ("id", "name", "description", "targetDate", "createdAt", "updatedAt", "archivedAt"),
            LinearStream.issues: ("id", "identifier", "previousIdentifiers", "title", "description", "priority", "estimate", "dueDate", "createdAt", "updatedAt", "archivedAt", "canceledAt", "completedAt", "url"),
            LinearStream.states: ("id", "name", "type", "position", "description", "createdAt", "updatedAt", "archivedAt"),
            LinearStream.labels: ("id", "name", "color", "description", "createdAt", "updatedAt", "archivedAt"),
            LinearStream.initiative_projects: ("id", "createdAt", "updatedAt", "archivedAt"),
            LinearStream.relations: ("id", "type", "createdAt", "updatedAt", "archivedAt"),
            LinearStream.comments: ("id", "body", "url", "createdAt", "updatedAt", "archivedAt"),
            LinearStream.attachments: ("id", "title", "subtitle", "url", "sourceType", "metadata", "createdAt", "updatedAt", "archivedAt"),
        }[stream]
        result = {key: node.get(key) for key in simple_fields}
        if stream is LinearStream.projects:
            result["teams"] = {"nodes": [{"key": team.get("key")} for team in (node.get("teams") or {}).get("nodes", []) if isinstance(team, dict)]}
            result["lead"] = cls._user(node.get("lead"))
            status = node.get("status")
            result["status"] = {key: status.get(key) for key in ("id", "name", "type")} if isinstance(status, dict) and status.get("id") else None
        elif stream in {LinearStream.project_updates, LinearStream.milestones}:
            result["project"] = {"id": (node.get("project") or {}).get("id")}
            if stream is LinearStream.project_updates:
                result["user"] = cls._user(node.get("user"))
        elif stream is LinearStream.issues:
            result["team"] = {"key": (node.get("team") or {}).get("key")}
            for field in ("parent", "project", "projectMilestone"):
                value = node.get(field)
                result[field] = {"id": value.get("id")} if isinstance(value, dict) and value.get("id") else None
            state = node.get("state")
            result["state"] = {key: state.get(key) for key in ("id", "name", "type")} if isinstance(state, dict) and state.get("id") else None
            result["labels"] = {"nodes": [{key: label.get(key) for key in ("id", "name")} for label in (node.get("labels") or {}).get("nodes", []) if isinstance(label, dict) and label.get("id")]}
            result["assignee"] = cls._user(node.get("assignee"))
            result["creator"] = cls._user(node.get("creator"))
        elif stream in {LinearStream.states, LinearStream.labels}:
            result["team"] = {"key": (node.get("team") or {}).get("key")}
        elif stream is LinearStream.initiative_projects:
            result["initiative"] = {"id": (node.get("initiative") or {}).get("id")}
            result["project"] = {"id": (node.get("project") or {}).get("id")}
        elif stream is LinearStream.relations:
            result["issue"] = {"id": (node.get("issue") or {}).get("id")}
            result["relatedIssue"] = {"id": (node.get("relatedIssue") or {}).get("id")}
        elif stream is LinearStream.comments:
            issue = node.get("issue") or {}
            result["issue"] = {"id": issue.get("id"), "identifier": issue.get("identifier"), "team": {"key": (issue.get("team") or {}).get("key")}}
            result["user"] = cls._user(node.get("user"))
            parent = node.get("parent")
            result["parent"] = {"id": parent.get("id")} if isinstance(parent, dict) and parent.get("id") else None
        elif stream is LinearStream.attachments:
            issue = node.get("issue") or {}
            result["issue"] = {"id": issue.get("id"), "identifier": issue.get("identifier"), "team": {"key": (issue.get("team") or {}).get("key")}}
            result["creator"] = cls._user(node.get("creator"))
        return result

    def _indexes(
        self,
        records: dict[LinearStream, list[dict[str, Any]]],
        _artifacts: dict[str, ArtifactRecord],
        provenance: dict[tuple[LinearStream, str], str] | None = None,
        anomalies: list[Anomaly] | None = None,
        upper: datetime | None = None,
    ) -> tuple[SourceHashIndex, set[str]]:
        mappings = {
            LinearStream.initiatives: "worlds",
            LinearStream.projects: "surfaces",
            LinearStream.project_updates: "project_updates",
            LinearStream.milestones: "promises",
            LinearStream.issues: "work_items",
            LinearStream.states: "states",
            LinearStream.labels: "labels",
            LinearStream.initiative_projects: "relations",
            LinearStream.relations: "relations",
            LinearStream.comments: "comments",
            LinearStream.attachments: "attachments",
        }
        data: dict[str, dict[str, SourceHashEntry]] = {name: {} for name in (*DIMENSIONS, "project_updates")}
        found: set[str] = set()
        for stream, dimension in mappings.items():
            for node in records[stream]:
                identifier = str(node.get("id", ""))
                if not identifier:
                    continue
                updated = self._date(node.get("updatedAt"))
                if upper is not None and updated is not None and updated > upper:
                    continue
                found.add(identifier)
                source_hash = sha256(node)
                owner = str((node.get("project") or {}).get("id")) if stream is LinearStream.project_updates and (node.get("project") or {}).get("id") else None
                entry = SourceHashEntry(
                    id=identifier,
                    key=node.get("identifier") if dimension == "work_items" else None,
                    owner_id=owner,
                    updated_at=updated,
                    source_sha256=source_hash if dimension == "surfaces" else None,
                    record_sha256=source_hash,
                    artifact_ref=(provenance or {}).get((stream, identifier), ""),
                )
                self._merge_entry(data[dimension], entry, anomalies)
        for stream, fields in (
            (LinearStream.projects, ("lead",)),
            (LinearStream.project_updates, ("user",)),
            (LinearStream.issues, ("assignee", "creator")),
            (LinearStream.comments, ("user",)),
            (LinearStream.attachments, ("creator",)),
        ):
            for node in records[stream]:
                for field in fields:
                    person = node.get(field)
                    if not isinstance(person, dict) or not person.get("id"):
                        continue
                    identifier = str(person["id"])
                    found.add(identifier)
                    normalized = {key: person.get(key) for key in ("id", "name", "displayName", "active")}
                    entry = SourceHashEntry(
                        id=identifier,
                        record_sha256=sha256(normalized),
                        artifact_ref=(provenance or {}).get((stream, str(node.get("id", ""))), ""),
                    )
                    self._merge_entry(data["users"], entry, anomalies)
        self._fold_project_updates(data)
        return SourceHashIndex(**data), found

    @staticmethod
    def _merge_entry(values: dict[str, SourceHashEntry], entry: SourceHashEntry, anomalies: list[Anomaly] | None) -> None:
        existing = values.get(entry.id)
        if existing is None:
            values[entry.id] = entry
            return
        if existing.updated_at == entry.updated_at and existing.record_sha256 != entry.record_sha256:
            if anomalies is None:
                raise RuntimeError("pagination_count_hash_gap")
            anomalies.append(Anomaly(code="pagination_count_hash_gap", disposition="blocking"))
        if (entry.updated_at or datetime.min.replace(tzinfo=timezone.utc), entry.record_sha256) > (
            existing.updated_at or datetime.min.replace(tzinfo=timezone.utc),
            existing.record_sha256,
        ):
            values[entry.id] = entry

    @staticmethod
    def _fold_project_updates(data: dict[str, dict[str, SourceHashEntry]]) -> None:
        by_project: dict[str, list[SourceHashEntry]] = defaultdict(list)
        for update in data["project_updates"].values():
            if update.owner_id:
                by_project[update.owner_id].append(update)
        for project_id, project in tuple(data["surfaces"].items()):
            source_hash = project.source_sha256 or project.record_sha256
            updates = by_project.get(project_id, [])
            record_hash = source_hash if not updates else sha256(
                {
                    "project": source_hash,
                    "updates": [{"id": entry.id, "record_sha256": entry.record_sha256} for entry in sorted(updates, key=lambda item: item.id)],
                }
            )
            data["surfaces"][project_id] = project.model_copy(update={"source_sha256": source_hash, "record_sha256": record_hash})

    @classmethod
    def _merge_indexes(
        cls,
        base: SourceHashIndex,
        incoming: SourceHashIndex,
        upper: datetime,
        anomalies: list[Anomaly] | None = None,
    ) -> SourceHashIndex:
        merged: dict[str, dict[str, SourceHashEntry]] = {}
        for dimension in (*DIMENSIONS, "project_updates"):
            values = dict(getattr(base, dimension))
            for entry in getattr(incoming, dimension).values():
                if entry.updated_at is not None and entry.updated_at > upper:
                    continue
                cls._merge_entry(values, entry, anomalies)
            merged[dimension] = values
        cls._fold_project_updates(merged)
        return SourceHashIndex(**merged)

    @classmethod
    def _merge_record_sets(
        cls,
        base: dict[LinearStream, list[dict[str, Any]]],
        incoming: dict[LinearStream, list[dict[str, Any]]],
        upper: datetime,
        anomalies: list[Anomaly],
    ) -> dict[LinearStream, list[dict[str, Any]]]:
        result: dict[LinearStream, list[dict[str, Any]]] = defaultdict(list)
        for stream in LinearStream:
            values: dict[str, tuple[datetime | None, str, dict[str, Any]]] = {}
            anonymous: list[dict[str, Any]] = []
            for node in (*base[stream], *incoming[stream]):
                identifier = str(node.get("id", ""))
                updated = cls._date(node.get("updatedAt"))
                if updated is not None and updated > upper:
                    continue
                if not identifier:
                    anonymous.append(node)
                    continue
                digest = sha256(node)
                existing = values.get(identifier)
                if existing and existing[0] == updated and existing[1] != digest:
                    anomalies.append(Anomaly(code="pagination_count_hash_gap", disposition="blocking"))
                if existing is None or (updated or datetime.min.replace(tzinfo=timezone.utc), digest) > (
                    existing[0] or datetime.min.replace(tzinfo=timezone.utc),
                    existing[1],
                ):
                    values[identifier] = (updated, digest, node)
            result[stream] = [values[key][2] for key in sorted(values)] + anonymous
        return result

    @staticmethod
    def _date(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return result if result.tzinfo is not None and result.utcoffset() is not None else None

    @classmethod
    def _anomalies(cls, records: dict[LinearStream, list[dict[str, Any]]], found: set[str] | SourceHashIndex) -> list[Anomaly]:
        result: list[Anomaly] = []
        if isinstance(found, SourceHashIndex):
            ids = {
                "initiatives": set(found.worlds),
                "projects": set(found.surfaces),
                "milestones": set(found.promises),
                "issues": set(found.work_items),
                "states": set(found.states),
                "labels": set(found.labels),
                "comments": set(found.comments),
                "users": set(found.users),
            }
        else:
            ids = {name: found for name in ("initiatives", "projects", "milestones", "issues", "states", "labels", "comments", "users")}

        def missing(target: str, value: Any) -> None:
            if isinstance(value, dict) and value.get("id") and str(value["id"]) not in ids[target]:
                result.append(Anomaly(code="missing_relation_endpoint", disposition="blocking"))

        for link in records[LinearStream.initiative_projects]:
            missing("initiatives", link.get("initiative"))
            missing("projects", link.get("project"))
        for project in records[LinearStream.projects]:
            missing("users", project.get("lead"))
        for update in records[LinearStream.project_updates]:
            missing("projects", update.get("project"))
            missing("users", update.get("user"))
        for milestone in records[LinearStream.milestones]:
            missing("projects", milestone.get("project"))
        for issue in records[LinearStream.issues]:
            missing("issues", issue.get("parent"))
            missing("projects", issue.get("project"))
            missing("milestones", issue.get("projectMilestone"))
            missing("states", issue.get("state"))
            for label in (issue.get("labels") or {}).get("nodes", []):
                missing("labels", label)
            missing("users", issue.get("assignee"))
            missing("users", issue.get("creator"))
        for relation in records[LinearStream.relations]:
            missing("issues", relation.get("issue"))
            missing("issues", relation.get("relatedIssue"))
        for comment in records[LinearStream.comments]:
            missing("issues", comment.get("issue"))
            missing("comments", comment.get("parent"))
            missing("users", comment.get("user"))
        for attachment in records[LinearStream.attachments]:
            missing("issues", attachment.get("issue"))
            missing("users", attachment.get("creator"))

        seen: dict[str, str] = {}
        for issue in records[LinearStream.issues]:
            key = str(issue.get("identifier", ""))
            identifier = str(issue.get("id", ""))
            if key and key in seen and seen[key] != identifier:
                result.append(Anomaly(code="duplicate_uuid_key_mapping", disposition="blocking"))
            if key:
                seen[key] = identifier

        edges: list[RelationEdge] = []
        relation_kinds = {
            "blocks": RelationKind.BLOCKS,
            "duplicate": RelationKind.DUPLICATE_OF,
            "duplicate_of": RelationKind.DUPLICATE_OF,
            "related": RelationKind.RELATED,
        }
        for relation in records[LinearStream.relations]:
            raw_kind = str(relation.get("type", "")).lower()
            kind = relation_kinds.get(raw_kind)
            if kind is None:
                result.append(Anomaly(code="unsupported_non_workflow_object", disposition="quarantined"))
                continue
            try:
                edge = RelationEdge(
                    workspace_id=UUID(int=0),
                    source_work_id=UUID(str(relation["issue"]["id"])),
                    target_work_id=UUID(str(relation["relatedIssue"]["id"])),
                    kind=kind,
                )
            except (KeyError, TypeError, ValueError):
                continue
            if would_create_cycle(tuple(edges), edge):
                result.append(Anomaly(code="relation_cycle", disposition="blocking"))
            else:
                edges.append(edge)
        for issue in records[LinearStream.issues]:
            if not isinstance(issue.get("parent"), dict):
                continue
            try:
                edge = RelationEdge(
                    workspace_id=UUID(int=0),
                    source_work_id=UUID(str(issue["id"])),
                    target_work_id=UUID(str(issue["parent"]["id"])),
                    kind=RelationKind.PARENT,
                )
            except (KeyError, TypeError, ValueError):
                continue
            if would_create_cycle(tuple(edges), edge):
                result.append(Anomaly(code="relation_cycle", disposition="blocking"))
            else:
                edges.append(edge)

        plans: dict[str, tuple[str, str]] = {}
        for comment in sorted(records[LinearStream.comments], key=lambda item: (str(item.get("createdAt", "")), str(item.get("id", "")))):
            body = str(comment.get("body", ""))
            created = str(comment.get("createdAt", ""))
            issue_id = str((comment.get("issue") or {}).get("id", ""))
            if not issue_id:
                continue
            if body.startswith("**Plan approved**"):
                match = re.search(r"SHA-256: `([a-f0-9]{64})`", body)
                if match:
                    plans[issue_id] = match.group(1), created
                continue
            plan = plans.get(issue_id)
            if plan is None or created <= plan[1]:
                continue
            if body.startswith("**Session review**"):
                match = re.search(r"Plan SHA-256: `([a-f0-9]{64})`", body)
                if match and match.group(1) != plan[0]:
                    result.append(Anomaly(code="legacy_authority_claim", disposition="blocking"))

        now_label_ids = {str(label["id"]) for label in records[LinearStream.labels] if label.get("id") and str(label.get("name", "")).casefold() == "now"}
        focused = [
            issue
            for issue in records[LinearStream.issues]
            if not issue.get("archivedAt")
            and not issue.get("completedAt")
            and not issue.get("canceledAt")
            and any(str(label.get("id")) in now_label_ids for label in (issue.get("labels") or {}).get("nodes", []))
        ]
        if len(focused) > 1:
            result.append(Anomaly(code="multiple_focus_slots", disposition="blocking"))
        for attachment in records[LinearStream.attachments]:
            issue_id = str((attachment.get("issue") or {}).get("id", ""))
            if not attachment.get("id") or not issue_id or not (attachment.get("url") or attachment.get("metadata")):
                result.append(Anomaly(code="attachment_content_unavailable", disposition="quarantined"))
        return cls._unique_anomalies(result)

    @staticmethod
    def _attachment_dispositions(attachments: list[dict[str, Any]], issues: dict[str, SourceHashEntry]) -> AttachmentDisposition:
        metadata_only = former_metadata = quarantined = 0
        for attachment in attachments:
            issue_id = str((attachment.get("issue") or {}).get("id", ""))
            usable = bool(attachment.get("id") and issue_id in issues and (attachment.get("url") or attachment.get("metadata")))
            if not usable:
                quarantined += 1
            elif attachment.get("archivedAt"):
                former_metadata += 1
            else:
                metadata_only += 1
        return AttachmentDisposition(metadata_only=metadata_only, former_metadata=former_metadata, quarantined=quarantined)

    @staticmethod
    def _unique_anomalies(anomalies: list[Anomaly]) -> list[Anomaly]:
        unique = {(item.code, item.disposition): item for item in anomalies}
        return [unique[key] for key in sorted(unique)]

    def _write_report(self, root: Path, staging: Path, name: str, payload: object, artifacts: dict[str, ArtifactRecord]) -> ArtifactRecord:
        rel_path, digest, ciphertext_hash = write_json_artifact(
            root,
            staging,
            name,
            payload,
            self.config.secret_path("gpg-passphrase"),
            self.config.data_dir,
            encrypt_fn=encrypt_file,
            decrypt_fn=decrypt_file,
        )
        artifact = ArtifactRecord(path=rel_path, plaintext_sha256=digest, ciphertext_sha256=ciphertext_hash)
        artifacts[name] = artifact
        return artifact
    def _load_base(self, export_id: UUID) -> tuple[ExportManifest, SourceHashIndex, dict[LinearStream, list[dict[str, Any]]]]:
        manifest, source_hashes, pages = load_export(self.config, export_id)
        records: dict[LinearStream, list[dict[str, Any]]] = defaultdict(list)
        for page in pages:
            stream_name = page.stream.split(":", 1)[1]
            records[LinearStream(stream_name)].extend(page.nodes)
        return manifest, source_hashes, records

    @staticmethod
    def _page_artifacts(artifacts: dict[str, ArtifactRecord]) -> list[tuple[str, ArtifactRecord]]:
        return [(key, artifact) for key, artifact in artifacts.items() if re.fullmatch(r"(?:baseline|overlap|delta):[^:]+:\d+", key)]

    @staticmethod
    def _page_sort_key(item: tuple[str, ArtifactRecord]) -> tuple[int, int, int]:
        return page_sort_key(item)


def page_sort_key(item: tuple[str, ArtifactRecord]) -> tuple[int, int, int]:
    key, _ = item
    phase, stream, index = key.rsplit(":", 2)
    return (("baseline", "overlap", "delta").index(phase), list(LinearStream).index(LinearStream(stream)), int(index))


def load_export(config: OperationsConfig, export_id: UUID) -> tuple[ExportManifest, SourceHashIndex, tuple[SourcePage, ...]]:
    manifest = load_manifest(config, export_id)
    artifact = manifest.artifacts.get("source-hashes")
    if artifact is None:
        raise RuntimeError("pagination_count_hash_gap")
    staging = config.state_dir / "staging" / f"load-{export_id}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o700, parents=True)
    try:
        source_hashes_path = resolve_artifact_path(artifact.path, config.data_dir)
        payload = read_json_artifact(
            source_hashes_path,
            staging / "source-hashes.json",
            config.secret_path("gpg-passphrase"),
            expected_plaintext_sha256=artifact.plaintext_sha256,
            expected_ciphertext_sha256=artifact.ciphertext_sha256,
            data_dir=config.data_dir,
            decrypt_fn=decrypt_file,
        )
        try:
            source_hashes = SourceHashIndex.model_validate(payload)
        except Exception:
            raise RuntimeError("pagination_count_hash_gap") from None
        if source_hashes != manifest.source_hashes:
            raise RuntimeError("pagination_count_hash_gap")

        page_items = [(key, art) for key, art in manifest.artifacts.items() if re.fullmatch(r"(?:baseline|overlap|delta):[^:]+:\d+", key)]
        page_items.sort(key=page_sort_key)
        pages: list[SourcePage] = []
        for idx, (key, page_artifact) in enumerate(page_items):
            page_path = resolve_artifact_path(page_artifact.path, config.data_dir)
            page_payload = read_json_artifact(
                page_path,
                staging / f"page-{idx}.json",
                config.secret_path("gpg-passphrase"),
                expected_plaintext_sha256=page_artifact.plaintext_sha256,
                expected_ciphertext_sha256=page_artifact.ciphertext_sha256,
                data_dir=config.data_dir,
                decrypt_fn=decrypt_file,
            )
            try:
                page = SourcePage.model_validate(page_payload)
                _, stream_name, page_index = key.rsplit(":", 2)
                _stream = LinearStream(stream_name)
            except Exception:
                raise RuntimeError("pagination_count_hash_gap") from None
            if page.export_id != export_id or page.stream != key.rsplit(":", 1)[0] or page.page_index != int(page_index):
                raise RuntimeError("pagination_count_hash_gap")
            pages.append(page)
        return manifest, source_hashes, tuple(pages)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
def load_manifest(config: OperationsConfig, export_id: UUID) -> ExportManifest:
    matches = list((config.data_dir / "linear-exports").glob(f"*/{export_id}/manifest-*.json.gpg"))
    if len(matches) != 1:
        raise ValueError("linear_manifest_missing")
    encrypted = matches[0]
    if encrypted.stat().st_mode & 0o777 != 0o400:
        raise RuntimeError("pagination_count_hash_gap")
    staging = config.state_dir / "staging" / str(export_id)
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o700, parents=True)
    try:
        plain = staging / "manifest.json"
        decrypt_file(encrypted, plain, config.secret_path("gpg-passphrase"))
        try:
            manifest = ExportManifest.model_validate_json(plain.read_text(encoding="utf-8"))
        except Exception:
            raise RuntimeError("pagination_count_hash_gap") from None
        if sha256(manifest.model_dump(mode="json", exclude={"manifest_sha256"})) != manifest.manifest_sha256:
            raise RuntimeError("pagination_count_hash_gap")
        artifact = ArtifactRecord(
            path=str(encrypted.relative_to(config.data_dir)),
            plaintext_sha256=sha256(manifest.model_dump(mode="json")),
            ciphertext_sha256=bytes_sha256(encrypted.read_bytes()).hexdigest(),
        )
        return manifest.model_copy(update={"artifacts": {**manifest.artifacts, "manifest": artifact}})
    finally:
        shutil.rmtree(staging, ignore_errors=True)
