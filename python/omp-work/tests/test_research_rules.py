"""R02 acceptance: campaign transitions, compatibility, and role boundaries.

Pure pairs go through research_campaign_transition_error. Service cases drive
the research commands on the shared native Postgres workflow service.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.v1.semantics import (
    RESEARCH_CAMPAIGN_TRANSITIONS,
    research_campaign_transition_error,
)
from test_research_contract import (
    _admit_payload,
    _manifest,
    _register_component,
    _sample_spec,
    _setup_research_fixtures,
    _trial_payload,
)
from test_workflow_service import _command, _create, _grant, _owner_headers

pytest_plugins = ("test_workflow_service",)

CAMPAIGN_STATES = (
    "draft",
    "admitted",
    "running",
    "paused",
    "evaluating",
    "blocked",
    "concluded",
    "cancelled",
)
_RESUME_STATES = ("admitted", "running", "paused", "evaluating")
_BLOCKED = {"kind": "capability", "ref": "gpu-pool", "reason": "waiting on capacity"}

requires_postgres = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _assert_refused(status: int, body: dict, code: str) -> None:
    expected = {
        "invalid_request": 400,
        "forbidden": 403,
        "idempotency_conflict": 409,
        "revision_conflict": 409,
        "stale_evidence": 409,
    }[code]
    assert status == expected, body
    assert body["error"]["code"] == code


def _diagnostics(body: dict) -> list[str]:
    return list(body["error"].get("diagnostics") or [])


def _workspace(service, title: str) -> SimpleNamespace:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, title)
    spec, spec_sha = _sample_spec()
    policy, evaluator, environment, manifest, manifest_sha = _setup_research_fixtures(
        service, workspace_id
    )
    return SimpleNamespace(
        service=service,
        ws=workspace_id,
        item=item,
        work=UUID(item["work_id"]),
        rev=UUID(item["revision_id"]),
        spec=spec,
        spec_sha=spec_sha,
        policy=policy,
        evaluator=evaluator,
        environment=environment,
        manifest=manifest,
        manifest_sha=manifest_sha,
    )


def _create_campaign(ctx: SimpleNamespace) -> UUID:
    campaign_id = uuid4()
    status, body = _command(
        ctx.service,
        ctx.ws,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": str(ctx.work),
                "revision_id": str(ctx.rev),
                "domain": "machine_learning",
                "spec": ctx.spec,
                "spec_sha256": ctx.spec_sha,
            },
        },
    )
    assert status == 200, body
    return campaign_id


def _admit(ctx: SimpleNamespace, campaign_id: UUID, **overrides: object) -> tuple[int, dict]:
    payload = _admit_payload(
        campaign_id,
        ctx.work,
        ctx.rev,
        ctx.spec_sha,
        overrides.get("policy", ctx.policy),
        overrides.get("manifest", ctx.manifest),
        overrides.get("manifest_sha", ctx.manifest_sha),
    )
    return _command(
        ctx.service,
        ctx.ws,
        {"type": "admit_research_campaign", "payload": payload},
    )


def _move(
    ctx: SimpleNamespace,
    campaign_id: UUID,
    expected: str,
    target: str,
    *,
    policy: str | None = None,
    blocked: dict[str, str] | None = None,
) -> tuple[int, dict]:
    payload: dict[str, object] = {
        "campaign_id": str(campaign_id),
        "work_id": str(ctx.work),
        "expected_state": expected,
        "target_state": target,
        "policy_sha256": policy or ctx.policy,
    }
    if blocked is not None:
        payload["blocked_dependency"] = blocked
    return _command(
        ctx.service,
        ctx.ws,
        {"type": "set_research_campaign_state", "payload": payload},
    )


def _cancel(ctx: SimpleNamespace, campaign_id: UUID, reason: str) -> tuple[int, dict]:
    return _command(
        ctx.service,
        ctx.ws,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": str(ctx.work),
                "reason": reason,
            },
        },
    )


def _conclude(ctx: SimpleNamespace, campaign_id: UUID, *, policy: str | None = None) -> tuple[int, dict]:
    return _command(
        ctx.service,
        ctx.ws,
        {
            "type": "conclude_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": str(ctx.work),
                "policy_sha256": policy or ctx.policy,
                "outcome": "supported",
                "reason": "evidence is sufficient",
            },
        },
    )


def _propose(
    ctx: SimpleNamespace,
    campaign_id: UUID,
    *,
    policy: str | None = None,
    evaluator: str | None = None,
    environment: str | None = None,
) -> tuple[int, dict]:
    return _command(
        ctx.service,
        ctx.ws,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                campaign_id,
                ctx.work,
                policy or ctx.policy,
                evaluator or ctx.evaluator,
                environment or ctx.environment,
            ),
        },
    )


def _applied(status: int, body: dict) -> None:
    assert status == 200 and body["result"]["status"] == "applied", body


def _reach(ctx: SimpleNamespace, state: str) -> UUID:
    campaign_id = _create_campaign(ctx)
    if state == "draft":
        return campaign_id
    _applied(*_admit(ctx, campaign_id))
    if state == "admitted":
        return campaign_id
    if state == "cancelled":
        _applied(*_cancel(ctx, campaign_id, "stopped before running"))
        return campaign_id
    _applied(*_move(ctx, campaign_id, "admitted", "running"))
    if state == "running":
        return campaign_id
    if state == "paused":
        _applied(*_move(ctx, campaign_id, "running", "paused"))
        return campaign_id
    if state == "blocked":
        _applied(*_move(ctx, campaign_id, "running", "blocked", blocked=_BLOCKED))
        return campaign_id
    _applied(*_move(ctx, campaign_id, "running", "evaluating"))
    if state == "evaluating":
        return campaign_id
    if state == "concluded":
        _applied(*_conclude(ctx, campaign_id))
        return campaign_id
    raise AssertionError(state)


def _campaign_state(ctx: SimpleNamespace, campaign_id: UUID) -> dict:
    response = ctx.service.client.get(
        f"/v1/work-items/{ctx.item['key']}/research",
        headers=_owner_headers(ctx.ws),
    )
    assert response.status_code == 200, response.text
    return next(
        row for row in response.json()["campaigns"] if row["campaign_id"] == str(campaign_id)
    )


def test_campaign_transition_pairs_match_the_declared_set() -> None:
    assert len(CAMPAIGN_STATES) == 8
    declared = set(RESEARCH_CAMPAIGN_TRANSITIONS)
    assert declared <= {(current, target) for current in CAMPAIGN_STATES for target in CAMPAIGN_STATES}
    for current in CAMPAIGN_STATES:
        for target in CAMPAIGN_STATES:
            error = research_campaign_transition_error(current, target)
            if (current, target) in declared:
                assert error is None
            else:
                assert error == f"campaign in state {current} cannot transition to {target}"


@requires_postgres
def test_blocked_campaign_resumes_only_to_blocked_from_state(service) -> None:
    ctx = _workspace(service, "blocked resume")
    for origin in _RESUME_STATES:
        campaign_id = _create_campaign(ctx)
        _applied(*_admit(ctx, campaign_id))
        for expected, target in {
            "admitted": [],
            "running": [("admitted", "running")],
            "paused": [("admitted", "running"), ("running", "paused")],
            "evaluating": [("admitted", "running"), ("running", "evaluating")],
        }[origin]:
            _applied(*_move(ctx, campaign_id, expected, target))
        _applied(*_move(ctx, campaign_id, origin, "blocked", blocked=_BLOCKED))
        assert _campaign_state(ctx, campaign_id)["blocked_from_state"] == origin
        for target in _RESUME_STATES:
            if target == origin:
                continue
            status, body = _move(ctx, campaign_id, "blocked", target)
            _assert_refused(status, body, "invalid_request")
            assert any(f"resumes only to {origin}" in item for item in _diagnostics(body))
        _applied(*_move(ctx, campaign_id, "blocked", origin))
        resumed = _campaign_state(ctx, campaign_id)
        assert resumed["state"] == origin
        assert resumed["blocked_from_state"] is None
        assert resumed["blocked_dependency"] is None


@requires_postgres
def test_concluded_campaign_cannot_be_cancelled(service) -> None:
    ctx = _workspace(service, "concluded cancel")
    campaign_id = _reach(ctx, "concluded")
    status, body = _cancel(ctx, campaign_id, "too late")
    _assert_refused(status, body, "invalid_request")
    assert any(
        "campaign in state concluded cannot transition to cancelled" in item
        for item in _diagnostics(body)
    )
    assert _campaign_state(ctx, campaign_id)["state"] == "concluded"


@requires_postgres
def test_proposals_refused_outside_admitted_and_running(service) -> None:
    ctx = _workspace(service, "proposal states")
    for state in CAMPAIGN_STATES:
        campaign_id = _reach(ctx, state)
        status, body = _propose(ctx, campaign_id)
        if state in {"admitted", "running"}:
            _applied(status, body)
            assert body["result"]["trial"]["state"] == "proposed"
        else:
            _assert_refused(status, body, "invalid_request")
            assert any(
                f"cannot propose trial for campaign in state {state}" in item
                for item in _diagnostics(body)
            )


@requires_postgres
def test_evaluator_or_environment_outside_manifest_refused(service) -> None:
    ctx = _workspace(service, "manifest membership")
    other_evaluator = _register_component(
        ctx.service, ctx.ws, "evaluator", name="other-evaluator"
    )
    other_environment = _register_component(
        ctx.service, ctx.ws, "environment", name="other-environment"
    )
    campaign_id = _reach(ctx, "running")
    status, body = _propose(ctx, campaign_id, evaluator=other_evaluator)
    _assert_refused(status, body, "stale_evidence")
    assert any("evaluator fingerprint is not declared compatible" in item for item in _diagnostics(body))
    status, body = _propose(ctx, campaign_id, environment=other_environment)
    _assert_refused(status, body, "stale_evidence")
    assert any(
        "environment fingerprint is not declared compatible" in item for item in _diagnostics(body)
    )
    _applied(*_propose(ctx, campaign_id))


@requires_postgres
def test_legacy_campaign_without_manifest_refuses_proposals(service) -> None:
    ctx = _workspace(service, "legacy manifest")
    campaign_id = uuid4()
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO omp_research.campaigns(
                campaign_id, workspace_id, work_id, revision_id,
                domain, state, spec, spec_sha256, policy_sha256,
                created_at, admitted_at
            ) VALUES (
                %s, %s, %s, %s,
                'machine_learning', 'admitted', %s::jsonb, %s, %s,
                clock_timestamp(), clock_timestamp()
            )
            """,
            (
                campaign_id,
                ctx.ws,
                ctx.work,
                ctx.rev,
                psycopg.types.json.Jsonb(ctx.spec),
                ctx.spec_sha,
                ctx.policy,
            ),
        )
    viewed = _campaign_state(ctx, campaign_id)
    assert viewed["compatibility"] is None
    assert viewed["compatibility_sha256"] is None
    status, body = _propose(ctx, campaign_id)
    _assert_refused(status, body, "stale_evidence")
    assert any(
        "legacy campaign has no compatibility manifest" in item for item in _diagnostics(body)
    )


