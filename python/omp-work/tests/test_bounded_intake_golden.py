"""OMP-266 s07: independently adjudicated golden corpus for bounded intake.

Unit fixtures check model validation and the deterministic rules. Service
fixtures drive TestClient against SQL-seeded OMP-249 admission evidence.
Expected values live in the corpus; this runner does not snapshot evaluator
output back into the file.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from omp_work import contract_sha256
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.canonical import sha256, text_sha256
from omp_work.v1.models import BoundedIntakeDraft, FableAdvicePayload
from omp_work.v1.semantics import (
    BOUNDED_INTAKE_RULE_BUNDLE_SHA256,
    bounded_intake_semantic_sha256,
    evaluate_bounded_intake,
)
from omp_work.v1.server import create_app
from omp_work.v1.store import PostgresWorkStore
from pg_native import native_postgres, seed_authority
from psycopg.rows import dict_row
from pydantic import ValidationError

_CORPUS = Path(__file__).parent / "fixtures" / "bounded_intake_golden.json"
_CLAIM_GROUPS = ("constraints", "unknowns", "acceptance_criteria")


def _load() -> list[dict]:
    data = json.loads(_CORPUS.read_text())
    if not isinstance(data, list):
        raise AssertionError("golden corpus must be a JSON array")
    return data


def _by_id() -> dict[str, dict]:
    return {fixture["id"]: fixture for fixture in _load()}


def _claim_ids(body: dict) -> list[str]:
    ids = [body["goal"]["id"]]
    for group in _CLAIM_GROUPS:
        ids.extend(item["id"] for item in body[group])
    return ids


def _span_ids(body: dict) -> list[str]:
    return [span["id"] for span in body["source"]["spans"]]


def _question_view(questions: tuple) -> list[dict]:
    return [
        {
            "rule_class": question.rule_class,
            "deduplication_key": question.deduplication_key,
            "statement": question.statement,
            "priority": question.priority,
            "claim_ids": list(question.claim_ids),
        }
        for question in questions
    ]


def test_corpus_shape() -> None:
    fixtures = _load()
    assert 12 <= len(fixtures) <= 20
    ids = [fixture["id"] for fixture in fixtures]
    assert len(ids) == len(set(ids))
    assert any(
        fixture["input"].get("scenario") == "restart_replay" for fixture in fixtures
    )
    for fixture in fixtures:
        assert set(fixture) == {
            "id",
            "level",
            "description",
            "adjudication",
            "input",
            "expected",
        }
        assert fixture["level"] in {"unit", "service"}
        assert fixture["description"].strip()
        assert fixture["adjudication"].strip()
        if fixture["level"] == "service" or fixture["expected"].get("valid", True):
            BoundedIntakeDraft.model_validate(fixture["input"]["draft"])
            if "publish_draft" in fixture["input"]:
                BoundedIntakeDraft.model_validate(fixture["input"]["publish_draft"])


@pytest.mark.parametrize(
    "fixture",
    [fixture for fixture in _load() if fixture["level"] == "unit"],
    ids=lambda fixture: fixture["id"],
)
def test_unit_golden_fixture(fixture: dict) -> None:
    expected = fixture["expected"]
    raw = fixture["input"]["draft"]
    if expected.get("valid") is False:
        assert raw["source"]["sha256"] == text_sha256(raw["source"]["text"])
        with pytest.raises(ValidationError, match=expected["validation_error"]):
            BoundedIntakeDraft.model_validate(raw)
        return

    draft = BoundedIntakeDraft.model_validate(raw)
    questions, issue_count = evaluate_bounded_intake(draft)
    assert issue_count == expected["issue_count"]
    assert (issue_count == 0) is expected["ready"]
    assert _question_view(questions) == expected["questions"]
    assert len(questions) == min(issue_count, 2)

    corpus = _by_id()
    if "semantic_hash_equals" in expected:
        other_raw = corpus[expected["semantic_hash_equals"]]["input"]["draft"]
        other = BoundedIntakeDraft.model_validate(other_raw)
        assert raw != other_raw
        assert sorted(_claim_ids(raw)) == sorted(_claim_ids(other_raw))
        assert sorted(_span_ids(raw)) == sorted(_span_ids(other_raw))
        assert bounded_intake_semantic_sha256(draft) == bounded_intake_semantic_sha256(
            other
        )
    if "questions_equal" in expected:
        other = BoundedIntakeDraft.model_validate(
            corpus[expected["questions_equal"]]["input"]["draft"]
        )
        assert evaluate_bounded_intake(draft) == evaluate_bounded_intake(other)


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


def _insert_receipt(
    config: OperationsConfig,
    *,
    workspace_id: UUID,
    work_id: UUID,
    revision_id: UUID,
    candidate_id: UUID,
    receipt_id: UUID,
    kind: str,
    body: dict,
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


class _Seeded:
    def __init__(
        self,
        workspace_id: UUID,
        work_id: UUID,
        revision_id: UUID,
        plan_id: UUID,
        advice_id: UUID,
        audit_id: UUID,
    ) -> None:
        self.workspace_id = workspace_id
        self.work_id = work_id
        self.revision_id = revision_id
        self.plan_id = plan_id
        self.advice_id = advice_id
        self.audit_id = audit_id

    def attest_payload(self, advice_id: UUID | None = None) -> dict[str, str]:
        return {
            "work_id": str(self.work_id),
            "revision_id": str(self.revision_id),
            "plan_receipt_id": str(self.plan_id),
            "fable_advice_receipt_id": str(advice_id or self.advice_id),
            "native_acceptance_receipt_id": str(self.audit_id),
        }


def _seed(
    config: OperationsConfig,
    workspace_id: UUID,
    actor_id: UUID,
    draft: BoundedIntakeDraft,
) -> _Seeded:
    """SQL-seed OMP-249's s05 admission evidence for one workspace."""
    work_id, revision_id, candidate_id = uuid4(), uuid4(), uuid4()
    plan_id, advice_id, audit_id = uuid4(), uuid4(), uuid4()
    promotion_id, floor_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
    seed_authority(config.connection_kwargs("postgres"), workspace_id, actor_id)
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
            (
                candidate_id,
                workspace_id,
                work_id,
                revision_id,
                "b" * 64,
                "c" * 40,
                now,
            ),
        )
    seeded = _Seeded(workspace_id, work_id, revision_id, plan_id, advice_id, audit_id)
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


