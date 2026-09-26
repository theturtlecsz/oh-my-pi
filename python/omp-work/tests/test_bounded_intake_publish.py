"""OMP-266 s06: publish_bounded_intake ratifies one ready bounded intake draft.

The suite pins the observable contract: an assess→admit→publish cycle mints one
work item, its related OMP-249 edge, a planned candidate, and an
intake_publication receipt atomically; an edited draft refuses stale_intake; a
draft that is not ready refuses intake_not_ready; a non-owner principal is
refused 403; a replay returns the stored result and mints nothing; and a fault
after the item is created rolls the whole transaction back to zero rows.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import omp_work
import psycopg
import pytest
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.canonical import sha256, text_sha256
from omp_work.v1.models import (
    BoundedIntakeDraft,
    Command,
    CommandEnvelope,
    FableAdvicePayload,
    IntakeAcceptanceCriterion,
    IntakeGoal,
    IntakeSource,
    IntakeSourceSpan,
    PublishBoundedIntakeCommand,
)
from omp_work.v1.semantics import (
    BOUNDED_INTAKE_RULE_BUNDLE_SHA256,
    bounded_intake_semantic_sha256,
)
from omp_work.v1.service import Principal, WorkError, WorkService
from omp_work.v1.store import PostgresWorkStore
from pg_native import native_postgres, seed_authority
from psycopg.rows import dict_row
from pydantic import TypeAdapter, ValidationError

OWNER = UUID("00000000-0000-7000-8000-0000000000a1")
OWNER_SCOPES = frozenset({"work.read", "work.mutate", "work.approve"})
SOURCE = (
    "Change the cache flag to false. Keep the value valid. "
    "Verify with an automated regression test."
)
GOAL_STATEMENT = "Change the cache flag"
OBSERVABLE_OUTCOME = "the automated regression test exits zero"


def _draft() -> BoundedIntakeDraft:
    span = IntakeSourceSpan(
        id="all",
        start=0,
        end=len(SOURCE.encode("utf-8")),
        exact_text_sha256=text_sha256(SOURCE),
    )
    return BoundedIntakeDraft(
        archetype="small_code_change",
        source=IntakeSource(text=SOURCE, sha256=text_sha256(SOURCE), spans=(span,)),
        goal=IntakeGoal(id="goal", statement=GOAL_STATEMENT, source_span_ids=("all",)),
        acceptance_criteria=(
            IntakeAcceptanceCriterion(
                id="test",
                statement="Automated regression test passes",
                observable_outcome=OBSERVABLE_OUTCOME,
                oracle="automated_test",
                source_span_ids=("all",),
            ),
        ),
    )


def _publish_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "draft": _draft().model_dump(mode="json"),
        "assessment_operation_id": str(uuid4()),
        "ratified_semantic_sha256": bounded_intake_semantic_sha256(_draft()),
        "admission_work_id": str(uuid4()),
        "admission_revision_id": str(uuid4()),
        "admission_receipt_id": str(uuid4()),
    }
    payload.update(overrides)
    return payload


def test_publish_command_parses_via_command_discriminator() -> None:
    payload = _publish_payload()
    command = TypeAdapter(Command).validate_python(
        {"type": "publish_bounded_intake", "payload": payload}
    )
    assert isinstance(command, PublishBoundedIntakeCommand)
    assert command.type == "publish_bounded_intake"
    assert str(command.payload.admission_receipt_id) == payload["admission_receipt_id"]


def test_publish_payload_rejects_missing_and_unknown_fields() -> None:
    missing = _publish_payload()
    missing.pop("admission_receipt_id")
    with pytest.raises(ValidationError):
        PublishBoundedIntakeCommand.model_validate(
            {"type": "publish_bounded_intake", "payload": missing}
        )

    unknown = _publish_payload()
    unknown["admission"] = {"work_id": str(uuid4())}
    with pytest.raises(ValidationError):
        PublishBoundedIntakeCommand.model_validate(
            {"type": "publish_bounded_intake", "payload": unknown}
        )


def test_publish_scope_is_work_approve() -> None:
    assert WorkService._scopes["publish_bounded_intake"] == "work.approve"


def test_contract_closure_includes_publish_bounded_intake() -> None:
    contract = omp_work.load_contract()
    assert "publish_bounded_intake" in contract.command_types
    assert "publish_bounded_intake" in omp_work._COMMAND_TYPES
    for code in ("intake_not_ready", "stale_intake"):
        assert code in contract.error_codes
        assert code in omp_work._ERROR_CODES


def _config(root: Path) -> OperationsConfig:
    credentials = root / "config" / "credentials"
    credentials.mkdir(parents=True, mode=0o700)
    for role in (
        "postgres",
        "omp_work_migrator",
        "omp_work_app",
        "omp_work_importer",
        "omp_work_readonly",
        "omp_work_backup",
        "gpg-passphrase",
        "operator-actor-id",
    ):
        path = credentials / role
        path.write_text(secrets.token_urlsafe(24))
        path.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return OperationsConfig(
        config_dir=root / "config",
        state_dir=root / "state",
        data_dir=root / "data",
        port=port,
    )


def _envelope(
    workspace_id: UUID,
    command: dict[str, object],
    *,
    operation_id: UUID | None = None,
) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(workspace_id),
            "operation_id": str(operation_id or uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": command,
        }
    )


class _Seeded:
    def __init__(
        self,
        workspace_id: UUID,
        work_id: UUID,
        revision_id: UUID,
        candidate_id: UUID,
        plan_id: UUID,
        advice_id: UUID,
        audit_id: UUID,
        promotion_id: UUID,
        floor_id: UUID,
    ) -> None:
        self.workspace_id = workspace_id
        self.work_id = work_id
        self.revision_id = revision_id
        self.candidate_id = candidate_id
        self.plan_id = plan_id
        self.advice_id = advice_id
        self.audit_id = audit_id
        self.promotion_id = promotion_id
        self.floor_id = floor_id

    def attest_payload(self) -> dict[str, object]:
        return {
            "work_id": str(self.work_id),
            "revision_id": str(self.revision_id),
            "plan_receipt_id": str(self.plan_id),
            "fable_advice_receipt_id": str(self.advice_id),
            "native_acceptance_receipt_id": str(self.audit_id),
        }


def _insert_receipt(
    config: OperationsConfig,
    *,
    workspace_id: UUID,
    work_id: UUID,
    revision_id: UUID,
    candidate_id: UUID,
    receipt_id: UUID,
    kind: str,
    body: dict[str, object],
    issuer: str,
    verdict: str | None = None,
    independent: bool | None = None,
) -> None:
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "INSERT INTO omp_evidence.receipts(receipt_id,workspace_id,work_id,revision_id,candidate_id,kind,payload,payload_sha256,issuer,issued_at,candidate_sha256,candidate_commit,verdict,independent) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                receipt_id,
                workspace_id,
                work_id,
                revision_id,
                candidate_id,
                kind,
                json.dumps(body),
                sha256(body),
                issuer,
                datetime.now(UTC),
                "b" * 64,
                "c" * 40,
                verdict,
                independent,
            ),
        )


def _seed_omp249(
    config: OperationsConfig, workspace_id: UUID, draft: BoundedIntakeDraft
) -> _Seeded:
    """Direct-SQL seed of OMP-249's current lineage with the plan, fresh Fable
    advice, native PASS audit, and the s05 admission inputs it must re-read."""
    work_id, revision_id, candidate_id = uuid4(), uuid4(), uuid4()
    plan_id, advice_id, audit_id = uuid4(), uuid4(), uuid4()
    promotion_id, floor_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    with psycopg.connect(
        **config.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
    seed_authority(config.connection_kwargs("postgres"), workspace_id, OWNER)
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "INSERT INTO omp_work.work_items(work_id,workspace_id,state,current_revision_id,current_candidate_id) VALUES(%s,%s,'DONE',%s,%s)",
            (work_id, workspace_id, revision_id, candidate_id),
        )
        conn.execute(
            "INSERT INTO omp_work.work_aliases(work_id,workspace_id,key,origin) VALUES(%s,%s,'OMP-249','local')",
            (work_id, workspace_id),
        )
        conn.execute(
            "INSERT INTO omp_work.work_revisions(revision_id,work_id,workspace_id,revision_number,title,description,scope,content_sha256,created_by,supplied_at) VALUES(%s,%s,%s,1,'Initial promotion','qualified','P11',%s,'service',%s)",
            (revision_id, work_id, workspace_id, "a" * 64, now),
        )
        conn.execute(
            "INSERT INTO omp_work.candidates(candidate_id,workspace_id,work_id,revision_id,candidate_sha256,commit_sha,kind,allocated_at) VALUES(%s,%s,%s,%s,%s,%s,'final',%s)",
            (candidate_id, workspace_id, work_id, revision_id, "b" * 64, "c" * 40, now),
        )
    seeded = _Seeded(
        workspace_id,
        work_id,
        revision_id,
        candidate_id,
        plan_id,
        advice_id,
        audit_id,
        promotion_id,
        floor_id,
    )
    _insert_receipt(
        config,
        workspace_id=workspace_id,
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        receipt_id=plan_id,
        kind="plan",
        body={"plan": "current P11"},
        issuer="owner",
    )
    _insert_receipt(
        config,
        workspace_id=workspace_id,
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        receipt_id=advice_id,
        kind="verification",
        body=FableAdvicePayload(
            advice_sha256="1" * 64,
            intake_semantic_sha256=bounded_intake_semantic_sha256(draft),
            rule_bundle_sha256=BOUNDED_INTAKE_RULE_BUNDLE_SHA256,
        ).model_dump(mode="json"),
        issuer="work-service/fable-advisor",
    )
    _insert_receipt(
        config,
        workspace_id=workspace_id,
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        receipt_id=audit_id,
        kind="audit",
        body={"report": "VERDICT: PASS"},
        issuer="work-service/auditor-settle",
        verdict="PASS",
        independent=True,
    )
    _insert_receipt(
        config,
        workspace_id=workspace_id,
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        receipt_id=promotion_id,
        kind="verification",
        body={
            "phase": "initial_p11_promotion",
            "qualified": True,
            "natively_accepted": True,
            "plan_receipt_id": str(plan_id),
        },
        issuer="service",
    )
    _insert_receipt(
        config,
        workspace_id=workspace_id,
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        receipt_id=floor_id,
        kind="verification",
        body={
            "deterministic_floor_passed": True,
            "rule_bundle_sha256": BOUNDED_INTAKE_RULE_BUNDLE_SHA256,
        },
        issuer="service",
    )
    return seeded


def _row_counts(config: OperationsConfig, workspace_id: UUID) -> dict[str, int]:
    with (
        psycopg.connect(
            **config.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn,
        conn.cursor() as cur,
    ):
        counts: dict[str, int] = {}
        for label, sql in (
            (
                "items",
                "SELECT count(*) AS n FROM omp_work.work_items WHERE workspace_id=%s",
            ),
            (
                "relations",
                "SELECT count(*) AS n FROM omp_work.work_relations WHERE workspace_id=%s",
            ),
            (
                "candidates",
                "SELECT count(*) AS n FROM omp_work.candidates WHERE workspace_id=%s",
            ),
            (
                "publications",
                "SELECT count(*) AS n FROM omp_evidence.receipts WHERE workspace_id=%s AND kind='intake_publication'",
            ),
        ):
            cur.execute(sql, (workspace_id,))
            counts[label] = cur.fetchone()["n"]
        return counts


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_publish_bounded_intake_integration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        monkeypatch.setattr(
            "omp_work.operations.database.validate_bundle", lambda **kw: None
        )
        bootstrap(config)
        draft = _draft()
        main = _seed_omp249(config, uuid4(), draft)
        fault = _seed_omp249(config, uuid4(), draft)

        store = PostgresWorkStore(config)
        service = WorkService(store)
        owner = Principal(
            actor_id=OWNER,
            actor_kind="owner",
            workspaces=frozenset({main.workspace_id, fault.workspace_id}),
            scopes=OWNER_SCOPES,
        )

        def execute(
            seeded: _Seeded,
            command: dict[str, object],
            *,
            operation_id: UUID | None = None,
        ) -> tuple[object, dict[str, object]]:
            return service.execute(
                owner,
                _envelope(seeded.workspace_id, command, operation_id=operation_id),
            )

        # Assess: a ready draft yields an applied positive assessment we ratify.
        assessment_operation_id = uuid4()
        _, assessment = execute(
            main,
            {
                "type": "assess_bounded_intake",
                "payload": {"draft": draft.model_dump(mode="json")},
            },
            operation_id=assessment_operation_id,
        )
        assert assessment["ready_for_ratification"] is True
        assert assessment["semantic_sha256"] == bounded_intake_semantic_sha256(draft)

        # Admit: the s05 native gate mints the admission receipt on OMP-249's lineage.
        _, admission = execute(
            main,
            {"type": "attest_intake_admission", "payload": main.attest_payload()},
        )
        admission_receipt_id = admission["receipt"]["receipt_id"]

        publish_command = {
            "type": "publish_bounded_intake",
            "payload": {
                "draft": draft.model_dump(mode="json"),
                "assessment_operation_id": str(assessment_operation_id),
                "ratified_semantic_sha256": assessment["semantic_sha256"],
                "admission_work_id": str(main.work_id),
                "admission_revision_id": str(main.revision_id),
                "admission_receipt_id": admission_receipt_id,
            },
        }

        # 1. Publish mints the item, related edge, planned candidate, and receipt.
        publish_operation_id = uuid4()
        receipt, result = execute(
            main, publish_command, operation_id=publish_operation_id
        )
        assert receipt.state.value == "applied"
        assert result["type"] == "publish_bounded_intake"
        item = result["item"]
        assert item["client_ref"] == "bounded-intake"
        assert item["state"] == "BACKLOG"
        new_work_id = UUID(item["work_id"])
        new_revision_id = UUID(item["revision_id"])

        minted = result["receipt"]
        assert minted["kind"] == "intake_publication"
        assert minted["issuer"] == "work-service/bounded-intake"
        assert minted["work_id"] == str(new_work_id)
        assert minted["artifact_sha256"] == draft.source.sha256
        assert minted["payload_sha256"] == sha256(minted["payload"])
        body = minted["payload"]
        assert body["ratified_by"] == str(OWNER)
        assert body["assessment_operation_id"] == str(assessment_operation_id)
        assert body["admission_receipt_id"] == admission_receipt_id
        assert body["rule_bundle_sha256"] == BOUNDED_INTAKE_RULE_BUNDLE_SHA256
        assert body["semantic_sha256"] == bounded_intake_semantic_sha256(draft)
        assert body["draft"]["source"]["text"] == SOURCE
        assert body["draft"]["source"]["spans"] == [
            {
                "id": "all",
                "start": 0,
                "end": len(SOURCE.encode("utf-8")),
                "exact_text_sha256": text_sha256(SOURCE),
            }
        ]

        with (
            psycopg.connect(
                **config.connection_kwargs("postgres"), row_factory=dict_row
            ) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT title,description,scope FROM omp_work.work_revisions WHERE revision_id=%s",
                (new_revision_id,),
            )
            revision = cur.fetchone()
            assert revision["title"] == GOAL_STATEMENT
            assert revision["description"] == SOURCE
            assert revision["scope"] == "small_code_change"
            cur.execute(
                "SELECT criterion FROM omp_work.acceptance_criteria WHERE revision_id=%s ORDER BY position",
                (new_revision_id,),
            )
            assert [row["criterion"] for row in cur.fetchall()] == [OBSERVABLE_OUTCOME]
            cur.execute(
                "SELECT candidate_sha256,commit_sha,kind FROM omp_work.candidates WHERE work_id=%s",
                (new_work_id,),
            )
            candidate = cur.fetchone()
            assert candidate["kind"] == "planned"
            assert candidate["commit_sha"] is None
            assert candidate["candidate_sha256"] == bounded_intake_semantic_sha256(
                draft
            )
            cur.execute(
                "SELECT source_work_id,target_work_id,kind,active FROM omp_work.work_relations WHERE workspace_id=%s AND kind='related'",
                (main.workspace_id,),
            )
            relations = cur.fetchall()
            assert len(relations) == 1
            source, target = sorted((new_work_id, main.work_id), key=str)
            assert relations[0]["source_work_id"] == source
            assert relations[0]["target_work_id"] == target
            assert relations[0]["active"] is True

        # 2. Replay returns the stored result and mints exactly one item.
        replay_receipt, replay_result = execute(
            main, publish_command, operation_id=publish_operation_id
        )
        assert replay_receipt.state.value == "replayed"
        assert replay_result == result
        assert _row_counts(config, main.workspace_id) == {
            "items": 2,
            "relations": 1,
            "candidates": 2,
            "publications": 1,
        }

        # 3. Edited draft (ratified to its own new hash) no longer matches the
        #    assessment operation -> stale_intake, no new rows.
        edited = draft.model_copy(
            update={
                "goal": draft.goal.model_copy(
                    update={"statement": "Change the cache flag now"}
                )
            }
        )
        edited_command = {
            **publish_command,
            "payload": {
                **publish_command["payload"],
                "draft": edited.model_dump(mode="json"),
                "ratified_semantic_sha256": bounded_intake_semantic_sha256(edited),
            },
        }
        with pytest.raises(WorkError, match="stale_intake") as stale:
            execute(main, edited_command)
        assert stale.value.status == 409

        # 4. An assessment operation that was never applied -> stale_intake.
        missing_assessment = {
            **publish_command,
            "payload": {
                **publish_command["payload"],
                "assessment_operation_id": str(uuid4()),
            },
        }
        with pytest.raises(WorkError, match="stale_intake"):
            execute(main, missing_assessment)

        # 5. Not-ready draft (criterion without an oracle) -> intake_not_ready.
        not_ready = draft.model_copy(
            update={
                "acceptance_criteria": (
                    draft.acceptance_criteria[0].model_copy(update={"oracle": None}),
                )
            }
        )
        not_ready_command = {
            **publish_command,
            "payload": {
                **publish_command["payload"],
                "draft": not_ready.model_dump(mode="json"),
                "ratified_semantic_sha256": bounded_intake_semantic_sha256(not_ready),
            },
        }
        with pytest.raises(WorkError, match="intake_not_ready") as not_ready_error:
            execute(main, not_ready_command)
        assert not_ready_error.value.status == 409

        # 6. Admission lineage that does not match OMP-249's current revision.
        for field, value in (
            ("admission_work_id", str(uuid4())),
            ("admission_revision_id", str(uuid4())),
            ("admission_receipt_id", str(uuid4())),
        ):
            mismatched = {
                **publish_command,
                "payload": {**publish_command["payload"], field: value},
            }
            with pytest.raises(WorkError, match="intake_admission_blocked") as blocked:
                execute(main, mismatched)
            assert blocked.value.status == 409, field

        # 7. Non-owner principal is refused (403) before any row is written.
        non_owner = Principal(
            actor_id=uuid4(),
            actor_kind="task-agent",
            workspaces=frozenset({main.workspace_id}),
            scopes=OWNER_SCOPES,
        )
        with pytest.raises(WorkError, match="forbidden") as forbidden:
            service.execute(non_owner, _envelope(main.workspace_id, publish_command))
        assert forbidden.value.status == 403
        assert _row_counts(config, main.workspace_id) == {
            "items": 2,
            "relations": 1,
            "candidates": 2,
            "publications": 1,
        }

        # 8. A fault inside the receipt mint rolls the whole transaction back: the
        #    item, relation, and candidate never survive without their receipt.
        fault_assessment_operation_id = uuid4()
        _, fault_assessment = execute(
            fault,
            {
                "type": "assess_bounded_intake",
                "payload": {"draft": draft.model_dump(mode="json")},
            },
            operation_id=fault_assessment_operation_id,
        )
        _, fault_admission = execute(
            fault,
            {"type": "attest_intake_admission", "payload": fault.attest_payload()},
        )
        fault_command = {
            "type": "publish_bounded_intake",
            "payload": {
                "draft": draft.model_dump(mode="json"),
                "assessment_operation_id": str(fault_assessment_operation_id),
                "ratified_semantic_sha256": fault_assessment["semantic_sha256"],
                "admission_work_id": str(fault.work_id),
                "admission_revision_id": str(fault.revision_id),
                "admission_receipt_id": fault_admission["receipt"]["receipt_id"],
            },
        }

        def _mint_fault(*args: object, **kwargs: object) -> object:
            raise RuntimeError("injected mint fault")

        monkeypatch.setattr(store, "_mint_intake_publication_receipt", _mint_fault)
        with pytest.raises(RuntimeError, match="injected mint fault"):
            execute(fault, fault_command)
        monkeypatch.undo()
        assert _row_counts(config, fault.workspace_id) == {
            "items": 1,
            "relations": 0,
            "candidates": 1,
            "publications": 0,
        }
