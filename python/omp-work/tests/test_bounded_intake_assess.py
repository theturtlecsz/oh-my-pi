from __future__ import annotations
 
import json
import os
from pathlib import Path
import secrets
import socket
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import omp_work
from omp_work import contract_sha256
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.api_models import AssessBoundedIntakeResult, CommandResponse
from omp_work.v1.canonical import text_sha256
from omp_work.v1.models import (
    AssessBoundedIntakeCommand,
    AssessBoundedIntakePayload,
    BoundedIntakeDraft,
    CommandEnvelope,
    IntakeAcceptanceCriterion,
    IntakeBlockingQuestion,
    IntakeConstraint,
    IntakeGoal,
    IntakeSource,
    IntakeSourceSpan,
    KnownIntakeValue,
)
from omp_work.v1.semantics import (
    BOUNDED_INTAKE_RULE_BUNDLE_SHA256,
    bounded_intake_semantic_sha256,
    evaluate_bounded_intake,
)
from omp_work.v1.server import create_app
from omp_work.v1.service import WorkService
from pg_native import native_postgres, seed_authority


def _valid_draft() -> BoundedIntakeDraft:
    text = "Optimize cache eviction policy to avoid unbounded memory growth in worker pool."
    s1_text = "Optimize cache eviction policy"
    s1_start = text.index(s1_text)
    s1_end = s1_start + len(s1_text.encode("utf-8"))
    span1 = IntakeSourceSpan(
        id="span-1",
        start=s1_start,
        end=s1_end,
        exact_text_sha256=text_sha256(s1_text),
    )
    source = IntakeSource(
        text=text,
        sha256=text_sha256(text),
        spans=(span1,),
    )
    return BoundedIntakeDraft(
        archetype="small_code_change",
        source=source,
        goal=IntakeGoal(
            id="claim-goal-1",
            statement="Bound memory growth in cache",
            source_span_ids=("span-1",),
        ),
        constraints=(
            IntakeConstraint(
                id="claim-constraint-1",
                statement="Must not regress hit latency",
                source_span_ids=("span-1",),
                key="max_latency_ms",
                value=KnownIntakeValue(value=5),
                polarity="positive",
            ),
        ),
        unknowns=(),
        acceptance_criteria=(
            IntakeAcceptanceCriterion(
                id="claim-ac-1",
                statement="Verify latency under load",
                observable_outcome="latency remains < 5ms",
                oracle="automated_test",
            ),
        ),
    )


def test_command_envelope_accepts_assess_command_with_valid_draft() -> None:
    draft = _valid_draft()
    envelope = CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": uuid4(),
            "operation_id": uuid4(),
            "request_id": uuid4(),
            "correlation_id": uuid4(),
            "command": {
                "type": "assess_bounded_intake",
                "payload": {
                    "draft": draft.model_dump(mode="json"),
                },
            },
        }
    )
    assert isinstance(envelope.command, AssessBoundedIntakeCommand)
    assert envelope.command.type == "assess_bounded_intake"
    assert envelope.command.payload.draft.goal.statement == "Bound memory growth in cache"


def test_command_envelope_rejects_unknown_payload_fields() -> None:
    draft = _valid_draft()
    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(
            {
                "api_version": "work.omp.dev/v1",
                "workspace_id": uuid4(),
                "operation_id": uuid4(),
                "request_id": uuid4(),
                "correlation_id": uuid4(),
                "command": {
                    "type": "assess_bounded_intake",
                    "payload": {
                        "draft": draft.model_dump(mode="json"),
                        "unknown_field": "not_allowed",
                    },
                },
            }
        )


def test_assess_bounded_intake_payload_rejects_unknown_fields() -> None:
    draft = _valid_draft()
    with pytest.raises(ValidationError):
        AssessBoundedIntakePayload.model_validate(
            {
                "draft": draft.model_dump(mode="json"),
                "unrecognized_field": True,
            }
        )


def test_command_response_accepts_assess_result() -> None:
    response = CommandResponse.model_validate(
        {
            "receipt": {
                "operation_id": uuid4(),
                "request_id": uuid4(),
                "state": "applied",
                "request_sha256": "0" * 64,
                "result_sha256": "1" * 64,
            },
            "result": {
                "type": "assess_bounded_intake",
                "semantic_sha256": "a" * 64,
                "rule_bundle_sha256": "b" * 64,
                "ready_for_ratification": True,
                "issue_count": 0,
                "questions": [],
            },
        }
    )
    assert isinstance(response.result, AssessBoundedIntakeResult)
    assert response.result.type == "assess_bounded_intake"
    assert response.result.semantic_sha256 == "a" * 64
    assert response.result.rule_bundle_sha256 == "b" * 64
    assert response.result.ready_for_ratification is True
    assert response.result.issue_count == 0
    assert response.result.questions == ()