def _capabilities(
    root: Path, workspace_id: UUID, actor_id: UUID
) -> tuple[Path, dict[str, str], dict[str, str]]:
    root.mkdir(mode=0o700)
    owner_token = f"owner-{workspace_id}"
    other_token = f"other-{workspace_id}"
    (root / "owner.json").write_text(
        json.dumps(
            {
                "token": owner_token,
                "actor_id": str(actor_id),
                "actor_kind": "owner",
                "workspaces": [str(workspace_id)],
                "scopes": ["work.read", "work.mutate", "work.approve"],
            }
        )
    )
    (root / "owner.json").chmod(0o600)
    (root / "other.json").write_text(
        json.dumps(
            {
                "token": other_token,
                "actor_id": str(uuid4()),
                "actor_kind": "owner",
                "workspaces": [str(uuid4())],
                "scopes": ["work.read", "work.mutate", "work.approve"],
            }
        )
    )
    (root / "other.json").chmod(0o600)
    owner_headers = {
        "Authorization": f"Bearer {owner_token}",
        "X-OMP-Workspace-ID": str(workspace_id),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }
    other_headers = {
        "Authorization": f"Bearer {other_token}",
        "X-OMP-Contract-SHA256": contract_sha256(),
    }
    return root, owner_headers, other_headers


