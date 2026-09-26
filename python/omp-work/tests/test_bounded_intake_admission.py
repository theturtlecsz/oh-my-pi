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
from omp_work.v1.canonical import sha256
from omp_work.v1.models import (
    AttestIntakeAdmissionCommand,
    Command,
    CommandEnvelope,
    FableAdvicePayload,
    IntakeAdmissionReceiptPayload,
    OperationReceipt,
)
from omp_work.v1.semantics import BOUNDED_INTAKE_RULE_BUNDLE_SHA256
from omp_work.v1.service import Principal, WorkError, WorkService
from omp_work.v1.store import PostgresWorkStore
from pg_native import native_postgres, seed_authority
from psycopg.rows import dict_row
from pydantic import TypeAdapter, ValidationError

OWNER = UUID("00000000-0000-7000-8000-0000000000a1")
OWNER_SCOPES = frozenset({"work.read", "work.mutate", "work.approve"})


def _valid_payload() -> dict[str, object]:
    return {
        "work_id": str(uuid4()),
        "revision_id": str(uuid4()),
        "plan_receipt_id": str(uuid4()),
        "fable_advice_receipt_id": str(uuid4()),
        "native_acceptance_receipt_id": str(uuid4()),
    }


def test_attest_intake_admission_parses_via_command_discriminator() -> None:
    payload = _valid_payload()
    command = TypeAdapter(Command).validate_python(
        {"type": "attest_intake_admission", "payload": payload}
    )
    assert isinstance(command, AttestIntakeAdmissionCommand)
    assert command.type == "attest_intake_admission"
    assert str(command.payload.plan_receipt_id) == payload["plan_receipt_id"]


def test_attest_intake_admission_payload_rejects_missing_and_unknown_fields() -> None:
    missing = _valid_payload()
    missing.pop("native_acceptance_receipt_id")
    with pytest.raises(ValidationError):
        AttestIntakeAdmissionCommand.model_validate(
            {"type": "attest_intake_admission", "payload": missing}
        )

    unknown = _valid_payload()
    unknown["qualified"] = True
    with pytest.raises(ValidationError):
        AttestIntakeAdmissionCommand.model_validate(
            {"type": "attest_intake_admission", "payload": unknown}
        )


def test_intake_admission_receipt_payload_pins_flags_true() -> None:
    receipt = IntakeAdmissionReceiptPayload.model_validate(
        {
            **_valid_payload(),
            "qualified": True,
            "natively_accepted": True,
            "deterministic_floor_passed": True,
            "rule_bundle_sha256": BOUNDED_INTAKE_RULE_BUNDLE_SHA256,
            "operator_actor_id": str(uuid4()),
        }
    )
    assert receipt.qualified is True
    assert receipt.deterministic_floor_passed is True
    with pytest.raises(ValidationError):
        IntakeAdmissionReceiptPayload.model_validate(
            {
                **_valid_payload(),
                "qualified": False,
                "natively_accepted": True,
                "deterministic_floor_passed": True,
                "rule_bundle_sha256": BOUNDED_INTAKE_RULE_BUNDLE_SHA256,
                "operator_actor_id": str(uuid4()),
            }
        )


def test_attest_intake_admission_scope_is_work_approve() -> None:
    assert WorkService._scopes["attest_intake_admission"] == "work.approve"


def test_contract_closure_includes_attest_intake_admission() -> None:
    contract = omp_work.load_contract()
    assert "attest_intake_admission" in contract.command_types
    assert "attest_intake_admission" in omp_work._COMMAND_TYPES
    assert "intake_admission_blocked" in contract.error_codes
    assert "intake_admission_blocked" in omp_work._ERROR_CODES


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


def _attest_command(payload: dict[str, object]) -> dict[str, object]:
    return {"type": "attest_intake_admission", "payload": payload}


class _Seeded:
    def __init__(
        self,
        workspace_id: UUID,
        work_id: UUID,
        revision_id: UUID,
        candidate_id: UUID,
        second_candidate_id: UUID,
        plan_id: UUID,
        advice_id: UUID,
        audit_id: UUID,
    ) -> None:
        self.workspace_id = workspace_id
        self.work_id = work_id
        self.revision_id = revision_id
        self.candidate_id = candidate_id
        self.second_candidate_id = second_candidate_id
        self.plan_id = plan_id
        self.advice_id = advice_id
        self.audit_id = audit_id

    def payload(self) -> dict[str, object]:
        return {
            "work_id": str(self.work_id),
            "revision_id": str(self.revision_id),
            "plan_receipt_id": str(self.plan_id),
            "fable_advice_receipt_id": str(self.advice_id),
            "native_acceptance_receipt_id": str(self.audit_id),
        }

    def add_receipt(
        self,
        config: OperationsConfig,
        *,
        kind: str,
        body: dict[str, object],
        issuer: str,
        receipt_id: UUID | None = None,
        verdict: str | None = None,
        independent: bool | None = None,
        candidate_id: UUID | None = None,
        payload_sha256: str | None = None,
    ) -> UUID:
        receipt_id = receipt_id or uuid4()
        with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
            conn.execute(
                "INSERT INTO omp_evidence.receipts(receipt_id,workspace_id,work_id,revision_id,candidate_id,kind,payload,payload_sha256,issuer,issued_at,candidate_sha256,candidate_commit,verdict,independent) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    receipt_id,
                    self.workspace_id,
                    self.work_id,
                    self.revision_id,
                    candidate_id or self.candidate_id,
                    kind,
                    json.dumps(body),
                    payload_sha256 or sha256(body),
                    issuer,
                    datetime.now(UTC),
                    "b" * 64,
                    "c" * 40,
                    verdict,
                    independent,
                ),
            )
        return receipt_id


