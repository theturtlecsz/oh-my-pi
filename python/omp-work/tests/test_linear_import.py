from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import socket
from typing import Any
from uuid import UUID, uuid4

import pytest

from collections import defaultdict
from hashlib import sha256 as bytes_sha256
import shutil

import omp_work.integration.importer as importer_module
from omp_work.integration.legacy_artifacts import (
    DIMENSIONS,
    Anomaly,
    ArtifactRecord,
    AttachmentDisposition,
    ExportManifest,
    LinearStream,
    PrivacyReport,
    ReconciliationCounts,
    ReconciliationHashes,
    ScopeReport,
    SourceHashEntry,
    SourceHashIndex,
    SourcePage,
    StreamSummary,
    load_export,
)
from omp_work.integration.importer import (
    ImportBatchSummary,
    LinearImporter,
    parse_acceptance_criteria,
)
from omp_work.operations import cli as operations_cli
from omp_work.operations.artifacts import (
    decrypt_file,
    encrypt_file,
    read_json_artifact,
    resolve_artifact_path,
    write_json_artifact,
)
from omp_work.operations.config import OperationsConfig
from pg_native import native_postgres, seed_authority
from omp_work.operations.database import bootstrap, _connect
from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.models import RelationEdge, RelationKind
from omp_work.v1.semantics import would_create_cycle

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def config(tmp_path: Path) -> OperationsConfig:
    credentials = tmp_path / "config" / "credentials"
    credentials.mkdir(parents=True, mode=0o700)
    for role in (
        "postgres",
        "omp_work_migrator",
        "omp_work_app",
        "omp_work_importer",
        "omp_work_readonly",
        "omp_work_backup",
        "gpg-passphrase",
    ):
        path = credentials / role
        path.write_text(secrets.token_urlsafe(24))
        path.chmod(0o600)
    op_path = credentials / "operator-actor-id"
    op_path.write_text(str(uuid4()))
    op_path.chmod(0o600)
    linear = credentials / "linear-export.json"
    linear.write_text(
        json.dumps(
            {
                "kind": "oauth",
                "access_token": "read-only-token",
                "refresh_token": "refresh-token",
                "client_id": "test-client",
                "scopes": ["read"],
                "expires_at": "2099-01-01T00:00:00Z",
            }
        )
    )
    linear.chmod(0o600)
    return OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        port=_free_port(),
    )


@pytest.fixture
def postgres_service(config: OperationsConfig):
    with native_postgres(config.state_dir, config.port):
        bootstrap(config)
        yield config


def _make_mapping_file(tmp_path: Path, filename: str = "mapping.json") -> Path:
    mapping_data = {
        "schema_version": "linear-import-map/v1",
        "repositories": {
            "main": {
                "key": "main",
                "name": "Main Repo",
                "url": "https://github.com/example/main",
            },
            "side": {
                "key": "side",
                "name": "Side Repo",
                "url": "https://github.com/example/side",
            },
        },
        "project_repositories": {
            "project-home": "main",
        },
        "unprojected_repository": "side",
    }
    path = tmp_path / filename
    path.write_text(json.dumps(mapping_data), encoding="utf-8")
    path.chmod(0o600)
    return path


def _sample_nodes(
    operation: str, variables: dict[str, object], call_index: int = 0
) -> list[dict[str, object]]:
    del call_index
    filter_val = variables.get("filter")
    bounded = isinstance(filter_val, dict) and "updatedAt" in filter_val
    updated = (
        str(filter_val["updatedAt"]["gte"]) if bounded else "2026-08-01T00:00:00+00:00"
    )

    desc = """Summary of task.

# Acceptance criteria
- [ ] First criterion
- [ ] Second criterion with
  continuation line
  - nested item to ignore
1. Third numbered item
"""

    values: dict[str, list[dict[str, object]]] = {
        "teams": [
            {"id": "team-home", "key": "HOME", "name": "Home", "updatedAt": updated}
        ],
        "initiatives": [
            {
                "id": "initiative-home",
                "name": "The Initiative",
                "updatedAt": updated,
                "targetDate": "2026-12-31",
            }
        ],
        "projects": [
            {
                "id": "project-home",
                "name": "The Surface",
                "updatedAt": updated,
                "targetDate": "2026-10-31",
                "teams": {"nodes": [{"key": "HOME"}]},
                "lead": {
                    "id": "user-lead",
                    "name": "Lead User",
                    "displayName": "Lead",
                    "active": True,
                },
            }
        ],
        "projectUpdates": [
            {
                "id": "update-1",
                "body": "Update text",
                "health": "onTrack",
                "updatedAt": updated,
                "project": {"id": "project-home"},
                "user": {
                    "id": "user-lead",
                    "name": "Lead User",
                    "displayName": "Lead",
                    "active": True,
                },
            }
        ],
        "projectMilestones": [
            {
                "id": "milestone-1",
                "name": "The Promise",
                "targetDate": "2026-09-30",
                "updatedAt": updated,
                "project": {"id": "project-home"},
            }
        ],
        "issues": [
            {
                "id": "issue-1",
                "identifier": "HOME-146",
                "title": "Build the importer",
                "description": desc,
                "priority": 1,
                "updatedAt": updated,
                "team": {"key": "HOME"},
                "state": {
                    "id": "state-in-progress",
                    "type": "started",
                    "name": "In Progress",
                },
                "project": {"id": "project-home"},
                "assignee": {
                    "id": "user-lead",
                    "name": "Lead User",
                    "displayName": "Lead",
                    "active": True,
                },
                "labels": {
                    "nodes": [{"id": "label-now", "name": "now", "color": "#ff0000"}]
                },
                "parent": None,
                "previousIdentifiers": [],
                "url": "https://linear.app/issue/HOME-146",
            },
            {
                "id": "issue-2",
                "identifier": "HOME-145",
                "title": "Build the exporter",
                "description": "Export description",
                "priority": 2,
                "updatedAt": updated,
                "team": {"key": "HOME"},
                "state": {"id": "state-done", "type": "completed", "name": "Done"},
                "completedAt": updated,
                "project": {"id": "project-home"},
                "assignee": {
                    "id": "user-lead",
                    "name": "Lead User",
                    "displayName": "Lead",
                    "active": True,
                },
                "labels": {"nodes": []},
                "parent": {"id": "issue-1"},
                "previousIdentifiers": [],
                "url": "https://linear.app/issue/HOME-145",
            },
        ],
        "workflowStates": [
            {
                "id": "state-in-progress",
                "name": "In Progress",
                "type": "started",
                "position": 1,
                "updatedAt": updated,
                "team": {"key": "HOME"},
            },
            {
                "id": "state-done",
                "name": "Done",
                "type": "completed",
                "position": 2,
                "updatedAt": updated,
                "team": {"key": "HOME"},
            },
        ],
        "issueLabels": [
            {
                "id": "label-now",
                "name": "now",
                "color": "#ff0000",
                "updatedAt": updated,
                "team": {"key": "HOME"},
            },
        ],
        "initiativeToProjects": [
            {
                "id": "init-to-proj-1",
                "updatedAt": updated,
                "initiative": {"id": "initiative-home"},
                "project": {"id": "project-home"},
            },
        ],
        "issueRelations": [],
        "comments": [
            {
                "id": "comment-1",
                "body": "**Plan approved**\n- SHA-256: `" + "a" * 64 + "`",
                "createdAt": updated,
                "updatedAt": updated,
                "issue": {"id": "issue-1"},
                "user": {
                    "id": "user-lead",
                    "name": "Lead User",
                    "displayName": "Lead",
                    "active": True,
                },
            },
            {
                "id": "comment-2",
                "body": "**Session review**\n- Plan SHA-256: `" + "a" * 64 + "`",
                "createdAt": updated,
                "updatedAt": updated,
                "issue": {"id": "issue-1"},
                "user": {
                    "id": "user-lead",
                    "name": "Lead User",
                    "displayName": "Lead",
                    "active": True,
                },
            },
        ],
        "attachments": [
            {
                "id": "att-1",
                "title": "Attachment",
                "url": "https://example.com/doc",
                "updatedAt": updated,
                "issue": {"id": "issue-1"},
                "creator": {
                    "id": "user-lead",
                    "name": "Lead User",
                    "displayName": "Lead",
                    "active": True,
                },
            }
        ],
    }
    return values.get(operation, [])


def _normalize_node(stream: LinearStream, node: dict[str, Any]) -> dict[str, Any]:
    return dict(node)