def _post(
    client: TestClient,
    headers: dict[str, str],
    workspace_id: UUID,
    command: dict,
    operation_id: UUID | None = None,
) -> tuple[dict, object]:
    envelope = {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(workspace_id),
        "operation_id": str(operation_id or uuid4()),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": command,
    }
    return envelope, client.post("/v1/commands", headers=headers, json=envelope)


def _counts(config: OperationsConfig, workspace_id: UUID) -> dict[str, int]:
    with psycopg.connect(
        **config.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn:
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
                "receipts",
                "SELECT count(*) AS n FROM omp_evidence.receipts WHERE workspace_id=%s",
            ),
        ):
            counts[label] = conn.execute(sql, (workspace_id,)).fetchone()["n"]
        return counts


def _published_items(config: OperationsConfig, workspace_id: UUID) -> int:
    with psycopg.connect(
        **config.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM omp_work.work_items i JOIN omp_work.work_aliases a ON a.work_id=i.work_id AND a.workspace_id=i.workspace_id WHERE i.workspace_id=%s AND a.key<>'OMP-249'",
            (workspace_id,),
        ).fetchone()
        return int(row["n"])


def _publication_receipts(config: OperationsConfig, workspace_id: UUID) -> int:
    with psycopg.connect(
        **config.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM omp_evidence.receipts WHERE workspace_id=%s AND kind='intake_publication'",
            (workspace_id,),
        ).fetchone()
        return int(row["n"])


def _assert_ok(response: object, what: str) -> dict:
    assert response.status_code == 200, f"{what}: {response.status_code} {response.text}"
    return response.json()