@requires_postgres
def test_admission_refuses_unregistered_or_wrong_kind_component(service) -> None:
    ctx = _workspace(service, "admission components")
    campaign_id = _create_campaign(ctx)
    unregistered, unregistered_sha = _manifest(workers=("1" * 64,))
    status, body = _admit(
        ctx, campaign_id, manifest=unregistered, manifest_sha=unregistered_sha
    )
    _assert_refused(status, body, "invalid_request")
    assert any("unknown worker component" in item for item in _diagnostics(body))

    wrong_kind, wrong_kind_sha = _manifest(workers=(ctx.evaluator,))
    status, body = _admit(ctx, campaign_id, manifest=wrong_kind, manifest_sha=wrong_kind_sha)
    _assert_refused(status, body, "invalid_request")
    assert any("worker fingerprint is not a worker component" in item for item in _diagnostics(body))

    environment_as_policy = _register_component(
        ctx.service, ctx.ws, "environment", name="env-as-policy"
    )
    status, body = _admit(ctx, campaign_id, policy=environment_as_policy)
    _assert_refused(status, body, "invalid_request")
    assert any("policy fingerprint is not a policy component" in item for item in _diagnostics(body))
    assert _campaign_state(ctx, campaign_id)["state"] == "draft"
    _applied(*_admit(ctx, campaign_id))