def _synthesize_export(
    config: OperationsConfig,
    workspace_id: UUID,
    node_builder: Any = _sample_nodes,
    *,
    mode: str = "full",
    base_export_id: UUID | None = None,
    export_id: UUID | None = None,
    started: datetime | None = None,
    boundary: datetime | None = None,
) -> ExportManifest:
    export_id = export_id or uuid4()
    started = started or datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    boundary = boundary or (
        datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        if mode == "full"
        else datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    )
    root = config.data_dir / "linear-exports" / str(workspace_id) / str(export_id)
    staging = config.state_dir / "staging" / str(export_id)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging.mkdir(mode=0o700, parents=True, exist_ok=True)

    artifacts: dict[str, ArtifactRecord] = {}
    summaries: dict[str, StreamSummary] = {}
    records: dict[LinearStream, list[dict[str, Any]]] = defaultdict(list)
    provenance: dict[tuple[LinearStream, str], str] = {}
    phase = "baseline" if mode == "full" else "delta"

    for stream in LinearStream:
        stream_key = f"{phase}:{stream.value}"
        raw_nodes = (
            node_builder(stream.value, {}, 1)
            if callable(node_builder)
            else node_builder.get(stream.value, [])
        )
        norm_nodes = tuple(_normalize_node(stream, n) for n in raw_nodes)
        records[stream].extend(norm_nodes)

        page = SourcePage(
            export_id=export_id,
            stream=stream_key,
            page_index=0,
            has_next_page=False,
            nodes=norm_nodes,
        )
        payload = page.model_dump(mode="json")
        plaintext_hash = sha256(payload)
        encrypted = root / f"{phase}-{stream.value}-0-{plaintext_hash}.json.gpg"
        plain = staging / encrypted.name.removesuffix(".gpg")
        plain.write_text(canonical_json(payload), encoding="utf-8")
        plain.chmod(0o600)
        c_hash = encrypt_file(
            plain, encrypted, config.secret_path("gpg-passphrase"), mode=0o400
        )
        plain.unlink(missing_ok=True)

        rel_path = str(encrypted.relative_to(config.data_dir))
        for n in norm_nodes:
            if n.get("id"):
                provenance[(stream, str(n["id"]))] = rel_path

        artifact = ArtifactRecord(
            path=rel_path,
            plaintext_sha256=plaintext_hash,
            ciphertext_sha256=c_hash,
            variables_sha256=bytes_sha256(
                json.dumps({"first": 50, "after": None}, sort_keys=True).encode()
            ).hexdigest(),
            stream=stream_key,
            page_index=0,
            has_next_page=False,
        )
        artifacts[f"{stream_key}:0"] = artifact
        summaries[stream.value] = StreamSummary(
            scanned=len(norm_nodes), retained=len(norm_nodes), excluded=0
        )

    # Build source hashes
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
    data: dict[str, dict[str, SourceHashEntry]] = {
        name: {} for name in (*DIMENSIONS, "project_updates")
    }
    for stream, dim in mappings.items():
        for node in records[stream]:
            ident = str(node.get("id", ""))
            if not ident:
                continue
            dt_str = node.get("updatedAt")
            dt = (
                datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                if isinstance(dt_str, str)
                else None
            )
            s_hash = sha256(node)
            owner = (
                str((node.get("project") or {}).get("id"))
                if stream is LinearStream.project_updates
                and (node.get("project") or {}).get("id")
                else None
            )
            entry = SourceHashEntry(
                id=ident,
                key=node.get("identifier") if dim == "work_items" else None,
                owner_id=owner,
                updated_at=dt,
                source_sha256=s_hash if dim == "surfaces" else None,
                record_sha256=s_hash,
                artifact_ref=provenance.get((stream, ident), ""),
            )
            data[dim][ident] = entry

    for stream, fields in (
        (LinearStream.projects, ("lead",)),
        (LinearStream.project_updates, ("user",)),
        (LinearStream.issues, ("assignee", "creator")),
        (LinearStream.comments, ("user",)),
        (LinearStream.attachments, ("creator",)),
    ):
        for node in records[stream]:
            for field in fields:
                p = node.get(field)
                if isinstance(p, dict) and p.get("id"):
                    p_id = str(p["id"])
                    data["users"][p_id] = SourceHashEntry(
                        id=p_id,
                        record_sha256=sha256(
                            {
                                k: p.get(k)
                                for k in ("id", "name", "displayName", "active")
                            }
                        ),
                        artifact_ref=provenance.get(
                            (stream, str(node.get("id", ""))), ""
                        ),
                    )

    # Fold project updates
    by_proj: dict[str, list[SourceHashEntry]] = defaultdict(list)
    for up in data["project_updates"].values():
        if up.owner_id:
            by_proj[up.owner_id].append(up)
    for p_id, proj in tuple(data["surfaces"].items()):
        s_hash = proj.source_sha256 or proj.record_sha256
        ups = by_proj.get(p_id, [])
        r_hash = (
            s_hash
            if not ups
            else sha256(
                {
                    "project": s_hash,
                    "updates": [
                        {"id": e.id, "record_sha256": e.record_sha256}
                        for e in sorted(ups, key=lambda x: x.id)
                    ],
                }
            )
        )
        data["surfaces"][p_id] = proj.model_copy(
            update={"source_sha256": s_hash, "record_sha256": r_hash}
        )

    source_hashes = SourceHashIndex(**data)
    base_records: dict[LinearStream, list[dict[str, Any]]] = defaultdict(list)
    if base_export_id is not None:
        base_manifest, base_hashes, base_pages = load_export(config, base_export_id)
        for bp in base_pages:
            st = LinearStream(bp.stream.split(":", 1)[1])
            base_records[st].extend(bp.nodes)
        merged_dims: dict[str, dict[str, SourceHashEntry]] = {}
        for d in (*DIMENSIONS, "project_updates"):
            vals = dict(getattr(base_hashes, d))
            vals.update(getattr(source_hashes, d))
            merged_dims[d] = vals
        source_hashes = SourceHashIndex(**merged_dims)

    # Merge record sets for validation
    validation_records: dict[LinearStream, list[dict[str, Any]]] = defaultdict(list)
    for st in LinearStream:
        node_map = {}
        for n in (*base_records[st], *records[st]):
            ident = str(n.get("id", ""))
            if ident:
                node_map[ident] = n
            else:
                validation_records[st].append(n)
        validation_records[st].extend(node_map.values())

    # Write reports
    def write_rep(name: str, payload_obj: object) -> ArtifactRecord:
        rel, plain_h, cipher_h = write_json_artifact(
            root,
            staging,
            name,
            payload_obj,
            config.secret_path("gpg-passphrase"),
            config.data_dir,
            encrypt_fn=encrypt_file,
            decrypt_fn=decrypt_file,
        )
        rec = ArtifactRecord(
            path=rel, plaintext_sha256=plain_h, ciphertext_sha256=cipher_h
        )
        artifacts[name] = rec
        return rec

    write_rep("source-hashes", source_hashes.model_dump(mode="json"))
    scope = ScopeReport(streams=summaries)
    write_rep("scope-report", scope.model_dump(mode="json"))
    privacy = PrivacyReport(staging_cleanup=True, forbidden_fields_removed=True)
    write_rep("privacy-report", privacy.model_dump(mode="json"))

    # Anomalies
    anomalies_list: list[Anomaly] = []
    found_ids = {
        "initiatives": set(source_hashes.worlds),
        "projects": set(source_hashes.surfaces),
        "milestones": set(source_hashes.promises),
        "issues": set(source_hashes.work_items),
        "states": set(source_hashes.states),
        "labels": set(source_hashes.labels),
        "comments": set(source_hashes.comments),
        "users": set(source_hashes.users),
    }

    def missing(target: str, value: Any) -> None:
        if (
            isinstance(value, dict)
            and value.get("id")
            and str(value["id"]) not in found_ids[target]
        ):
            anomalies_list.append(
                Anomaly(code="missing_relation_endpoint", disposition="blocking")
            )

    for link in validation_records[LinearStream.initiative_projects]:
        missing("initiatives", link.get("initiative"))
        missing("projects", link.get("project"))
    for project in validation_records[LinearStream.projects]:
        missing("users", project.get("lead"))
    for update in validation_records[LinearStream.project_updates]:
        missing("projects", update.get("project"))
        missing("users", update.get("user"))
    for milestone in validation_records[LinearStream.milestones]:
        missing("projects", milestone.get("project"))
    for issue in validation_records[LinearStream.issues]:
        missing("issues", issue.get("parent"))
        missing("projects", issue.get("project"))
        missing("milestones", issue.get("projectMilestone"))
        missing("states", issue.get("state"))
        for label in (issue.get("labels") or {}).get("nodes", []):
            missing("labels", label)
        missing("users", issue.get("assignee"))
        missing("users", issue.get("creator"))
    for relation in validation_records[LinearStream.relations]:
        missing("issues", relation.get("issue"))
        missing("issues", relation.get("relatedIssue"))
    for comment in validation_records[LinearStream.comments]:
        missing("issues", comment.get("issue"))
        missing("users", comment.get("user"))
    for attachment in validation_records[LinearStream.attachments]:
        missing("issues", attachment.get("issue"))
        missing("users", attachment.get("creator"))

    seen: dict[str, str] = {}
    for issue in validation_records[LinearStream.issues]:
        key = str(issue.get("identifier", ""))
        ident = str(issue.get("id", ""))
        if key and key in seen and seen[key] != ident:
            anomalies_list.append(
                Anomaly(code="duplicate_uuid_key_mapping", disposition="blocking")
            )
        if key:
            seen[key] = ident

    for state in validation_records[LinearStream.states]:
        if str(state.get("type", "")) not in (
            "started",
            "completed",
            "canceled",
            "triage",
            "backlog",
            "unstarted",
        ):
            anomalies_list.append(
                Anomaly(code="unsupported_non_workflow_object", disposition="blocking")
            )

    edges: list[RelationEdge] = []
    for relation in validation_records[LinearStream.relations]:
        raw_kind = str(relation.get("type", "")).lower()
        kind_map = {
            "blocks": RelationKind.BLOCKS,
            "duplicate": RelationKind.DUPLICATE_OF,
            "duplicate_of": RelationKind.DUPLICATE_OF,
            "related": RelationKind.RELATED,
        }
        kind = kind_map.get(raw_kind)
        if kind is None:
            anomalies_list.append(
                Anomaly(
                    code="unsupported_non_workflow_object", disposition="quarantined"
                )
            )
            continue
        try:
            e = RelationEdge(
                workspace_id=UUID(int=0),
                source_work_id=UUID(str(relation["issue"]["id"])),
                target_work_id=UUID(str(relation["relatedIssue"]["id"])),
                kind=kind,
            )
            if would_create_cycle(tuple(edges), e):
                anomalies_list.append(
                    Anomaly(code="relation_cycle", disposition="blocking")
                )
            else:
                edges.append(e)
        except Exception:
            pass

    for issue in validation_records[LinearStream.issues]:
        if not isinstance(issue.get("parent"), dict):
            continue
        try:
            e = RelationEdge(
                workspace_id=UUID(int=0),
                source_work_id=UUID(str(issue["id"])),
                target_work_id=UUID(str(issue["parent"]["id"])),
                kind=RelationKind.PARENT,
            )
            if would_create_cycle(tuple(edges), e):
                anomalies_list.append(
                    Anomaly(code="relation_cycle", disposition="blocking")
                )
            else:
                edges.append(e)
        except Exception:
            pass

    plans: dict[str, tuple[str, str]] = {}
    for comment in sorted(
        validation_records[LinearStream.comments],
        key=lambda item: (str(item.get("createdAt", "")), str(item.get("id", ""))),
    ):
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
                anomalies_list.append(
                    Anomaly(code="legacy_authority_claim", disposition="blocking")
                )

    now_label_ids = {
        str(label["id"])
        for label in validation_records[LinearStream.labels]
        if label.get("id") and str(label.get("name", "")).casefold() == "now"
    }
    focused = [
        issue
        for issue in validation_records[LinearStream.issues]
        if not issue.get("archivedAt")
        and not issue.get("completedAt")
        and not issue.get("canceledAt")
        and any(
            str(label.get("id")) in now_label_ids
            for label in (issue.get("labels") or {}).get("nodes", [])
        )
    ]
    if len(focused) > 1:
        anomalies_list.append(
            Anomaly(code="multiple_focus_slots", disposition="blocking")
        )

    metadata_only = former_metadata = quarantined = 0
    for attachment in validation_records[LinearStream.attachments]:
        issue_id = str((attachment.get("issue") or {}).get("id", ""))
        usable = bool(
            attachment.get("id")
            and issue_id in source_hashes.work_items
            and (attachment.get("url") or attachment.get("metadata"))
        )
        if not usable:
            quarantined += 1
            anomalies_list.append(
                Anomaly(
                    code="attachment_content_unavailable", disposition="quarantined"
                )
            )
        elif attachment.get("archivedAt"):
            former_metadata += 1
        else:
            metadata_only += 1

    unique_anomalies = {(item.code, item.disposition): item for item in anomalies_list}
    anomalies_tuple = tuple(unique_anomalies[k] for k in sorted(unique_anomalies))
    write_rep("anomaly-report", [a.model_dump(mode="json") for a in anomalies_tuple])

    counts = ReconciliationCounts(
        **{name: len(getattr(source_hashes, name)) for name in DIMENSIONS}
    )
    hashes = ReconciliationHashes(
        **{
            name: sha256(
                [
                    {"id": e.id, "record_sha256": e.record_sha256}
                    for e in sorted(
                        getattr(source_hashes, name).values(), key=lambda x: x.id
                    )
                ]
            )
            for name in DIMENSIONS
        }
    )
    raw_hash = sha256(
        {
            "source_hashes": source_hashes.model_dump(mode="json"),
            "pages": [
                a.plaintext_sha256
                for _, a in sorted(artifacts.items())
                if not _
                in ("source-hashes", "scope-report", "privacy-report", "anomaly-report")
            ],
        }
    )

    manifest_draft = ExportManifest(
        export_id=export_id,
        workspace_id=workspace_id,
        mode=mode,
        base_export_id=base_export_id,
        source_started_at=started,
        source_lower_bound=started if mode == "delta" else None,
        source_boundary=boundary,
        source_hashes=source_hashes,
        dimension_counts=counts,
        dimension_hashes=hashes,
        raw_export_sha256=raw_hash,
        artifacts=artifacts,
        scope_report=scope,
        privacy_report=privacy,
        attachment_dispositions=AttachmentDisposition(
            metadata_only=metadata_only,
            former_metadata=former_metadata,
            quarantined=quarantined,
        ),
        anomalies=anomalies_tuple,
    )
    manifest_hash = sha256(
        manifest_draft.model_dump(mode="json", exclude={"manifest_sha256"})
    )
    manifest = manifest_draft.model_copy(update={"manifest_sha256": manifest_hash})
    man_art = write_rep(f"manifest-{manifest_hash}", manifest.model_dump(mode="json"))
    final_manifest = manifest.model_copy(
        update={"artifacts": {**artifacts, "manifest": man_art}}
    )

    # Insert DB record
    with (
        _connect(config, "omp_work_importer") as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
            (str(workspace_id), str(config.actor_id())),
        )
        cur.execute(
            "INSERT INTO omp_integration.raw_exports (export_id, workspace_id, team_key, mode, base_export_id, source_started_at, source_lower_bound, source_boundary, state, storage_root, raw_export_sha256, manifest_sha256, completed_at) VALUES (%s, %s, 'HOME', %s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp()) ON CONFLICT (export_id, workspace_id) DO NOTHING",
            (
                export_id,
                workspace_id,
                mode,
                base_export_id,
                started,
                started if mode == "delta" else None,
                boundary,
                "blocked"
                if any(a.disposition == "blocking" for a in anomalies_tuple)
                else "complete",
                str(root.relative_to(config.data_dir)),
                raw_hash,
                manifest_hash,
            ),
        )
    shutil.rmtree(staging, ignore_errors=True)
    return final_manifest