def _persisted_admission_receipt_id(
    config: OperationsConfig, workspace_id: UUID, work_id: UUID
) -> str:
    with psycopg.connect(
        **config.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn:
        row = conn.execute(
            "SELECT receipt_id FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s AND kind='intake_admission' ORDER BY issued_at DESC LIMIT 1",
            (workspace_id, work_id),
        ).fetchone()
    assert row is not None, "attest did not persist an intake_admission receipt"
    return str(row["receipt_id"])


def _admit(
    config: OperationsConfig,
    client: TestClient,
    headers: dict[str, str],
    seeded: _Seeded,
    advice_id: UUID | None = None,
) -> str:
    """POST attest. CommandResponse has no attest result tag, so an applied
    command comes back as a union-tag 400; the receipt id is the persisted row."""
    _, admitted = _post(
        client,
        headers,
        seeded.workspace_id,
        {
            "type": "attest_intake_admission",
            "payload": seeded.attest_payload(advice_id),
        },
    )
    if admitted.status_code == 200:
        return admitted.json()["result"]["receipt"]["receipt_id"]
    assert admitted.status_code == 400, admitted.text
    assert "union_tag_invalid" in admitted.text
    assert "attest_intake_admission" in admitted.text
    return _persisted_admission_receipt_id(config, seeded.workspace_id, seeded.work_id)


def _publish_command(
    draft: dict,
    *,
    assessment_operation_id: UUID,
    ratified: str,
    seeded: _Seeded,
    admission_receipt_id: str,
) -> dict:
    return {
        "type": "publish_bounded_intake",
        "payload": {
            "draft": draft,
            "assessment_operation_id": str(assessment_operation_id),
            "ratified_semantic_sha256": ratified,
            "admission_work_id": str(seeded.work_id),
            "admission_revision_id": str(seeded.revision_id),
            "admission_receipt_id": admission_receipt_id,
        },
    }


def _assess_and_admit(
    config: OperationsConfig,
    client: TestClient,
    headers: dict[str, str],
    seeded: _Seeded,
    draft: dict,
    *,
    advice_id: UUID | None = None,
) -> tuple[UUID, dict, str]:
    assessment_operation_id = uuid4()
    _, response = _post(
        client,
        headers,
        seeded.workspace_id,
        {"type": "assess_bounded_intake", "payload": {"draft": draft}},
        assessment_operation_id,
    )
    body = _assert_ok(response, "assess")
    assert body["receipt"]["state"] == "applied"
    assert body["result"]["ready_for_ratification"] is True
    admission_receipt_id = _admit(config, client, headers, seeded, advice_id)
    return assessment_operation_id, body["result"], admission_receipt_id


def _record_fable(
    client: TestClient,
    headers: dict[str, str],
    seeded: _Seeded,
    *,
    advice_text: str,
    intake_semantic_sha256: str,
) -> str:
    _, response = _post(
        client,
        headers,
        seeded.workspace_id,
        {
            "type": "record_fable_advice",
            "payload": {
                "work_id": str(seeded.work_id),
                "revision_id": str(seeded.revision_id),
                "advice_sha256": hashlib.sha256(advice_text.encode()).hexdigest(),
                "disposition": "considered",
                "intake_semantic_sha256": intake_semantic_sha256,
                "rule_bundle_sha256": BOUNDED_INTAKE_RULE_BUNDLE_SHA256,
            },
        },
    )
    body = _assert_ok(response, "fable")
    assert body["result"]["receipt"]["issuer"] == "work-service/fable-advisor"
    return body["result"]["receipt"]["receipt_id"]


def _assert_published_verbatim(
    config: OperationsConfig, draft: dict, result: dict
) -> None:
    revision_id = result["item"]["revision_id"]
    with psycopg.connect(
        **config.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn:
        revision = conn.execute(
            "SELECT title FROM omp_work.work_revisions WHERE revision_id=%s",
            (revision_id,),
        ).fetchone()
        criteria = conn.execute(
            "SELECT criterion FROM omp_work.acceptance_criteria WHERE revision_id=%s ORDER BY position",
            (revision_id,),
        ).fetchall()
    assert revision["title"] == draft["goal"]["statement"]
    assert [row["criterion"] for row in criteria] == [
        item["observable_outcome"] for item in draft["acceptance_criteria"]
    ]
    receipt_source = result["receipt"]["payload"]["draft"]["source"]
    assert receipt_source["text"] == draft["source"]["text"]
    assert receipt_source["spans"] == draft["source"]["spans"]


def _run_service(config: OperationsConfig, root: Path, fixture: dict) -> None:
    workspace_id, actor_id = uuid4(), uuid4()
    raw = fixture["input"]["draft"]
    draft = BoundedIntakeDraft.model_validate(raw)
    seeded = _seed(config, workspace_id, actor_id, draft)
    capabilities, owner_headers, other_headers = _capabilities(
        root, workspace_id, actor_id
    )
    scenario = fixture["input"]["scenario"]
    expected = fixture["expected"]

    if scenario == "foreign_workspace":
        with TestClient(create_app(config, capabilities_dir=capabilities)) as client:
            _, response = _post(
                client,
                other_headers,
                workspace_id,
                _publish_command(
                    raw,
                    assessment_operation_id=uuid4(),
                    ratified=bounded_intake_semantic_sha256(draft),
                    seeded=seeded,
                    admission_receipt_id=str(uuid4()),
                ),
            )
        assert response.status_code == expected["status"]
        assert response.json()["error"]["code"] == expected["error_code"]
        assert _published_items(config, workspace_id) == expected["published_work_items"]
        return

    publish_raw = fixture["input"].get("publish_draft", raw)
    publish_draft = BoundedIntakeDraft.model_validate(publish_raw)
    if expected.get("semantic_hash_differs"):
        assert bounded_intake_semantic_sha256(draft) != bounded_intake_semantic_sha256(
            publish_draft
        )
        assert evaluate_bounded_intake(publish_draft)[1] == 0

    with TestClient(create_app(config, capabilities_dir=capabilities)) as client:
        if scenario == "full_path":
            assessment_operation_id = uuid4()
            _, assessed = _post(
                client,
                owner_headers,
                workspace_id,
                {"type": "assess_bounded_intake", "payload": {"draft": raw}},
                assessment_operation_id,
            )
            assessment_body = _assert_ok(assessed, "assess")
            assert assessment_body["result"]["ready_for_ratification"] is True
            advice_id = _record_fable(
                client,
                owner_headers,
                seeded,
                advice_text=fixture["input"]["advice_text"],
                intake_semantic_sha256=assessment_body["result"]["semantic_sha256"],
            )
            assessment = assessment_body["result"]
            admission_receipt_id = _admit(
                config, client, owner_headers, seeded, UUID(advice_id)
            )
        else:
            assessment_operation_id, assessment, admission_receipt_id = _assess_and_admit(
                config, client, owner_headers, seeded, raw
            )

        ratified = (
            bounded_intake_semantic_sha256(publish_draft)
            if fixture["input"].get("ratify") == "publish_draft"
            else assessment["semantic_sha256"]
        )
        command = _publish_command(
            publish_raw,
            assessment_operation_id=assessment_operation_id,
            ratified=ratified,
            seeded=seeded,
            admission_receipt_id=admission_receipt_id,
        )

        if scenario == "receipt_mint_fault":
            before = _counts(config, workspace_id)
            store = PostgresWorkStore(config)

            def _mint_fault(*_args: object, **_kwargs: object) -> object:
                raise RuntimeError("injected mint fault")

            store._mint_intake_publication_receipt = _mint_fault  # type: ignore[method-assign]
            with TestClient(
                create_app(config, capabilities_dir=capabilities, store=store)
            ) as broken:
                try:
                    response = broken.post(
                        "/v1/commands",
                        headers=owner_headers,
                        json={
                            "api_version": "work.omp.dev/v1",
                            "workspace_id": str(workspace_id),
                            "operation_id": str(uuid4()),
                            "request_id": str(uuid4()),
                            "correlation_id": str(uuid4()),
                            "command": command,
                        },
                    )
                except RuntimeError as exc:
                    assert "injected mint fault" in str(exc)
                else:
                    assert response.status_code >= 400
                    assert "injected mint fault" in response.text
            delta = {
                label: _counts(config, workspace_id)[label] - before[label]
                for label in before
            }
            assert delta == expected["persisted_delta"]
            assert _publication_receipts(config, workspace_id) == 0
            return

        envelope, response = _post(
            client, owner_headers, workspace_id, command
        )
        if scenario in {"stale_ratification", "stale_assessment"}:
            assert response.status_code == expected["status"]
            assert response.json()["error"]["code"] == expected["error_code"]
            assert _published_items(config, workspace_id) == expected["published_work_items"]
            return

        body = _assert_ok(response, "publish")
        assert body["receipt"]["state"] == expected.get("first_state", "applied")
        if scenario == "full_path":
            assert expected["title_equals_goal_statement"] is True
            assert expected["acceptance_criteria_are_observable_outcomes"] is True
            assert expected["receipt_source_text_equals_input"] is True
            assert expected["receipt_source_spans_equal_input"] is True
            _assert_published_verbatim(config, raw, body["result"])
            return

        assert scenario == "restart_replay"
        first_result = body["result"]

    with TestClient(create_app(config, capabilities_dir=capabilities)) as restarted:
        replay = restarted.post("/v1/commands", headers=owner_headers, json=envelope)
    replay_body = _assert_ok(replay, "replay")
    assert replay_body["receipt"]["state"] == expected["replay_state"]
    assert replay_body["result"] == first_result
    assert _published_items(config, workspace_id) == expected["published_work_items"]
    assert _publication_receipts(config, workspace_id) == expected[
        "intake_publication_receipts"
    ]


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_service_golden_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = [fixture for fixture in _load() if fixture["level"] == "service"]
    assert len(fixtures) >= 1
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        monkeypatch.setattr(
            "omp_work.operations.database.validate_bundle", lambda **kw: None
        )
        bootstrap(config)
        for fixture in fixtures:
            try:
                _run_service(config, tmp_path / fixture["id"], fixture)
            except Exception as exc:
                raise AssertionError(f"{fixture['id']}: {exc}") from exc
