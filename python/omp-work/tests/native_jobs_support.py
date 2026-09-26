"""Fixture support for native research job tests (R03, OMP-324).

Builds a migrated service with one workspace, the six R02 component kinds, an
admitted campaign whose compatibility manifest lists them, and a proposed trial —
the minimum a native research job binds to. ``apply_jobs_migrations`` runs the
additive 0003 set on the same service the R02 tests use; nothing here touches
the flood sqlite loop or the shadow mirror.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from omp_work.operations.jobs_migrate import apply_jobs_migrations
from test_research_contract import (
    _admit_payload,
    _manifest,
    _register_component,
    _sample_spec,
    _trial_payload,
)
from test_workflow_service import OWNER, _command, _create, _grant

_COMPONENT_KINDS = (
    "worker",
    "evaluator",
    "policy",
    "audit",
    "release",
    "environment",
)


@pytest.fixture(scope="module")
def native_jobs(service):
    """One migrated workspace with a worker component, admitted campaign, trial."""
    apply_jobs_migrations(service.config)
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "native jobs target")
    spec, spec_sha = _sample_spec()

    components: dict[str, str] = {}
    for kind in _COMPONENT_KINDS:
        capabilities = ("compute.gpu", "compute.cpu") if kind == "worker" else ()
        components[kind] = _register_component(
            service,
            workspace_id,
            kind,
            name=f"jobs-{kind}",
            capabilities=capabilities,
        )
    manifest, manifest_sha = _manifest(
        workers=(components["worker"],),
        evaluators=(components["evaluator"],),
        audits=(components["audit"],),
        releases=(components["release"],),
        environments=(components["environment"],),
    )

    campaign_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": item["work_id"],
                "revision_id": item["revision_id"],
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    assert status == 200, body
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                campaign_id,
                item["work_id"],
                item["revision_id"],
                spec_sha,
                components["policy"],
                manifest,
                manifest_sha,
            ),
        },
    )
    assert status == 200, body

    trial_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                campaign_id,
                item["work_id"],
                components["policy"],
                components["evaluator"],
                components["environment"],
                trial_id=trial_id,
            ),
        },
    )
    assert status == 200, body

    return SimpleNamespace(
        service=service,
        workspace_id=workspace_id,
        actor_id=OWNER,
        item=item,
        campaign_id=campaign_id,
        trial_id=trial_id,
        components=components,
        manifest=manifest,
        manifest_sha=manifest_sha,
    )