class StaticExportFixture:
    def __init__(self, config: OperationsConfig, node_builder: Any = _sample_nodes):
        self.config = config
        self.node_builder = node_builder

    def full(self, workspace_id: UUID) -> ExportManifest:
        return _synthesize_export(
            self.config, workspace_id, self.node_builder, mode="full"
        )

    def delta(self, workspace_id: UUID) -> ExportManifest:
        with _connect(self.config, "omp_work_importer") as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
                (str(workspace_id), str(self.config.actor_id())),
            )
            cur.execute(
                "SELECT export_id, source_boundary FROM omp_integration.raw_exports WHERE workspace_id=%s AND state='complete' ORDER BY completed_at DESC LIMIT 1",
                (workspace_id,),
            )
            row = cur.fetchone()
            base_id = row[0] if row else None
            lower = row[1] if row else None
        return _synthesize_export(
            self.config,
            workspace_id,
            self.node_builder,
            mode="delta",
            base_export_id=base_id,
            started=lower,
        )


def test_parse_acceptance_criteria() -> None:
    desc = """## Overview
Some notes.

# Acceptance criteria
- [ ] Task one
- [x] Task two with
  continuation line
  - ignored sublist item
* Bullet task three
1. Numbered task four

## Out of scope
Not here.
"""
    criteria = parse_acceptance_criteria(desc)
    assert criteria == (
        "Task one",
        "Task two with continuation line",
        "Bullet task three",
        "Numbered task four",
    )


def test_linear_import_map_validation(tmp_path: Path) -> None:
    valid = _make_mapping_file(tmp_path)
    importer = LinearImporter(
        OperationsConfig(config_dir=tmp_path, state_dir=tmp_path, data_dir=tmp_path)
    )
    mapping, map_hash = importer._read_mapping(valid)
    assert mapping.unprojected_repository == "side"
    assert len(map_hash) == 64

    invalid_perms = tmp_path / "bad_perms.json"
    invalid_perms.write_text(valid.read_text())
    invalid_perms.chmod(0o644)
    with pytest.raises(ValueError, match="linear_import_mapping_invalid"):
        importer._read_mapping(invalid_perms)

    invalid_json = tmp_path / "bad_json.json"
    invalid_json.write_text("{broken", encoding="utf-8")
    invalid_json.chmod(0o600)
    with pytest.raises(ValueError, match="linear_import_mapping_invalid"):
        importer._read_mapping(invalid_json)


def test_full_export_stage_reconcile_promote_end_to_end(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    config = postgres_service
    workspace_id = uuid4()

    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute(
                    "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (workspace_id,),
                )

    exporter = StaticExportFixture(config)
    export_manifest = exporter.full(workspace_id)
    mapping_file = _make_mapping_file(tmp_path)
    importer = LinearImporter(config)
    summary_staged = importer.stage(
        workspace_id, export_manifest.export_id, mapping_file
    )
    assert summary_staged.state == "staged"
    assert summary_staged.export_id == export_manifest.export_id

    summary_staged_again = importer.stage(
        workspace_id, export_manifest.export_id, mapping_file
    )
    assert summary_staged_again.batch_id == summary_staged.batch_id
    assert summary_staged_again.state == "staged"

    summary_reconciled = importer.reconcile(summary_staged.batch_id)
    assert summary_reconciled.state == "reconciled"
    assert summary_reconciled.reconciliation_sha256 is not None
    assert "import-manifest" in summary_reconciled.artifacts
    assert "parity-report" in summary_reconciled.artifacts

    summary_promoted = importer.promote(summary_staged.batch_id)
    assert summary_promoted.state == "promoted"
    assert summary_promoted.disposition_counts.get("created", 0) > 0

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))

            cur.execute(
                "SELECT COUNT(*) FROM omp_work.work_items WHERE workspace_id = %s",
                (workspace_id,),
            )
            assert cur.fetchone()[0] == 2

            cur.execute(
                "SELECT COUNT(*) FROM omp_work.projects WHERE workspace_id = %s",
                (workspace_id,),
            )
            assert cur.fetchone()[0] == 3

            cur.execute(
                "SELECT COUNT(*) FROM omp_work.repositories WHERE workspace_id = %s",
                (workspace_id,),
            )
            assert cur.fetchone()[0] == 2

            cur.execute(
                "SELECT COUNT(*) FROM omp_work.principals WHERE workspace_id = %s",
                (workspace_id,),
            )
            assert cur.fetchone()[0] >= 1

            cur.execute(
                "SELECT COUNT(*) FROM omp_work.workflow_states WHERE workspace_id = %s",
                (workspace_id,),
            )
            assert cur.fetchone()[0] == 2

            cur.execute(
                "SELECT COUNT(*) FROM omp_work.labels WHERE workspace_id = %s",
                (workspace_id,),
            )
            assert cur.fetchone()[0] >= 1

            cur.execute(
                "SELECT work_id FROM omp_work.focus_slots WHERE workspace_id = %s",
                (workspace_id,),
            )
            focus_work_id = cur.fetchone()[0]
            assert focus_work_id is not None

            cur.execute(
                "SELECT health FROM omp_work.project_health WHERE workspace_id = %s",
                (workspace_id,),
            )
            assert cur.fetchone()[0] == "onTrack"

            cur.execute(
                "SELECT COUNT(*) FROM omp_work.work_relations WHERE workspace_id = %s",
                (workspace_id,),
            )
            assert cur.fetchone()[0] == 1

            cur.execute(
                "SELECT COUNT(*) FROM omp_integration.external_refs WHERE workspace_id = %s",
                (workspace_id,),
            )
            assert cur.fetchone()[0] >= 5


