"""Repository-only NSI-6 proof fixtures. Writes confined to disposable PostgreSQL.

Evaluation uses a read-only transaction; fixture mutation never reaches a live
ledger. Existing native launcher and repository fixture helpers own lifecycle.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql

from omp_work.contracts.nsi.v1.compat import bounded_to_nsi, intake_contract, nsi_bounded_evaluation
from omp_work.contracts.nsi.v1.models import Binding
from omp_work.intake_repository import DecisionHistory, IntakeRevisionBody, append_revision, lock_revision, read_revision
from omp_work.operations.database import bootstrap
from omp_work.v1.api_models import IntakeCriterionLink
from omp_work.v1.intake_provenance import evaluate_provenance
from omp_work.v1.models import BoundedIntakeDraft
from omp_work.v1.store_shared import WorkStoreError

# This offline dev command deliberately reuses source-checkout fixture helpers.
TESTS = Path(__file__).resolve().parents[2] / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
from installed_runtime_support import ReservedPort
from pg_native import native_postgres
from test_nsi2_provenance_closure import _entities, _retarget, advance, insert_event, seal, sync_links
from test_nsi2_repository import connection, write_repository
from test_workflow_service import _config


def implementation_hashes() -> dict[str, str]:
    names = ("omp_work.nsi_eval_postgres", "omp_work.frozen_corpus", "omp_work.intake_repository",
             "omp_work.operations.database", "omp_work.v1.canonical", "omp_work.v1.store_shared", "pg_native", "installed_runtime_support",
             "test_nsi2_provenance_closure", "test_nsi2_repository", "test_workflow_service", "test_project_store")
    result = {name: hashlib.sha256(Path(sys.modules[name].__file__).read_bytes()).hexdigest() for name in names}
    result["pg_supervisor.py"] = hashlib.sha256((TESTS / "pg_supervisor.py").read_bytes()).hexdigest()
    return result


def _body(raw: dict, workspace_id) -> IntakeRevisionBody:
    recipe = raw["nsi2_proof_scenario"]
    draft = BoundedIntakeDraft.model_validate(raw["bounded"]["draft"])
    if (draft.source.text != recipe["source_bytes_utf8"] or draft.source.sha256 != recipe["source_sha256"]):
        raise ValueError("proof recipe source differs from bounded input")
    snapshot = bounded_to_nsi(draft)
    snapshot.run_id, snapshot.revision = recipe["run_id"], recipe["revision"]
    snapshot.artifacts[0].revision = recipe.get("source_revision", "source-v1")
    snapshot.claims = _retarget(snapshot.claims)
    snapshot.entities = _entities(snapshot.claims)
    contract = intake_contract(snapshot)
    findings = nsi_bounded_evaluation(snapshot)
    links = [IntakeCriterionLink(criterion=criterion, requirements=tuple(
        requirement for requirement in contract.requirements if requirement.id in criterion.requirement_ids
    )) for criterion in contract.acceptance_criteria]
    return IntakeRevisionBody(
        workspace_id=workspace_id, snapshot=snapshot,
        binding=Binding(run_id=snapshot.run_id, revision=snapshot.revision,
                        **{name: "0" * 64 for name in ("contract_hash", "claim_set_hash", "source_hash", "rule_bundle_hash", "schema_hash")}),
        artifact_bytes={recipe["source_artifact_id"]: recipe["source_bytes_utf8"].encode()},
        criteria=contract.acceptance_criteria, history=DecisionHistory(criteria=links),
        gaps=list(findings.gaps), conflicts=list(findings.conflicts), questions=list(findings.questions),
    )


def _append(cur, repository, body, expected):
    workspace_id, mission_id = repository
    operation_id = uuid4()
    locked = lock_revision(cur, workspace_id=workspace_id, mission_id=mission_id,
                           run_id=body.snapshot.run_id, expected_revision=expected, operation_id=operation_id)
    append_revision(cur, locked, seal(sync_links(body)), operation_id=operation_id)
    insert_event(cur, workspace_id, mission_id, operation_id, {"type": "intake_revision"}, "intake_revision")


def evaluate_fixture(service, raw: dict) -> dict:
    """Persist genuine proof, pin it, mutate disposable records, read-only replay."""
    recipe = raw["nsi2_proof_scenario"]
    if recipe["recompute_hashes"] is not False:
        raise ValueError("shadow mutations must retain original hashes")
    repository = write_repository.__wrapped__(service)
    workspace_id, _ = repository
    body = _body(raw, workspace_id)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        _append(cur, repository, body, -1)
    with connection(service, workspace_id, "omp_work_readonly") as conn:
        conn.commit()
        conn.execute("SET TRANSACTION READ ONLY")
        with conn.cursor() as cur:
            pinned = read_revision(cur, workspace_id=workspace_id, run_id=recipe["run_id"], revision=recipe["revision"]).body
            baseline = evaluate_provenance(cur, pinned)
    if any(m["table"] == "omp_work.intake_heads" for m in recipe["mutations"]):
        with connection(service, workspace_id) as conn, conn.cursor() as cur:
            _append(cur, repository, advance(pinned, 1), 0)
    # Only the disposable administrator can mutate immutable fixture evidence.
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        conn.execute("SET session_replication_role = replica")
        for mutation in recipe["mutations"]:
            if mutation["table"] == "omp_work.intake_heads":
                if mutation["field_path"] != ["current_revision"] or (mutation["before"], mutation["after"]) != (0, 1):
                    raise ValueError("unsupported head mutation")
                continue  # Already advanced through the real append-only repository.
            if mutation["table"] != "omp_work.intake_artifacts" or mutation["field_path"] not in (["source_revision"], ["content_hash"]):
                raise ValueError("unsupported proof mutation")
            key = mutation["row_key"]
            if key != {"run_id": recipe["run_id"], "revision": recipe["revision"], "artifact_id": recipe["source_artifact_id"]}:
                raise ValueError("proof mutation row mismatch")
            field = mutation["field_path"][0]
            cur = conn.execute(sql.SQL(
                "UPDATE omp_work.intake_artifacts SET {}=%s WHERE workspace_id=%s AND run_id=%s AND revision=%s AND artifact_id=%s AND {}=%s"
            ).format(sql.Identifier(field), sql.Identifier(field)),
                (mutation["after"], workspace_id, key["run_id"], key["revision"], key["artifact_id"], mutation["before"]))
            if cur.rowcount != 1:
                raise ValueError("proof mutation preimage mismatch")
    with connection(service, workspace_id, "omp_work_readonly") as conn:
        conn.commit()
        conn.execute("SET TRANSACTION READ ONLY")
        with conn.cursor() as cur:
            current = cur.execute("SELECT current_revision FROM omp_work.intake_heads WHERE workspace_id=%s AND run_id=%s",
                                  (workspace_id, recipe["run_id"])).fetchone()[0]
            evidence = cur.execute(
                "SELECT artifact_id,source_revision,content_hash FROM omp_work.intake_artifacts "
                "WHERE workspace_id=%s AND run_id=%s AND revision=%s ORDER BY artifact_id",
                (workspace_id, recipe["run_id"], recipe["revision"]),
            ).fetchall()
            observed = {"baseline_binding": baseline.binding.model_dump(mode="json"),
                        "pinned_body": pinned.model_dump(mode="json"),
                        "persisted_artifacts": evidence, "current_revision": current,
                        "pinned_revision": pinned.snapshot.revision}
            try:
                result = evaluate_provenance(cur, pinned)
            except WorkStoreError as exc:
                return {"status": "refused", "code": exc.code, "diagnostics": list(exc.diagnostics),
                        "provenance_valid": False, "raw": observed}
            return {"readiness": result.assessment.status,
                    "current_revision": current, "pinned_revision": pinned.snapshot.revision,
                    "stale_revision": current != pinned.snapshot.revision,
                    "binding_matches_baseline": result.binding == baseline.binding,
                    "provenance_valid": current == pinned.snapshot.revision and result.binding == baseline.binding,
                    "diagnostics": ["stale revision"] if current != pinned.snapshot.revision else [],
                    "raw": {**observed, "assessment": result.assessment.model_dump(mode="json"),
                            "receipts": [r.model_dump(mode="json") for r in result.receipts],
                            "closure": result.closure.closure.model_dump(mode="json")}}


@contextmanager
def disposable_provenance():
    # Never accept an environment DSN or existing database path.
    with TemporaryDirectory(prefix=".nsi6-", dir=TESTS.parents[2]) as temp, ReservedPort() as reserve:
        root = Path(temp)
        config = _config(root)
        # OperationsConfig is a dataclass; reuse fixture config with reserved port.
        config = replace(config, port=reserve.port)
        with native_postgres(root, reserve.port, reserve=reserve):
            # Same uninstalled dev-bundle seam as existing PostgreSQL fixtures.
            with patch("omp_work.operations.database.validate_bundle"):
                bootstrap(config)
            capabilities = root / "capabilities"
            capabilities.mkdir(mode=0o700)
            (capabilities / "owner.json").write_text(json.dumps({"workspaces": []}))
            service = SimpleNamespace(config=config, capabilities=capabilities)
            yield lambda raw: evaluate_fixture(service, raw)
