from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from .v1.models import (
    Approval,
    BindingManifest,
    Contract,
    ContractExamples,
    StrictModel,
)
from .v1.semantics import validate_cutover_manifest, validate_examples

CONTRACT_VERSION = "work.omp.dev/v1"

_READS = frozenset(
    {
        "GET /v1/work-items/{key}",
        "GET /v1/work-items/{key}/workflow",
        "GET /v1/workspaces/{workspace_id}/tree",
        "GET /v1/workspaces/{workspace_id}/focus/{owner_id}",
        "GET /v1/operations/{operation_id}",
        "GET /v1/workspaces/{workspace_id}/authority",
        "GET /v1/workspaces/{workspace_id}/execution",
        "GET /v1/workspaces/{workspace_id}/execution/{grant_id}",
        "GET /v1/health/live",
        "GET /v1/health/ready",
    }
)
_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "unauthenticated",
        "forbidden",
        "approval_required",
        "revision_conflict",
        "idempotency_conflict",
        "relation_cycle",
        "focus_conflict",
        "stale_evidence",
        "completion_blocked",
        "contract_mismatch",
        "cutover_invariant",
        "unavailable",
        "execution_judge_drift",
        "execution_worktree_not_clean",
        "execution_grant_stale",
        "execution_grant_inactive",
        "execution_no_progress",
        "execution_caps_exceeded",
    }
)
_SCOPES = frozenset(
    {
        "work.read",
        "work.candidate.read",
        "work.mutate",
        "work.approve",
        "work.close",
        "work.execute",
        "work.import",
        "work.operate",
    }
)
_COMMAND_TYPES = frozenset(
    {
        "create_work_batch",
        "create_same_session_child",
        "revise_work",
        "set_work_state",
        "put_relation",
        "remove_relation",
        "set_focus",
        "clear_focus",
        "append_evidence",
        "finalize_candidate",
        "begin_close_attempt",
        "seal_audit_manifest",
        "reserve_auditor_launch",
        "cancel_auditor_launch",
        "settle_auditor_launch",
        "attest_checkpoint_delivery",
        "record_closeout_review",
        "complete_work",
        "record_project_health",
        "stage_import_batch",
        "promote_import_batch",
        "activate_cutover",
        "attest_cutover_plan",
        "begin_execution",
        "activate_execution_item",
        "seal_execution_criteria",
        "stamp_execution_plan",
        "set_execution_state",
        "complete_execution_item",
    }
)


def _contract_dir() -> Path:
    return Path(str(files("omp_work").joinpath("contracts/v1")))


def _load_json(name: str) -> dict[str, Any]:
    return json.loads((_contract_dir() / name).read_text())


def load_contract() -> Contract:
    return Contract.model_validate(_load_json("contract.json"))


def load_examples() -> ContractExamples:
    return ContractExamples.model_validate(_load_json("examples.json"))


def generate_schema() -> dict[str, object]:
    from .v1 import models

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "contract_version": CONTRACT_VERSION,
        "models": {
            name: model.model_json_schema()
            for name, model in vars(models).items()
            if isinstance(model, type)
            and hasattr(model, "model_json_schema")
            and issubclass(model, StrictModel)
        },
    }


def generate_api_schema() -> dict[str, object]:
    from .v1 import api_models

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "contract_version": CONTRACT_VERSION,
        "models": {
            name: model.model_json_schema()
            for name, model in vars(api_models).items()
            if isinstance(model, type)
            and hasattr(model, "model_json_schema")
            and issubclass(model, StrictModel)
        },
    }


def _manifest_paths() -> tuple[str, ...]:
    manifest = BindingManifest.model_validate(_load_json("manifest.json"))
    paths = list(manifest.paths)
    if paths != sorted(set(paths)) or "manifest.json" not in paths:
        raise ValueError("manifest paths must be sorted, unique, and include itself")
    for path in paths:
        if path.startswith("/") or ".." in Path(path).parts or path == "approval.json":
            raise ValueError("manifest contains unsafe path")
    return tuple(paths)


def contract_sha256() -> str:
    root = _contract_dir()
    digest = hashlib.sha256()
    for relative_path in _manifest_paths():
        data = (root / relative_path).read_bytes()
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).hexdigest().encode())
        digest.update(b"\n")
    return digest.hexdigest()


def validate_bundle(*, require_approval: bool = True) -> None:
    contract = load_contract()
    examples = load_examples()
    if contract.contract_version != CONTRACT_VERSION:
        raise ValueError("contract version mismatch")
    checked_schema = _load_json("schema.json")
    if checked_schema != generate_schema():
        raise ValueError("generated schema drift")
    checked_api_schema = _load_json("api-schema.json")
    if checked_api_schema != generate_api_schema():
        raise ValueError("generated API schema drift")
    paths = _manifest_paths()
    root = _contract_dir()
    for path in paths:
        if not (root / path).is_file():
            raise ValueError(f"missing binding file: {path}")
    if (
        frozenset(contract.reads) != _READS
        or frozenset(contract.command_types) != _COMMAND_TYPES
        or frozenset(contract.error_codes) != _ERROR_CODES
    ):
        raise ValueError("API reference closure failed")
    if frozenset(contract.scopes) != _SCOPES:
        raise ValueError("capability separation failed")
    policy = contract.security_policy
    if set(policy.database_roles) != {
        "omp_work_owner",
        "omp_work_migrator",
        "omp_work_app",
        "omp_work_importer",
        "omp_work_readonly",
        "omp_work_backup",
    } or set(policy.owner_host_scopes) != {
        "work.read",
        "work.mutate",
        "work.approve",
        "work.close",
        "work.execute",
    }:
        raise ValueError("capability separation failed")
    validate_examples(examples)
    validate_cutover_manifest(
        examples.cutover.anomalies, examples.cutover.parity_differences
    )
    approval_path = _contract_dir() / "approval.json"
    if require_approval:
        if not approval_path.is_file():
            raise ValueError("owner approval is required")
        approval = Approval.model_validate_json(approval_path.read_text())
        if (
            approval.contract_version != CONTRACT_VERSION
            or approval.contract_sha256 != contract_sha256()
        ):
            raise ValueError("approval hash mismatch")


__all__ = [
    "CONTRACT_VERSION",
    "contract_sha256",
    "generate_api_schema",
    "generate_schema",
    "load_contract",
    "load_examples",
    "validate_bundle",
]