def test_delta_import_and_revision_advancement(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    config = postgres_service
    workspace_id = uuid4()

    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute(
                    "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (workspace_id,),
                )

    exporter = StaticExportFixture(config)
    base_export = exporter.full(workspace_id)

    mapping_file = _make_mapping_file(tmp_path)
    importer = LinearImporter(config)
    summary_base = importer.stage(workspace_id, base_export.export_id, mapping_file)
    importer.reconcile(summary_base.batch_id)
    importer.promote(summary_base.batch_id)

    def delta_nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        del call_index
        filter_val = variables.get("filter")
        bounded = isinstance(filter_val, dict) and "updatedAt" in filter_val
        updated = (
            str(filter_val["updatedAt"]["gte"])
            if bounded
            else "2026-08-05T00:00:00+00:00"
        )

        if operation == "issues":
            return [
                {
                    "id": "issue-1",
                    "identifier": "HOME-146",
                    "title": "Build the importer (revised title)",
                    "description": "Updated description with new criteria\n\n# Acceptance criteria\n- [ ] Revised item 1",
                    "priority": 1,
                    "updatedAt": updated,
                    "team": {"key": "HOME"},
                    "state": {
                        "id": "state-in-progress",
                        "type": "started",
                        "name": "In Progress",
                    },
                    "project": {"id": "project-home"},
                    "assignee": {
                        "id": "user-lead",
                        "name": "Lead User",
                        "displayName": "Lead",
                        "active": True,
                    },
                    "labels": {
                        "nodes": [
                            {"id": "label-now", "name": "now", "color": "#ff0000"}
                        ]
                    },
                    "parent": None,
                    "previousIdentifiers": [],
                    "url": "https://linear.app/issue/HOME-146",
                }
            ]
        if operation in {"initiativeToProjects", "issueRelations", "attachments"}:
            return _sample_nodes(operation, variables)
        return []

    delta_exporter = StaticExportFixture(config, delta_nodes)
    delta_export = delta_exporter.delta(workspace_id)

    delta_summary = importer.stage(workspace_id, delta_export.export_id, mapping_file)
    assert delta_summary.base_batch_id == summary_base.batch_id
    importer.reconcile(delta_summary.batch_id)
    promoted_delta = importer.promote(delta_summary.batch_id)
    assert promoted_delta.state == "promoted"
    assert promoted_delta.disposition_counts.get("revised", 0) == 1

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))

            cur.execute(
                """
                SELECT wi.row_version, wr.revision_number, wr.title
                FROM omp_work.work_items wi
                JOIN omp_work.work_revisions wr ON wi.current_revision_id = wr.revision_id
                WHERE wi.workspace_id = %s AND wi.work_id = (
                    SELECT local_id FROM omp_integration.external_refs WHERE workspace_id = %s AND external_id = 'issue-1'
                )
                """,
                (workspace_id, workspace_id),
            )
            row = cur.fetchone()
            assert row[0] == 2
            assert row[1] == 2
            assert "revised title" in row[2]


