"""OMP-523-s05: assess, publish and mission-intake consumers share one NSI-6 evaluator.

Defended contracts: the route state under ``config_dir`` selects the evaluator for
all three consumers per request; qualified typed never reaches the legacy
evaluator; rollback restores current; a stale or malformed typed binding refuses
(never ready); rejected requests create no mission or item; positive assessment
replay preserves history without bypassing publication gates; the pure helper defaults to current. Evidence and state live under temporary directories only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from starlette.testclient import TestClient

from omp_work import contract_sha256, mission_intake_rules, nsi_rollout, nsi_route
from omp_work.contracts.nsi.v1 import compat
from omp_work.operations.database import bootstrap
from omp_work.v1 import mission_intake
from omp_work.v1.models import BoundedIntakeDraft
from omp_work.v1.semantics import bounded_intake_semantic_sha256
from omp_work.v1.server import create_app
from omp_work.v1.store import PostgresWorkStore
from omp_work.v1.store_shared import WorkStoreError
from pg_native import native_postgres
from test_bounded_intake_publish import OWNER, _draft, _envelope, _row_counts, _seed_omp249
from test_mission_intake_e2e import OWNER_SCOPES, _bearer, _config, _instruction, _scope, _seed_project
from test_nsi6_rollout import evidence, record_review, report  # noqa: F401  (read-only fixture reuse)


class NoDatabase:
    """Cursor stand-in: any use proves a rejected draft reached persistence."""

    def __getattr__(self, name):
        raise AssertionError(f"database touched: {name}")


def _corpus_draft(evidence, *, oracle: bool) -> BoundedIntakeDraft:
    cases = json.loads((evidence.corpus / "inputs/cases.json").read_bytes())["cases"]
    raw = next(c for c in cases if c["id"] == "ready-complete")["input"]["bounded"]["draft"]
    if not oracle:
        for criterion in raw["acceptance_criteria"]:
            criterion["oracle"] = None
    return BoundedIntakeDraft.model_validate(raw)


def _promote(evidence, monkeypatch) -> None:
    report(evidence, monkeypatch)
    record_review(evidence, monkeypatch)
    nsi_rollout.promote(evidence.report, evidence.review, evidence.state, evidence.out)


@pytest.fixture
def calls(monkeypatch):
    """Count evaluator entries by lineage across every consumer module."""
    counts = {"legacy": 0, "typed": 0}
    for module, name, kind in (
        (nsi_route, "evaluate_bounded_intake", "legacy"),
        (mission_intake_rules, "evaluate_bounded_intake", "legacy"),
        (compat, "evaluate_bounded_intake", "legacy"),
        (nsi_route, "evaluate_typed_bounded_intake", "typed"),
        (mission_intake_rules, "evaluate_typed_bounded_intake", "typed"),
    ):
        original = getattr(module, name)

        def counted(draft, original=original, kind=kind):
            counts[kind] += 1
            return original(draft)

        monkeypatch.setattr(module, name, counted)
    return counts


def _consumers(store, draft):
    """Run all three consumers on one draft; return (assess, publish error, mission questions)."""
    assess = store._assess_bounded_intake(SimpleNamespace(command=SimpleNamespace(payload=SimpleNamespace(draft=draft))))
    publish = SimpleNamespace(draft=draft, ratified_semantic_sha256=bounded_intake_semantic_sha256(draft))
    try:
        store._publish_bounded_intake(NoDatabase(), SimpleNamespace(command=SimpleNamespace(payload=publish)), uuid4(), "owner")
        published = None
    except WorkStoreError as error:
        published = error
    payload = SimpleNamespace(mission_id=uuid4(), intake=draft, instruction=None)
    result, _event = mission_intake.draft_mission_intake(
        NoDatabase(), SimpleNamespace(workspace_id=uuid4(), command=SimpleNamespace(payload=payload)),
        uuid4(), "owner", None, None, config=store._config)
    return assess, published, result["questions"]


def test_cycle_current_typed_current_shares_one_evaluator(evidence, monkeypatch, calls):
    monkeypatch.setattr(mission_intake, "latest_mission", lambda cur, workspace, mission: None)
    store = PostgresWorkStore(evidence.config)
    draft = _corpus_draft(evidence, oracle=False)

    assess, published, questions = _consumers(store, draft)
    assert (calls["legacy"] > 0, calls["typed"]) == (True, 0)
    current_questions = assess["questions"]
    assert not assess["ready_for_ratification"] and published.code == "intake_not_ready"
    assert [q["deduplication_key"] for q in current_questions] == [q["deduplication_key"] for q in questions]

    _promote(evidence, monkeypatch)
    calls.update(legacy=0, typed=0)
    assess, published, questions = _consumers(PostgresWorkStore(evidence.config), draft)
    assert calls["legacy"] == 0 and calls["typed"] == 3
    assert assess["issue_count"] == 4 and not assess["ready_for_ratification"]
    assert [q["claim_ids"] for q in assess["questions"]] == [["ac-export"], ["ac-failure"]]
    assert published.code == "intake_not_ready"
    assert [q["deduplication_key"] for q in questions] == [q["deduplication_key"] for q in assess["questions"]]
    assert assess["semantic_sha256"] == bounded_intake_semantic_sha256(draft)

    nsi_rollout.rollback(evidence.state, evidence.out.with_name("rollback.json"))
    calls.update(legacy=0, typed=0)
    assess, published, _ = _consumers(PostgresWorkStore(evidence.config), draft)
    assert calls["typed"] == 0 and calls["legacy"] > 0
    assert assess["questions"] == current_questions

    out = os.environ.get("NSI6_ROLLBACK_OUT")
    if out:
        Path(out).write_text(json.dumps({
            "verified": True,
            "receipt": f"current->typed->current rehearsal; rollback restored {nsi_route.route_mode(evidence.config)}",
        }, indent=2) + "\n")


def test_pure_helper_omitted_selection_stays_current(evidence, monkeypatch, calls):
    _promote(evidence, monkeypatch)
    draft = _corpus_draft(evidence, oracle=False)
    assert mission_intake_rules.clarifying_questions(draft) == mission_intake_rules.clarifying_questions(draft, "current")
    assert calls["typed"] == 0 and calls["legacy"] == 2
    assert mission_intake_rules.clarifying_questions(draft, "typed")
    assert calls["typed"] == 1


@pytest.fixture
def service_world(evidence, monkeypatch, tmp_path):
    if os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1":
        pytest.skip("set OMP_WORK_POSTGRES_INTEGRATION=1")
    config = _config(tmp_path)
    evidence.state = config.config_dir / "nsi-route.json"
    with native_postgres(tmp_path, config.port):
        monkeypatch.setattr("omp_work.operations.database.validate_bundle", lambda **kw: None)
        bootstrap(config)
        ready = _draft()
        seeded = _seed_omp249(config, uuid4(), ready)
        project_id = uuid4()
        _seed_project(config, seeded.workspace_id, project_id)
        capabilities = tmp_path / "capabilities"
        _bearer(capabilities, "owner.json", token="owner-token", actor_id=OWNER,
                actor_kind="owner", workspace_id=seeded.workspace_id, scopes=OWNER_SCOPES)
        yield SimpleNamespace(config=config, seeded=seeded, ready=ready,
                              project_id=project_id, capabilities=capabilities, token="owner-token")


def _request(client, world, command, *, operation_id=None):
    return client.post("/v1/commands", headers={
        "Authorization": f"Bearer {world.token}",
        "X-OMP-Workspace-ID": str(world.seeded.workspace_id),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }, json=_envelope(world.seeded.workspace_id, command,
                     operation_id=operation_id).model_dump(mode="json"))


def _assessment(draft):
    return {"type": "assess_bounded_intake", "payload": {"draft": draft.model_dump(mode="json")}}


def _mission(world, draft):
    return {"type": "draft_mission_intake", "payload": {
        "mission_id": str(uuid4()), "base_revision": None,
        "intake": draft.model_dump(mode="json"), "scope": _scope(world.project_id),
        "instruction": _instruction().model_dump(mode="json"),
    }}


def _publication(world, draft, assessment_id, admission_id):
    return {"type": "publish_bounded_intake", "payload": {
        "draft": draft.model_dump(mode="json"),
        "assessment_operation_id": str(assessment_id),
        "ratified_semantic_sha256": bounded_intake_semantic_sha256(draft),
        "admission_work_id": str(world.seeded.work_id),
        "admission_revision_id": str(world.seeded.revision_id),
        "admission_receipt_id": str(admission_id),
    }}


def _assert_no_mission(world):
    with psycopg.connect(**world.config.connection_kwargs("postgres")) as conn:
        created = conn.execute(
            "SELECT count(*) FROM omp_audit.domain_events WHERE workspace_id=%s AND aggregate_type='mission' AND outcome='applied' AND event_type <> 'draft_mission_intake'",
            (world.seeded.workspace_id,),
        ).fetchone()[0]
    assert created == 0


def test_requests_restart_and_saved_assessment_replay(evidence, monkeypatch, service_world, calls, tmp_path):
    worlds = [service_world]
    # Independent publication lineages avoid the existing duplicate-open-title gate.
    for index in (1, 2):
        seeded = _seed_omp249(service_world.config, uuid4(), service_world.ready)
        project_id = uuid4()
        _seed_project(service_world.config, seeded.workspace_id, project_id)
        token = f"owner-token-{index}"
        _bearer(service_world.capabilities, f"owner-{index}.json", token=token,
                actor_id=OWNER, actor_kind="owner", workspace_id=seeded.workspace_id,
                scopes=OWNER_SCOPES)
        worlds.append(SimpleNamespace(config=service_world.config, seeded=seeded,
                                      ready=service_world.ready, project_id=project_id,
                                      capabilities=service_world.capabilities, token=token))
    # Save all positive assessments while current is selected, before cutover.
    for world in worlds:
        world.saved_id = uuid4()
        with TestClient(create_app(world.config, capabilities_dir=world.capabilities)) as client:
            response = _request(client, world, _assessment(world.ready), operation_id=world.saved_id)
            assert response.status_code == 200
            world.saved = response.json()["result"]
            assert world.saved["ready_for_ratification"] is True
            admitted = _request(client, world, {
                "type": "attest_intake_admission", "payload": world.seeded.attest_payload(),
            })
            assert admitted.status_code == 200
            world.admission_id = admitted.json()["result"]["receipt"]["receipt_id"]

    rejected = _corpus_draft(evidence, oracle=False)
    current_questions = None
    for index, mode in enumerate(("current", "typed", "current")):
        world = worlds[index]
        saved_id, saved, admission_id = world.saved_id, world.saved, world.admission_id
        if index == 1:
            _promote(evidence, monkeypatch)
        elif index == 2:
            nsi_rollout.rollback(evidence.state, tmp_path / "rollback.json")
        # Fresh application after each state transition exercises restart behavior.
        with TestClient(create_app(world.config, capabilities_dir=world.capabilities)) as client:
            calls.update(legacy=0, typed=0)
            replay = _request(client, world, _assessment(world.ready), operation_id=saved_id)
            assert replay.status_code == 200
            assert replay.json()["receipt"]["state"] == "replayed"
            assert replay.json()["result"] == saved
            assert calls == {"legacy": 0, "typed": 0}
            before = _row_counts(world.config, world.seeded.workspace_id)

            assessment = _request(client, world, _assessment(rejected))
            assert assessment.status_code == 200
            result = assessment.json()["result"]
            assert result["ready_for_ratification"] is False
            assert result["semantic_sha256"] == bounded_intake_semantic_sha256(rejected)
            mission = _request(client, world, _mission(world, rejected))
            assert mission.status_code == 200
            assert mission.json()["result"]["outcome"] == "clarify"
            assert mission.json()["result"]["mission"] is None
            assert mission.json()["result"]["questions"] == result["questions"]
            # A saved positive assessment cannot bless a different rejected draft.
            refused = _request(client, world, _publication(world, rejected, saved_id, admission_id))
            assert refused.status_code == 409
            assert refused.json()["error"]["code"] == "intake_not_ready"
            if mode == "typed":
                assert calls == {"legacy": 0, "typed": 3}
                assert result["issue_count"] == 4
                assert [q["claim_ids"] for q in result["questions"]] == [["ac-export"], ["ac-failure"]]
            else:
                assert calls == {"legacy": 3, "typed": 0}
                if current_questions is None:
                    current_questions = result["questions"]
                assert result["questions"] == current_questions

            # Ready but changed semantics still requires an exact positive assessment.
            changed = world.ready.model_copy(update={
                "goal": world.ready.goal.model_copy(update={"statement": "Change another cache flag"}),
            })
            stale = _request(client, world, _publication(world, changed, saved_id, admission_id))
            assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_intake"
            command = _publication(world, world.ready, saved_id, admission_id)
            bad_ratification = _publication(world, world.ready, saved_id, admission_id)
            bad_ratification["payload"]["ratified_semantic_sha256"] = "0" * 64
            stale = _request(client, world, bad_ratification)
            assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_intake"
            blocked = _request(client, world, _publication(world, world.ready, saved_id, uuid4()))
            assert blocked.status_code == 409
            assert blocked.json()["error"]["code"] == "intake_admission_blocked"
            assert _row_counts(world.config, world.seeded.workspace_id) == before
            _assert_no_mission(world)

            # Valid owner ratification and native lineage survive cutover and rollback.
            operation_id = uuid4()
            published = _request(client, world, command, operation_id=operation_id)
            assert published.status_code == 200, published.text
            assert published.json()["receipt"]["state"] == "applied"
            publication = published.json()["result"]
            assert publication["receipt"]["payload"]["draft"] == world.ready.model_dump(mode="json")
            assert publication["receipt"]["payload"]["semantic_sha256"] == saved["semantic_sha256"]
            assert publication["receipt"]["payload"]["ratified_by"] == str(OWNER)
            assert calls == ({"legacy": 0, "typed": 6} if mode == "typed" else {"legacy": 6, "typed": 0})

        with TestClient(create_app(world.config, capabilities_dir=world.capabilities)) as client:
            counts = calls.copy()
            replay = _request(client, world, command, operation_id=operation_id)
            assert replay.status_code == 200 and replay.json()["receipt"]["state"] == "replayed"
            assert replay.json()["result"] == publication and calls == counts
        assert _row_counts(world.config, world.seeded.workspace_id) == {
            "items": 2, "relations": 1, "candidates": 2, "publications": 1,
        }


@pytest.mark.parametrize("drift", ["code", "corpus", "malformed"])
def test_stale_typed_binding_never_reports_ready(evidence, monkeypatch, service_world, calls, drift):
    world = service_world
    saved_id = uuid4()
    with TestClient(create_app(world.config, capabilities_dir=world.capabilities)) as client:
        saved = _request(client, world, _assessment(world.ready), operation_id=saved_id)
        assert saved.status_code == 200 and saved.json()["result"]["ready_for_ratification"] is True
    _promote(evidence, monkeypatch)
    if drift == "code":
        binding = nsi_route.implementation_binding()
        binding["python/omp-work/src/omp_work/mission_intake_rules.py"] = "0" * 64
        monkeypatch.setattr(nsi_route, "implementation_binding", lambda: binding)
    elif drift == "corpus":
        path = evidence.corpus / "inputs/cases.json"
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        evidence.state.write_text("{}")
    calls.update(legacy=0, typed=0)
    before = _row_counts(world.config, world.seeded.workspace_id)
    with TestClient(create_app(world.config, capabilities_dir=world.capabilities)) as client:
        # Replay remains historical; each fresh readiness consumer must refuse independently.
        replay = _request(client, world, _assessment(world.ready), operation_id=saved_id)
        assert replay.status_code == 200 and replay.json()["result"] == saved.json()["result"]
        for command in (
            _assessment(world.ready),
            _publication(world, world.ready, saved_id, uuid4()),
            _mission(world, world.ready),
        ):
            refused = _request(client, world, command)
            assert refused.status_code == 409
            assert refused.json()["error"]["code"] == "intake_not_ready"
            assert "NSI-6 route state invalid" in str(refused.json())
    assert calls == {"legacy": 0, "typed": 0}
    assert _row_counts(world.config, world.seeded.workspace_id) == before
    _assert_no_mission(world)
