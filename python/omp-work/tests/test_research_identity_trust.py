"""R02 acceptance: missing, stale, and foreign identities are not trust.

Research commands run through the workflow service. A completed observation
records execution status and leaves the campaign outcome unset.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from omp_work.v1.canonical import sha256
from test_research_contract import (
    _admit_payload,
    _revise,
    _sample_spec,
    _setup_research_fixtures,
    _trial_payload,
)
from test_workflow_service import _command, _create, _grant, _owner_headers, _receipt

pytest_plugins = ("test_workflow_service",)

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


def _open(service, title: str) -> SimpleNamespace:
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


def _create_campaign(ctx: SimpleNamespace, *, work: UUID | None = None, revision: UUID | None = None) -> UUID:
    campaign_id = uuid4()
    status, body = _command(
        ctx.service,
        ctx.ws,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": str(work or ctx.work),
                "revision_id": str(revision or ctx.rev),
                "domain": "machine_learning",
                "spec": ctx.spec,
                "spec_sha256": ctx.spec_sha,
            },
        },
    )
    assert status == 200, body
    return campaign_id


def _admit(ctx: SimpleNamespace, campaign_id: UUID, *, work: UUID | None = None, revision: UUID | None = None) -> None:
    status, body = _command(
        ctx.service,
        ctx.ws,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                campaign_id,
                work or ctx.work,
                revision or ctx.rev,
                ctx.spec_sha,
                ctx.policy,
                ctx.manifest,
                ctx.manifest_sha,
            ),
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body


def _running(ctx: SimpleNamespace, *, work: UUID | None = None, revision: UUID | None = None) -> UUID:
    campaign_id = _create_campaign(ctx, work=work, revision=revision)
    _admit(ctx, campaign_id, work=work, revision=revision)
    status, body = _command(
        ctx.service,
        ctx.ws,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": str(work or ctx.work),
                "expected_state": "admitted",
                "target_state": "running",
                "policy_sha256": ctx.policy,
            },
        },
    )
    assert status == 200, body
    return campaign_id


def _propose(ctx: SimpleNamespace, campaign_id: UUID, *, work: UUID | None = None, trial_id: UUID | None = None, digest: str = "c" * 64) -> UUID:
    trial_id = trial_id or uuid4()
    status, body = _command(
        ctx.service,
        ctx.ws,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                campaign_id,
                work or ctx.work,
                ctx.policy,
                ctx.evaluator,
                ctx.environment,
                trial_id=trial_id,
                candidate_digest=digest,
            ),
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    return trial_id


def _binding_sha(
    ctx: SimpleNamespace,
    *,
    trial_id: UUID,
    campaign_id: UUID,
    work: UUID,
    revision: UUID,
    digest: str,
    native_candidate_id: UUID,
) -> str:
    return sha256(
        {
            "candidate_digest": digest,
            "campaign_id": str(campaign_id),
            "native_candidate_id": str(native_candidate_id),
            "revision_id": str(revision),
            "trial_id": str(trial_id),
            "work_id": str(work),
            "workspace_id": str(ctx.ws),
        }
    )


def _bind(
    ctx: SimpleNamespace,
    *,
    trial_id: UUID,
    campaign_id: UUID,
    work: UUID,
    revision: UUID,
    digest: str,
    native_candidate_id: object,
) -> tuple[int, dict]:
    native = native_candidate_id if isinstance(native_candidate_id, str) else str(native_candidate_id)
    binding_sha = "0" * 64
    if isinstance(native_candidate_id, UUID):
        binding_sha = _binding_sha(
            ctx,
            trial_id=trial_id,
            campaign_id=campaign_id,
            work=work,
            revision=revision,
            digest=digest,
            native_candidate_id=native_candidate_id,
        )
    return _command(
        ctx.service,
        ctx.ws,
        {
            "type": "bind_research_deliverable",
            "payload": {
                "trial_id": str(trial_id),
                "campaign_id": str(campaign_id),
                "work_id": str(work),
                "revision_id": str(revision),
                "candidate_digest": digest,
                "native_candidate_id": native,
                "binding_sha256": binding_sha,
            },
        },
    )


def _finalize(ctx: SimpleNamespace, item: dict, candidate_id: UUID, candidate_hash: str) -> None:
    receipt = _receipt(
        item["work_id"],
        item["revision_id"],
        str(uuid4()),
        "plan",
        body={"body": "## Approach\n1. do it\n\n## Verification\n1. prove it"},
        candidate_sha256=sha256({"plan": str(candidate_id)}),
    )
    status, body = _command(
        ctx.service,
        ctx.ws,
        {"type": "append_evidence", "payload": {"receipt": receipt}},
    )
    assert status == 200, body
    status, body = _command(
        ctx.service,
        ctx.ws,
        {
            "type": "finalize_candidate",
            "payload": {
                "work_id": item["work_id"],
                "revision_id": item["revision_id"],
                "planned_candidate_id": body["result"]["receipt"]["candidate_id"],
                "candidate_id": str(candidate_id),
                "candidate_sha256": candidate_hash,
                "commit_sha": "ab" * 20,
            },
        },
    )
    assert status == 200, body


def _research(ctx: SimpleNamespace) -> dict:
    response = ctx.service.client.get(
        f"/v1/work-items/{ctx.item['key']}/research",
        headers=_owner_headers(ctx.ws),
    )
    assert response.status_code == 200, response.text
    return response.json()


@requires_postgres
def test_unknown_campaign_trial_or_component_sha_refused(service) -> None:
    ctx = _open(service, "missing identity")
    campaign_id = _running(ctx)
    missing_campaign = uuid4()
    status, body = _command(
        ctx.service,
        ctx.ws,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(missing_campaign),
                "work_id": str(ctx.work),
                "expected_state": "running",
                "target_state": "paused",
                "policy_sha256": ctx.policy,
            },
        },
    )
    _assert_refused(status, body, "invalid_request")
    assert any("campaign not found" in item for item in _diagnostics(body))

    status, body = _command(
        ctx.service,
        ctx.ws,
        {
            "type": "record_research_observation",
            "payload": {
                "observation_id": str(uuid4()),
                "campaign_id": str(campaign_id),
                "trial_id": str(uuid4()),
                "issuer_kind": "candidate_authored",
                "source_ref": f"missing-trial-{uuid4()}",
                "execution_status": "completed",
                "payload": {"note": "missing trial"},
                "payload_sha256": sha256({"note": "missing trial"}),
                "observed_at": datetime.now(UTC).isoformat(),
            },
        },
    )
    _assert_refused(status, body, "invalid_request")
    assert any("trial does not belong to campaign" in item for item in _diagnostics(body))

    draft_id = _create_campaign(ctx)
    status, body = _command(
        ctx.service,
        ctx.ws,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                draft_id,
                ctx.work,
                ctx.rev,
                ctx.spec_sha,
                "0" * 64,
                ctx.manifest,
                ctx.manifest_sha,
            ),
        },
    )
    _assert_refused(status, body, "invalid_request")
    assert any("unknown policy component" in item for item in _diagnostics(body))


@requires_postgres
def test_stale_expected_state_and_revision_refused(service) -> None:
    ctx = _open(service, "stale identity")
    campaign_id = _create_campaign(ctx)
    _admit(ctx, campaign_id)
    status, body = _command(
        ctx.service,
        ctx.ws,
        {
            "type": "set_research_campaign_state",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": str(ctx.work),
                "expected_state": "running",
                "target_state": "paused",
                "policy_sha256": ctx.policy,
            },
        },
    )
    _assert_refused(status, body, "revision_conflict")
    assert any("expected running" in item for item in _diagnostics(body))

    draft_id = _create_campaign(ctx)
    revised = _revise(service, ctx.ws, ctx.work, ctx.rev, 2, "stale identity v2")
    assert revised != ctx.rev
    status, body = _command(
        ctx.service,
        ctx.ws,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(uuid4()),
                "work_id": str(ctx.work),
                "revision_id": str(ctx.rev),
                "domain": "machine_learning",
                "spec": ctx.spec,
                "spec_sha256": ctx.spec_sha,
            },
        },
    )
    _assert_refused(status, body, "stale_evidence")
    assert any("does not match current work revision" in item for item in _diagnostics(body))

    status, body = _command(
        ctx.service,
        ctx.ws,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                draft_id,
                ctx.work,
                ctx.rev,
                ctx.spec_sha,
                ctx.policy,
                ctx.manifest,
                ctx.manifest_sha,
            ),
        },
    )
    _assert_refused(status, body, "stale_evidence")
    assert any("work revision has drifted" in item for item in _diagnostics(body))


@requires_postgres
def test_foreign_campaign_trial_and_native_candidate_refused(service) -> None:
    home = _open(service, "home work")
    other = _create(service, home.ws, "other work")
    other_work = UUID(other["work_id"])
    other_rev = UUID(other["revision_id"])
    abroad_ws = uuid4()
    _grant(service, abroad_ws)
    abroad = _create(service, abroad_ws, "abroad work")
    abroad_ctx = SimpleNamespace(
        service=service,
        ws=abroad_ws,
        item=abroad,
        work=UUID(abroad["work_id"]),
        rev=UUID(abroad["revision_id"]),
        spec=home.spec,
        spec_sha=home.spec_sha,
        policy=None,
        evaluator=None,
        environment=None,
        manifest=None,
        manifest_sha=None,
    )
    policy, evaluator, environment, manifest, manifest_sha = _setup_research_fixtures(
        service, abroad_ws
    )
    abroad_ctx.policy = policy
    abroad_ctx.evaluator = evaluator
    abroad_ctx.environment = environment
    abroad_ctx.manifest = manifest
    abroad_ctx.manifest_sha = manifest_sha

    home_campaign = _running(home)
    home_trial = _propose(home, home_campaign)
    other_campaign = _running(home, work=other_work, revision=other_rev)
    _propose(home, other_campaign, work=other_work)
    abroad_campaign = _running(abroad_ctx)

    digest = "d" * 64
    home_candidate = uuid4()
    _finalize(home, home.item, home_candidate, digest)
    other_candidate = uuid4()
    _finalize(home, other, other_candidate, "e" * 64)
    abroad_candidate = uuid4()
    _finalize(abroad_ctx, abroad, abroad_candidate, "f" * 64)

    status, body = _command(
        service,
        abroad_ws,
        {
            "type": "cancel_research_campaign",
            "payload": {
                "campaign_id": str(home_campaign),
                "work_id": str(abroad_ctx.work),
                "reason": "foreign workspace",
            },
        },
    )
    _assert_refused(status, body, "invalid_request")
    assert any("campaign not found" in item for item in _diagnostics(body))

    status, body = _command(
        service,
        home.ws,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                home_campaign,
                other_work,
                home.policy,
                home.evaluator,
                home.environment,
            ),
        },
    )
    _assert_refused(status, body, "invalid_request")
    assert any("campaign work mismatch" in item for item in _diagnostics(body))

    status, body = _command(
        service,
        abroad_ws,
        {
            "type": "record_research_observation",
            "payload": {
                "observation_id": str(uuid4()),
                "campaign_id": str(abroad_campaign),
                "trial_id": str(home_trial),
                "issuer_kind": "candidate_authored",
                "source_ref": f"foreign-trial-{uuid4()}",
                "execution_status": "completed",
                "payload": {"note": "foreign trial"},
                "payload_sha256": sha256({"note": "foreign trial"}),
                "observed_at": datetime.now(UTC).isoformat(),
            },
        },
    )
    _assert_refused(status, body, "invalid_request")
    assert any("trial does not belong to campaign" in item for item in _diagnostics(body))

    status, body = _bind(
        home,
        trial_id=home_trial,
        campaign_id=other_campaign,
        work=other_work,
        revision=other_rev,
        digest="c" * 64,
        native_candidate_id=other_candidate,
    )
    _assert_refused(status, body, "stale_evidence")
    assert any("trial does not match campaign or work" in item for item in _diagnostics(body))

    status, body = _bind(
        home,
        trial_id=home_trial,
        campaign_id=home_campaign,
        work=home.work,
        revision=home.rev,
        digest="c" * 64,
        native_candidate_id=abroad_candidate,
    )
    _assert_refused(status, body, "stale_evidence")
    assert any("native candidate work or revision mismatch" in item for item in _diagnostics(body))

    status, body = _bind(
        home,
        trial_id=home_trial,
        campaign_id=home_campaign,
        work=home.work,
        revision=home.rev,
        digest="c" * 64,
        native_candidate_id=other_candidate,
    )
    _assert_refused(status, body, "stale_evidence")
    assert any("native candidate work or revision mismatch" in item for item in _diagnostics(body))


@requires_postgres
def test_trial_id_or_candidate_digest_cannot_bind_as_native_candidate(service) -> None:
    ctx = _open(service, "identity is not trust")
    digest = "a" * 64
    campaign_id = _running(ctx)
    trial_id = _propose(ctx, campaign_id, digest=digest)
    _finalize(ctx, ctx.item, trial_id, digest)
    status, body = _bind(
        ctx,
        trial_id=trial_id,
        campaign_id=campaign_id,
        work=ctx.work,
        revision=ctx.rev,
        digest=digest,
        native_candidate_id=trial_id,
    )
    _assert_refused(status, body, "stale_evidence")
    assert any("research trial id cannot bind as native candidate" in item for item in _diagnostics(body))

    status, body = _bind(
        ctx,
        trial_id=trial_id,
        campaign_id=campaign_id,
        work=ctx.work,
        revision=ctx.rev,
        digest=digest,
        native_candidate_id=digest,
    )
    _assert_refused(status, body, "invalid_request")
    assert any("native_candidate_id" in item for item in _diagnostics(body))


@requires_postgres
def test_completed_observation_leaves_campaign_outcome_unset(service) -> None:
    ctx = _open(service, "observation is not outcome")
    campaign_id = _running(ctx)
    trial_id = _propose(ctx, campaign_id)
    payload = {"loss": 0.2, "execution_status": "completed"}
    status, body = _command(
        ctx.service,
        ctx.ws,
        {
            "type": "record_research_observation",
            "payload": {
                "observation_id": str(uuid4()),
                "campaign_id": str(campaign_id),
                "trial_id": str(trial_id),
                "issuer_kind": "candidate_authored",
                "source_ref": f"completed-{uuid4()}",
                "execution_status": "completed",
                "commit_sha": "cd" * 20,
                "payload": payload,
                "payload_sha256": sha256(payload),
                "observed_at": datetime.now(UTC).isoformat(),
            },
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["observation"]["execution_status"] == "completed"
    campaign = next(
        row for row in _research(ctx)["campaigns"] if row["campaign_id"] == str(campaign_id)
    )
    assert campaign["state"] == "running"
    assert campaign["outcome"] is None
    assert campaign["outcome_reason"] is None
    assert campaign["concluded_at"] is None
