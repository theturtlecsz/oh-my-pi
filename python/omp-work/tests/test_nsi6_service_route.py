"""OMP-523-s05: assess, publish and mission-intake consumers share one NSI-6 evaluator.

Defended contracts: the route state under ``config_dir`` selects the evaluator for
all three consumers per request; qualified typed never reaches the legacy
evaluator; rollback restores current; a stale or malformed typed binding refuses
(never ready); rejected drafts touch no database row; the pure helper defaults to
current. Evidence and state live under temporary directories only.
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

from omp_work import mission_intake_rules, nsi_rollout, nsi_route
from omp_work.contracts.nsi.v1 import compat
from omp_work.operations.database import bootstrap
from omp_work.v1 import mission_intake
from omp_work.v1.client import WorkClient
from omp_work.v1.models import (
    AssessBoundedIntakeCommand, AssessBoundedIntakePayload, BoundedIntakeDraft, CommandEnvelope,
    DraftMissionIntakePayload,
)
from omp_work.v1.semantics import bounded_intake_semantic_sha256
from omp_work.v1.server import create_app
from omp_work.v1.store import PostgresWorkStore
from omp_work.v1.store_shared import WorkStoreError
from pg_native import native_postgres, seed_authority
from test_mission_intake_e2e import OWNER_SCOPES, _bearer, _config, _instruction, _intake, _scope, _seed_project
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


@pytest.mark.parametrize("drift", ["code", "corpus", "malformed"])
def test_stale_typed_binding_never_reports_ready(evidence, monkeypatch, calls, drift):
    ready = _corpus_draft(evidence, oracle=True)
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
    store = PostgresWorkStore(evidence.config)
    with pytest.raises(WorkStoreError, match="intake_not_ready"):
        _consumers(store, ready)
    assert calls == {"legacy": 0, "typed": 0}


def test_pure_helper_omitted_selection_stays_current(evidence, monkeypatch, calls):
    _promote(evidence, monkeypatch)
    draft = _corpus_draft(evidence, oracle=False)
    assert mission_intake_rules.clarifying_questions(draft) == mission_intake_rules.clarifying_questions(draft, "current")
    assert calls["typed"] == 0 and calls["legacy"] == 2
    assert mission_intake_rules.clarifying_questions(draft, "typed")
    assert calls["typed"] == 1


@pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="set OMP_WORK_POSTGRES_INTEGRATION=1")
def test_requests_restart_and_saved_assessment_replay(evidence, monkeypatch, tmp_path):
    config = _config(tmp_path)
    # Route state lives next to the service credentials; evidence stays in the fixture tree.
    state = config.config_dir / "nsi-route.json"
    monkeypatch.setattr(evidence, "state", state)
    with native_postgres(tmp_path, config.port):
        monkeypatch.setattr("omp_work.operations.database.validate_bundle", lambda **kw: None)
        bootstrap(config)
        workspace_id, project_id, owner_id = uuid4(), uuid4(), uuid4()
        seed_authority(config.connection_kwargs("postgres"), workspace_id, owner_id)
        _seed_project(config, workspace_id, project_id)
        capabilities = tmp_path / "capabilities"
        bearer = _bearer(capabilities, "owner.json", token="owner-token", actor_id=owner_id,
                         actor_kind="owner", workspace_id=workspace_id, scopes=OWNER_SCOPES)
        draft = _corpus_draft(evidence, oracle=False).model_dump(mode="json")

        def service():
            """A fresh app per call models a service restart over the same config_dir."""
            tc = TestClient(create_app(config, capabilities_dir=capabilities))
            return tc, WorkClient("http://testserver", workspace_id, bearer, transport=tc._transport)

        def assess(client, operation_id):
            return client.execute(_assess_envelope(workspace_id, operation_id, draft)).result

        current_op = uuid4()
        _tc, client = service()
        before = assess(client, current_op)
        assert before.ready_for_ratification is False

        # Mission consumer: clarify audit only; no mission lifecycle event or work item.
        def mission_request(client, mission_id):
            return client.draft_mission_intake(DraftMissionIntakePayload.model_validate({
                "mission_id": str(mission_id), "base_revision": None,
                "intake": _intake(criteria=(("ac-1", "RSS <= 256MB", None),)),
                "scope": _scope(project_id), "instruction": _instruction()}))

        mission_id = uuid4()
        current_clarify = mission_request(client, mission_id).result
        assert current_clarify.outcome == "clarify" and current_clarify.mission is None

        _promote(evidence, monkeypatch)
        _tc, client = service()
        typed = assess(client, uuid4())
        assert typed.issue_count == 4 and [q.claim_ids for q in typed.questions] == [("ac-export",), ("ac-failure",)]
        assert typed.semantic_sha256 == before.semantic_sha256
        # Saved assessment replays byte-identically; routing never rewrites stored results.
        assert assess(client, current_op) == before
        typed_clarify = mission_request(client, uuid4()).result
        assert typed_clarify.outcome == "clarify" and typed_clarify.mission is None

        nsi_rollout.rollback(evidence.state, tmp_path / "rollback.json")
        _tc, client = service()
        assert assess(client, uuid4()).questions == before.questions

        with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
            created = conn.execute(
                "SELECT count(*) FROM omp_audit.domain_events WHERE aggregate_type='mission' AND outcome='applied' AND event_type <> 'draft_mission_intake'"
            ).fetchone()[0]
            items = conn.execute("SELECT count(*) FROM omp_work.work_items WHERE workspace_id=%s", (workspace_id,)).fetchone()[0]
        assert (created, items) == (0, 0)


def _assess_envelope(workspace_id, operation_id, draft):
    return CommandEnvelope(
        api_version="work.omp.dev/v1", workspace_id=workspace_id, operation_id=operation_id,
        request_id=uuid4(), correlation_id=uuid4(),
        command=AssessBoundedIntakeCommand(type="assess_bounded_intake",
                                           payload=AssessBoundedIntakePayload(draft=BoundedIntakeDraft.model_validate(draft))))