def test_source_local_conflict_blocks_and_preserves_local_row(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    config = postgres_service
    workspace_id = uuid4()
    actor_id = uuid4()

    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(actor_id),)
                )
                cur.execute(
                    "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (workspace_id,),
                )

    exporter = StaticExportFixture(config)
    base_export = exporter.full(workspace_id)

    mapping_file = _make_mapping_file(tmp_path)
    importer = LinearImporter(config)
    summary_base = importer.stage(workspace_id, base_export.export_id, mapping_file)
    importer.reconcile(summary_base.batch_id)
    importer.promote(summary_base.batch_id)

    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(actor_id),)
                )

                cur.execute(
                    "SELECT work_id FROM omp_work.work_aliases WHERE workspace_id = %s AND key = 'HOME-146'",
                    (workspace_id,),
                )
                work_id = cur.fetchone()[0]

                cur.execute("SELECT uuidv7()")
                local_rev_id = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO omp_work.work_revisions (
                        revision_id, work_id, workspace_id, revision_number,
                        title, description, scope, content_sha256, created_by, supplied_at
                    ) VALUES (%s, %s, %s, 2, 'Local title', 'Local desc', '', %s, 'local_author', clock_timestamp())
                    """,
                    (
                        local_rev_id,
                        work_id,
                        workspace_id,
                        sha256(
                            {
                                "title": "Local title",
                                "description": "Local desc",
                                "scope": "",
                                "acceptance_criteria": (),
                            }
                        ),
                    ),
                )
                cur.execute(
                    "UPDATE omp_work.work_items SET current_revision_id = %s, row_version = 2 WHERE workspace_id = %s AND work_id = %s",
                    (local_rev_id, workspace_id, work_id),
                )

    def conflicting_delta_nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        del call_index
        filter_val = variables.get("filter")
        bounded = isinstance(filter_val, dict) and "updatedAt" in filter_val
        updated = (
            str(filter_val["updatedAt"]["gte"])
            if bounded
            else "2026-08-05T00:00:00+00:00"
        )

        if operation == "issues":
            return [
                {
                    "id": "issue-1",
                    "identifier": "HOME-146",
                    "title": "Remote conflicting title",
                    "description": "Remote desc",
                    "priority": 1,
                    "updatedAt": updated,
                    "team": {"key": "HOME"},
                    "state": {
                        "id": "state-in-progress",
                        "type": "started",
                        "name": "In Progress",
                    },
                    "project": {"id": "project-home"},
                    "assignee": {
                        "id": "user-lead",
                        "name": "Lead User",
                        "displayName": "Lead",
                        "active": True,
                    },
                    "labels": {
                        "nodes": [
                            {"id": "label-now", "name": "now", "color": "#ff0000"}
                        ]
                    },
                    "parent": None,
                    "previousIdentifiers": [],
                    "url": "https://linear.app/issue/HOME-146",
                }
            ]
        if operation in {"initiativeToProjects", "issueRelations", "attachments"}:
            return _sample_nodes(operation, variables)
        return []

    delta_exporter = StaticExportFixture(config, conflicting_delta_nodes)
    delta_export = delta_exporter.delta(workspace_id)

    delta_summary = importer.stage(workspace_id, delta_export.export_id, mapping_file)
    reconcile_summary = importer.reconcile(delta_summary.batch_id)
    assert reconcile_summary.state == "blocked"
    assert "source_local_conflict" in reconcile_summary.anomaly_codes

    with pytest.raises(ValueError, match="linear_import_blocked"):
        importer.promote(delta_summary.batch_id)

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(actor_id),))
            cur.execute(
                "SELECT wr.title FROM omp_work.work_items wi JOIN omp_work.work_revisions wr ON wi.current_revision_id = wr.revision_id WHERE wi.workspace_id = %s",
                (workspace_id,),
            )
            assert cur.fetchone()[0] == "Local title"


def test_relation_cycle_detection_blocks_reconciliation(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    config = postgres_service
    workspace_id = uuid4()

    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute(
                    "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (workspace_id,),
                )

    def cyclic_nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        base = _sample_nodes(operation, variables, call_index)
        if operation == "issues":
            return [
                {**base[0], "parent": {"id": "issue-2"}},
                {**base[1], "parent": {"id": "issue-1"}},
            ]
        return base

    exporter = StaticExportFixture(config, cyclic_nodes)
    export_manifest = exporter.full(workspace_id)

    mapping_file = _make_mapping_file(tmp_path)
    importer = LinearImporter(config)

    staged = importer.stage(workspace_id, export_manifest.export_id, mapping_file)
    reconciled = importer.reconcile(staged.batch_id)
    assert reconciled.state == "blocked"
    assert "relation_cycle" in reconciled.anomaly_codes

    with pytest.raises(ValueError, match="linear_import_blocked"):
        importer.promote(staged.batch_id)


def test_unchanged_source_preserves_local_canonical_edits(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    config = postgres_service
    workspace_id = uuid4()

    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute(
                    "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (workspace_id,),
                )

    exporter = StaticExportFixture(config)
    first_export = exporter.full(workspace_id)

    mapping_file = _make_mapping_file(tmp_path)
    importer = LinearImporter(config)
    first_batch = importer.stage(workspace_id, first_export.export_id, mapping_file)
    importer.reconcile(first_batch.batch_id)
    importer.promote(first_batch.batch_id)

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT local_id FROM omp_integration.external_refs WHERE workspace_id = %s AND external_id = 'issue-1'",
                (workspace_id,),
            )
            work_id = cur.fetchone()[0]

    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute(
                    """
                    UPDATE omp_work.work_items
                    SET state = 'CANCELED', priority = 99, archived = true, row_version = 7
                    WHERE workspace_id = %s AND work_id = %s
                    """,
                    (workspace_id, work_id),
                )

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT * FROM omp_work.work_items WHERE workspace_id = %s AND work_id = %s",
                (workspace_id, work_id),
            )
            local_row = cur.fetchone()
            cur.execute(
                "SELECT COUNT(*) FROM omp_work.work_revisions WHERE workspace_id = %s AND work_id = %s",
                (workspace_id, work_id),
            )
            revision_count = cur.fetchone()[0]

    second_exporter = StaticExportFixture(config)
    second_export = second_exporter.full(workspace_id)
    second_batch = importer.stage(workspace_id, second_export.export_id, mapping_file)
    reconciled = importer.reconcile(second_batch.batch_id)
    assert reconciled.state == "reconciled"
    promoted = importer.promote(second_batch.batch_id)
    assert promoted.state == "promoted"
    assert promoted.disposition_counts.get("revised", 0) == 0

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT * FROM omp_work.work_items WHERE workspace_id = %s AND work_id = %s",
                (workspace_id, work_id),
            )
            assert cur.fetchone() == local_row
            cur.execute(
                "SELECT COUNT(*) FROM omp_work.work_revisions WHERE workspace_id = %s AND work_id = %s",
                (workspace_id, work_id),
            )
            assert cur.fetchone()[0] == revision_count
            cur.execute(
                "SELECT disposition FROM omp_integration.import_record_results WHERE batch_id = %s AND entity_type = 'work_items' AND source_id = 'issue-1'",
                (second_batch.batch_id,),
            )
            assert cur.fetchone()[0] == "unchanged"
            cur.execute(
                "SELECT local_id FROM omp_integration.external_refs WHERE workspace_id = %s AND external_id = 'issue-1'",
                (workspace_id,),
            )
            assert cur.fetchone()[0] == work_id


def test_source_label_removal_deactivates_imported_join(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    config = postgres_service
    workspace_id = uuid4()

    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute(
                    "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (workspace_id,),
                )

    exporter = StaticExportFixture(config)
    first_export = exporter.full(workspace_id)

    mapping_file = _make_mapping_file(tmp_path)
    importer = LinearImporter(config)
    first_batch = importer.stage(workspace_id, first_export.export_id, mapping_file)
    importer.reconcile(first_batch.batch_id)
    importer.promote(first_batch.batch_id)

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT local_id FROM omp_integration.external_refs WHERE workspace_id = %s AND external_id = 'issue-1'",
                (workspace_id,),
            )
            work_id = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM omp_work.work_item_labels WHERE workspace_id = %s AND work_id = %s AND active",
                (workspace_id, work_id),
            )
            assert cur.fetchone()[0] == 1

    def no_label_nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        base = _sample_nodes(operation, {}, 0)
        if operation == "issues":
            return [
                {
                    **base[0],
                    "labels": {"nodes": []},
                    "updatedAt": "2026-08-06T00:00:00+00:00",
                },
                base[1],
            ]
        return base

    second_exporter = StaticExportFixture(config, no_label_nodes)
    second_export = second_exporter.full(workspace_id)
    second_batch = importer.stage(workspace_id, second_export.export_id, mapping_file)
    reconciled = importer.reconcile(second_batch.batch_id)
    assert reconciled.state == "reconciled"
    promoted = importer.promote(second_batch.batch_id)
    assert promoted.state == "promoted"
    assert promoted.disposition_counts.get("revised", 0) == 0

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT active, origin FROM omp_work.work_item_labels WHERE workspace_id = %s AND work_id = %s",
                (workspace_id, work_id),
            )
            row = cur.fetchone()
            assert row == (False, "imported")

    third_exporter = StaticExportFixture(config, no_label_nodes)
    third_export = third_exporter.full(workspace_id)
    third_batch = importer.stage(workspace_id, third_export.export_id, mapping_file)
    importer.reconcile(third_batch.batch_id)
    third_promoted = importer.promote(third_batch.batch_id)
    assert third_promoted.disposition_counts.get("projection_updated", 0) == 0
    assert third_promoted.disposition_counts.get("revised", 0) == 0


def test_repeated_full_and_no_change_delta_parity(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    config = postgres_service
    workspace_id = uuid4()

    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute(
                    "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (workspace_id,),
                )

    def _canonical_snapshot() -> tuple[list[tuple], int, int, int, int]:
        with _connect(config, "omp_work_readonly") as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute(
                    "SELECT system, external_id, local_id FROM omp_integration.external_refs WHERE workspace_id = %s ORDER BY system, external_id",
                    (workspace_id,),
                )
                refs = cur.fetchall()
                cur.execute(
                    "SELECT COUNT(*) FROM omp_work.work_revisions WHERE workspace_id = %s",
                    (workspace_id,),
                )
                revisions = cur.fetchone()[0]
                cur.execute(
                    "SELECT COUNT(*) FROM omp_work.work_relations WHERE workspace_id = %s",
                    (workspace_id,),
                )
                relations = cur.fetchone()[0]
                cur.execute(
                    "SELECT COUNT(*) FROM omp_work.project_relations WHERE workspace_id = %s",
                    (workspace_id,),
                )
                project_relations = cur.fetchone()[0]
                cur.execute(
                    "SELECT COUNT(*) FROM omp_work.work_item_labels WHERE workspace_id = %s AND active",
                    (workspace_id,),
                )
                label_joins = cur.fetchone()[0]
        return refs, revisions, relations, project_relations, label_joins

    def static_nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        # Force the default static timestamps so repeated exports are byte-identical.
        return _sample_nodes(operation, {}, 0)

    mapping_file = _make_mapping_file(tmp_path)
    importer = LinearImporter(config)

    first_export = StaticExportFixture(config, static_nodes).full(workspace_id)
    first_batch = importer.stage(workspace_id, first_export.export_id, mapping_file)
    first_reconciled = importer.reconcile(first_batch.batch_id)
    importer.promote(first_batch.batch_id)
    snapshot_first = _canonical_snapshot()

    second_export = StaticExportFixture(config, static_nodes).full(workspace_id)
    second_batch = importer.stage(workspace_id, second_export.export_id, mapping_file)
    second_reconciled = importer.reconcile(second_batch.batch_id)
    second_promoted = importer.promote(second_batch.batch_id)
    snapshot_second = _canonical_snapshot()
    assert second_reconciled.state == "reconciled"
    assert second_reconciled.anomaly_codes == []
    assert second_reconciled.parity_hashes == first_reconciled.parity_hashes
    assert second_promoted.disposition_counts.get("revised", 0) == 0
    assert snapshot_second == snapshot_first

    def empty_delta_nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        return []

    delta_export = StaticExportFixture(config, empty_delta_nodes).delta(workspace_id)
    delta_batch = importer.stage(workspace_id, delta_export.export_id, mapping_file)
    assert delta_batch.base_batch_id == second_batch.batch_id
    delta_reconciled = importer.reconcile(delta_batch.batch_id)
    delta_promoted = importer.promote(delta_batch.batch_id)
    snapshot_delta = _canonical_snapshot()

    assert delta_reconciled.state == "reconciled"
    assert delta_reconciled.anomaly_codes == []
    assert delta_reconciled.parity_hashes == first_reconciled.parity_hashes
    assert delta_promoted.disposition_counts.get("revised", 0) == 0
    assert snapshot_delta == snapshot_first

    staging_root = config.state_dir / "staging"
    leftover_plaintext = (
        [path for path in staging_root.rglob("*") if path.is_file()]
        if staging_root.exists()
        else []
    )
    assert leftover_plaintext == []


def test_duplicate_alias_mapping_blocks_import(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    config = postgres_service
    workspace_id = uuid4()

    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute(
                    "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (workspace_id,),
                )
                cur.execute("SELECT uuidv7()")
                local_work_id = cur.fetchone()[0]
                cur.execute("SELECT uuidv7()")
                local_revision_id = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO omp_work.work_items (work_id, workspace_id, state, current_revision_id, row_version)
                    VALUES (%s, %s, 'BACKLOG', %s, 1)
                    """,
                    (local_work_id, workspace_id, local_revision_id),
                )
                cur.execute(
                    """
                    INSERT INTO omp_work.work_revisions (
                        revision_id, work_id, workspace_id, revision_number,
                        title, description, scope, content_sha256, created_by, supplied_at
                    ) VALUES (%s, %s, %s, 1, 'Local item', '', '', %s, 'local_author', clock_timestamp())
                    """,
                    (
                        local_revision_id,
                        local_work_id,
                        workspace_id,
                        sha256(
                            {
                                "title": "Local item",
                                "description": "",
                                "scope": "",
                                "acceptance_criteria": (),
                            }
                        ),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO omp_work.work_aliases (work_id, workspace_id, key, primary_alias, origin)
                    VALUES (%s, %s, 'HOME-146', true, 'local')
                    """,
                    (local_work_id, workspace_id),
                )

    def static_nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        return _sample_nodes(operation, {}, 0)

    exporter = StaticExportFixture(config, static_nodes)
    export_manifest = exporter.full(workspace_id)

    mapping_file = _make_mapping_file(tmp_path)
    importer = LinearImporter(config)
    staged = importer.stage(workspace_id, export_manifest.export_id, mapping_file)
    reconciled = importer.reconcile(staged.batch_id)
    assert reconciled.state == "blocked"
    assert "duplicate_uuid_key_mapping" in reconciled.anomaly_codes

    with pytest.raises(ValueError, match="linear_import_blocked"):
        importer.promote(staged.batch_id)

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT work_id FROM omp_work.work_aliases WHERE workspace_id = %s AND key = 'HOME-146'",
                (workspace_id,),
            )
            assert cur.fetchone()[0] == local_work_id


def _stage_with_nodes(
    postgres_service: OperationsConfig, tmp_path: Path, node_builder
) -> tuple[OperationsConfig, UUID, LinearImporter, ImportBatchSummary]:
    config = postgres_service
    workspace_id = uuid4()
    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute(
                    "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (workspace_id,),
                )
    exporter = StaticExportFixture(config, node_builder)
    export_manifest = exporter.full(workspace_id)
    importer = LinearImporter(config)
    staged = importer.stage(
        workspace_id, export_manifest.export_id, _make_mapping_file(tmp_path)
    )
    return config, workspace_id, importer, staged


def _assert_blocked_preserves_canonical(
    config: OperationsConfig,
    workspace_id: UUID,
    importer: LinearImporter,
    staged: ImportBatchSummary,
    expected_code: str,
) -> None:
    reconciled = importer.reconcile(staged.batch_id)
    assert reconciled.state == "blocked"
    assert expected_code in reconciled.anomaly_codes
    with pytest.raises(ValueError, match="linear_import_blocked"):
        importer.promote(staged.batch_id)
    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT COUNT(*) FROM omp_work.work_items WHERE workspace_id = %s",
                (workspace_id,),
            )
            assert cur.fetchone()[0] == 0


def test_multiple_now_labels_block_focus(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    def nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        base = _sample_nodes(operation, {}, 0)
        if operation == "issues":
            second_now = {
                **base[0],
                "id": "issue-3",
                "identifier": "HOME-147",
                "url": "https://linear.app/issue/HOME-147",
            }
            return [*base, second_now]
        return base

    config, workspace_id, importer, staged = _stage_with_nodes(
        postgres_service, tmp_path, nodes
    )
    _assert_blocked_preserves_canonical(
        config, workspace_id, importer, staged, "multiple_focus_slots"
    )


def test_mismatched_legacy_review_blocks(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    def nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        base = _sample_nodes(operation, {}, 0)
        if operation == "comments":
            return [
                base[0],
                {
                    **base[1],
                    "body": "**Session review**\n- Plan SHA-256: `" + "b" * 64 + "`",
                },
            ]
        return base

    config, workspace_id, importer, staged = _stage_with_nodes(
        postgres_service, tmp_path, nodes
    )
    _assert_blocked_preserves_canonical(
        config, workspace_id, importer, staged, "legacy_authority_claim"
    )


def test_missing_relation_endpoint_blocks(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    def nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        base = _sample_nodes(operation, {}, 0)
        if operation == "issueRelations":
            return [
                {
                    "id": "rel-missing",
                    "type": "blocks",
                    "updatedAt": "2026-08-01T00:00:00+00:00",
                    "issue": {"id": "issue-1"},
                    "relatedIssue": {"id": "issue-999"},
                }
            ]
        return base

    config, workspace_id, importer, staged = _stage_with_nodes(
        postgres_service, tmp_path, nodes
    )
    _assert_blocked_preserves_canonical(
        config, workspace_id, importer, staged, "missing_relation_endpoint"
    )


def test_previous_identifier_only_change_imports_alias(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    config = postgres_service
    workspace_id = uuid4()

    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute(
                    "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (workspace_id,),
                )

    def static_nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        return _sample_nodes(operation, {}, 0)

    mapping_file = _make_mapping_file(tmp_path)
    importer = LinearImporter(config)

    first_export = StaticExportFixture(config, static_nodes).full(workspace_id)
    first_batch = importer.stage(workspace_id, first_export.export_id, mapping_file)
    importer.reconcile(first_batch.batch_id)
    importer.promote(first_batch.batch_id)

    def aliased_nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        base = _sample_nodes(operation, {}, 0)
        if operation == "issues":
            # Only previousIdentifiers changes: content hash and projections are identical.
            return [{**base[0], "previousIdentifiers": ["HOME-100"]}, base[1]]
        return base

    second_export = StaticExportFixture(config, aliased_nodes).full(workspace_id)
    second_batch = importer.stage(workspace_id, second_export.export_id, mapping_file)
    reconciled = importer.reconcile(second_batch.batch_id)
    assert reconciled.state == "reconciled"
    promoted = importer.promote(second_batch.batch_id)
    assert promoted.state == "promoted"
    assert promoted.disposition_counts.get("revised", 0) == 0

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT local_id FROM omp_integration.external_refs WHERE workspace_id = %s AND external_id = 'issue-1'",
                (workspace_id,),
            )
            work_id = cur.fetchone()[0]
            cur.execute(
                "SELECT primary_alias, origin, work_id FROM omp_work.work_aliases WHERE workspace_id = %s AND key = 'HOME-100'",
                (workspace_id,),
            )
            row = cur.fetchone()
            assert row == (False, "imported", work_id)
            cur.execute(
                "SELECT disposition FROM omp_integration.import_record_results WHERE batch_id = %s AND entity_type = 'work_items' AND source_id = 'issue-1'",
                (second_batch.batch_id,),
            )
            assert cur.fetchone()[0] == "projection_updated"
            cur.execute(
                "SELECT COUNT(*) FROM omp_work.work_revisions WHERE workspace_id = %s AND work_id = %s",
                (workspace_id, work_id),
            )
            assert cur.fetchone()[0] == 1


def test_same_owner_local_alias_stays_unchanged(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    config = postgres_service
    workspace_id = uuid4()

    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute(
                    "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (workspace_id,),
                )
                cur.execute("SELECT uuidv7()")
                work_id = cur.fetchone()[0]
                cur.execute("SELECT uuidv7()")
                revision_id = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO omp_work.work_items (work_id, workspace_id, state, current_revision_id, row_version)
                    VALUES (%s, %s, 'IN_PROGRESS', %s, 1)
                    """,
                    (work_id, workspace_id, revision_id),
                )
                cur.execute(
                    """
                    INSERT INTO omp_work.work_revisions (
                        revision_id, work_id, workspace_id, revision_number,
                        title, description, scope, content_sha256, created_by, supplied_at
                    ) VALUES (%s, %s, %s, 1, 'Local item', '', '', %s, 'local_author', clock_timestamp())
                    """,
                    (
                        revision_id,
                        work_id,
                        workspace_id,
                        sha256(
                            {
                                "title": "Local item",
                                "description": "",
                                "scope": "",
                                "acceptance_criteria": (),
                            }
                        ),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO omp_work.work_aliases (work_id, workspace_id, key, primary_alias, origin)
                    VALUES (%s, %s, 'HOME-146', true, 'local')
                    """,
                    (work_id, workspace_id),
                )

    with _connect(config, "omp_work_importer") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute(
                    """
                    INSERT INTO omp_integration.external_refs (
                        workspace_id, system, external_id, local_id, local_type, source_identifier
                    ) VALUES (%s, 'linear', 'issue-1', %s, 'work_item', 'HOME-146')
                    """,
                    (workspace_id, work_id),
                )

    def static_nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        return _sample_nodes(operation, {}, 0)

    mapping_file = _make_mapping_file(tmp_path)
    importer = LinearImporter(config)

    dispositions: list[str] = []
    for _ in range(3):
        export = StaticExportFixture(config, static_nodes).full(workspace_id)
        batch = importer.stage(workspace_id, export.export_id, mapping_file)
        importer.reconcile(batch.batch_id)
        importer.promote(batch.batch_id)
        with _connect(config, "omp_work_readonly") as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute(
                    "SELECT disposition FROM omp_integration.import_record_results WHERE batch_id = %s AND entity_type = 'work_items' AND source_id = 'issue-1'",
                    (batch.batch_id,),
                )
                dispositions.append(cur.fetchone()[0])

    assert dispositions[0] == "revised"
    assert dispositions[1] == "unchanged"
    assert dispositions[2] == "unchanged"

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT origin, work_id FROM omp_work.work_aliases WHERE workspace_id = %s AND key = 'HOME-146'",
                (workspace_id,),
            )
            assert cur.fetchall() == [("local", work_id)]


def test_foreign_team_previous_identifier_filtered(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    config, workspace_id, importer, staged = _stage_with_nodes(
        postgres_service, tmp_path, _foreign_alias_nodes
    )
    reconciled = importer.reconcile(staged.batch_id)
    assert reconciled.state == "reconciled"
    promoted = importer.promote(staged.batch_id)
    assert promoted.state == "promoted"

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT local_id FROM omp_integration.external_refs WHERE workspace_id = %s AND external_id = 'issue-1'",
                (workspace_id,),
            )
            work_id = cur.fetchone()[0]
            cur.execute(
                "SELECT key, primary_alias FROM omp_work.work_aliases WHERE workspace_id = %s AND work_id = %s ORDER BY key",
                (workspace_id, work_id),
            )
            assert cur.fetchall() == [("HOME-100", False), ("HOME-146", True)]
            cur.execute(
                "SELECT transformed_json->'previous_identifiers' FROM omp_integration.import_records WHERE batch_id = %s AND entity_type = 'work_items' AND source_id = 'issue-1'",
                (staged.batch_id,),
            )
            raw_previous = cur.fetchone()[0]
            assert "ENG-42" in raw_previous
            assert "HOME-100" in raw_previous


def _foreign_alias_nodes(
    operation: str, variables: dict[str, object], call_index: int
) -> list[dict[str, object]]:
    base = _sample_nodes(operation, {}, 0)
    if operation == "issues":
        return [{**base[0], "previousIdentifiers": ["ENG-42", "HOME-100"]}, base[1]]
    return base


def test_transformation_version_bump_restages_export(
    postgres_service: OperationsConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, workspace_id, importer, staged_v0 = _stage_with_nodes(
        postgres_service, tmp_path, lambda op, v, i: _sample_nodes(op, {}, 0)
    )
    assert staged_v0.transformation_version == importer_module.TRANSFORMATION_VERSION

    monkeypatch.setattr(
        importer_module, "TRANSFORMATION_VERSION", "linear-transform/v0-legacy"
    )
    legacy = importer.stage(
        workspace_id, staged_v0.export_id, _make_mapping_file(tmp_path)
    )
    assert legacy.batch_id != staged_v0.batch_id
    assert legacy.transformation_version == "linear-transform/v0-legacy"
    assert legacy.state == "staged"
    monkeypatch.undo()

    reconciled = importer.reconcile(staged_v0.batch_id)
    assert reconciled.state == "reconciled"
    promoted = importer.promote(staged_v0.batch_id)
    assert promoted.state == "promoted"

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT key FROM omp_work.work_aliases WHERE workspace_id = %s ORDER BY key",
                (workspace_id,),
            )
            assert [r[0] for r in cur.fetchall()] == ["HOME-145", "HOME-146"]


def test_delta_base_resolves_current_transformation_version(
    postgres_service: OperationsConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def static_nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        return _sample_nodes(operation, {}, 0)

    config, workspace_id, importer, current_base = _stage_with_nodes(
        postgres_service, tmp_path, static_nodes
    )

    # A legacy-version batch for the same base export, left unpromoted: the delta must
    # still bind to the current-version batch, never to this one.
    monkeypatch.setattr(
        importer_module, "TRANSFORMATION_VERSION", "linear-transform/v0-legacy"
    )
    legacy_base = importer.stage(
        workspace_id, current_base.export_id, _make_mapping_file(tmp_path)
    )
    assert legacy_base.batch_id != current_base.batch_id
    monkeypatch.undo()

    importer.reconcile(current_base.batch_id)
    importer.promote(current_base.batch_id)

    delta_export = StaticExportFixture(config, lambda op, v, i: []).delta(workspace_id)
    delta_batch = importer.stage(
        workspace_id, delta_export.export_id, _make_mapping_file(tmp_path)
    )
    assert delta_batch.base_batch_id == current_base.batch_id
    reconciled = importer.reconcile(delta_batch.batch_id)
    assert reconciled.state == "reconciled"
    promoted = importer.promote(delta_batch.batch_id)
    assert promoted.state == "promoted"


def test_canonical_mutation_between_reconcile_and_promote(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    config, workspace_id, importer, staged = _stage_with_nodes(
        postgres_service, tmp_path, lambda op, v, i: _sample_nodes(op, {}, 0)
    )
    reconciled = importer.reconcile(staged.batch_id)
    assert reconciled.state == "reconciled"

    # Canonical mutation after reconciliation: a locally created item claims HOME-146.
    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute("SELECT uuidv7()")
                local_work_id = cur.fetchone()[0]
                cur.execute("SELECT uuidv7()")
                local_revision_id = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO omp_work.work_items (work_id, workspace_id, state, current_revision_id, row_version)
                    VALUES (%s, %s, 'BACKLOG', %s, 1)
                    """,
                    (local_work_id, workspace_id, local_revision_id),
                )
                cur.execute(
                    """
                    INSERT INTO omp_work.work_revisions (
                        revision_id, work_id, workspace_id, revision_number,
                        title, description, scope, content_sha256, created_by, supplied_at
                    ) VALUES (%s, %s, %s, 1, 'Local item', '', '', %s, 'local_author', clock_timestamp())
                    """,
                    (
                        local_revision_id,
                        local_work_id,
                        workspace_id,
                        sha256(
                            {
                                "title": "Local item",
                                "description": "",
                                "scope": "",
                                "acceptance_criteria": (),
                            }
                        ),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO omp_work.work_aliases (work_id, workspace_id, key, primary_alias, origin)
                    VALUES (%s, %s, 'HOME-146', true, 'local')
                    """,
                    (local_work_id, workspace_id),
                )

    with pytest.raises(ValueError, match="linear_import_blocked"):
        importer.promote(staged.batch_id)

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT COUNT(*) FROM omp_work.work_items WHERE workspace_id = %s",
                (workspace_id,),
            )
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT work_id FROM omp_work.work_aliases WHERE workspace_id = %s AND key = 'HOME-146'",
                (workspace_id,),
            )
            assert cur.fetchone()[0] == local_work_id
            cur.execute(
                "SELECT state FROM omp_integration.import_batches WHERE batch_id = %s",
                (staged.batch_id,),
            )
            assert cur.fetchone()[0] == "reconciled"


def _related_nodes(
    operation: str, variables: dict[str, object], call_index: int
) -> list[dict[str, object]]:
    base = _sample_nodes(operation, {}, 0)
    if operation == "issueRelations":
        return [
            {
                "id": "rel-1",
                "type": "related",
                "updatedAt": "2026-08-01T00:00:00+00:00",
                "issue": {"id": "issue-1"},
                "relatedIssue": {"id": "issue-2"},
            }
        ]
    if operation == "issues":
        return [base[0], {**base[1], "parent": None}]
    return base


def test_relation_deactivation_after_reconcile_trips_drift(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    config, workspace_id, importer, first = _stage_with_nodes(
        postgres_service, tmp_path, _related_nodes
    )
    importer.reconcile(first.batch_id)
    importer.promote(first.batch_id)

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT COUNT(*) FROM omp_work.work_relations WHERE workspace_id = %s AND active",
                (workspace_id,),
            )
            assert cur.fetchone()[0] == 1

    second = importer.stage(
        workspace_id,
        StaticExportFixture(config, _related_nodes).full(workspace_id).export_id,
        _make_mapping_file(tmp_path),
    )
    reconciled = importer.reconcile(second.batch_id)
    assert reconciled.state == "reconciled"

    # Canonical drift after reconciliation: the relation is deactivated locally. The
    # promotion insert conflicts on the immutable relation ID and cannot reactivate it.
    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute(
                    "UPDATE omp_work.work_relations SET active = false, revoked_at = clock_timestamp() WHERE workspace_id = %s",
                    (workspace_id,),
                )

    with pytest.raises(ValueError, match="linear_import_drift"):
        importer.promote(second.batch_id)

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT state FROM omp_integration.import_batches WHERE batch_id = %s",
                (second.batch_id,),
            )
            assert cur.fetchone()[0] == "reconciled"
            cur.execute(
                "SELECT COUNT(*) FROM omp_integration.import_record_results WHERE batch_id = %s",
                (second.batch_id,),
            )
            assert cur.fetchone()[0] == 0


def test_source_count_hash_discrepancy_blocks(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    config, workspace_id, importer, staged = _stage_with_nodes(
        postgres_service, tmp_path, lambda op, v, i: _sample_nodes(op, {}, 0)
    )

    # Perturb the staged record set after staging: one extra work_items record makes the
    # dimension count/hash check fail at reconcile.
    with _connect(config, "omp_work_importer") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute("SELECT uuidv7()")
                ghost_id = cur.fetchone()[0]
                ghost_transformed = json.dumps(
                    {
                        "state": "BACKLOG",
                        "acceptance_criteria": [],
                        "label_ids": [],
                        "source_label_ids": [],
                        "content_sha256": sha256("ghost-content"),
                        "provenance": {
                            "import_batch_id": str(staged.batch_id),
                            "source_id": "issue-ghost",
                            "identifier": "HOME-999",
                        },
                    }
                )
                cur.execute(
                    """
                    INSERT INTO omp_integration.import_records (
                        batch_id, workspace_id, entity_type, source_id, local_id, local_type,
                        artifact_ref, source_sha256, logical_sha256, transformed_json
                    ) VALUES (%s, %s, 'work_items', 'issue-ghost', %s, 'work_item', 'pages', %s, %s, %s)
                    """,
                    (
                        staged.batch_id,
                        workspace_id,
                        ghost_id,
                        sha256("ghost-source"),
                        sha256("ghost-logical"),
                        ghost_transformed,
                    ),
                )

    reconciled = importer.reconcile(staged.batch_id)
    assert reconciled.state == "blocked"
    assert "pagination_count_hash_gap" in reconciled.anomaly_codes
    with pytest.raises(ValueError, match="linear_import_blocked"):
        importer.promote(staged.batch_id)

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT COUNT(*) FROM omp_work.work_items WHERE workspace_id = %s",
                (workspace_id,),
            )
            assert cur.fetchone()[0] == 0


def test_importer_relation_pass_flags_missing_endpoint(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    config, workspace_id, importer, staged = _stage_with_nodes(
        postgres_service, tmp_path, lambda op, v, i: _sample_nodes(op, {}, 0)
    )

    # The export itself is clean; a pending relation with a missing target is injected
    # into the staged second pass so the importer's own endpoint check must fire.
    with _connect(config, "omp_work_importer") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute("SELECT uuidv7()")
                rel_id = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO omp_integration.import_relations (
                        relation_id, batch_id, workspace_id, relation_kind,
                        source_entity_type, source_id, target_entity_type, target_id, state
                    ) VALUES (%s, %s, %s, 'blocks', 'work_items', 'issue-1', 'work_items', 'issue-ghost', 'pending')
                    """,
                    (rel_id, staged.batch_id, workspace_id),
                )

    reconciled = importer.reconcile(staged.batch_id)
    assert reconciled.state == "blocked"
    assert "missing_relation_endpoint" in reconciled.anomaly_codes

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT origin FROM omp_integration.migration_anomalies WHERE batch_id = %s AND code = 'missing_relation_endpoint'",
                (staged.batch_id,),
            )
            assert cur.fetchone()[0] == "importer"
    with pytest.raises(ValueError, match="linear_import_blocked"):
        importer.promote(staged.batch_id)


def test_quarantined_attachment_excluded_from_parity(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    def nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        base = _sample_nodes(operation, {}, 0)
        if operation == "attachments":
            # No URL and no metadata: unusable, quarantined without blocking.
            return [
                {
                    "id": "att-1",
                    "title": "Attachment",
                    "updatedAt": "2026-08-01T00:00:00+00:00",
                    "issue": {"id": "issue-1"},
                    "creator": {
                        "id": "user-lead",
                        "name": "Lead User",
                        "displayName": "Lead",
                        "active": True,
                    },
                }
            ]
        return base

    config, workspace_id, importer, staged = _stage_with_nodes(
        postgres_service, tmp_path, nodes
    )
    reconciled = importer.reconcile(staged.batch_id)
    assert reconciled.state == "reconciled"

    staging = tmp_path / "artifact-readback"
    staging.mkdir(mode=0o700)
    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT name, artifact_path, plaintext_sha256, ciphertext_sha256 FROM omp_integration.import_artifacts WHERE batch_id = %s",
                (staged.batch_id,),
            )
            artifact_rows = {row[0]: row for row in cur.fetchall()}

    def _read_artifact(name: str):
        _, artifact_path, p_hash, c_hash = artifact_rows[name]
        return read_json_artifact(
            resolve_artifact_path(artifact_path, config.data_dir),
            staging / f"{name}.json",
            config.secret_path("gpg-passphrase"),
            expected_plaintext_sha256=p_hash,
            expected_ciphertext_sha256=c_hash,
            data_dir=config.data_dir,
            decrypt_fn=decrypt_file,
        )

    quarantine = _read_artifact("quarantine-manifest")
    assert [entry["source_id"] for entry in quarantine] == ["att-1"]

    parity = _read_artifact("parity-report")
    # 16 staged records minus the quarantined attachment = 15 in the non-quarantined denominator.
    assert parity["counts"]["entity_type"] == 15
    assert parity["counts"]["external_reference"] == 15
    assert parity["counts"]["attachment_disposition"] == 1
    assert parity["counts"]["relation_type"] == 6


def test_reverse_edge_added_after_reconcile_blocks_promote(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    def no_relation_nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        base = _sample_nodes(operation, {}, 0)
        if operation == "issues":
            return [base[0], {**base[1], "parent": None}]
        return base

    config, workspace_id, importer, first = _stage_with_nodes(
        postgres_service, tmp_path, no_relation_nodes
    )
    importer.reconcile(first.batch_id)
    importer.promote(first.batch_id)

    def with_blocks_nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        base = no_relation_nodes(operation, variables, call_index)
        if operation == "issueRelations":
            return [
                {
                    "id": "rel-1",
                    "type": "blocks",
                    "updatedAt": "2026-08-01T00:00:00+00:00",
                    "issue": {"id": "issue-1"},
                    "relatedIssue": {"id": "issue-2"},
                }
            ]
        return base

    second = importer.stage(
        workspace_id,
        StaticExportFixture(config, with_blocks_nodes).full(workspace_id).export_id,
        _make_mapping_file(tmp_path),
    )
    reconciled = importer.reconcile(second.batch_id)
    assert reconciled.state == "reconciled"

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT external_id, local_id FROM omp_integration.external_refs WHERE workspace_id = %s AND external_id IN ('issue-1', 'issue-2')",
                (workspace_id,),
            )
            local_ids = dict(cur.fetchall())

    # Canonical mutation after reconciliation: a local reverse edge blocks issue-2 -> issue-1.
    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute("SELECT uuidv7()")
                reverse_rel_id = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO omp_work.work_relations (relation_id, workspace_id, source_work_id, target_work_id, kind, active)
                    VALUES (%s, %s, %s, %s, 'blocks', true)
                    """,
                    (
                        reverse_rel_id,
                        workspace_id,
                        local_ids["issue-2"],
                        local_ids["issue-1"],
                    ),
                )

    with pytest.raises(ValueError, match="linear_import_blocked"):
        importer.promote(second.batch_id)

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT relation_id FROM omp_work.work_relations WHERE workspace_id = %s AND active",
                (workspace_id,),
            )
            assert [row[0] for row in cur.fetchall()] == [reverse_rel_id]
            cur.execute(
                "SELECT state FROM omp_integration.import_batches WHERE batch_id = %s",
                (second.batch_id,),
            )
            assert cur.fetchone()[0] == "reconciled"


def test_unknown_state_type_blocks_batch_at_staging(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    def nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        base = _sample_nodes(operation, {}, 0)
        if operation == "issues":
            return [
                {
                    **base[0],
                    "state": {"id": "state-weird", "type": "mystery", "name": "Weird"},
                },
                base[1],
            ]
        if operation == "workflowStates":
            return [
                *base,
                {
                    "id": "state-weird",
                    "name": "Weird",
                    "type": "mystery",
                    "position": 3,
                    "updatedAt": "2026-08-01T00:00:00+00:00",
                    "team": {"key": "HOME"},
                },
            ]
        return base

    config, workspace_id, importer, staged = _stage_with_nodes(
        postgres_service, tmp_path, nodes
    )
    assert staged.state == "blocked"
    assert "unsupported_non_workflow_object" in staged.anomaly_codes

    reconciled = importer.reconcile(staged.batch_id)
    assert reconciled.state == "blocked"
    with pytest.raises(ValueError, match="linear_import_blocked"):
        importer.promote(staged.batch_id)

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT COUNT(*) FROM omp_work.work_items WHERE workspace_id = %s",
                (workspace_id,),
            )
            assert cur.fetchone()[0] == 0


def test_siblings_sharing_one_parent_import_cleanly(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    def nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        base = _sample_nodes(operation, {}, 0)
        if operation == "issues":
            return [
                base[0],
                base[1],
                {
                    **base[1],
                    "id": "issue-3",
                    "identifier": "HOME-147",
                    "title": "Second sibling",
                    "url": "https://linear.app/issue/HOME-147",
                    "labels": {"nodes": []},
                    "parent": {"id": "issue-1"},
                },
            ]
        return base

    config, workspace_id, importer, staged = _stage_with_nodes(
        postgres_service, tmp_path, nodes
    )
    reconciled = importer.reconcile(staged.batch_id)
    assert reconciled.state == "reconciled"
    assert "relation_cycle" not in reconciled.anomaly_codes
    promoted = importer.promote(staged.batch_id)
    assert promoted.state == "promoted"

    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT COUNT(*) FROM omp_work.work_relations WHERE workspace_id = %s AND kind = 'parent' AND active",
                (workspace_id,),
            )
            assert cur.fetchone()[0] == 2


def test_in_batch_current_previous_alias_collision_blocks(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    def nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        base = _sample_nodes(operation, {}, 0)
        if operation == "issues":
            # issue-2 lists issue-1's current identifier among its previous identifiers:
            # same key claimed by two different staged Linear UUIDs.
            return [base[0], {**base[1], "previousIdentifiers": ["HOME-146"]}]
        return base

    config, workspace_id, importer, staged = _stage_with_nodes(
        postgres_service, tmp_path, nodes
    )
    _assert_blocked_preserves_canonical(
        config, workspace_id, importer, staged, "duplicate_uuid_key_mapping"
    )


def test_reconcile_rerun_resumes_without_rewrite(
    postgres_service: OperationsConfig, tmp_path: Path
) -> None:
    def nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        return _sample_nodes(operation, {}, 0)

    config, workspace_id, importer, staged = _stage_with_nodes(
        postgres_service, tmp_path, nodes
    )
    first = importer.reconcile(staged.batch_id)
    assert first.state == "reconciled"
    second = importer.reconcile(staged.batch_id)
    assert second.state == "reconciled"
    assert second.reconciliation_sha256 == first.reconciliation_sha256
    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT state, COUNT(*) FROM omp_integration.import_relations WHERE batch_id = %s GROUP BY state",
                (staged.batch_id,),
            )
            states = dict(cur.fetchall())
            assert states.get("validated", 0) > 0
            assert "pending" not in states

    # Within-pass interruption is atomic by construction (one transaction); promotion must
    # not rewrite relations reconcile already validated — staging rows keep their state and
    # exactly one canonical row exists per validated graph relation.
    promoted = importer.promote(staged.batch_id)
    assert promoted.state == "promoted"
    with _connect(config, "omp_work_readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)", (str(workspace_id),)
            )
            cur.execute("SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),))
            cur.execute(
                "SELECT state, COUNT(*) FROM omp_integration.import_relations WHERE batch_id = %s GROUP BY state",
                (staged.batch_id,),
            )
            assert dict(cur.fetchall()) == states
            cur.execute(
                "SELECT (SELECT COUNT(*) FROM omp_work.work_relations WHERE workspace_id = %(ws)s) + (SELECT COUNT(*) FROM omp_work.project_relations WHERE workspace_id = %(ws)s)",
                {"ws": workspace_id},
            )
            assert cur.fetchone()[0] == 3


def test_cli_subcommands_redaction_and_exit_codes(
    postgres_service: OperationsConfig,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = postgres_service
    workspace_id = uuid4()

    with _connect(config, "omp_work_app") as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true)",
                    (str(workspace_id),),
                )
                cur.execute(
                    "SELECT set_config('omp.actor_id', %s, true)", (str(uuid4()),)
                )
                cur.execute(
                    "INSERT INTO omp_control.workspaces (workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (workspace_id,),
                )

    exporter = StaticExportFixture(config)
    export_manifest = exporter.full(workspace_id)

    mapping_file = _make_mapping_file(tmp_path)

    parser = argparse.ArgumentParser()
    operations_cli.add_parser(parser)

    args_stage = parser.parse_args(
        [
            "linear-import",
            "stage",
            "--workspace-id",
            str(workspace_id),
            "--export-id",
            str(export_manifest.export_id),
            "--mapping-file",
            str(mapping_file),
        ]
    )
    operations_cli.run(args_stage, config)
    stage_out = capsys.readouterr().out
    stage_json = json.loads(stage_out)
    assert stage_json["state"] == "staged"
    batch_id = stage_json["batch_id"]

    args_reconcile = parser.parse_args(
        ["linear-import", "reconcile", "--batch-id", batch_id]
    )
    operations_cli.run(args_reconcile, config)
    rec_out = capsys.readouterr().out
    rec_json = json.loads(rec_out)
    assert rec_json["state"] == "reconciled"

    args_promote = parser.parse_args(
        ["linear-import", "promote", "--batch-id", batch_id]
    )
    operations_cli.run(args_promote, config)
    prom_out = capsys.readouterr().out
    prom_json = json.loads(prom_out)
    assert prom_json["state"] == "promoted"

    # Redaction: CLI output carries IDs, hashes, states, counts, and artifact paths only —
    # never source titles, descriptions, comment bodies, or attachment URLs.
    combined_output = stage_out + rec_out + prom_out
    for leaked in (
        "Summary of task",
        "Build the importer",
        "Plan approved",
        "Session review",
        "https://example.com/doc",
    ):
        assert leaked not in combined_output

    # A blocked batch reports exit code 2 through the CLI, at both stage and reconcile.
    def weird_state_nodes(
        operation: str, variables: dict[str, object], call_index: int
    ) -> list[dict[str, object]]:
        base = _sample_nodes(operation, {}, 0)
        if operation == "issues":
            return [
                {
                    **base[0],
                    "id": "issue-9",
                    "identifier": "HOME-150",
                    "url": "https://linear.app/issue/HOME-150",
                    "state": {"id": "state-weird", "type": "mystery", "name": "Weird"},
                },
                base[1],
            ]
        if operation == "workflowStates":
            return [
                *base,
                {
                    "id": "state-weird",
                    "name": "Weird",
                    "type": "mystery",
                    "position": 3,
                    "updatedAt": "2026-08-01T00:00:00+00:00",
                    "team": {"key": "HOME"},
                },
            ]
        return base

    blocked_export = StaticExportFixture(config, weird_state_nodes).full(workspace_id)
    blocked_args = parser.parse_args(
        [
            "linear-import",
            "stage",
            "--workspace-id",
            str(workspace_id),
            "--export-id",
            str(blocked_export.export_id),
            "--mapping-file",
            str(mapping_file),
        ]
    )
    with pytest.raises(SystemExit) as stage_exit:
        operations_cli.run(blocked_args, config)
    assert stage_exit.value.code == 2
    blocked_batch_id = json.loads(capsys.readouterr().out)["batch_id"]

    blocked_rec_args = parser.parse_args(
        ["linear-import", "reconcile", "--batch-id", blocked_batch_id]
    )
    with pytest.raises(SystemExit) as reconcile_exit:
        operations_cli.run(blocked_rec_args, config)
    assert reconcile_exit.value.code == 2
    assert json.loads(capsys.readouterr().out)["state"] == "blocked"