@requires_postgres
def test_manifest_immutable_after_admission(service) -> None:
    ctx = _workspace(service, "immutable manifest")
    campaign_id = _create_campaign(ctx)
    _applied(*_admit(ctx, campaign_id))
    worker = _register_component(ctx.service, ctx.ws, "worker", name="later-worker")
    changed, changed_sha = _manifest(
        evaluators=(ctx.evaluator,),
        environments=(ctx.environment,),
        workers=(worker,),
    )
    status, body = _admit(ctx, campaign_id, manifest=changed, manifest_sha=changed_sha)
    _assert_refused(status, body, "idempotency_conflict")
    assert any("differs from existing admission" in item for item in _diagnostics(body))
    stored = _campaign_state(ctx, campaign_id)
    assert stored["compatibility_sha256"] == ctx.manifest_sha
    assert stored["compatibility"] == ctx.manifest
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="campaign compatibility is immutable once set",
            ):
                cur.execute(
                    "UPDATE omp_research.campaigns SET compatibility_sha256 = %s WHERE campaign_id = %s",
                    ("7" * 64, campaign_id),
                )
        conn.rollback()


@requires_postgres
def test_policy_mismatch_refused_on_transition_proposal_and_conclude(service) -> None:
    ctx = _workspace(service, "policy fingerprint")
    other_policy = _register_component(ctx.service, ctx.ws, "policy", name="other-policy")
    campaign_id = _create_campaign(ctx)
    _applied(*_admit(ctx, campaign_id))

    status, body = _move(ctx, campaign_id, "admitted", "running", policy=other_policy)
    _assert_refused(status, body, "stale_evidence")
    assert any("policy fingerprint incompatible" in item for item in _diagnostics(body))
    assert _campaign_state(ctx, campaign_id)["state"] == "admitted"
    _applied(*_move(ctx, campaign_id, "admitted", "running"))

    status, body = _propose(ctx, campaign_id, policy=other_policy)
    _assert_refused(status, body, "stale_evidence")
    assert any("trial policy digest does not match" in item for item in _diagnostics(body))
    _applied(*_propose(ctx, campaign_id))

    _applied(*_move(ctx, campaign_id, "running", "evaluating"))
    status, body = _conclude(ctx, campaign_id, policy=other_policy)
    _assert_refused(status, body, "stale_evidence")
    assert any("policy fingerprint incompatible" in item for item in _diagnostics(body))
    assert _campaign_state(ctx, campaign_id)["outcome"] is None
    _applied(*_conclude(ctx, campaign_id))


