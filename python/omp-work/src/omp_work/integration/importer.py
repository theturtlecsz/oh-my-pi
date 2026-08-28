from __future__ import annotations

import json
import re
import shutil
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omp_work.operations.artifacts import write_json_artifact
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import _connect
from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.models import (
    Anomaly,
    ReconciliationCounts,
    ReconciliationHashes,
    RelationEdge,
    RelationKind,
)
from omp_work.v1.semantics import would_create_cycle

from .legacy_artifacts import (
    ExportManifest,
    SourceHashIndex,
    SourcePage,
    load_export,
)

MAPPING_SCHEMA_VERSION = "linear-import-map/v1"
# v2: alias eligibility filtering (alias_identifier / alias_previous_identifiers) and
# provenance-free logical hashes. Batches are keyed by (export, transformation_version),
# so a bump re-transforms an existing export under a fresh batch instead of reusing
# records staged under the old shape.
TRANSFORMATION_VERSION = "linear-transform/v2"
WORKFLOW_PREFIXES = (
    "**Plan approved**",
    "**Execution handoff**",
    "**Session review**",
    "**Close proposed**",
    "**Owner verdict in session:",
)
# omp_work.work_aliases.key CHECK constraint: only HOME/OMP keys are alias-eligible.
_ALIAS_KEY_RE = re.compile(r"^(HOME|OMP)-[1-9][0-9]*$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RepositoryMapping(_Strict):
    key: str
    name: str
    url: str


class LinearImportMap(_Strict):
    schema_version: str = MAPPING_SCHEMA_VERSION
    repositories: dict[str, RepositoryMapping]
    project_repositories: dict[str, str] = Field(default_factory=dict)
    unprojected_repository: str

    @model_validator(mode="after")
    def validate_map(self) -> LinearImportMap:
        if self.schema_version != MAPPING_SCHEMA_VERSION:
            raise ValueError("linear_import_mapping_invalid")
        if not self.repositories:
            raise ValueError("linear_import_mapping_invalid")
        seen_urls: set[str] = set()
        for key, repo in self.repositories.items():
            if repo.key != key:
                raise ValueError("linear_import_mapping_invalid")
            if repo.url in seen_urls:
                raise ValueError("linear_import_mapping_invalid")
            seen_urls.add(repo.url)
        if self.unprojected_repository not in self.repositories:
            raise ValueError("linear_import_mapping_invalid")
        for project_id, repo_key in self.project_repositories.items():
            if not project_id or repo_key not in self.repositories:
                raise ValueError("linear_import_mapping_invalid")
        return self


class ImportBatchSummary(_Strict):
    batch_id: UUID
    workspace_id: UUID
    export_id: UUID
    state: Literal["staging", "staged", "reconciled", "promoted", "blocked"]
    transformation_version: str
    base_batch_id: UUID | None = None
    mapping_file_sha256: str
    reconciliation_sha256: str | None = None
    parity_hashes: dict[str, str] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    disposition_counts: dict[str, int] = Field(default_factory=dict)
    anomaly_codes: list[str] = Field(default_factory=list)


def parse_acceptance_criteria(description: str) -> tuple[str, ...]:
    lines = description.splitlines()
    in_criteria = False
    items: list[str] = []
    current_item: list[str] = []

    for line in lines:
        stripped = line.strip()
        if re.match(r"^#{1,6}\s+Acceptance criteria", stripped, re.IGNORECASE):
            in_criteria = True
            continue
        if in_criteria and re.match(r"^#{1,6}\s+", stripped):
            break
        if in_criteria:
            match = re.match(r"^(?:[-*]|\d+\.)\s+(?:\[[ xX]\]\s+)?(.*)$", line.lstrip())
            if match and (
                line.startswith("-")
                or line.startswith("*")
                or re.match(r"^\d+\.", line)
            ):
                if current_item:
                    items.append(" ".join(current_item).strip())
                    current_item = []
                current_item.append(match.group(1).strip())
            elif current_item and (line.startswith("  ") or line.startswith("\t")):
                sublist = re.match(r"^\s+(?:[-*]|\d+\.)\s+", line)
                if not sublist:
                    current_item.append(stripped)

    if current_item:
        items.append(" ".join(current_item).strip())

    return tuple(item for item in items if item)


def map_work_item_state(
    state_type: str | None, completed_at: str | None, canceled_at: str | None
) -> str:
    if canceled_at or state_type == "canceled":
        return "CANCELED"
    if completed_at or state_type == "completed":
        return "DONE"
    if state_type == "started":
        return "IN_PROGRESS"
    if state_type == "unstarted":
        return "PLANNED"
    if state_type == "backlog":
        return "BACKLOG"
    if state_type == "triage":
        return "TRIAGE"
    raise ValueError("invalid_state")


def classify_comment_prefix(body: str) -> str | None:
    for prefix in WORKFLOW_PREFIXES:
        if body.startswith(prefix):
            if prefix == "**Plan approved**":
                return "plan"
            if prefix == "**Execution handoff**":
                return "handoff"
            if prefix == "**Session review**":
                return "review"
            if prefix == "**Close proposed**":
                return "close_proposed"
            if prefix == "**Owner verdict in session:":
                return "owner_verdict"
    return None


def _logical_hash(transformed: dict[str, Any]) -> str:
    # Batch-local provenance must never leak into parity hashes: identical exports staged
    # by different batches have to produce identical logical hashes.
    return sha256(
        {key: value for key, value in transformed.items() if key != "provenance"}
    )


_PROJECTION_FIELDS: dict[str, tuple[str, ...]] = {
    "repositories": ("key", "name", "url", "archived"),
    "users": ("name", "display_name", "active"),
    "states": ("name", "state_type", "position", "archived"),
    "labels": ("name", "color", "archived"),
    "worlds": ("key", "name", "kind", "target_date", "archived"),
    "surfaces": ("key", "name", "kind", "target_date", "archived"),
    "promises": ("key", "name", "kind", "target_date", "archived"),
    "project_health": ("health", "updated_at"),
}

_PROJECTION_TABLES: dict[str, tuple[str, str]] = {
    "repositories": ("omp_work.repositories", "repository_id"),
    "users": ("omp_work.principals", "principal_id"),
    "states": ("omp_work.workflow_states", "workflow_state_id"),
    "labels": ("omp_work.labels", "label_id"),
    "worlds": ("omp_work.projects", "project_id"),
    "surfaces": ("omp_work.projects", "project_id"),
    "promises": ("omp_work.projects", "project_id"),
    "project_health": ("omp_work.project_health", "project_id"),
}


def _norm_ts(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return (
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        .astimezone(UTC)
        .isoformat()
    )


class LinearImporter:
    def __init__(self, config: OperationsConfig) -> None:
        self.config = config

    def _read_mapping(self, mapping_file: Path) -> tuple[LinearImportMap, str]:
        if not mapping_file.is_file() or mapping_file.stat().st_mode & 0o777 != 0o600:
            raise ValueError("linear_import_mapping_invalid")
        try:
            payload = json.loads(mapping_file.read_text(encoding="utf-8"))
            mapping = LinearImportMap.model_validate(payload)
        except Exception:
            raise ValueError("linear_import_mapping_invalid") from None
        mapping_hash = sha256(canonical_json(mapping.model_dump(mode="json")))
        return mapping, mapping_hash

    def _get_operator_actor_id(self) -> UUID:
        try:
            raw = self.config.read_secret("operator-actor-id")
            return UUID(raw.strip())
        except Exception as error:
            raise RuntimeError("operator_actor_id_missing") from error

    def stage(
        self, workspace_id: UUID, export_id: UUID, mapping_file: Path
    ) -> ImportBatchSummary:
        mapping, mapping_hash = self._read_mapping(mapping_file)
        operator_actor_id = self._get_operator_actor_id()
        manifest, source_hashes, pages = load_export(self.config, export_id)

        if manifest.workspace_id != workspace_id:
            raise ValueError("linear_import_missing")

        # Issues attached to a Linear project require an explicit project_repositories
        # entry; unprojected_repository is reserved for issues without any project.
        referenced_projects = {
            str(node["project"]["id"])
            for page in pages
            if page.stream.split(":", 1)[1] == "issues"
            for node in page.nodes
            if isinstance(node.get("project"), dict) and node["project"].get("id")
        }
        if any(
            project_id not in mapping.project_repositories
            for project_id in referenced_projects
        ):
            raise ValueError("linear_import_mapping_invalid")

        batch_id, base_batch_id, is_already_done = self._init_batch(
            workspace_id, export_id, manifest, mapping_hash, operator_actor_id
        )
        if is_already_done:
            with _connect(self.config, "omp_work_importer") as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('omp.workspace_id', %s, true)",
                        (str(workspace_id),),
                    )
                    cur.execute(
                        "SELECT set_config('omp.actor_id', %s, true)",
                        (str(operator_actor_id),),
                    )
                    return self._build_summary(cur, workspace_id, batch_id)

        for page in pages:
            self._stage_single_page(workspace_id, batch_id, page, operator_actor_id)

        return self._finalize_staging(
            workspace_id,
            batch_id,
            export_id,
            mapping,
            source_hashes,
            pages,
            manifest,
            operator_actor_id,
        )

    def _init_batch(
        self,
        workspace_id: UUID,
        export_id: UUID,
        manifest: ExportManifest,
        mapping_hash: str,
        operator_actor_id: UUID,
    ) -> tuple[UUID, UUID | None, bool]:
        with _connect(self.config, "omp_work_importer") as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL search_path = pg_catalog")
                    cur.execute(
                        "SELECT set_config('omp.workspace_id', %s, true)",
                        (str(workspace_id),),
                    )
                    cur.execute(
                        "SELECT set_config('omp.actor_id', %s, true)",
                        (str(operator_actor_id),),
                    )
                    cur.execute(
                        "SELECT state FROM omp_integration.raw_exports WHERE workspace_id = %s AND export_id = %s",
                        (workspace_id, export_id),
                    )
                    raw_export_row = cur.fetchone()
                    if not raw_export_row or raw_export_row[0] not in (
                        "complete",
                        "blocked",
                    ):
                        raise ValueError("linear_import_missing")
                    raw_export_state = raw_export_row[0]

                    base_batch_id: UUID | None = None
                    if manifest.mode == "delta":
                        if manifest.base_export_id is None:
                            raise ValueError("linear_import_base_invalid")
                        cur.execute(
                            "SELECT batch_id, state, parity_hashes FROM omp_integration.import_batches WHERE workspace_id = %s AND export_id = %s AND transformation_version = %s",
                            (
                                workspace_id,
                                manifest.base_export_id,
                                TRANSFORMATION_VERSION,
                            ),
                        )
                        row = cur.fetchone()
                        if not row or row[1] != "promoted":
                            raise ValueError("linear_import_base_invalid")
                        base_batch_id = row[0]
                        stored_parity = (
                            row[2] if isinstance(row[2], dict) else json.loads(row[2])
                        )
                        base_manifest, _, _ = load_export(
                            self.config, manifest.base_export_id
                        )
                        if (
                            stored_parity.get("dimension_hashes")
                            != base_manifest.dimension_hashes.model_dump()
                            or stored_parity.get("dimension_counts")
                            != base_manifest.dimension_counts.model_dump()
                        ):
                            raise ValueError("linear_import_base_invalid")

                    cur.execute(
                        "SELECT batch_id, state, mapping_file_sha256, artifact_root FROM omp_integration.import_batches WHERE workspace_id = %s AND export_id = %s AND transformation_version = %s",
                        (workspace_id, export_id, TRANSFORMATION_VERSION),
                    )
                    existing = cur.fetchone()
                    if existing:
                        b_id, state, existing_map_hash, _ = (
                            existing[0],
                            existing[1],
                            existing[2],
                            existing[3],
                        )
                        if existing_map_hash != mapping_hash:
                            raise ValueError("linear_import_mapping_invalid")
                        if state in ("staged", "reconciled", "promoted", "blocked"):
                            return b_id, base_batch_id, True
                        return b_id, base_batch_id, False

                    cur.execute("SELECT uuidv7()")
                    b_id = cur.fetchone()[0]
                    # Always begin in staging, even for blocked source exports: a crash
                    # mid-staging must remain resumable. _finalize_staging transitions to
                    # blocked once every page and anomaly is durably copied.
                    artifact_root = f"linear-imports/{workspace_id}/{b_id}"
                    cur.execute(
                        """
                        INSERT INTO omp_integration.import_batches (
                            batch_id, workspace_id, export_id, base_batch_id,
                            transformation_version, mapping_file_sha256, state, artifact_root
                        ) VALUES (%s, %s, %s, %s, %s, %s, 'staging', %s)
                        """,
                        (
                            b_id,
                            workspace_id,
                            export_id,
                            base_batch_id,
                            TRANSFORMATION_VERSION,
                            mapping_hash,
                            artifact_root,
                        ),
                    )
                    return b_id, base_batch_id, False

    def _stage_single_page(
        self,
        workspace_id: UUID,
        batch_id: UUID,
        page: SourcePage,
        operator_actor_id: UUID,
    ) -> None:
        with _connect(self.config, "omp_work_importer") as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL search_path = pg_catalog")
                    cur.execute(
                        "SELECT set_config('omp.workspace_id', %s, true)",
                        (str(workspace_id),),
                    )
                    cur.execute(
                        "SELECT set_config('omp.actor_id', %s, true)",
                        (str(operator_actor_id),),
                    )

                    cur.execute(
                        "SELECT artifact_key, plaintext_sha256 FROM omp_integration.import_pages WHERE batch_id = %s AND stream = %s AND page_index = %s",
                        (batch_id, page.stream, page.page_index),
                    )
                    page_row = cur.fetchone()
                    page_key = f"{page.stream}:{page.page_index}"
                    page_digest = sha256(page.model_dump(mode="json"))
                    if page_row:
                        if page_row[0] != page_key or page_row[1] != page_digest:
                            raise RuntimeError("pagination_count_hash_gap")
                        return

                    cur.execute(
                        """
                        INSERT INTO omp_integration.import_pages (
                            batch_id, workspace_id, stream, page_index, artifact_key, plaintext_sha256, node_count
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            batch_id,
                            workspace_id,
                            page.stream,
                            page.page_index,
                            page_key,
                            page_digest,
                            len(page.nodes),
                        ),
                    )
                    for occ_idx, node in enumerate(page.nodes):
                        source_id = str(node["id"])
                        raw_hash = sha256(node)
                        cur.execute(
                            """
                            INSERT INTO omp_integration.import_page_records (
                                batch_id, workspace_id, stream, page_index, occurrence_index, source_id, raw_record_sha256
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                batch_id,
                                workspace_id,
                                page.stream,
                                page.page_index,
                                occ_idx,
                                source_id,
                                raw_hash,
                            ),
                        )

    def _finalize_staging(
        self,
        workspace_id: UUID,
        batch_id: UUID,
        export_id: UUID,
        mapping: LinearImportMap,
        source_hashes: SourceHashIndex,
        pages: tuple[SourcePage, ...],
        manifest: ExportManifest,
        operator_actor_id: UUID,
    ) -> ImportBatchSummary:
        with _connect(self.config, "omp_work_importer") as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL search_path = pg_catalog")
                    cur.execute(
                        "SELECT set_config('omp.workspace_id', %s, true)",
                        (str(workspace_id),),
                    )
                    cur.execute(
                        "SELECT set_config('omp.actor_id', %s, true)",
                        (str(operator_actor_id),),
                    )

                    cur.execute(
                        "SELECT COUNT(*) FROM omp_integration.import_pages WHERE batch_id = %s",
                        (batch_id,),
                    )
                    if cur.fetchone()[0] != len(pages):
                        raise RuntimeError("pagination_count_hash_gap")

                    for anomaly in manifest.anomalies:
                        cur.execute("SELECT uuidv7()")
                        anomaly_id = cur.fetchone()[0]
                        cur.execute(
                            """
                            INSERT INTO omp_integration.migration_anomalies (
                                anomaly_id, batch_id, workspace_id, origin, code, disposition, message, details
                            ) VALUES (%s, %s, %s, 'exporter', %s, %s, %s, '{}'::jsonb)
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                anomaly_id,
                                batch_id,
                                workspace_id,
                                anomaly.code,
                                anomaly.disposition,
                                anomaly.code,
                            ),
                        )
                    # Stage exactly one logical entity per SourceHashEntry: keep only the
                    # page occurrence whose hash matches the exporter-selected winner, so
                    # baseline/overlap duplicates never diverge from the manifest index.
                    stream_dimensions: dict[str, tuple[str, bool]] = {
                        "initiatives": ("worlds", False),
                        "projects": ("surfaces", True),
                        "projectUpdates": ("project_updates", False),
                        "projectMilestones": ("promises", False),
                        "issues": ("work_items", False),
                        "workflowStates": ("states", False),
                        "issueLabels": ("labels", False),
                        "initiativeToProjects": ("relations", False),
                        "issueRelations": ("relations", False),
                        "comments": ("comments", False),
                        "attachments": ("attachments", False),
                    }
                    selected: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
                    for page in pages:
                        stream_name = page.stream.split(":", 1)[1]
                        dim_info = stream_dimensions.get(stream_name)
                        for node in page.nodes:
                            node_id = str(node.get("id", ""))
                            if not node_id:
                                continue
                            if dim_info is not None:
                                dim_name, use_source_hash = dim_info
                                entry = getattr(source_hashes, dim_name).get(node_id)
                                if entry is None:
                                    continue
                                expected = (
                                    entry.source_sha256
                                    if use_source_hash
                                    else entry.record_sha256
                                )
                                if sha256(node) != expected:
                                    continue
                            if node_id not in selected[stream_name]:
                                selected[stream_name][node_id] = node
                    records_by_stream: dict[str, list[dict[str, Any]]] = {
                        stream_name: list(nodes.values())
                        for stream_name, nodes in selected.items()
                    }

                    self._stage_lookup_and_domain_records(
                        cur,
                        workspace_id,
                        batch_id,
                        export_id,
                        mapping,
                        source_hashes,
                        records_by_stream,
                        manifest,
                    )

                    # Block on any blocking anomaly copied from the export OR raised by
                    # importer-side staging validation (e.g. unknown workflow state types).
                    cur.execute(
                        "SELECT 1 FROM omp_integration.migration_anomalies WHERE batch_id = %s AND disposition = 'blocking' LIMIT 1",
                        (batch_id,),
                    )
                    is_blocked = cur.fetchone() is not None
                    if is_blocked:
                        cur.execute(
                            "UPDATE omp_integration.import_batches SET state = 'blocked' WHERE batch_id = %s AND state <> 'blocked'",
                            (batch_id,),
                        )
                    else:
                        cur.execute(
                            "UPDATE omp_integration.import_batches SET state = 'staged', staged_at = clock_timestamp() WHERE batch_id = %s AND state = 'staging'",
                            (batch_id,),
                        )

                    return self._build_summary(cur, workspace_id, batch_id)

    def _get_or_create_local_id(
        self,
        cur: Any,
        workspace_id: UUID,
        batch_id: UUID,
        system: str,
        external_id: str,
        local_type: str,
        identifier: str | None = None,
    ) -> UUID:
        cur.execute(
            "SELECT local_id, local_type FROM omp_integration.external_refs WHERE workspace_id = %s AND system = %s AND external_id = %s",
            (workspace_id, system, external_id),
        )
        row = cur.fetchone()
        if row:
            if row[1] != local_type:
                raise ValueError("duplicate_uuid_key_mapping")
            return row[0]

        if identifier:
            cur.execute(
                "SELECT work_id FROM omp_work.work_aliases WHERE workspace_id = %s AND key = %s",
                (workspace_id, identifier),
            )
            alias_row = cur.fetchone()
            if alias_row:
                cur.execute(
                    "SELECT external_id FROM omp_integration.external_refs WHERE workspace_id = %s AND local_id = %s AND system = %s",
                    (workspace_id, alias_row[0], system),
                )
                ref_row = cur.fetchone()
                if ref_row and ref_row[0] != external_id:
                    raise ValueError("duplicate_uuid_key_mapping")

        cur.execute(
            "SELECT local_id FROM omp_integration.import_records WHERE batch_id = %s AND source_id = %s AND local_type = %s",
            (batch_id, external_id, local_type),
        )
        rec_row = cur.fetchone()
        if rec_row:
            return rec_row[0]

        cur.execute("SELECT uuidv7()")
        return cur.fetchone()[0]

    def _stage_lookup_and_domain_records(
        self,
        cur: Any,
        workspace_id: UUID,
        batch_id: UUID,
        export_id: UUID,
        mapping: LinearImportMap,
        source_hashes: SourceHashIndex,
        records: dict[str, list[dict[str, Any]]],
        manifest: ExportManifest,
    ) -> None:
        for repo_key, repo_map in sorted(mapping.repositories.items()):
            local_repo_id = self._get_or_create_local_id(
                cur, workspace_id, batch_id, "linear_repo", repo_key, "repository"
            )
            transformed_repo = {
                "key": repo_map.key,
                "name": repo_map.name,
                "url": repo_map.url,
                "archived": False,
                "provenance": {"import_batch_id": str(batch_id), "source": "mapping"},
            }
            repo_hash = _logical_hash(transformed_repo)
            cur.execute(
                """
                INSERT INTO omp_integration.import_records (
                    batch_id, workspace_id, entity_type, source_id, local_id, local_type,
                    artifact_ref, source_sha256, logical_sha256, transformed_json
                ) VALUES (%s, %s, 'repositories', %s, %s, 'repository', 'mapping', %s, %s, %s)
                ON CONFLICT (batch_id, entity_type, source_id) DO NOTHING
                """,
                (
                    batch_id,
                    workspace_id,
                    repo_key,
                    local_repo_id,
                    repo_hash,
                    repo_hash,
                    json.dumps(transformed_repo),
                ),
            )

        users_dict: dict[str, dict[str, Any]] = {}
        for stream_name, fields in (
            ("projects", ("lead",)),
            ("projectUpdates", ("user",)),
            ("issues", ("assignee", "creator")),
            ("comments", ("user",)),
            ("attachments", ("creator",)),
        ):
            for node in records.get(stream_name, []):
                for field in fields:
                    person = node.get(field)
                    if isinstance(person, dict) and person.get("id"):
                        u_id = str(person["id"])
                        if u_id not in users_dict:
                            users_dict[u_id] = {
                                k: person.get(k)
                                for k in ("id", "name", "displayName", "active")
                            }

        for user_id, u_node in sorted(users_dict.items()):
            local_user_id = self._get_or_create_local_id(
                cur, workspace_id, batch_id, "linear", user_id, "principal"
            )
            transformed_user = {
                "name": u_node.get("name") or u_node.get("displayName") or "User",
                "display_name": u_node.get("displayName")
                or u_node.get("name")
                or "User",
                "active": bool(u_node.get("active", True)),
                "provenance": {"import_batch_id": str(batch_id), "source_id": user_id},
            }
            u_hash = sha256(u_node)
            art_ref = (
                source_hashes.users[user_id].artifact_ref
                if user_id in source_hashes.users
                else "pages"
            )
            cur.execute(
                """
                INSERT INTO omp_integration.import_records (
                    batch_id, workspace_id, entity_type, source_id, local_id, local_type,
                    artifact_ref, source_sha256, logical_sha256, transformed_json
                ) VALUES (%s, %s, 'users', %s, %s, 'principal', %s, %s, %s, %s)
                ON CONFLICT (batch_id, entity_type, source_id) DO NOTHING
                """,
                (
                    batch_id,
                    workspace_id,
                    user_id,
                    local_user_id,
                    art_ref,
                    u_hash,
                    u_hash,
                    json.dumps(transformed_user),
                ),
            )

        for state in records.get("workflowStates", []):
            s_id = str(state["id"])
            local_state_id = self._get_or_create_local_id(
                cur, workspace_id, batch_id, "linear", s_id, "workflow_state"
            )
            transformed_state = {
                "name": state.get("name", ""),
                "state_type": state.get("type", "backlog"),
                "position": int(state.get("position", 0)),
                "archived": bool(state.get("archivedAt")),
                "provenance": {"import_batch_id": str(batch_id), "source_id": s_id},
            }
            s_hash = _logical_hash(transformed_state)
            raw_state_hash = sha256(state)
            art_ref = (
                source_hashes.states[s_id].artifact_ref
                if s_id in source_hashes.states
                else "pages"
            )
            cur.execute(
                """
                INSERT INTO omp_integration.import_records (
                    batch_id, workspace_id, entity_type, source_id, local_id, local_type,
                    artifact_ref, source_sha256, logical_sha256, transformed_json
                ) VALUES (%s, %s, 'states', %s, %s, 'workflow_state', %s, %s, %s, %s)
                ON CONFLICT (batch_id, entity_type, source_id) DO NOTHING
                """,
                (
                    batch_id,
                    workspace_id,
                    s_id,
                    local_state_id,
                    art_ref,
                    raw_state_hash,
                    s_hash,
                    json.dumps(transformed_state),
                ),
            )

        for label in records.get("issueLabels", []):
            l_id = str(label["id"])
            local_label_id = self._get_or_create_local_id(
                cur, workspace_id, batch_id, "linear", l_id, "label"
            )
            transformed_label = {
                "name": label.get("name", ""),
                "color": label.get("color"),
                "archived": bool(label.get("archivedAt")),
                "provenance": {"import_batch_id": str(batch_id), "source_id": l_id},
            }
            l_hash = _logical_hash(transformed_label)
            raw_label_hash = sha256(label)
            art_ref = (
                source_hashes.labels[l_id].artifact_ref
                if l_id in source_hashes.labels
                else "pages"
            )
            cur.execute(
                """
                INSERT INTO omp_integration.import_records (
                    batch_id, workspace_id, entity_type, source_id, local_id, local_type,
                    artifact_ref, source_sha256, logical_sha256, transformed_json
                ) VALUES (%s, %s, 'labels', %s, %s, 'label', %s, %s, %s, %s)
                ON CONFLICT (batch_id, entity_type, source_id) DO NOTHING
                """,
                (
                    batch_id,
                    workspace_id,
                    l_id,
                    local_label_id,
                    art_ref,
                    raw_label_hash,
                    l_hash,
                    json.dumps(transformed_label),
                ),
            )

        for world in records.get("initiatives", []):
            w_id = str(world["id"])
            local_world_id = self._get_or_create_local_id(
                cur, workspace_id, batch_id, "linear", w_id, "project"
            )
            transformed_world = {
                "key": None,
                "name": world.get("name", ""),
                "kind": "world",
                "target_date": world.get("targetDate"),
                "archived": bool(world.get("archivedAt")),
                "provenance": {"import_batch_id": str(batch_id), "source_id": w_id},
            }
            w_hash = _logical_hash(transformed_world)
            raw_world_hash = sha256(world)
            art_ref = (
                source_hashes.worlds[w_id].artifact_ref
                if w_id in source_hashes.worlds
                else "pages"
            )
            cur.execute(
                """
                INSERT INTO omp_integration.import_records (
                    batch_id, workspace_id, entity_type, source_id, local_id, local_type,
                    artifact_ref, source_sha256, logical_sha256, transformed_json
                ) VALUES (%s, %s, 'worlds', %s, %s, 'project', %s, %s, %s, %s)
                ON CONFLICT (batch_id, entity_type, source_id) DO NOTHING
                """,
                (
                    batch_id,
                    workspace_id,
                    w_id,
                    local_world_id,
                    art_ref,
                    raw_world_hash,
                    w_hash,
                    json.dumps(transformed_world),
                ),
            )

        for surface in records.get("projects", []):
            surf_id = str(surface["id"])
            local_surf_id = self._get_or_create_local_id(
                cur, workspace_id, batch_id, "linear", surf_id, "project"
            )
            source_hash = sha256(surface)
            updates = [
                upd
                for upd in records.get("projectUpdates", [])
                if str((upd.get("project") or {}).get("id")) == surf_id
            ]
            record_hash = (
                source_hash
                if not updates
                else sha256(
                    {
                        "project": source_hash,
                        "updates": [
                            {"id": str(u["id"]), "record_sha256": sha256(u)}
                            for u in sorted(
                                updates, key=lambda item: str(item.get("id", ""))
                            )
                        ],
                    }
                )
            )
            transformed_surf = {
                "key": None,
                "name": surface.get("name", ""),
                "kind": "surface",
                "target_date": surface.get("targetDate"),
                "archived": bool(surface.get("archivedAt")),
                "health": surface.get("health"),
                "source_updated_at": surface.get("updatedAt"),
                "provenance": {"import_batch_id": str(batch_id), "source_id": surf_id},
            }
            art_ref = (
                source_hashes.surfaces[surf_id].artifact_ref
                if surf_id in source_hashes.surfaces
                else "pages"
            )
            cur.execute(
                """
                INSERT INTO omp_integration.import_records (
                    batch_id, workspace_id, entity_type, source_id, local_id, local_type,
                    artifact_ref, source_sha256, logical_sha256, transformed_json
                ) VALUES (%s, %s, 'surfaces', %s, %s, 'project', %s, %s, %s, %s)
                ON CONFLICT (batch_id, entity_type, source_id) DO NOTHING
                """,
                (
                    batch_id,
                    workspace_id,
                    surf_id,
                    local_surf_id,
                    art_ref,
                    source_hash,
                    record_hash,
                    json.dumps(transformed_surf),
                ),
            )
        for promise in records.get("projectMilestones", []):
            prom_id = str(promise["id"])
            local_prom_id = self._get_or_create_local_id(
                cur, workspace_id, batch_id, "linear", prom_id, "project"
            )
            proj_id = str((promise.get("project") or {}).get("id", ""))
            transformed_prom = {
                "key": None,
                "name": promise.get("name", ""),
                "kind": "promise",
                "project_id": proj_id if proj_id else None,
                "target_date": promise.get("targetDate"),
                "archived": bool(promise.get("archivedAt")),
                "provenance": {"import_batch_id": str(batch_id), "source_id": prom_id},
            }
            prom_hash = _logical_hash(transformed_prom)
            raw_prom_hash = sha256(promise)
            art_ref = (
                source_hashes.promises[prom_id].artifact_ref
                if prom_id in source_hashes.promises
                else "pages"
            )
            cur.execute(
                """
                INSERT INTO omp_integration.import_records (
                    batch_id, workspace_id, entity_type, source_id, local_id, local_type,
                    artifact_ref, source_sha256, logical_sha256, transformed_json
                ) VALUES (%s, %s, 'promises', %s, %s, 'project', %s, %s, %s, %s)
                ON CONFLICT (batch_id, entity_type, source_id) DO NOTHING
                """,
                (
                    batch_id,
                    workspace_id,
                    prom_id,
                    local_prom_id,
                    art_ref,
                    raw_prom_hash,
                    prom_hash,
                    json.dumps(transformed_prom),
                ),
            )

        for issue in records.get("issues", []):
            i_id = str(issue["id"])
            identifier = issue.get("identifier") or ""
            local_work_id = self._get_or_create_local_id(
                cur, workspace_id, batch_id, "linear", i_id, "work_item", identifier
            )

            # Only HOME/OMP keys can ever live in work_aliases; moved-in issues carry
            # foreign-team identifiers (e.g. ENG-42) that must never reach the CHECKed
            # column. Raw values remain in the encrypted source pages/record hashes;
            # every downstream alias path uses these filtered fields.
            alias_identifier = identifier if _ALIAS_KEY_RE.match(identifier) else ""
            alias_previous_identifiers = [
                key
                for key in (str(p) for p in issue.get("previousIdentifiers", []) if p)
                if _ALIAS_KEY_RE.match(key)
            ]

            state_node = issue.get("state") or {}
            state_type = state_node.get("type")
            completed_at = issue.get("completedAt")
            canceled_at = issue.get("canceledAt")
            try:
                canonical_state = map_work_item_state(
                    state_type, completed_at, canceled_at
                )
            except ValueError:
                cur.execute("SELECT uuidv7()")
                anomaly_id = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO omp_integration.migration_anomalies (
                        anomaly_id, batch_id, workspace_id, origin, code, disposition, entity_type, source_id, message, details
                    ) VALUES (%s, %s, %s, 'importer', 'unsupported_non_workflow_object', 'blocking', 'issues', %s, 'invalid state type', '{}'::jsonb)
                    ON CONFLICT DO NOTHING
                    """,
                    (anomaly_id, batch_id, workspace_id, i_id),
                )
                canonical_state = "BACKLOG"

            title = (issue.get("title") or "").strip()
            description = issue.get("description") or ""
            criteria = parse_acceptance_criteria(description)
            revision_payload = {
                "title": title,
                "description": description,
                "scope": "",
                "acceptance_criteria": criteria,
            }
            content_hash = sha256(revision_payload)

            proj_id = (issue.get("project") or {}).get("id")
            if proj_id:
                repo_key = mapping.project_repositories[str(proj_id)]
            else:
                repo_key = mapping.unprojected_repository

            local_repo_id = self._get_or_create_local_id(
                cur, workspace_id, batch_id, "linear_repo", repo_key, "repository"
            )
            local_proj_id = (
                self._get_or_create_local_id(
                    cur, workspace_id, batch_id, "linear", str(proj_id), "project"
                )
                if proj_id
                else None
            )
            state_id = state_node.get("id")
            local_state_id = (
                self._get_or_create_local_id(
                    cur,
                    workspace_id,
                    batch_id,
                    "linear",
                    str(state_id),
                    "workflow_state",
                )
                if state_id
                else None
            )
            assignee_id = (issue.get("assignee") or {}).get("id")
            local_assignee_id = (
                self._get_or_create_local_id(
                    cur, workspace_id, batch_id, "linear", str(assignee_id), "principal"
                )
                if assignee_id
                else None
            )

            label_ids = [
                str(l["id"])
                for l in (issue.get("labels") or {}).get("nodes", [])
                if l.get("id")
            ]
            local_label_ids = [
                self._get_or_create_local_id(
                    cur, workspace_id, batch_id, "linear", lid, "label"
                )
                for lid in label_ids
            ]

            transformed_issue = {
                "identifier": identifier,
                "alias_identifier": alias_identifier,
                "previous_identifiers": [
                    str(p) for p in issue.get("previousIdentifiers", []) if p
                ],
                "alias_previous_identifiers": alias_previous_identifiers,
                "title": title,
                "description": description,
                "scope": "",
                "acceptance_criteria": list(criteria),
                "content_sha256": content_hash,
                "state": canonical_state,
                "priority": issue.get("priority"),
                "archived": bool(issue.get("archivedAt")),
                "source_updated_at": issue.get("updatedAt"),
                "repository_id": str(local_repo_id),
                "project_id": str(local_proj_id) if local_proj_id else None,
                "workflow_state_id": str(local_state_id) if local_state_id else None,
                "assignee_id": str(local_assignee_id) if local_assignee_id else None,
                "label_ids": [str(lid) for lid in local_label_ids],
                "source_label_ids": label_ids,
                "parent_id": str((issue.get("parent") or {}).get("id"))
                if isinstance(issue.get("parent"), dict)
                and issue.get("parent", {}).get("id")
                else None,
                "url": issue.get("url"),
                "provenance": {
                    "import_batch_id": str(batch_id),
                    "source_id": i_id,
                    "identifier": identifier,
                },
            }
            raw_issue_hash = sha256(issue)
            art_ref = (
                source_hashes.work_items[i_id].artifact_ref
                if i_id in source_hashes.work_items
                else "pages"
            )
            cur.execute(
                """
                INSERT INTO omp_integration.import_records (
                    batch_id, workspace_id, entity_type, source_id, local_id, local_type,
                    artifact_ref, source_sha256, logical_sha256, transformed_json
                ) VALUES (%s, %s, 'work_items', %s, %s, 'work_item', %s, %s, %s, %s)
                ON CONFLICT (batch_id, entity_type, source_id) DO NOTHING
                """,
                (
                    batch_id,
                    workspace_id,
                    i_id,
                    local_work_id,
                    art_ref,
                    raw_issue_hash,
                    content_hash,
                    json.dumps(transformed_issue),
                ),
            )

        for update in records.get("projectUpdates", []):
            u_id = str(update["id"])
            proj_node = update.get("project") or {}
            p_id = str(proj_node.get("id", ""))
            local_proj_id = (
                self._get_or_create_local_id(
                    cur, workspace_id, batch_id, "linear", p_id, "project"
                )
                if p_id
                else None
            )
            local_upd_id = self._get_or_create_local_id(
                cur, workspace_id, batch_id, "linear", u_id, "project_update"
            )
            transformed_upd = {
                "project_id": str(local_proj_id) if local_proj_id else None,
                "source_project_id": p_id,
                "body": update.get("body", ""),
                "health": update.get("health"),
                "updated_at": update.get("updatedAt"),
                "created_at": update.get("createdAt"),
                "archived": bool(update.get("archivedAt")),
                "provenance": {"import_batch_id": str(batch_id), "source_id": u_id},
            }
            upd_hash = _logical_hash(transformed_upd)
            raw_upd_hash = sha256(update)
            art_ref = (
                source_hashes.project_updates[u_id].artifact_ref
                if u_id in source_hashes.project_updates
                else "pages"
            )
            cur.execute(
                """
                INSERT INTO omp_integration.import_records (
                    batch_id, workspace_id, entity_type, source_id, local_id, local_type,
                    artifact_ref, source_sha256, logical_sha256, transformed_json
                ) VALUES (%s, %s, 'project_updates', %s, %s, 'project_update', %s, %s, %s, %s)
                ON CONFLICT (batch_id, entity_type, source_id) DO NOTHING
                """,
                (
                    batch_id,
                    workspace_id,
                    u_id,
                    local_upd_id,
                    art_ref,
                    raw_upd_hash,
                    upd_hash,
                    json.dumps(transformed_upd),
                ),
            )

        for comment in records.get("comments", []):
            c_id = str(comment["id"])
            issue_node = comment.get("issue") or {}
            i_id = str(issue_node.get("id", ""))
            body = str(comment.get("body", ""))
            prefix_kind = classify_comment_prefix(body)
            local_comment_id = self._get_or_create_local_id(
                cur, workspace_id, batch_id, "linear", c_id, "comment"
            )
            local_issue_id = (
                self._get_or_create_local_id(
                    cur, workspace_id, batch_id, "linear", i_id, "work_item"
                )
                if i_id
                else None
            )
            transformed_comment = {
                "issue_id": str(local_issue_id) if local_issue_id else None,
                "source_issue_id": i_id,
                "body": body,
                "prefix_kind": prefix_kind,
                "created_at": comment.get("createdAt"),
                "updated_at": comment.get("updatedAt"),
                "archived": bool(comment.get("archivedAt")),
                "provenance": {"import_batch_id": str(batch_id), "source_id": c_id},
            }
            c_hash = _logical_hash(transformed_comment)
            raw_comment_hash = sha256(comment)
            art_ref = (
                source_hashes.comments[c_id].artifact_ref
                if c_id in source_hashes.comments
                else "pages"
            )
            cur.execute(
                """
                INSERT INTO omp_integration.import_records (
                    batch_id, workspace_id, entity_type, source_id, local_id, local_type,
                    artifact_ref, source_sha256, logical_sha256, transformed_json
                ) VALUES (%s, %s, 'comments', %s, %s, 'comment', %s, %s, %s, %s)
                ON CONFLICT (batch_id, entity_type, source_id) DO NOTHING
                """,
                (
                    batch_id,
                    workspace_id,
                    c_id,
                    local_comment_id,
                    art_ref,
                    raw_comment_hash,
                    c_hash,
                    json.dumps(transformed_comment),
                ),
            )

        for attachment in records.get("attachments", []):
            a_id = str(attachment.get("id", ""))
            issue_node = attachment.get("issue") or {}
            i_id = str(issue_node.get("id", ""))
            url = attachment.get("url")
            meta = attachment.get("metadata")
            usable = bool(a_id and i_id and (url or meta))
            local_att_id = (
                self._get_or_create_local_id(
                    cur, workspace_id, batch_id, "linear", a_id, "attachment"
                )
                if a_id
                else uuid4()
            )
            local_issue_id = (
                self._get_or_create_local_id(
                    cur, workspace_id, batch_id, "linear", i_id, "work_item"
                )
                if i_id
                else None
            )
            transformed_att = {
                "issue_id": str(local_issue_id) if local_issue_id else None,
                "source_issue_id": i_id,
                "title": attachment.get("title"),
                "subtitle": attachment.get("subtitle"),
                "url": url,
                "metadata": meta,
                "archived": bool(attachment.get("archivedAt")),
                "usable": usable,
                "provenance": {"import_batch_id": str(batch_id), "source_id": a_id},
            }
            a_hash = _logical_hash(transformed_att)
            raw_att_hash = sha256(attachment)
            art_ref = (
                source_hashes.attachments[a_id].artifact_ref
                if a_id in source_hashes.attachments
                else "pages"
            )
            cur.execute(
                """
                INSERT INTO omp_integration.import_records (
                    batch_id, workspace_id, entity_type, source_id, local_id, local_type,
                    artifact_ref, source_sha256, logical_sha256, transformed_json
                ) VALUES (%s, %s, 'attachments', %s, %s, 'attachment', %s, %s, %s, %s)
                ON CONFLICT (batch_id, entity_type, source_id) DO NOTHING
                """,
                (
                    batch_id,
                    workspace_id,
                    a_id or str(local_att_id),
                    local_att_id,
                    art_ref,
                    raw_att_hash,
                    a_hash,
                    json.dumps(transformed_att),
                ),
            )

        for init_proj in records.get("initiativeToProjects", []):
            r_id = str(init_proj["id"])
            init_id = str((init_proj.get("initiative") or {}).get("id", ""))
            proj_id = str((init_proj.get("project") or {}).get("id", ""))
            local_rel_id = self._get_or_create_local_id(
                cur, workspace_id, batch_id, "linear", r_id, "project_relation"
            )
            transformed_ip = {
                "initiative_id": init_id,
                "project_id": proj_id,
                "kind": "initiative_project",
                "archived": bool(init_proj.get("archivedAt")),
                "provenance": {"import_batch_id": str(batch_id), "source_id": r_id},
            }
            ip_hash = sha256(init_proj)
            art_ref = (
                source_hashes.relations[r_id].artifact_ref
                if r_id in source_hashes.relations
                else "pages"
            )
            cur.execute(
                """
                INSERT INTO omp_integration.import_records (
                    batch_id, workspace_id, entity_type, source_id, local_id, local_type,
                    artifact_ref, source_sha256, logical_sha256, transformed_json
                ) VALUES (%s, %s, 'relations', %s, %s, 'project_relation', %s, %s, %s, %s)
                ON CONFLICT (batch_id, entity_type, source_id) DO NOTHING
                """,
                (
                    batch_id,
                    workspace_id,
                    r_id,
                    local_rel_id,
                    art_ref,
                    ip_hash,
                    ip_hash,
                    json.dumps(transformed_ip),
                ),
            )

        for issue_rel in records.get("issueRelations", []):
            r_id = str(issue_rel["id"])
            src_i_id = str((issue_rel.get("issue") or {}).get("id", ""))
            tgt_i_id = str((issue_rel.get("relatedIssue") or {}).get("id", ""))
            local_rel_id = self._get_or_create_local_id(
                cur, workspace_id, batch_id, "linear", r_id, "work_relation"
            )
            transformed_ir = {
                "issue_id": src_i_id,
                "related_issue_id": tgt_i_id,
                "type": issue_rel.get("type"),
                "archived": bool(issue_rel.get("archivedAt")),
                "provenance": {"import_batch_id": str(batch_id), "source_id": r_id},
            }
            ir_hash = sha256(issue_rel)
            art_ref = (
                source_hashes.relations[r_id].artifact_ref
                if r_id in source_hashes.relations
                else "pages"
            )
            cur.execute(
                """
                INSERT INTO omp_integration.import_records (
                    batch_id, workspace_id, entity_type, source_id, local_id, local_type,
                    artifact_ref, source_sha256, logical_sha256, transformed_json
                ) VALUES (%s, %s, 'relations', %s, %s, 'work_relation', %s, %s, %s, %s)
                ON CONFLICT (batch_id, entity_type, source_id) DO NOTHING
                """,
                (
                    batch_id,
                    workspace_id,
                    r_id,
                    local_rel_id,
                    art_ref,
                    ir_hash,
                    ir_hash,
                    json.dumps(transformed_ir),
                ),
            )

    def _materialize_and_validate_relations(
        self, cur: Any, workspace_id: UUID, batch_id: UUID, operator_actor_id: UUID
    ) -> list[Anomaly]:
        anomalies: list[Anomaly] = []

        cur.execute(
            "SELECT base_batch_id FROM omp_integration.import_batches WHERE batch_id = %s",
            (batch_id,),
        )
        base_b_row = cur.fetchone()
        base_batch_id = base_b_row[0] if base_b_row else None
        records_map: dict[tuple[str, str], tuple[UUID, dict[str, Any]]] = {}
        if base_batch_id is not None:
            cur.execute(
                """
                SELECT entity_type, source_id, local_id, transformed_json
                FROM omp_integration.import_records
                WHERE batch_id = %s
                """,
                (base_batch_id,),
            )
            for row in cur.fetchall():
                records_map[(row[0], row[1])] = (
                    row[2],
                    row[3] if isinstance(row[3], dict) else json.loads(row[3]),
                )

        cur.execute(
            """
            SELECT entity_type, source_id, local_id, transformed_json
            FROM omp_integration.import_records
            WHERE batch_id = %s
            """,
            (batch_id,),
        )
        for row in cur.fetchall():
            records_map[(row[0], row[1])] = (
                row[2],
                row[3] if isinstance(row[3], dict) else json.loads(row[3]),
            )

        cur.execute(
            "SELECT relation_id, relation_kind, source_id, target_id, state FROM omp_integration.import_relations WHERE batch_id = %s",
            (batch_id,),
        )
        existing_rels = {(r[1], r[2], r[3]): (r[0], r[4]) for r in cur.fetchall()}

        def record_rel(
            relation_kind: str,
            source_entity_type: str,
            source_id: str,
            target_entity_type: str,
            target_id: str,
            canonical_id: UUID | None = None,
        ) -> tuple[UUID, str]:
            key = (relation_kind, source_id, target_id)
            if key in existing_rels:
                return existing_rels[key]
            cur.execute("SELECT uuidv7()")
            rel_id = cur.fetchone()[0]
            src_entry = records_map.get((source_entity_type, source_id))
            tgt_entry = records_map.get((target_entity_type, target_id))
            local_src = src_entry[0] if src_entry else None
            local_tgt = tgt_entry[0] if tgt_entry else None
            cur.execute(
                """
                INSERT INTO omp_integration.import_relations (
                    relation_id, batch_id, workspace_id, relation_kind,
                    source_entity_type, source_id, target_entity_type, target_id,
                    local_source_id, local_target_id, canonical_id, state
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                """,
                (
                    rel_id,
                    batch_id,
                    workspace_id,
                    relation_kind,
                    source_entity_type,
                    source_id,
                    target_entity_type,
                    target_id,
                    local_src,
                    local_tgt,
                    canonical_id,
                ),
            )
            existing_rels[key] = (rel_id, "pending")
            return rel_id, "pending"

        work_items = [v for k, v in records_map.items() if k[0] == "work_items"]
        for _, issue_data in work_items:
            source_id = issue_data["provenance"]["source_id"]
            parent_source_id = issue_data.get("parent_id")
            if parent_source_id:
                record_rel(
                    "parent", "work_items", source_id, "work_items", parent_source_id
                )

            for label_source_id in issue_data.get("source_label_ids", []):
                record_rel(
                    "issue_label", "work_items", source_id, "labels", label_source_id
                )

        comments = [v for k, v in records_map.items() if k[0] == "comments"]
        for _, comment_data in comments:
            c_source_id = comment_data["provenance"]["source_id"]
            i_source_id = comment_data.get("source_issue_id")
            if i_source_id:
                record_rel(
                    "comment_parent", "comments", c_source_id, "work_items", i_source_id
                )

        attachments = [v for k, v in records_map.items() if k[0] == "attachments"]
        for _, att_data in attachments:
            a_source_id = att_data["provenance"]["source_id"]
            i_source_id = att_data.get("source_issue_id")
            if i_source_id:
                record_rel(
                    "attachment_owner",
                    "attachments",
                    a_source_id,
                    "work_items",
                    i_source_id,
                )

        for (e_type, s_id), (record_local_id, data) in list(records_map.items()):
            if e_type == "relations":
                if data.get("kind") == "initiative_project":
                    init_id = data.get("initiative_id")
                    proj_id = data.get("project_id")
                    if init_id and proj_id:
                        record_rel(
                            "initiative_project",
                            "worlds",
                            init_id,
                            "surfaces",
                            proj_id,
                            canonical_id=record_local_id,
                        )
                elif data.get("type"):
                    raw_kind = str(data.get("type", "")).lower()
                    kind_map = {
                        "blocks": ("blocks", False),
                        "blockedby": ("blocks", True),
                        "duplicate": ("duplicate_of", False),
                        "duplicateof": ("duplicate_of", False),
                        "related": ("related", False),
                    }
                    mapping_entry = kind_map.get(raw_kind)
                    src_id = data.get("issue_id")
                    tgt_id = data.get("related_issue_id")
                    if not mapping_entry or not src_id or not tgt_id:
                        anomalies.append(
                            Anomaly(
                                code="unsupported_non_workflow_object",
                                disposition="quarantined",
                            )
                        )
                        continue
                    rel_kind_val, is_reverse = mapping_entry
                    if is_reverse:
                        record_rel(
                            rel_kind_val,
                            "work_items",
                            tgt_id,
                            "work_items",
                            src_id,
                            canonical_id=record_local_id,
                        )
                    else:
                        record_rel(
                            rel_kind_val,
                            "work_items",
                            src_id,
                            "work_items",
                            tgt_id,
                            canonical_id=record_local_id,
                        )
            elif e_type == "promises":
                proj_id = data.get("project_id")
                if proj_id:
                    record_rel(
                        "project_milestone", "surfaces", proj_id, "promises", s_id
                    )
        rels_dict: dict[tuple[str, str, str], tuple[Any, ...]] = {}
        if base_batch_id is not None:
            cur.execute(
                "SELECT relation_id, relation_kind, source_entity_type, source_id, target_entity_type, target_id, local_source_id, local_target_id, state FROM omp_integration.import_relations WHERE batch_id = %s",
                (base_batch_id,),
            )
            for r in cur.fetchall():
                rels_dict[(r[1], r[3], r[5])] = r

        cur.execute(
            "SELECT relation_id, relation_kind, source_entity_type, source_id, target_entity_type, target_id, local_source_id, local_target_id, state FROM omp_integration.import_relations WHERE batch_id = %s",
            (batch_id,),
        )
        for r in cur.fetchall():
            rels_dict[(r[1], r[3], r[5])] = r

        all_rels = list(rels_dict.values())
        cur.execute(
            "SELECT source_work_id, target_work_id, kind FROM omp_work.work_relations WHERE workspace_id = %s AND active = true",
            (workspace_id,),
        )
        raw_canonical = cur.fetchall()
        canonical_edges = [
            RelationEdge(
                workspace_id=workspace_id,
                source_work_id=r[0],
                target_work_id=r[1],
                kind=RelationKind(r[2]),
                active=True,
            )
            for r in raw_canonical
        ]
        canonical_parent_map: dict[UUID, UUID] = {
            r[0]: r[1] for r in raw_canonical if r[2] == "parent"
        }
        edge_list: list[RelationEdge] = list(canonical_edges)

        for rel in all_rels:
            rel_id, rel_kind, s_type, s_id, t_type, t_id, local_s, local_t, state = rel
            if state != "pending":
                continue

            src_entry = records_map.get((s_type, s_id))
            tgt_entry = records_map.get((t_type, t_id))

            if rel_kind == "attachment_owner":
                if not tgt_entry or not src_entry or not src_entry[1].get("usable"):
                    cur.execute(
                        "UPDATE omp_integration.import_relations SET state = 'quarantined', anomaly_code = 'attachment_content_unavailable' WHERE relation_id = %s",
                        (rel_id,),
                    )
                    anomalies.append(
                        Anomaly(
                            code="attachment_content_unavailable",
                            disposition="quarantined",
                        )
                    )
                    continue

            if not src_entry or not tgt_entry:
                cur.execute(
                    "UPDATE omp_integration.import_relations SET state = 'blocked', anomaly_code = 'missing_relation_endpoint' WHERE relation_id = %s",
                    (rel_id,),
                )
                anomalies.append(
                    Anomaly(code="missing_relation_endpoint", disposition="blocking")
                )
                continue

            if rel_kind in ("parent", "blocks", "duplicate_of", "related"):
                if local_s == local_t:
                    cur.execute(
                        "UPDATE omp_integration.import_relations SET state = 'blocked', anomaly_code = 'relation_cycle' WHERE relation_id = %s",
                        (rel_id,),
                    )
                    anomalies.append(
                        Anomaly(code="relation_cycle", disposition="blocking")
                    )
                    continue

                if rel_kind == "parent":
                    existing_parent = canonical_parent_map.get(local_s)
                    if existing_parent is not None and existing_parent != local_t:
                        cur.execute(
                            "UPDATE omp_integration.import_relations SET state = 'blocked', anomaly_code = 'relation_cycle' WHERE relation_id = %s",
                            (rel_id,),
                        )
                        anomalies.append(
                            Anomaly(code="relation_cycle", disposition="blocking")
                        )
                        continue
                    canonical_parent_map[local_s] = local_t

                candidate_edge = RelationEdge(
                    workspace_id=workspace_id,
                    source_work_id=local_s,
                    target_work_id=local_t,
                    kind=RelationKind(rel_kind),
                    active=True,
                )
                if candidate_edge not in edge_list:
                    if would_create_cycle(tuple(edge_list), candidate_edge):
                        cur.execute(
                            "UPDATE omp_integration.import_relations SET state = 'blocked', anomaly_code = 'relation_cycle' WHERE relation_id = %s",
                            (rel_id,),
                        )
                        anomalies.append(
                            Anomaly(code="relation_cycle", disposition="blocking")
                        )
                        continue
                    edge_list.append(candidate_edge)
            cur.execute(
                "UPDATE omp_integration.import_relations SET state = 'validated' WHERE relation_id = %s",
                (rel_id,),
            )
        plans: dict[str, tuple[str, str]] = {}
        sorted_comments = sorted(
            [v[1] for k, v in records_map.items() if k[0] == "comments"],
            key=lambda c: (
                str(c.get("created_at", "")),
                str(c["provenance"]["source_id"]),
            ),
        )
        for comment_data in sorted_comments:
            body = comment_data.get("body", "")
            created = comment_data.get("created_at", "")
            issue_id = comment_data.get("source_issue_id", "")
            if not issue_id:
                continue
            if body.startswith("**Plan approved**"):
                match = re.search(r"SHA-256: `([a-f0-9]{64})`", body)
                if match:
                    plans[issue_id] = match.group(1), created
                continue
            plan = plans.get(issue_id)
            if plan is None or created < plan[1]:
                continue
            if body.startswith("**Session review**"):
                match = re.search(r"Plan SHA-256: `([a-f0-9]{64})`", body)
                if match and match.group(1) != plan[0]:
                    anomalies.append(
                        Anomaly(code="legacy_authority_claim", disposition="blocking")
                    )

        now_label_ids = {
            k[1]
            for k, v in records_map.items()
            if k[0] == "labels" and str(v[1].get("name", "")).casefold() == "now"
        }
        focused = [
            v
            for k, v in records_map.items()
            if k[0] == "work_items"
            and not v[1].get("archived")
            and v[1].get("state") not in ("DONE", "CANCELED")
            and any(lid in now_label_ids for lid in v[1].get("source_label_ids", []))
        ]
        if len(focused) > 1:
            anomalies.append(
                Anomaly(code="multiple_focus_slots", disposition="blocking")
            )
        elif len(focused) == 1:
            cur.execute(
                "SELECT work_id FROM omp_work.focus_slots WHERE workspace_id = %s AND owner_id = %s",
                (workspace_id, operator_actor_id),
            )
            focus_row = cur.fetchone()
            if focus_row and focus_row[0] is not None and focus_row[0] != focused[0][0]:
                candidate_batches = [batch_id] + (
                    [base_batch_id] if base_batch_id is not None else []
                )
                cur.execute(
                    "SELECT entity_type FROM omp_integration.import_records WHERE batch_id = ANY(%s) AND local_id = %s",
                    (candidate_batches, focus_row[0]),
                )
                if not cur.fetchone():
                    anomalies.append(
                        Anomaly(code="source_local_conflict", disposition="blocking")
                    )

        for item_entry in work_items:
            item_id = item_entry[0]
            item_data = item_entry[1]
            source_id = item_data["provenance"]["source_id"]
            incoming_hash = item_data["content_sha256"]

            cur.execute(
                "SELECT current_revision_id, row_version FROM omp_work.work_items WHERE workspace_id = %s AND work_id = %s",
                (workspace_id, item_id),
            )
            w_row = cur.fetchone()
            if w_row:
                cur.execute(
                    """
                    SELECT canonical_sha256, revision_id, resulting_row_version
                    FROM omp_integration.import_record_results
                    WHERE workspace_id = %s AND entity_type = 'work_items' AND source_id = %s
                    AND disposition IN ('created', 'revised', 'projection_updated')
                    ORDER BY promoted_at DESC LIMIT 1
                    """,
                    (workspace_id, source_id),
                )
                last_res = cur.fetchone()
                if last_res:
                    last_hash, last_rev_id, last_row_version = last_res
                    if incoming_hash != last_hash:
                        if w_row[0] != last_rev_id or w_row[1] != last_row_version:
                            anomalies.append(
                                Anomaly(
                                    code="source_local_conflict", disposition="blocking"
                                )
                            )

        # Projection conflicts: unchanged source must never overwrite locally edited
        # repositories, principals, states, projects, labels, or project health.
        for (e_type, proj_s_id), (proj_l_id, proj_data) in records_map.items():
            if e_type not in _PROJECTION_FIELDS:
                continue
            staged_payload = {
                field: proj_data.get(field) for field in _PROJECTION_FIELDS[e_type]
            }
            decision, _ = self._projection_decision(
                cur, workspace_id, e_type, proj_s_id, proj_l_id, staged_payload
            )
            if decision == "source_local_conflict":
                anomalies.append(
                    Anomaly(code="source_local_conflict", disposition="blocking")
                )

        health_candidates: dict[str, dict[str, Any]] = {}
        for (e_type, upd_s_id), (_, upd_data) in records_map.items():
            if (
                e_type == "project_updates"
                and not upd_data.get("archived")
                and upd_data.get("project_id")
                and upd_data.get("health")
            ):
                key = str(upd_data["project_id"])
                candidate = {
                    "health": upd_data["health"],
                    "updated_at": _norm_ts(upd_data.get("updated_at")),
                    "project_source_id": upd_data.get("source_project_id"),
                    "update_source_id": upd_s_id,
                }
                existing_candidate = health_candidates.get(key)
                if existing_candidate is None or (
                    candidate["updated_at"] or "",
                    upd_s_id,
                ) > (
                    existing_candidate["updated_at"] or "",
                    existing_candidate["update_source_id"],
                ):
                    health_candidates[key] = candidate
        for (e_type, surf_s_id), (surf_l_id, surf_data) in records_map.items():
            if (
                e_type == "surfaces"
                and str(surf_l_id) not in health_candidates
                and surf_data.get("health")
            ):
                health_candidates[str(surf_l_id)] = {
                    "health": surf_data["health"],
                    "updated_at": _norm_ts(surf_data.get("source_updated_at")),
                    "project_source_id": surf_s_id,
                    "update_source_id": "",
                }
        for key, candidate in health_candidates.items():
            staged_payload = {
                "health": candidate["health"],
                "updated_at": candidate["updated_at"],
            }
            decision, _ = self._projection_decision(
                cur,
                workspace_id,
                "project_health",
                candidate["project_source_id"],
                UUID(key),
                staged_payload,
            )
            if decision == "source_local_conflict":
                anomalies.append(
                    Anomaly(code="source_local_conflict", disposition="blocking")
                )

        # Alias ownership: every current and previous identifier must map to this issue's
        # local ID — against canonical aliases and across the staged batch. Previous keys
        # are claimed too: a current-vs-previous (or previous-vs-previous) collision inside
        # the batch must block rather than resolve by promotion insert order.
        alias_claims: dict[str, Any] = {}
        for item_local_id, item_data in work_items:
            current_key = item_data.get("alias_identifier") or ""
            previous_keys = [
                key
                for key in item_data.get("alias_previous_identifiers", [])
                if key and key != current_key
            ]
            for key in ([current_key] if current_key else []) + previous_keys:
                prior_claim = alias_claims.get(key)
                if prior_claim is not None and prior_claim != item_local_id:
                    anomalies.append(
                        Anomaly(
                            code="duplicate_uuid_key_mapping", disposition="blocking"
                        )
                    )
                else:
                    alias_claims[key] = item_local_id
                cur.execute(
                    "SELECT work_id FROM omp_work.work_aliases WHERE workspace_id = %s AND key = %s",
                    (workspace_id, key),
                )
                alias_row = cur.fetchone()
                if alias_row and alias_row[0] != item_local_id:
                    anomalies.append(
                        Anomaly(
                            code="duplicate_uuid_key_mapping", disposition="blocking"
                        )
                    )

        unique_anomalies: dict[tuple[str, str], Anomaly] = {}
        for a in anomalies:
            unique_anomalies[(a.code, a.disposition)] = a
            cur.execute("SELECT uuidv7()")
            anomaly_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO omp_integration.migration_anomalies (
                    anomaly_id, batch_id, workspace_id, origin, code, disposition, message, details
                ) VALUES (%s, %s, %s, 'importer', %s, %s, %s, '{}'::jsonb)
                ON CONFLICT DO NOTHING
                """,
                (anomaly_id, batch_id, workspace_id, a.code, a.disposition, a.code),
            )

        return list(unique_anomalies.values())

    def _canonical_projection_hash(
        self, cur: Any, workspace_id: UUID, entity_type: str, local_id: UUID
    ) -> str | None:
        table, id_column = _PROJECTION_TABLES[entity_type]
        fields = _PROJECTION_FIELDS[entity_type]
        cur.execute(
            f"SELECT {', '.join(fields)} FROM {table} WHERE workspace_id = %s AND {id_column} = %s",
            (workspace_id, local_id),
        )
        canonical_row = cur.fetchone()
        if canonical_row is None:
            return None
        return sha256(
            {
                field: _norm_ts(value)
                if field == "updated_at"
                else (str(value) if isinstance(value, (date, datetime)) else value)
                for field, value in zip(fields, canonical_row)
            }
        )

    def _projection_decision(
        self,
        cur: Any,
        workspace_id: UUID,
        entity_type: str,
        source_id: str,
        local_id: UUID,
        staged_payload: dict[str, Any],
    ) -> tuple[str, str]:
        """Apply the work-item conflict rules to imported projections: unchanged source
        never overwrites local state, source-only change applies, both-changed conflicts."""
        incoming = sha256(staged_payload)
        cur.execute(
            """
            SELECT canonical_sha256 FROM omp_integration.import_record_results
            WHERE workspace_id = %s AND entity_type = %s AND source_id = %s
            AND disposition IN ('created', 'revised', 'projection_updated')
            AND canonical_sha256 IS NOT NULL
            ORDER BY promoted_at DESC LIMIT 1
            """,
            (workspace_id, entity_type, source_id),
        )
        baseline_row = cur.fetchone()
        baseline = baseline_row[0] if baseline_row else None

        canonical_hash = self._canonical_projection_hash(
            cur, workspace_id, entity_type, local_id
        )

        if baseline is None:
            return "created", incoming
        if baseline == incoming:
            return "unchanged", incoming
        if canonical_hash is not None and canonical_hash != baseline:
            return "source_local_conflict", incoming
        return "projection_updated", incoming

    def _projection_matches(
        self, cur: Any, workspace_id: UUID, work_id: UUID, data: dict[str, Any]
    ) -> bool:
        cur.execute(
            """
            SELECT state, repository_id, project_id, workflow_state_id, assignee_id, priority, archived
            FROM omp_work.work_items WHERE workspace_id = %s AND work_id = %s
            """,
            (workspace_id, work_id),
        )
        row = cur.fetchone()
        if row is None:
            return False
        current = (
            row[0],
            str(row[1]) if row[1] is not None else None,
            str(row[2]) if row[2] is not None else None,
            str(row[3]) if row[3] is not None else None,
            str(row[4]) if row[4] is not None else None,
            row[5],
            bool(row[6]),
        )
        staged = (
            data.get("state"),
            data.get("repository_id"),
            data.get("project_id"),
            data.get("workflow_state_id"),
            data.get("assignee_id"),
            data.get("priority"),
            bool(data.get("archived")),
        )
        if current != staged:
            return False
        cur.execute(
            "SELECT label_id FROM omp_work.work_item_labels WHERE workspace_id = %s AND work_id = %s AND active AND origin = 'imported'",
            (workspace_id, work_id),
        )
        current_labels = {str(r[0]) for r in cur.fetchall()}
        if current_labels != {str(label_id) for label_id in data.get("label_ids", [])}:
            return False
        # Imported aliases are append-only history and immutable once written: any same-owner
        # alias key satisfies the staged requirement, regardless of local/imported origin.
        cur.execute(
            "SELECT key FROM omp_work.work_aliases WHERE workspace_id = %s AND work_id = %s",
            (workspace_id, work_id),
        )
        current_aliases = {r[0] for r in cur.fetchall()}
        required_aliases: set[str] = set()
        if data.get("alias_identifier"):
            required_aliases.add(data["alias_identifier"])
        required_aliases.update(
            key for key in data.get("alias_previous_identifiers", []) if key
        )
        return required_aliases.issubset(current_aliases)

    def _compute_dispositions(
        self, cur: Any, workspace_id: UUID, batch_id: UUID, records: list[Any]
    ) -> tuple[dict[tuple[str, str], str], dict[str, int]]:
        dispositions: dict[tuple[str, str], str] = {}
        counts: dict[str, int] = defaultdict(int)

        for row in records:
            e_type, s_id, _, _, t_json, l_id = row
            data = t_json if isinstance(t_json, dict) else json.loads(t_json)

            if e_type == "attachments":
                disp = "metadata_only" if data.get("usable") else "quarantined"
            elif e_type == "comments":
                disp = "legacy_untrusted" if data.get("prefix_kind") else "quarantined"
            elif e_type == "project_updates":
                disp = "legacy_untrusted"
            elif e_type == "work_items":
                cur.execute(
                    "SELECT current_revision_id, row_version FROM omp_work.work_items WHERE workspace_id = %s AND work_id = %s",
                    (workspace_id, l_id),
                )
                existing_item = cur.fetchone()
                if not existing_item:
                    disp = "created"
                else:
                    cur.execute(
                        """
                        SELECT canonical_sha256, revision_id, resulting_row_version
                        FROM omp_integration.import_record_results
                        WHERE workspace_id = %s AND entity_type = 'work_items' AND source_id = %s
                        AND disposition IN ('created', 'revised', 'projection_updated')
                        ORDER BY promoted_at DESC LIMIT 1
                        """,
                        (workspace_id, s_id),
                    )
                    last_res = cur.fetchone()
                    if last_res and last_res[0] == data["content_sha256"]:
                        if (
                            existing_item[0] != last_res[1]
                            or existing_item[1] != last_res[2]
                        ) or self._projection_matches(cur, workspace_id, l_id, data):
                            disp = "unchanged"
                        else:
                            disp = "projection_updated"
                    else:
                        disp = "revised"
            else:
                if e_type in _PROJECTION_FIELDS:
                    staged_payload = {
                        field: data.get(field) for field in _PROJECTION_FIELDS[e_type]
                    }
                    decision, _ = self._projection_decision(
                        cur, workspace_id, e_type, s_id, l_id, staged_payload
                    )
                    disp = (
                        "blocked" if decision == "source_local_conflict" else decision
                    )
                else:
                    cur.execute(
                        "SELECT 1 FROM omp_integration.external_refs WHERE workspace_id = %s AND local_id = %s",
                        (workspace_id, l_id),
                    )
                    disp = "projection_updated" if cur.fetchone() else "created"

            dispositions[(e_type, s_id)] = disp
            counts[disp] += 1

        return dispositions, dict(counts)

    def _merged_import_records(
        self, cur: Any, batch_id: UUID, base_batch_id: UUID | None
    ) -> list[Any]:
        import_recs_dict: dict[tuple[str, str], Any] = {}
        if base_batch_id is not None:
            cur.execute(
                "SELECT entity_type, source_id, source_sha256, logical_sha256, transformed_json, local_id FROM omp_integration.import_records WHERE batch_id = %s ORDER BY entity_type, source_id",
                (base_batch_id,),
            )
            for r in cur.fetchall():
                import_recs_dict[(r[0], r[1])] = r
        cur.execute(
            "SELECT entity_type, source_id, source_sha256, logical_sha256, transformed_json, local_id FROM omp_integration.import_records WHERE batch_id = %s ORDER BY entity_type, source_id",
            (batch_id,),
        )
        for r in cur.fetchall():
            import_recs_dict[(r[0], r[1])] = r
        return list(import_recs_dict.values())

    def _evaluate_parity(
        self,
        cur: Any,
        batch_id: UUID,
        base_batch_id: UUID | None,
        dimension_counts: ReconciliationCounts,
        dimension_hashes: ReconciliationHashes,
        import_recs: list[Any],
        dispositions: dict[tuple[str, str], str],
    ) -> tuple[dict[str, str], dict[str, int], bool]:
        """Parity group hashes plus the source dimension count/hash check. Shared by
        reconcile and the promote-time drift recheck; no side effects."""
        parity_groups: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
        for e_type, s_id, s_hash, l_hash, t_json, l_id in import_recs:
            data = t_json if isinstance(t_json, dict) else json.loads(t_json)
            disp = dispositions.get((e_type, s_id))
            if disp == "quarantined":
                # Quarantined records stay explicit in the quarantine manifest but are
                # excluded from the non-quarantined parity denominator; the attachment
                # disposition group still accounts for them.
                if e_type == "attachments":
                    parity_groups["attachment_disposition"].append(
                        (disp, str(s_id), str(l_id), str(l_hash))
                    )
                continue
            parity_groups["entity_type"].append(
                (e_type, str(s_id), str(l_id), str(l_hash))
            )

            if e_type in ("worlds", "surfaces", "promises"):
                parity_groups["project"].append(
                    ("project", str(s_id), str(l_id), str(l_hash))
                )
            elif e_type == "repositories":
                parity_groups["repository"].append(
                    ("repository", str(s_id), str(l_id), str(l_hash))
                )
            elif e_type == "states":
                parity_groups["workflow_state"].append(
                    ("workflow_state", str(s_id), str(l_id), str(l_hash))
                )
            elif e_type == "work_items":
                parity_groups["lifecycle_bucket"].append(
                    (data.get("state", "BACKLOG"), str(s_id), str(l_id), str(l_hash))
                )
                for pos, crit in enumerate(data.get("acceptance_criteria", [])):
                    parity_groups["acceptance_criterion"].append(
                        (
                            "acceptance_criterion",
                            f"{s_id}:{pos}",
                            str(l_id),
                            sha256(crit),
                        )
                    )
                if any(
                    lid
                    in [
                        str(r[5])
                        for r in import_recs
                        if r[0] == "labels"
                        and (
                            r[4].get("name")
                            if isinstance(r[4], dict)
                            else json.loads(r[4]).get("name", "")
                        ).casefold()
                        == "now"
                    ]
                    for lid in data.get("label_ids", [])
                ):
                    parity_groups["focus_slot"].append(
                        ("focus_slot", str(s_id), str(l_id), str(l_hash))
                    )
            elif e_type == "labels":
                parity_groups["label"].append(
                    ("label", str(s_id), str(l_id), str(l_hash))
                )
            elif e_type in ("comments", "project_updates"):
                p_kind = data.get("prefix_kind") or (
                    "project_update" if e_type == "project_updates" else "user_comment"
                )
                parity_groups["comment_activity_type"].append(
                    (p_kind, str(s_id), str(l_id), str(l_hash))
                )
                if data.get("prefix_kind"):
                    parity_groups["legacy_artifact_type"].append(
                        (data["prefix_kind"], str(s_id), str(l_id), str(l_hash))
                    )
            elif e_type == "attachments":
                parity_groups["attachment_disposition"].append(
                    (disp or "metadata_only", str(s_id), str(l_id), str(l_hash))
                )

            parity_groups["external_reference"].append(
                (
                    e_type,
                    str(s_id),
                    str(l_id),
                    sha256(
                        {
                            "system": "linear",
                            "external_id": str(s_id),
                            "local_id": str(l_id),
                        }
                    ),
                )
            )
        rels_map: dict[tuple[str, str, str], tuple[str, str, str, str]] = {}
        if base_batch_id is not None:
            cur.execute(
                "SELECT relation_kind, source_id, target_id, state FROM omp_integration.import_relations WHERE batch_id = %s",
                (base_batch_id,),
            )
            for r_kind, s_id, t_id, r_state in cur.fetchall():
                rels_map[(r_kind, s_id, t_id)] = (r_kind, s_id, t_id, r_state)

        cur.execute(
            "SELECT relation_kind, source_id, target_id, state FROM omp_integration.import_relations WHERE batch_id = %s",
            (batch_id,),
        )
        for r_kind, s_id, t_id, r_state in cur.fetchall():
            rels_map[(r_kind, s_id, t_id)] = (r_kind, s_id, t_id, r_state)

        for r_kind, s_id, t_id, r_state in rels_map.values():
            if r_state == "quarantined":
                # Quarantined relations (unusable attachment owners, unsupported objects)
                # keep their anomaly/quarantine evidence but stay out of relation parity.
                continue
            r_hash = sha256(
                {"source": s_id, "target": t_id, "kind": r_kind, "state": r_state}
            )
            parity_groups["relation_type"].append(
                (r_kind, str(s_id), str(t_id), r_hash)
            )

        parity_hashes: dict[str, str] = {}
        for group_name in (
            "entity_type",
            "project",
            "repository",
            "workflow_state",
            "lifecycle_bucket",
            "label",
            "relation_type",
            "comment_activity_type",
            "acceptance_criterion",
            "legacy_artifact_type",
            "focus_slot",
            "attachment_disposition",
            "external_reference",
        ):
            items = sorted(parity_groups.get(group_name, []))
            parity_hashes[group_name] = sha256(items)

        # Reconstruct the exporter's logical record hashes from the merged (base + delta)
        # staged view: surfaces fold their project updates the same way the exporter does;
        # every other dimension's record hash is the exporter-selected occurrence's hash.
        updates_by_project: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for r in import_recs:
            if r[0] != "project_updates":
                continue
            upd_data = r[4] if isinstance(r[4], dict) else json.loads(r[4])
            owner = upd_data.get("source_project_id")
            if owner:
                updates_by_project[str(owner)].append((str(r[1]), str(r[2])))

        def _record_hash(dim: str, r: Any) -> str:
            if dim == "surfaces":
                folded = sorted(updates_by_project.get(str(r[1]), []))
                if folded:
                    return sha256(
                        {
                            "project": str(r[2]),
                            "updates": [
                                {"id": u_id, "record_sha256": u_hash}
                                for u_id, u_hash in folded
                            ],
                        }
                    )
            return str(r[2])

        dimensions_match = True
        for dim in (
            "worlds",
            "surfaces",
            "promises",
            "work_items",
            "states",
            "labels",
            "relations",
            "comments",
            "attachments",
            "users",
        ):
            dim_items = [r for r in import_recs if r[0] == dim]
            expected_count = getattr(dimension_counts, dim)
            expected_hash = getattr(dimension_hashes, dim)
            candidate_hash = sha256(
                [
                    {"id": str(r[1]), "record_sha256": _record_hash(dim, r)}
                    for r in sorted(dim_items, key=lambda item: str(item[1]))
                ]
            )
            if len(dim_items) != expected_count or candidate_hash != expected_hash:
                dimensions_match = False
        parity_counts = {
            group_name: len(parity_groups.get(group_name, []))
            for group_name in parity_hashes
        }
        return parity_hashes, parity_counts, dimensions_match

    def reconcile(self, batch_id: UUID) -> ImportBatchSummary:
        operator_actor_id = self._get_operator_actor_id()

        with _connect(self.config, "omp_work_importer") as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL search_path = pg_catalog")
                    cur.execute(
                        "SELECT set_config('omp.actor_id', %s, true)",
                        (str(operator_actor_id),),
                    )
                    cur.execute(
                        "SELECT omp_integration.lookup_batch_workspace(%s)", (batch_id,)
                    )
                    ws_row = cur.fetchone()
                    if not ws_row or ws_row[0] is None:
                        raise ValueError("linear_import_missing")
                    workspace_id = ws_row[0]
                    cur.execute(
                        "SELECT set_config('omp.workspace_id', %s, true)",
                        (str(workspace_id),),
                    )
                    cur.execute(
                        "SELECT workspace_id, export_id, state, mapping_file_sha256, artifact_root FROM omp_integration.import_batches WHERE batch_id = %s",
                        (batch_id,),
                    )
                    batch_row = cur.fetchone()
                    if not batch_row:
                        raise ValueError("linear_import_missing")
                    _, export_id, batch_state, map_hash, artifact_root = batch_row
                    if batch_state == "blocked":
                        # A blocked batch (blocked source export or prior blocking reconcile)
                        # is terminal: report it for diagnosis, never reconcile or promote.
                        return self._build_summary(cur, workspace_id, batch_id)
                    manifest, _, _ = load_export(self.config, export_id)

                    self._materialize_and_validate_relations(
                        cur, workspace_id, batch_id, operator_actor_id
                    )

                    cur.execute(
                        "SELECT code, disposition FROM omp_integration.migration_anomalies WHERE batch_id = %s",
                        (batch_id,),
                    )
                    all_anomalies = [
                        Anomaly(code=r[0], disposition=r[1]) for r in cur.fetchall()
                    ]

                    cur.execute(
                        "SELECT base_batch_id FROM omp_integration.import_batches WHERE batch_id = %s",
                        (batch_id,),
                    )
                    base_batch_id = cur.fetchone()[0]

                    import_recs = self._merged_import_records(
                        cur, batch_id, base_batch_id
                    )
                    dispositions, disp_counts = self._compute_dispositions(
                        cur, workspace_id, batch_id, import_recs
                    )
                    parity_hashes, parity_counts, dimensions_match = (
                        self._evaluate_parity(
                            cur,
                            batch_id,
                            base_batch_id,
                            manifest.dimension_counts,
                            manifest.dimension_hashes,
                            import_recs,
                            dispositions,
                        )
                    )
                    if not dimensions_match:
                        cur.execute("SELECT uuidv7()")
                        anomaly_id = cur.fetchone()[0]
                        cur.execute(
                            """
                            INSERT INTO omp_integration.migration_anomalies (
                                anomaly_id, batch_id, workspace_id, origin, code, disposition, message, details
                            ) VALUES (%s, %s, %s, 'importer', 'pagination_count_hash_gap', 'blocking', 'count/hash mismatch', '{}'::jsonb)
                            ON CONFLICT DO NOTHING
                            """,
                            (anomaly_id, batch_id, workspace_id),
                        )
                        all_anomalies.append(
                            Anomaly(
                                code="pagination_count_hash_gap", disposition="blocking"
                            )
                        )
                    has_blocking = any(
                        a.disposition == "blocking" for a in all_anomalies
                    )

                    staging_dir = (
                        self.config.state_dir / "staging" / f"import-{batch_id}"
                    )
                    shutil.rmtree(staging_dir, ignore_errors=True)
                    staging_dir.mkdir(parents=True, mode=0o700)
                    art_dir = self.config.data_dir / artifact_root
                    art_dir.mkdir(parents=True, mode=0o700, exist_ok=True)

                    try:
                        passphrase_file = self.config.secret_path("gpg-passphrase")
                        reports: dict[str, object] = {
                            "import-manifest": {
                                "batch_id": str(batch_id),
                                "workspace_id": str(workspace_id),
                                "export_id": str(export_id),
                                "transformation_version": TRANSFORMATION_VERSION,
                                "mapping_file_sha256": map_hash,
                            },
                            "mapping-records": [
                                {
                                    "entity_type": r[0],
                                    "source_id": r[1],
                                    "local_id": str(r[5]),
                                    "disposition": dispositions.get((r[0], r[1])),
                                }
                                for r in import_recs
                            ],
                            "anomaly-manifest": [
                                a.model_dump(mode="json") for a in all_anomalies
                            ],
                            "quarantine-manifest": [
                                {
                                    "entity_type": r[0],
                                    "source_id": r[1],
                                    "local_id": str(r[5]),
                                }
                                for r in import_recs
                                if dispositions.get((r[0], r[1])) == "quarantined"
                            ],
                            "parity-report": {
                                "parity_hashes": parity_hashes,
                                "counts": parity_counts,
                            },
                            "legacy-artifact-classification": [
                                {
                                    "entity_type": r[0],
                                    "source_id": r[1],
                                    "disposition": dispositions.get((r[0], r[1])),
                                }
                                for r in import_recs
                                if dispositions.get((r[0], r[1])) == "legacy_untrusted"
                            ],
                        }

                        artifacts_summary: dict[str, str] = {}
                        for name, payload in reports.items():
                            cur.execute(
                                "SELECT artifact_path, plaintext_sha256, ciphertext_sha256 FROM omp_integration.import_artifacts WHERE batch_id = %s AND name = %s",
                                (batch_id, name),
                            )
                            existing_art = cur.fetchone()
                            rel_path, p_hash, c_hash = write_json_artifact(
                                art_dir,
                                staging_dir,
                                name,
                                payload,
                                passphrase_file,
                                self.config.data_dir,
                            )
                            if existing_art:
                                if (
                                    existing_art[0] != rel_path
                                    or existing_art[1] != p_hash
                                    or existing_art[2] != c_hash
                                ):
                                    raise RuntimeError("pagination_count_hash_gap")
                            else:
                                cur.execute("SELECT uuidv7()")
                                art_id = cur.fetchone()[0]
                                cur.execute(
                                    """
                                    INSERT INTO omp_integration.import_artifacts (
                                        artifact_id, batch_id, workspace_id, name, artifact_path, plaintext_sha256, ciphertext_sha256
                                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                                    """,
                                    (
                                        art_id,
                                        batch_id,
                                        workspace_id,
                                        name,
                                        rel_path,
                                        p_hash,
                                        c_hash,
                                    ),
                                )
                            artifacts_summary[name] = rel_path
                    finally:
                        shutil.rmtree(staging_dir, ignore_errors=True)

                    stored_parity_bundle = {
                        "dimension_counts": manifest.dimension_counts.model_dump(),
                        "dimension_hashes": manifest.dimension_hashes.model_dump(),
                        "parity_groups": parity_hashes,
                    }
                    reconciliation_payload = {
                        "batch_id": str(batch_id),
                        "workspace_id": str(workspace_id),
                        "export_id": str(export_id),
                        "parity_hashes": parity_hashes,
                        "disposition_counts": disp_counts,
                        "anomaly_codes": sorted({a.code for a in all_anomalies}),
                    }
                    reconciliation_sha256 = sha256(reconciliation_payload)

                    if has_blocking:
                        cur.execute(
                            "UPDATE omp_integration.import_batches SET state = 'blocked', reconciliation_sha256 = %s, parity_hashes = %s WHERE batch_id = %s",
                            (
                                reconciliation_sha256,
                                json.dumps(stored_parity_bundle),
                                batch_id,
                            ),
                        )
                    else:
                        cur.execute(
                            "UPDATE omp_integration.import_batches SET state = 'reconciled', reconciled_at = clock_timestamp(), reconciliation_sha256 = %s, parity_hashes = %s WHERE batch_id = %s AND state = 'staged'",
                            (
                                reconciliation_sha256,
                                json.dumps(stored_parity_bundle),
                                batch_id,
                            ),
                        )

                    return self._build_summary(cur, workspace_id, batch_id)

    def _revalidate_staged_edges(
        self, cur: Any, workspace_id: UUID, batch_id: UUID
    ) -> None:
        """Read-only replay of graph validation for staged edges already marked validated:
        canonical mutations between reconcile and promote must still block promotion."""
        cur.execute(
            "SELECT relation_kind, local_source_id, local_target_id FROM omp_integration.import_relations WHERE batch_id = %s AND state = 'validated'",
            (batch_id,),
        )
        staged_edges = cur.fetchall()
        cur.execute(
            "SELECT source_work_id, target_work_id, kind FROM omp_work.work_relations WHERE workspace_id = %s AND active = true",
            (workspace_id,),
        )
        raw_canonical = cur.fetchall()
        edge_list = [
            RelationEdge(
                workspace_id=workspace_id,
                source_work_id=r[0],
                target_work_id=r[1],
                kind=RelationKind(r[2]),
                active=True,
            )
            for r in raw_canonical
        ]
        canonical_parent_map: dict[UUID, UUID] = {
            r[0]: r[1] for r in raw_canonical if r[2] == "parent"
        }
        for rel_kind, local_s, local_t in staged_edges:
            if (
                rel_kind not in ("parent", "blocks", "duplicate_of", "related")
                or not local_s
                or not local_t
            ):
                continue
            if local_s == local_t:
                raise ValueError("linear_import_blocked")
            if rel_kind == "parent":
                existing_parent = canonical_parent_map.get(local_s)
                if existing_parent is not None and existing_parent != local_t:
                    raise ValueError("linear_import_blocked")
                canonical_parent_map[local_s] = local_t
            candidate = RelationEdge(
                workspace_id=workspace_id,
                source_work_id=local_s,
                target_work_id=local_t,
                kind=RelationKind(rel_kind),
                active=True,
            )
            if candidate not in edge_list:
                if would_create_cycle(tuple(edge_list), candidate):
                    raise ValueError("linear_import_blocked")
                edge_list.append(candidate)

    def promote(self, batch_id: UUID) -> ImportBatchSummary:
        operator_actor_id = self._get_operator_actor_id()

        with _connect(self.config, "omp_work_importer") as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                    cur.execute("SET LOCAL search_path = pg_catalog")
                    cur.execute(
                        "SELECT set_config('omp.actor_id', %s, true)",
                        (str(operator_actor_id),),
                    )
                    cur.execute(
                        "SELECT omp_integration.lookup_batch_workspace(%s)", (batch_id,)
                    )
                    ws_row = cur.fetchone()
                    if not ws_row or ws_row[0] is None:
                        raise ValueError("linear_import_missing")
                    workspace_id = ws_row[0]
                    cur.execute(
                        "SELECT set_config('omp.workspace_id', %s, true)",
                        (str(workspace_id),),
                    )
                    cur.execute(
                        "SELECT workspace_id, export_id, state, reconciliation_sha256, parity_hashes FROM omp_integration.import_batches WHERE batch_id = %s",
                        (batch_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise ValueError("linear_import_missing")
                    _, export_id, state, reconciliation_sha256, stored_parity = row
                    if state != "reconciled" or not reconciliation_sha256:
                        if state == "promoted":
                            return self._build_summary(cur, workspace_id, batch_id)
                        if state == "blocked":
                            raise ValueError("linear_import_blocked")
                        raise ValueError("linear_import_not_reconciled")
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"workspace:{workspace_id}",),
                    )

                    new_anomalies = self._materialize_and_validate_relations(
                        cur, workspace_id, batch_id, operator_actor_id
                    )
                    if any(a.disposition == "blocking" for a in new_anomalies):
                        raise ValueError("linear_import_blocked")
                    self._revalidate_staged_edges(cur, workspace_id, batch_id)

                    # Promote-time parity recheck: recompute the staged-view parity groups
                    # and source dimension counts/hashes against the bundle reconciliation
                    # stored. No page decryption here — the workspace advisory lock stays
                    # short; the manifest was already verified at stage/reconcile time.
                    cur.execute(
                        "SELECT base_batch_id FROM omp_integration.import_batches WHERE batch_id = %s",
                        (batch_id,),
                    )
                    promote_base_batch_id = cur.fetchone()[0]
                    promote_recs = self._merged_import_records(
                        cur, batch_id, promote_base_batch_id
                    )
                    promote_dispositions, _ = self._compute_dispositions(
                        cur, workspace_id, batch_id, promote_recs
                    )
                    stored_bundle = (
                        stored_parity
                        if isinstance(stored_parity, dict)
                        else json.loads(stored_parity or "{}")
                    )
                    try:
                        stored_counts = ReconciliationCounts(
                            **stored_bundle["dimension_counts"]
                        )
                        stored_hashes = ReconciliationHashes(
                            **stored_bundle["dimension_hashes"]
                        )
                        stored_groups = stored_bundle["parity_groups"]
                    except (KeyError, TypeError, ValueError):
                        raise ValueError("linear_import_drift") from None
                    promote_parity, _, promote_dims_match = self._evaluate_parity(
                        cur,
                        batch_id,
                        promote_base_batch_id,
                        stored_counts,
                        stored_hashes,
                        promote_recs,
                        promote_dispositions,
                    )
                    if not promote_dims_match or promote_parity != stored_groups:
                        raise ValueError("linear_import_drift")

                    cur.execute(
                        "SELECT entity_type, source_id, local_id, local_type, transformed_json FROM omp_integration.import_records WHERE batch_id = %s",
                        (batch_id,),
                    )
                    records = cur.fetchall()

                    for row in records:
                        e_type, s_id, l_id, l_type, t_json = row
                        data = (
                            t_json if isinstance(t_json, dict) else json.loads(t_json)
                        )
                        cur.execute(
                            """
                            INSERT INTO omp_integration.external_refs (
                                workspace_id, system, external_id, local_id, local_type, source_identifier, source_url
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (workspace_id, system, external_id) DO NOTHING
                            """,
                            (
                                workspace_id,
                                "linear_repo" if l_type == "repository" else "linear",
                                s_id,
                                l_id,
                                l_type,
                                data.get("identifier") or data.get("key"),
                                data.get("url"),
                            ),
                        )

                    projection_decisions: dict[tuple[str, str], str] = {}
                    projection_hashes: dict[tuple[str, str], str] = {}
                    for row in records:
                        e_type, s_id, l_id, l_type, t_json = row
                        if e_type not in _PROJECTION_FIELDS:
                            continue
                        data = (
                            t_json if isinstance(t_json, dict) else json.loads(t_json)
                        )
                        staged_payload = {
                            field: data.get(field)
                            for field in _PROJECTION_FIELDS[e_type]
                        }
                        decision, incoming = self._projection_decision(
                            cur, workspace_id, e_type, s_id, l_id, staged_payload
                        )
                        if decision == "source_local_conflict":
                            raise ValueError("linear_import_blocked")
                        projection_decisions[(e_type, s_id)] = decision
                        projection_hashes[(e_type, s_id)] = incoming

                    written_projections: list[tuple[str, UUID, str]] = []
                    written_items: list[tuple[UUID, dict[str, Any], UUID, int]] = []
                    written_work_relations: list[tuple[UUID, UUID, str]] = []
                    written_project_relations: list[tuple[UUID, UUID, str]] = []
                    written_health: list[tuple[UUID, str, str]] = []
                    focus_written: UUID | None = None

                    for row in records:
                        e_type, s_id, l_id, l_type, t_json = row
                        data = (
                            t_json if isinstance(t_json, dict) else json.loads(t_json)
                        )
                        if projection_decisions.get((e_type, s_id)) == "unchanged":
                            continue
                        if (e_type, s_id) in projection_decisions:
                            written_projections.append(
                                (e_type, l_id, projection_hashes[(e_type, s_id)])
                            )

                        if l_type == "repository":
                            cur.execute(
                                """
                                INSERT INTO omp_work.repositories (
                                    repository_id, workspace_id, key, name, url, archived, provenance
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (workspace_id, repository_id) DO UPDATE SET
                                    name = EXCLUDED.name,
                                    url = EXCLUDED.url,
                                    archived = EXCLUDED.archived
                                """,
                                (
                                    l_id,
                                    workspace_id,
                                    data["key"],
                                    data["name"],
                                    data["url"],
                                    data["archived"],
                                    json.dumps(data["provenance"]),
                                ),
                            )
                        elif l_type == "principal":
                            cur.execute(
                                """
                                INSERT INTO omp_work.principals (
                                    principal_id, workspace_id, name, display_name, active, provenance
                                ) VALUES (%s, %s, %s, %s, %s, %s)
                                ON CONFLICT (workspace_id, principal_id) DO UPDATE SET
                                    name = EXCLUDED.name,
                                    display_name = EXCLUDED.display_name,
                                    active = EXCLUDED.active
                                """,
                                (
                                    l_id,
                                    workspace_id,
                                    data["name"],
                                    data["display_name"],
                                    data["active"],
                                    json.dumps(data["provenance"]),
                                ),
                            )
                        elif l_type == "workflow_state":
                            cur.execute(
                                """
                                INSERT INTO omp_work.workflow_states (
                                    workflow_state_id, workspace_id, name, state_type, position, archived, provenance
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (workspace_id, workflow_state_id) DO UPDATE SET
                                    name = EXCLUDED.name,
                                    state_type = EXCLUDED.state_type,
                                    position = EXCLUDED.position,
                                    archived = EXCLUDED.archived
                                """,
                                (
                                    l_id,
                                    workspace_id,
                                    data["name"],
                                    data["state_type"],
                                    data["position"],
                                    data["archived"],
                                    json.dumps(data["provenance"]),
                                ),
                            )
                        elif l_type == "label":
                            cur.execute(
                                """
                                INSERT INTO omp_work.labels (
                                    label_id, workspace_id, name, color, archived, provenance
                                ) VALUES (%s, %s, %s, %s, %s, %s)
                                ON CONFLICT (workspace_id, label_id) DO UPDATE SET
                                    color = EXCLUDED.color,
                                    archived = EXCLUDED.archived
                                """,
                                (
                                    l_id,
                                    workspace_id,
                                    data["name"],
                                    data.get("color"),
                                    data["archived"],
                                    json.dumps(data["provenance"]),
                                ),
                            )
                        elif l_type == "project":
                            cur.execute(
                                """
                                INSERT INTO omp_work.projects (
                                    project_id, workspace_id, key, name, kind, target_date, archived, provenance
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (workspace_id, project_id) DO UPDATE SET
                                    key = EXCLUDED.key,
                                    name = EXCLUDED.name,
                                    target_date = EXCLUDED.target_date,
                                    archived = EXCLUDED.archived
                                """,
                                (
                                    l_id,
                                    workspace_id,
                                    data.get("key"),
                                    data["name"],
                                    data["kind"],
                                    data.get("target_date"),
                                    data["archived"],
                                    json.dumps(data["provenance"]),
                                ),
                            )

                    for row in records:
                        e_type, s_id, l_id, l_type, t_json = row
                        if l_type != "work_item":
                            continue
                        data = (
                            t_json if isinstance(t_json, dict) else json.loads(t_json)
                        )

                        cur.execute(
                            "SELECT current_revision_id, row_version, state FROM omp_work.work_items WHERE workspace_id = %s AND work_id = %s",
                            (workspace_id, l_id),
                        )
                        existing_item = cur.fetchone()

                        revision_id: UUID
                        rev_number = 1
                        resulting_row_version = 1
                        disposition = "created"

                        if not existing_item:
                            cur.execute("SELECT uuidv7()")
                            revision_id = cur.fetchone()[0]
                            cur.execute(
                                """
                                INSERT INTO omp_work.work_items (
                                    work_id, workspace_id, state, current_revision_id, archived,
                                    repository_id, project_id, workflow_state_id, assignee_id, priority,
                                    source_updated_at, row_version, provenance
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s)
                                """,
                                (
                                    l_id,
                                    workspace_id,
                                    data["state"],
                                    revision_id,
                                    data["archived"],
                                    data.get("repository_id"),
                                    data.get("project_id"),
                                    data.get("workflow_state_id"),
                                    data.get("assignee_id"),
                                    data.get("priority"),
                                    data.get("source_updated_at"),
                                    json.dumps(data["provenance"]),
                                ),
                            )
                            cur.execute(
                                """
                                INSERT INTO omp_work.work_revisions (
                                    revision_id, work_id, workspace_id, revision_number,
                                    title, description, scope, content_sha256, created_by, supplied_at
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                                (
                                    revision_id,
                                    l_id,
                                    workspace_id,
                                    rev_number,
                                    data["title"],
                                    data["description"],
                                    data["scope"],
                                    data["content_sha256"],
                                    "importer",
                                    data.get("source_updated_at")
                                    or datetime.now(UTC).isoformat(),
                                ),
                            )
                            for pos, crit in enumerate(
                                data.get("acceptance_criteria", [])
                            ):
                                cur.execute(
                                    "INSERT INTO omp_work.acceptance_criteria (revision_id, workspace_id, position, criterion) VALUES (%s, %s, %s, %s)",
                                    (revision_id, workspace_id, pos, crit),
                                )
                        else:
                            current_rev_id, current_row_version, _ = existing_item
                            cur.execute(
                                """
                                SELECT canonical_sha256, revision_id, resulting_row_version
                                FROM omp_integration.import_record_results
                                WHERE workspace_id = %s AND entity_type = 'work_items' AND source_id = %s
                                AND disposition IN ('created', 'revised', 'projection_updated')
                                ORDER BY promoted_at DESC LIMIT 1
                                """,
                                (workspace_id, s_id),
                            )
                            last_res = cur.fetchone()
                            apply_update = False
                            if last_res and last_res[0] == data["content_sha256"]:
                                # Result row records the actual current canonical revision/version;
                                # conflict baselines skip non-writing 'unchanged' results instead.
                                revision_id = current_rev_id
                                resulting_row_version = current_row_version
                                if (
                                    current_rev_id != last_res[1]
                                    or current_row_version != last_res[2]
                                ):
                                    # Source unchanged but local canonical state moved since the
                                    # last promoted import: preserve local work, write nothing.
                                    disposition = "unchanged"
                                elif self._projection_matches(
                                    cur, workspace_id, l_id, data
                                ):
                                    disposition = "unchanged"
                                else:
                                    disposition = "projection_updated"
                                    resulting_row_version = current_row_version + 1
                                    apply_update = True
                            else:
                                if last_res and (
                                    current_rev_id != last_res[1]
                                    or current_row_version != last_res[2]
                                ):
                                    raise ValueError("linear_import_blocked")
                                disposition = "revised"
                                cur.execute(
                                    "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM omp_work.work_revisions WHERE work_id = %s",
                                    (l_id,),
                                )
                                rev_number = cur.fetchone()[0]
                                cur.execute("SELECT uuidv7()")
                                revision_id = cur.fetchone()[0]
                                cur.execute(
                                    """
                                    INSERT INTO omp_work.work_revisions (
                                        revision_id, work_id, workspace_id, revision_number,
                                        title, description, scope, content_sha256, created_by, supplied_at
                                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                    """,
                                    (
                                        revision_id,
                                        l_id,
                                        workspace_id,
                                        rev_number,
                                        data["title"],
                                        data["description"],
                                        data["scope"],
                                        data["content_sha256"],
                                        "importer",
                                        data.get("source_updated_at")
                                        or datetime.now(UTC).isoformat(),
                                    ),
                                )
                                for pos, crit in enumerate(
                                    data.get("acceptance_criteria", [])
                                ):
                                    cur.execute(
                                        "INSERT INTO omp_work.acceptance_criteria (revision_id, workspace_id, position, criterion) VALUES (%s, %s, %s, %s)",
                                        (revision_id, workspace_id, pos, crit),
                                    )
                                resulting_row_version = current_row_version + 1
                                apply_update = True

                            if apply_update:
                                cur.execute(
                                    """
                                    UPDATE omp_work.work_items SET
                                        state = %s,
                                        current_revision_id = %s,
                                        repository_id = %s,
                                        project_id = %s,
                                        workflow_state_id = %s,
                                        assignee_id = %s,
                                        priority = %s,
                                        source_updated_at = %s,
                                        archived = %s,
                                        row_version = %s
                                    WHERE workspace_id = %s AND work_id = %s
                                    """,
                                    (
                                        data["state"],
                                        revision_id,
                                        data.get("repository_id"),
                                        data.get("project_id"),
                                        data.get("workflow_state_id"),
                                        data.get("assignee_id"),
                                        data.get("priority"),
                                        data.get("source_updated_at"),
                                        data["archived"],
                                        resulting_row_version,
                                        workspace_id,
                                        l_id,
                                    ),
                                )

                        if disposition != "unchanged":
                            if data.get("alias_identifier"):
                                cur.execute(
                                    """
                                    INSERT INTO omp_work.work_aliases (
                                        work_id, workspace_id, key, primary_alias, origin
                                    ) VALUES (%s, %s, %s, true, 'imported')
                                    ON CONFLICT (workspace_id, key) DO NOTHING
                                    """,
                                    (l_id, workspace_id, data["alias_identifier"]),
                                )
                            for previous_key in data.get(
                                "alias_previous_identifiers", []
                            ):
                                if previous_key and previous_key != data.get(
                                    "alias_identifier"
                                ):
                                    cur.execute(
                                        """
                                        INSERT INTO omp_work.work_aliases (
                                            work_id, workspace_id, key, primary_alias, origin
                                        ) VALUES (%s, %s, %s, false, 'imported')
                                        ON CONFLICT (workspace_id, key) DO NOTHING
                                        """,
                                        (l_id, workspace_id, previous_key),
                                    )
                            staged_labels = [
                                str(label_id) for label_id in data.get("label_ids", [])
                            ]
                            for lbl_id in staged_labels:
                                cur.execute(
                                    """
                                    INSERT INTO omp_work.work_item_labels (workspace_id, work_id, label_id, active, origin)
                                    VALUES (%s, %s, %s, true, 'imported')
                                    ON CONFLICT (workspace_id, work_id, label_id) DO UPDATE SET active = true
                                    WHERE omp_work.work_item_labels.origin = 'imported'
                                    """,
                                    (workspace_id, l_id, lbl_id),
                                )
                            cur.execute(
                                """
                                UPDATE omp_work.work_item_labels SET active = false
                                WHERE workspace_id = %s AND work_id = %s AND origin = 'imported' AND active = true
                                AND label_id <> ALL (%s::uuid[])
                                """,
                                (workspace_id, l_id, staged_labels),
                            )

                        cur.execute(
                            """
                            INSERT INTO omp_integration.import_record_results (
                                batch_id, workspace_id, entity_type, source_id, local_id, local_type,
                                disposition, canonical_sha256, revision_id, resulting_row_version
                            ) VALUES (%s, %s, 'work_items', %s, %s, 'work_item', %s, %s, %s, %s)
                            ON CONFLICT (batch_id, entity_type, source_id) DO NOTHING
                            """,
                            (
                                batch_id,
                                workspace_id,
                                s_id,
                                l_id,
                                disposition,
                                data["content_sha256"],
                                revision_id,
                                resulting_row_version,
                            ),
                        )
                        if disposition != "unchanged":
                            written_items.append(
                                (l_id, data, revision_id, resulting_row_version)
                            )

                    cur.execute(
                        "SELECT relation_id, relation_kind, local_source_id, local_target_id, canonical_id FROM omp_integration.import_relations WHERE batch_id = %s AND state = 'validated'",
                        (batch_id,),
                    )
                    for (
                        rel_id,
                        rel_kind,
                        l_src,
                        l_tgt,
                        rel_canonical_id,
                    ) in cur.fetchall():
                        # Source-backed relations reuse the stable mapped local ID; source-less
                        # relations (parent, project_milestone) derive one deterministically so
                        # repeated imports converge on the same canonical row.
                        canonical_rel_id = rel_canonical_id or uuid5(
                            NAMESPACE_URL,
                            f"omp-work/relation/{workspace_id}/{rel_kind}/{l_src}/{l_tgt}",
                        )
                        if (
                            rel_kind in ("parent", "blocks", "duplicate_of", "related")
                            and l_src
                            and l_tgt
                        ):
                            src_id = l_src
                            tgt_id = l_tgt
                            if rel_kind == "related" and str(src_id) > str(tgt_id):
                                src_id, tgt_id = tgt_id, src_id
                            cur.execute(
                                """
                                INSERT INTO omp_work.work_relations (
                                    relation_id, workspace_id, source_work_id, target_work_id, kind, active
                                ) VALUES (%s, %s, %s, %s, %s, true)
                                ON CONFLICT DO NOTHING
                                """,
                                (
                                    canonical_rel_id,
                                    workspace_id,
                                    src_id,
                                    tgt_id,
                                    rel_kind,
                                ),
                            )
                            written_work_relations.append((src_id, tgt_id, rel_kind))
                        elif (
                            rel_kind in ("initiative_project", "project_milestone")
                            and l_src
                            and l_tgt
                        ):
                            cur.execute(
                                """
                                INSERT INTO omp_work.project_relations (
                                    relation_id, workspace_id, source_project_id, target_project_id, kind, active
                                ) VALUES (%s, %s, %s, %s, %s, true)
                                ON CONFLICT DO NOTHING
                                """,
                                (
                                    canonical_rel_id,
                                    workspace_id,
                                    l_src,
                                    l_tgt,
                                    rel_kind,
                                ),
                            )
                            written_project_relations.append((l_src, l_tgt, rel_kind))

                    health_candidates: dict[str, dict[str, Any]] = {}
                    for row in records:
                        e_type, s_id, l_id, l_type, t_json = row
                        data = (
                            t_json if isinstance(t_json, dict) else json.loads(t_json)
                        )
                        if (
                            e_type == "project_updates"
                            and not data.get("archived")
                            and data.get("project_id")
                            and data.get("health")
                        ):
                            key = str(data["project_id"])
                            candidate = {
                                "health": data["health"],
                                "updated_at": _norm_ts(data.get("updated_at")),
                                "project_source_id": data.get("source_project_id"),
                                "update_source_id": s_id,
                            }
                            existing_candidate = health_candidates.get(key)
                            if existing_candidate is None or (
                                candidate["updated_at"] or "",
                                s_id,
                            ) > (
                                existing_candidate["updated_at"] or "",
                                existing_candidate["update_source_id"],
                            ):
                                health_candidates[key] = candidate
                    for row in records:
                        e_type, s_id, l_id, l_type, t_json = row
                        if e_type != "surfaces":
                            continue
                        data = (
                            t_json if isinstance(t_json, dict) else json.loads(t_json)
                        )
                        if str(l_id) not in health_candidates and data.get("health"):
                            health_candidates[str(l_id)] = {
                                "health": data["health"],
                                "updated_at": _norm_ts(data.get("source_updated_at")),
                                "project_source_id": s_id,
                                "update_source_id": "",
                            }

                    for key, candidate in health_candidates.items():
                        staged_payload = {
                            "health": candidate["health"],
                            "updated_at": candidate["updated_at"],
                        }
                        decision, incoming = self._projection_decision(
                            cur,
                            workspace_id,
                            "project_health",
                            candidate["project_source_id"],
                            UUID(key),
                            staged_payload,
                        )
                        if decision == "source_local_conflict":
                            raise ValueError("linear_import_blocked")
                        if decision != "unchanged":
                            written_ts = (
                                candidate["updated_at"] or datetime.now(UTC).isoformat()
                            )
                            cur.execute(
                                """
                                INSERT INTO omp_work.project_health (workspace_id, project_id, health, updated_at)
                                VALUES (%s, %s, %s, %s)
                                ON CONFLICT (workspace_id, project_id) DO UPDATE SET
                                    health = EXCLUDED.health,
                                    updated_at = EXCLUDED.updated_at
                                """,
                                (
                                    workspace_id,
                                    UUID(key),
                                    candidate["health"],
                                    written_ts,
                                ),
                            )
                            written_health.append(
                                (UUID(key), candidate["health"], written_ts)
                            )
                        cur.execute(
                            """
                            INSERT INTO omp_integration.import_record_results (
                                batch_id, workspace_id, entity_type, source_id, local_id, local_type, disposition, canonical_sha256
                            ) VALUES (%s, %s, 'project_health', %s, %s, 'project_health', %s, %s)
                            ON CONFLICT (batch_id, entity_type, source_id) DO NOTHING
                            """,
                            (
                                batch_id,
                                workspace_id,
                                candidate["project_source_id"],
                                UUID(key),
                                decision,
                                incoming,
                            ),
                        )

                    cur.execute(
                        """
                        SELECT local_id FROM omp_integration.import_records
                        WHERE batch_id = %s AND entity_type = 'labels' AND transformed_json->>'name' ILIKE 'now'
                        """,
                        (batch_id,),
                    )
                    now_lbl = cur.fetchone()
                    if now_lbl:
                        cur.execute(
                            """
                            SELECT ir.local_id FROM omp_integration.import_records ir
                            WHERE ir.batch_id = %s AND ir.entity_type = 'work_items'
                            AND ir.transformed_json->>'state' NOT IN ('DONE', 'CANCELED')
                            AND (ir.transformed_json->>'archived')::boolean = false
                            AND ir.transformed_json->'label_ids' ? %s
                            """,
                            (batch_id, str(now_lbl[0])),
                        )
                        focused_items = cur.fetchall()
                        if len(focused_items) == 1:
                            target_work_id = focused_items[0][0]
                            cur.execute(
                                """
                                INSERT INTO omp_work.focus_slots (workspace_id, owner_id, work_id, version)
                                VALUES (%s, %s, %s, 1)
                                ON CONFLICT (workspace_id, owner_id) DO UPDATE SET
                                    work_id = EXCLUDED.work_id,
                                    version = omp_work.focus_slots.version + 1
                                """,
                                (workspace_id, operator_actor_id, target_work_id),
                            )
                            focus_written = target_work_id

                    for row in records:
                        e_type, s_id, l_id, l_type, t_json = row
                        if l_type == "work_item":
                            continue
                        data = (
                            t_json if isinstance(t_json, dict) else json.loads(t_json)
                        )
                        disposition = projection_decisions.get((e_type, s_id))
                        if disposition is None:
                            disposition = (
                                "metadata_only"
                                if e_type == "attachments" and data.get("usable")
                                else "quarantined"
                                if e_type == "attachments"
                                or (
                                    e_type == "comments" and not data.get("prefix_kind")
                                )
                                else "legacy_untrusted"
                                if e_type in ("comments", "project_updates")
                                else "created"
                            )
                        cur.execute(
                            """
                            INSERT INTO omp_integration.import_record_results (
                                batch_id, workspace_id, entity_type, source_id, local_id, local_type, disposition, canonical_sha256
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (batch_id, entity_type, source_id) DO NOTHING
                            """,
                            (
                                batch_id,
                                workspace_id,
                                e_type,
                                s_id,
                                l_id,
                                l_type,
                                disposition,
                                projection_hashes.get((e_type, s_id)),
                            ),
                        )

                    # Canonical read-back: every row this batch committed must equal the
                    # candidate projection. Any divergence means drift slipped past the
                    # conflict rules; raising rolls back all canonical and promotion writes.
                    for e_type, l_id, incoming in written_projections:
                        if (
                            self._canonical_projection_hash(
                                cur, workspace_id, e_type, l_id
                            )
                            != incoming
                        ):
                            raise ValueError("linear_import_drift")
                    for (
                        l_id,
                        item_data,
                        revision_id,
                        resulting_row_version,
                    ) in written_items:
                        cur.execute(
                            "SELECT current_revision_id, row_version FROM omp_work.work_items WHERE workspace_id = %s AND work_id = %s",
                            (workspace_id, l_id),
                        )
                        written_row = cur.fetchone()
                        if (
                            written_row is None
                            or written_row[0] != revision_id
                            or written_row[1] != resulting_row_version
                        ):
                            raise ValueError("linear_import_drift")
                        if not self._projection_matches(
                            cur, workspace_id, l_id, item_data
                        ):
                            raise ValueError("linear_import_drift")
                    for src_id, tgt_id, rel_kind in written_work_relations:
                        cur.execute(
                            "SELECT 1 FROM omp_work.work_relations WHERE workspace_id = %s AND source_work_id = %s AND target_work_id = %s AND kind = %s AND active",
                            (workspace_id, src_id, tgt_id, rel_kind),
                        )
                        if not cur.fetchone():
                            raise ValueError("linear_import_drift")
                    for l_src, l_tgt, rel_kind in written_project_relations:
                        cur.execute(
                            "SELECT 1 FROM omp_work.project_relations WHERE workspace_id = %s AND source_project_id = %s AND target_project_id = %s AND kind = %s AND active",
                            (workspace_id, l_src, l_tgt, rel_kind),
                        )
                        if not cur.fetchone():
                            raise ValueError("linear_import_drift")
                    for p_id, health, written_ts in written_health:
                        cur.execute(
                            "SELECT health, updated_at FROM omp_work.project_health WHERE workspace_id = %s AND project_id = %s",
                            (workspace_id, p_id),
                        )
                        health_row = cur.fetchone()
                        if (
                            health_row is None
                            or health_row[0] != health
                            or _norm_ts(health_row[1]) != written_ts
                        ):
                            raise ValueError("linear_import_drift")
                    if focus_written is not None:
                        cur.execute(
                            "SELECT work_id FROM omp_work.focus_slots WHERE workspace_id = %s AND owner_id = %s",
                            (workspace_id, operator_actor_id),
                        )
                        focus_row = cur.fetchone()
                        if not focus_row or focus_row[0] != focus_written:
                            raise ValueError("linear_import_drift")

                    cur.execute(
                        "UPDATE omp_integration.import_batches SET state = 'promoted', promoted_at = clock_timestamp() WHERE batch_id = %s",
                        (batch_id,),
                    )

                    return self._build_summary(cur, workspace_id, batch_id)

    def _build_summary(
        self, cur: Any, workspace_id: UUID, batch_id: UUID
    ) -> ImportBatchSummary:
        cur.execute(
            """
            SELECT batch_id, workspace_id, export_id, state, transformation_version,
                   base_batch_id, mapping_file_sha256, reconciliation_sha256, parity_hashes
            FROM omp_integration.import_batches WHERE batch_id = %s
            """,
            (batch_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("linear_import_missing")

        cur.execute(
            "SELECT name, artifact_path FROM omp_integration.import_artifacts WHERE batch_id = %s",
            (batch_id,),
        )
        artifacts = {r[0]: r[1] for r in cur.fetchall()}

        cur.execute(
            "SELECT disposition, COUNT(*) FROM omp_integration.import_record_results WHERE batch_id = %s GROUP BY disposition",
            (batch_id,),
        )
        disp_counts = {r[0]: int(r[1]) for r in cur.fetchall()}

        cur.execute(
            "SELECT DISTINCT code FROM omp_integration.migration_anomalies WHERE batch_id = %s ORDER BY code",
            (batch_id,),
        )
        anomaly_codes = [r[0] for r in cur.fetchall()]
        raw_parity = (
            row[8] if isinstance(row[8], dict) else json.loads(row[8]) if row[8] else {}
        )
        parity_hashes = raw_parity.get("parity_groups", raw_parity)
        return ImportBatchSummary(
            batch_id=row[0],
            workspace_id=row[1],
            export_id=row[2],
            state=row[3],
            transformation_version=row[4],
            base_batch_id=row[5],
            mapping_file_sha256=row[6],
            reconciliation_sha256=row[7],
            parity_hashes=parity_hashes,
            artifacts=artifacts,
            disposition_counts=disp_counts,
            anomaly_codes=anomaly_codes,
        )