def _seed_workspace(
    config: OperationsConfig,
    workspace_id: UUID,
    *,
    promotion: str = "correct",
    floor: bool = True,
) -> _Seeded:
    """Direct-SQL seed of an OMP-249 alias, current final candidate, and the native
    admission receipt set. `promotion` is one of correct/wrong_plan/absent."""
    work_id, revision_id, candidate_id = uuid4(), uuid4(), uuid4()
    second_candidate_id = uuid4()
    plan_id, advice_id, audit_id = uuid4(), uuid4(), uuid4()
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
        conn.execute(
            "INSERT INTO omp_work.candidates(candidate_id,workspace_id,work_id,revision_id,candidate_sha256,commit_sha,kind,allocated_at) VALUES(%s,%s,%s,%s,%s,%s,'planned',%s)",
            (
                second_candidate_id,
                workspace_id,
                work_id,
                revision_id,
                "d" * 64,
                "e" * 40,
                now,
            ),
        )
    seeded = _Seeded(
        workspace_id,
        work_id,
        revision_id,
        candidate_id,
        second_candidate_id,
        plan_id,
        advice_id,
        audit_id,
    )
    seeded.add_receipt(
        config,
        receipt_id=plan_id,
        kind="plan",
        body={"plan": "current P11"},
        issuer="owner",
    )
    seeded.add_receipt(
        config,
        receipt_id=advice_id,
        kind="verification",
        body=FableAdvicePayload(
            advice_sha256="1" * 64,
            intake_semantic_sha256="2" * 64,
            rule_bundle_sha256=BOUNDED_INTAKE_RULE_BUNDLE_SHA256,
        ).model_dump(mode="json"),
        issuer="work-service/fable-advisor",
    )
    seeded.add_receipt(
        config,
        receipt_id=audit_id,
        kind="audit",
        body={"report": "VERDICT: PASS"},
        issuer="work-service/auditor-settle",
        verdict="PASS",
        independent=True,
    )
    if promotion != "absent":
        seeded.add_receipt(
            config,
            kind="verification",
            body={
                "phase": "initial_p11_promotion",
                "qualified": True,
                "natively_accepted": True,
                "plan_receipt_id": str(
                    plan_id if promotion == "correct" else uuid4()
                ),
            },
            issuer="service",
        )
    if floor:
        seeded.add_receipt(
            config,
            kind="verification",
            body={
                "deterministic_floor_passed": True,
                "rule_bundle_sha256": BOUNDED_INTAKE_RULE_BUNDLE_SHA256,
            },
            issuer="service",
        )
    return seeded


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_attest_intake_admission_integration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        monkeypatch.setattr(
            "omp_work.operations.database.validate_bundle", lambda **kw: None
        )
        bootstrap(config)
        main = _seed_workspace(config, uuid4())
        no_promotion = _seed_workspace(config, uuid4(), promotion="wrong_plan")
        no_floor = _seed_workspace(config, uuid4(), floor=False)

        store = PostgresWorkStore(config)
        service = WorkService(store)
        owner = Principal(
            actor_id=OWNER,
            actor_kind="owner",
            workspaces=frozenset(
                {
                    main.workspace_id,
                    no_promotion.workspace_id,
                    no_floor.workspace_id,
                }
            ),
            scopes=OWNER_SCOPES,
        )

        def execute(
            seeded: _Seeded,
            command: dict[str, object],
            *,
            operation_id: UUID | None = None,
        ) -> tuple[OperationReceipt, dict[str, object]]:
            return service.execute(
                owner,
                _envelope(
                    seeded.workspace_id, command, operation_id=operation_id
                ),
            )

        def blocked(
            seeded: _Seeded, command: dict[str, object], label: str
        ) -> None:
            with pytest.raises(WorkError, match="intake_admission_blocked") as error:
                execute(seeded, command)
            assert error.value.status == 409, label

        # 1. Full valid receipt set on OMP-249's current lineage admits.
        valid_operation = uuid4()
        receipt, result = execute(
            main, _attest_command(main.payload()), operation_id=valid_operation
        )
        assert receipt.state.value == "applied"
        assert result["type"] == "attest_intake_admission"
        assert result["operator_actor_id"] == str(OWNER)
        minted = result["receipt"]
        assert minted["kind"] == "intake_admission"
        assert minted["issuer"] == "work-service/intake-admission"
        assert minted["candidate_id"] == str(main.candidate_id)
        assert minted["candidate_sha256"] == "b" * 64
        assert minted["candidate_commit"] == "c" * 40
        assert minted["payload"]["qualified"] is True
        assert minted["payload"]["natively_accepted"] is True
        assert minted["payload"]["deterministic_floor_passed"] is True
        assert (
            minted["payload"]["rule_bundle_sha256"]
            == BOUNDED_INTAKE_RULE_BUNDLE_SHA256
        )
        assert minted["payload"]["operator_actor_id"] == str(OWNER)
        assert minted["payload"]["plan_receipt_id"] == str(main.plan_id)
        assert minted["payload_sha256"] == sha256(minted["payload"])
        minted_id = minted["receipt_id"]

        # 2. Replay is idempotent: same receipt_id, exactly one intake_admission row.
        replay_receipt, replay_result = execute(
            main, _attest_command(main.payload()), operation_id=valid_operation
        )
        assert replay_receipt.state.value == "replayed"
        assert replay_result == result
        with psycopg.connect(
            **config.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS count FROM omp_evidence.receipts WHERE workspace_id=%s AND kind='intake_admission'",
                    (main.workspace_id,),
                )
                assert cur.fetchone()["count"] == 1
                cur.execute(
                    "SELECT receipt_id FROM omp_evidence.receipts WHERE workspace_id=%s AND kind='intake_admission'",
                    (main.workspace_id,),
                )
                assert cur.fetchone()["receipt_id"] == UUID(minted_id)

        # 3. Each named receipt missing from the lineage -> blocked.
        for field in (
            "plan_receipt_id",
            "fable_advice_receipt_id",
            "native_acceptance_receipt_id",
        ):
            payload = main.payload()
            payload[field] = str(uuid4())
            blocked(main, _attest_command(payload), f"missing {field}")

        # 4. Receipt on a different candidate (wrong lineage) -> blocked.
        wrong_lineage_id = main.add_receipt(
            config,
            kind="audit",
            body={"report": "VERDICT: PASS"},
            issuer="work-service/auditor-settle",
            verdict="PASS",
            independent=True,
            candidate_id=main.second_candidate_id,
        )
        payload = main.payload()
        payload["native_acceptance_receipt_id"] = str(wrong_lineage_id)
        blocked(main, _attest_command(payload), "wrong lineage audit")

        # 5. Native acceptance with a non-PASS verdict -> blocked.
        bad_verdict_id = main.add_receipt(
            config,
            kind="audit",
            body={"report": "VERDICT: NEEDS_FIX"},
            issuer="work-service/auditor-settle",
            verdict="NEEDS_FIX",
            independent=True,
        )
        payload = main.payload()
        payload["native_acceptance_receipt_id"] = str(bad_verdict_id)
        blocked(main, _attest_command(payload), "wrong verdict")

        # 6. Fable advice receipt with an unforged-but-untrusted issuer -> blocked.
        bad_advice_id = main.add_receipt(
            config,
            kind="verification",
            body={"advice": "untrusted"},
            issuer="agent/x",
        )
        payload = main.payload()
        payload["fable_advice_receipt_id"] = str(bad_advice_id)
        blocked(main, _attest_command(payload), "untrusted advice issuer")

        # 7. Corrupted payload_sha256 on a current-lineage receipt -> blocked.
        corrupt_id = main.add_receipt(
            config,
            kind="plan",
            body={"plan": "corrupted"},
            issuer="owner",
            payload_sha256="0" * 64,
        )
        payload = main.payload()
        payload["plan_receipt_id"] = str(corrupt_id)
        blocked(main, _attest_command(payload), "bad payload_sha256")

        # 8. Service promotion receipt whose plan_receipt_id does not match -> blocked.
        blocked(
            no_promotion,
            _attest_command(no_promotion.payload()),
            "no promotion",
        )

        # 9. Missing deterministic-floor service receipt -> blocked.
        blocked(no_floor, _attest_command(no_floor.payload()), "no floor")

        # No blocked command minted any intake_admission receipt.
        with psycopg.connect(
            **config.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn:
            with conn.cursor() as cur:
                for seeded in (main, no_promotion, no_floor):
                    cur.execute(
                        "SELECT count(*) AS count FROM omp_evidence.receipts WHERE workspace_id=%s AND kind='intake_admission'",
                        (seeded.workspace_id,),
                    )
                    expected = 1 if seeded is main else 0
                    assert cur.fetchone()["count"] == expected