@requires_postgres
def test_execute_only_principal_cannot_admit_or_conclude(service) -> None:
    ctx = _workspace(service, "execute only")
    token = "exec-only-rules-token"
    capability = service.capabilities / "exec-only-rules.json"
    capability.write_text(
        json.dumps(
            {
                "token": token,
                "actor_id": str(uuid4()),
                "actor_kind": "worker",
                "workspaces": [str(ctx.ws)],
                "scopes": ["work.read", "work.mutate", "work.execute"],
            }
        )
    )
    capability.chmod(0o600)
    try:
        campaign_id = _create_campaign(ctx)
        status, body = _command(
            ctx.service,
            ctx.ws,
            {
                "type": "admit_research_campaign",
                "payload": _admit_payload(
                    campaign_id,
                    ctx.work,
                    ctx.rev,
                    ctx.spec_sha,
                    ctx.policy,
                    ctx.manifest,
                    ctx.manifest_sha,
                ),
            },
            token=token,
        )
        _assert_refused(status, body, "forbidden")
        assert _campaign_state(ctx, campaign_id)["state"] == "draft"
        _applied(*_admit(ctx, campaign_id))
        _applied(*_move(ctx, campaign_id, "admitted", "running"))
        _applied(*_move(ctx, campaign_id, "running", "evaluating"))
        status, body = _command(
            ctx.service,
            ctx.ws,
            {
                "type": "conclude_research_campaign",
                "payload": {
                    "campaign_id": str(campaign_id),
                    "work_id": str(ctx.work),
                    "policy_sha256": ctx.policy,
                    "outcome": "supported",
                    "reason": "executor must not conclude",
                },
            },
            token=token,
        )
        _assert_refused(status, body, "forbidden")
        assert _campaign_state(ctx, campaign_id)["state"] == "evaluating"
        assert _campaign_state(ctx, campaign_id)["outcome"] is None
        _applied(*_conclude(ctx, campaign_id))
    finally:
        capability.unlink(missing_ok=True)