def test_command_response_accepts_assess_result_with_blocking_questions() -> None:
    question = IntakeBlockingQuestion(
        rule_class="missing_verification_oracle",
        deduplication_key="oracle:claim-ac-1",
        statement="missing_verification_oracle:oracle:claim-ac-1",
        priority=1,
        claim_ids=("claim-ac-1",),
    )
    response = CommandResponse.model_validate(
        {
            "receipt": {
                "operation_id": uuid4(),
                "request_id": uuid4(),
                "state": "applied",
                "request_sha256": "0" * 64,
                "result_sha256": "1" * 64,
            },
            "result": {
                "type": "assess_bounded_intake",
                "semantic_sha256": "c" * 64,
                "rule_bundle_sha256": "d" * 64,
                "ready_for_ratification": False,
                "issue_count": 1,
                "questions": [question.model_dump(mode="json")],
            },
        }
    )
    assert isinstance(response.result, AssessBoundedIntakeResult)
    assert response.result.ready_for_ratification is False
    assert response.result.issue_count == 1
    assert len(response.result.questions) == 1
    assert response.result.questions[0].deduplication_key == "oracle:claim-ac-1"


def test_assess_bounded_intake_result_validates_bounds() -> None:
    with pytest.raises(ValidationError):
        AssessBoundedIntakeResult.model_validate(
            {
                "type": "assess_bounded_intake",
                "semantic_sha256": "a" * 64,
                "rule_bundle_sha256": "b" * 64,
                "ready_for_ratification": True,
                "issue_count": -1,
                "questions": [],
            }
        )

    with pytest.raises(ValidationError):
        AssessBoundedIntakeResult.model_validate(
            {
                "type": "assess_bounded_intake",
                "semantic_sha256": "not-a-sha",
                "rule_bundle_sha256": "b" * 64,
                "ready_for_ratification": True,
                "issue_count": 0,
                "questions": [],
            }
        )

    with pytest.raises(ValidationError):
        AssessBoundedIntakeResult.model_validate(
            {
                "type": "assess_bounded_intake",
                "semantic_sha256": "a" * 64,
                "rule_bundle_sha256": "b" * 64,
                "ready_for_ratification": True,
                "issue_count": 0,
                "questions": [],
                "extra_field": "forbidden",
            }
        )


def test_assess_bounded_intake_scope_is_work_approve() -> None:
    assert WorkService._scopes["assess_bounded_intake"] == "work.approve"


def test_command_types_closure_includes_assess_bounded_intake() -> None:
    contract = omp_work.load_contract()
    assert "assess_bounded_intake" in contract.command_types
    assert "assess_bounded_intake" in omp_work._COMMAND_TYPES


def _config(tmp_path: Path) -> OperationsConfig:
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
        "operator-actor-id",
    ):
        path = credentials / role
        path.write_text(secrets.token_urlsafe(24))
        path.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        port=port,
    )


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_assess_bounded_intake_integration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        monkeypatch.setattr(
            "omp_work.operations.database.validate_bundle", lambda **kw: None
        )
        bootstrap(config)
        workspace_id, actor_id, operation_id = uuid4(), uuid4(), uuid4()
        seed_authority(config.connection_kwargs("postgres"), workspace_id, actor_id)
        with psycopg.connect(
            **config.connection_kwargs("postgres"), autocommit=True
        ) as conn:
            conn.execute(
                "INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING",
                (workspace_id,),
            )
        capabilities = tmp_path / "capabilities"
        capabilities.mkdir(mode=0o700)
        (capabilities / "owner.json").write_text(
            json.dumps(
                {
                    "token": "owner-token",
                    "actor_id": str(actor_id),
                    "actor_kind": "owner",
                    "workspaces": [str(workspace_id)],
                    "scopes": ["work.read", "work.mutate", "work.approve"],
                }
            )
        )
        (capabilities / "owner.json").chmod(0o600)
        other_workspace_id = uuid4()
        (capabilities / "other.json").write_text(
            json.dumps(
                {
                    "token": "other-token",
                    "actor_id": str(uuid4()),
                    "actor_kind": "owner",
                    "workspaces": [str(other_workspace_id)],
                    "scopes": ["work.read", "work.mutate", "work.approve"],
                }
            )
        )
        (capabilities / "other.json").chmod(0o600)
        client = TestClient(create_app(config, capabilities_dir=capabilities))
        headers = {
            "Authorization": "Bearer owner-token",
            "X-OMP-Workspace-ID": str(workspace_id),
            "X-OMP-Contract-SHA256": contract_sha256(),
        }

        # 1. Ready draft -> ready_for_ratification: true, issue_count: 0
        ready_draft = _valid_draft()
        envelope = CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=operation_id,
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=AssessBoundedIntakeCommand(
                type="assess_bounded_intake",
                payload=AssessBoundedIntakePayload(draft=ready_draft),
            ),
        )
        resp = client.post(
            "/v1/commands",
            headers=headers,
            json=envelope.model_dump(mode="json"),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["receipt"]["state"] == "applied"
        assert data["receipt"]["operation_id"] == str(operation_id)
        assert data["result"]["type"] == "assess_bounded_intake"
        assert data["result"]["ready_for_ratification"] is True
        assert data["result"]["issue_count"] == 0
        assert data["result"]["questions"] == []
        assert (
            data["result"]["rule_bundle_sha256"]
            == BOUNDED_INTAKE_RULE_BUNDLE_SHA256
        )
        assert (
            data["result"]["semantic_sha256"]
            == bounded_intake_semantic_sha256(ready_draft)
        )

        # 2. Same operation_id resubmitted -> receipt state == "replayed", identical result body
        replay_same = client.post(
            "/v1/commands",
            headers=headers,
            json=envelope.model_dump(mode="json"),
        )
        assert replay_same.status_code == 200
        assert replay_same.json()["receipt"]["state"] == "replayed"
        assert replay_same.json()["receipt"]["operation_id"] == str(operation_id)
        assert replay_same.json()["result"] == data["result"]

        replay_new_request = client.post(
            "/v1/commands",
            headers=headers,
            json=envelope.model_copy(update={"request_id": uuid4()}).model_dump(
                mode="json"
            ),
        )
        assert replay_new_request.status_code == 200
        assert replay_new_request.json()["receipt"]["state"] == "replayed"
        assert replay_new_request.json()["receipt"]["operation_id"] == str(operation_id)
        assert replay_new_request.json()["result"] == data["result"]

        # 3. Draft with a criterion oracle=None -> false, exactly one question equal to
        # evaluate_bounded_intake(draft)[0][0].model_dump(mode="json")
        # (statement missing_verification_oracle:oracle:<criterion id>)
        criterion_id = "claim-ac-missing-oracle"
        draft_with_none_oracle = ready_draft.model_copy(
            update={
                "acceptance_criteria": (
                    IntakeAcceptanceCriterion(
                        id=criterion_id,
                        statement="Missing oracle criterion",
                        observable_outcome="Outcome without oracle",
                        oracle=None,
                    ),
                )
            }
        )
        expected_questions, expected_issues = evaluate_bounded_intake(
            draft_with_none_oracle
        )
        assert expected_issues == 1
        assert len(expected_questions) == 1
        expected_question_dict = expected_questions[0].model_dump(mode="json")
        assert (
            expected_question_dict["statement"]
            == f"missing_verification_oracle:oracle:{criterion_id}"
        )

        op_id_oracle = uuid4()
        envelope_oracle = CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=op_id_oracle,
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=AssessBoundedIntakeCommand(
                type="assess_bounded_intake",
                payload=AssessBoundedIntakePayload(draft=draft_with_none_oracle),
            ),
        )
        resp_oracle = client.post(
            "/v1/commands",
            headers=headers,
            json=envelope_oracle.model_dump(mode="json"),
        )
        assert resp_oracle.status_code == 200
        oracle_json = resp_oracle.json()
        assert oracle_json["receipt"]["state"] == "applied"
        assert oracle_json["result"]["type"] == "assess_bounded_intake"
        assert oracle_json["result"]["ready_for_ratification"] is False
        assert oracle_json["result"]["issue_count"] == 1
        assert len(oracle_json["result"]["questions"]) == 1
        assert oracle_json["result"]["questions"][0] == expected_question_dict
        assert (
            oracle_json["result"]["questions"][0]["statement"]
            == f"missing_verification_oracle:oracle:{criterion_id}"
        )
        assert (
            oracle_json["result"]["rule_bundle_sha256"]
            == BOUNDED_INTAKE_RULE_BUNDLE_SHA256
        )
        assert (
            oracle_json["result"]["semantic_sha256"]
            == bounded_intake_semantic_sha256(draft_with_none_oracle)
        )

        # 4. Capability token for a different workspace_id -> 403 via real TestClient auth (no hand-built Principal)
        envelope_forbidden = CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=AssessBoundedIntakeCommand(
                type="assess_bounded_intake",
                payload=AssessBoundedIntakePayload(draft=ready_draft),
            ),
        )
        resp_forbidden = client.post(
            "/v1/commands",
            headers={
                "Authorization": "Bearer other-token",
                "X-OMP-Contract-SHA256": contract_sha256(),
            },
            json=envelope_forbidden.model_dump(mode="json"),
        )
        assert resp_forbidden.status_code == 403
        assert resp_forbidden.json()["error"]["code"] == "forbidden"

        # Verify no domain rows were written, but idempotency and domain events were written
        conn_kwargs = config.connection_kwargs("postgres")
        with psycopg.connect(**conn_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) as count FROM omp_work.work_items WHERE workspace_id=%s",
                    (workspace_id,),
                )
                assert cur.fetchone()["count"] == 0
                cur.execute(
                    "SELECT count(*) as count FROM omp_control.idempotent_commands WHERE workspace_id=%s AND command_type='assess_bounded_intake'",
                    (workspace_id,),
                )
                assert cur.fetchone()["count"] == 2
                cur.execute(
                    "SELECT count(*) as count FROM omp_audit.domain_events WHERE workspace_id=%s AND event_type='assess_bounded_intake'",
                    (workspace_id,),
                )
                assert cur.fetchone()["count"] == 2
