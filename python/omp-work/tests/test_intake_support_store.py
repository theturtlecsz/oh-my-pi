"""Wire the label-blind support judge into claim writes (OMP-604-s06-s02 through s06-s04).

Echo claim writes require no budget, call no model completion, and record no
usage: the stored link retains the caller relation and an echo support receipt
binding the artifact byte span.

Live judge claim writes resolve budget admission, call model completion (or a
scripted hook), settle usage under O8 in its own transaction, and persist
support receipts binding the artifact byte span.

HTTP writes through ``complete_from_env`` persist a forced transport timeout,
measured and tokenless O8 usage, and receipts ``verify_receipt`` can check.
Replay and admission do not call the judge again.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from decimal import Decimal
import hashlib
import os
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg.rows import dict_row
import pytest

from omp_work.contracts.nsi.v1 import models as nsi
from omp_work.frozen_corpus import verify as verify_corpus
from omp_work.intake_repository import append_revision, lock_revision, read_revision, revision_hashes
from omp_work.intake_support import SourceSpan, render_claim_text, verify_receipt
from omp_work.intake_support_judge import (
    Completion, JudgeHook, complete_from_env, corpus_dir, current_hook, prompt_sha256, use_judge_hook,
)
from omp_work.operations.jobs_migrate import apply_jobs_migrations
from omp_work.routing.policy import load_policy
from omp_work.routing.router import route_stage
from omp_work.v1.api_models import IntakeRecordView, IntakeSupportJudgment
from omp_work.v1.canonical import text_sha256
from omp_work.v1.intake_commands import execute_intake, read_intake_view
from omp_work.v1.store_shared import WorkStoreError
from test_nsi2_claims import (
    claim_command,
    claim_envelope,
    envelope,
    evidence_command,
    http_intake,  # noqa: F401
)
from test_nsi2_repository import connection, revision_body
from test_workflow_service import OWNER, _command, _owner_headers, service  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

RUN_ID = "run-1"


@pytest.fixture(scope="module", autouse=True)
def _jobs_schema(service):  # noqa: F811 - reuse the shared service fixture
    """The usage ledger lives in omp_jobs; ensure its schema exists once."""
    apply_jobs_migrations(service.config)


def _scripted(verdict: str, *, input_tokens: int | None = 7, output_tokens: int | None = 3):
    """A completion that always returns one verdict, counting its calls."""
    seen: list[tuple[str, str]] = []

    def complete(prompt: str, model_id: str) -> Completion:
        seen.append((prompt, model_id))
        return Completion(f'{{"verdict": "{verdict}"}}', input_tokens, output_tokens)

    complete.seen = seen  # type: ignore[attr-defined]
    return complete


JUDGE_URL = "https://judge.invalid/complete"
_NULL_USAGE = ("input_tokens", "output_tokens", "cache_tokens", "tokens", "price_usd")


class _JudgeReply:
    """Stand-in for the JSON body ``complete_from_env`` reads. It never opens a socket."""

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


def _patch_judge_post(monkeypatch, respond: Callable[[], object]) -> list[dict[str, object]]:
    """Force ``httpx.Client.post`` only for the temporary judge URL.

    Every other URL, including the in-process HTTP test client, keeps the real
    method. The patch raises or returns before any network IO.
    """
    original = httpx.Client.post
    seen: list[dict[str, object]] = []

    def patched(self, url, *args, **kwargs):
        if "judge.invalid" in str(url):
            body = kwargs.get("json")
            seen.append(dict(body) if isinstance(body, dict) else {})
            return respond()
        return original(self, url, *args, **kwargs)

    monkeypatch.setenv("OMP_INTAKE_JUDGE_URL", JUDGE_URL)
    monkeypatch.setattr(httpx.Client, "post", patched)
    return seen


def _timeout_reply() -> object:
    raise httpx.ReadTimeout("forced read timeout")


def _research_model() -> str:
    return route_stage(load_policy(), "research").model


def _sealed_rule_version() -> str:
    base = corpus_dir()
    sealed = verify_corpus(base / "inputs", base / "manifest.json")
    return next(item.sha256 for item in sealed.files if item.path == "rules.json")


def _assert_echo_hook_restored() -> None:
    hook = current_hook()
    assert hook is not None and hook.echo_caller and hook.complete is None


def _start_and_evidence(service, workspace_id, mission_id, *, text: str | None = None) -> None:
    assert _command(
        service, workspace_id,
        envelope(workspace_id, mission_id).command.model_dump(mode="json"),
    )[0] == 200
    evidence = evidence_command(OWNER, "owner") if text is None else evidence_command(OWNER, "owner", text=text)
    assert _command(service, workspace_id, evidence)[0] == 200


def _seed_budget(service, workspace_id: UUID, mission_id: UUID, amount: str = "100") -> None:
    """Seed an active mission budget so resolve_admission succeeds."""
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True,
    ) as conn:
        row = conn.execute(
            "SELECT project_id FROM omp_work.project_missions WHERE workspace_id=%s AND mission_id=%s",
            (workspace_id, mission_id),
        ).fetchone()
        project_id = row[0]
        conn.execute(
            "INSERT INTO omp_work.spend_budgets "
            "(workspace_id, record_id, project_id, budget_id, mission_id, ceiling_usd, threshold_usd, decision_id, active) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true)",
            (workspace_id, uuid4(), project_id, str(mission_id), mission_id, Decimal(amount), Decimal(amount), uuid4()),
        )


def _domain_events(service, workspace_id: UUID) -> list[tuple[str, dict[str, object]]]:
    with connection(service, workspace_id) as conn:
        return conn.execute(
            "SELECT event_type, payload FROM omp_audit.domain_events WHERE workspace_id=%s ORDER BY sequence",
            (workspace_id,),
        ).fetchall()


def _stored_links(service, workspace_id, revision):
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        return read_revision(
            cur, workspace_id=workspace_id, run_id=RUN_ID, revision=revision,
        ).body.evidence_links


def _usage_count(service, workspace_id) -> int:
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True,
    ) as conn:
        return conn.execute(
            "SELECT count(*) FROM omp_jobs.usage_events WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()[0]


def _usage_row(service, usage_id: str) -> dict[str, object] | None:
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True,
    ) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT * FROM omp_jobs.usage_events WHERE usage_id=%s",
                (usage_id,),
            ).fetchone()


def _usage_rows(service, workspace_id) -> list[dict[str, object]]:
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True,
    ) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return list(cur.execute(
                "SELECT usage_id, request_id, work_id, role_derived, model, "
                "input_tokens, output_tokens, cache_tokens, tokens, price_usd, measurement "
                "FROM omp_jobs.usage_events WHERE workspace_id=%s ORDER BY step_index",
                (workspace_id,),
            ).fetchall())


def _revision_count(service, workspace_id) -> int:
    with connection(service, workspace_id) as conn:
        return conn.execute(
            "SELECT count(*) FROM omp_work.intake_revisions WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()[0]


def _head_revision(service, workspace_id) -> int:
    with connection(service, workspace_id) as conn:
        return conn.execute(
            "SELECT current_revision FROM omp_work.intake_heads WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()[0]


def _receipt_texts(service, workspace_id, revision: int) -> list[str | None]:
    with connection(service, workspace_id) as conn:
        rows = conn.execute(
            "SELECT support_receipt::text FROM omp_work.intake_claim_evidence "
            "WHERE workspace_id=%s AND revision=%s ORDER BY start_byte",
            (workspace_id, revision),
        ).fetchall()
    return [row[0] for row in rows]


def _http_judgments(service, workspace_id, revision: int | None = None) -> tuple[IntakeSupportJudgment, ...]:
    path = f"/v1/workspaces/{workspace_id}/intake/{RUN_ID}"
    if revision is not None:
        path += f"?revision={revision}"
    response = service.client.get(path, headers=_owner_headers(workspace_id))
    assert response.status_code == 200
    return IntakeRecordView.model_validate(response.json()).support_judgments


def _claim_text(claim: dict, **overrides: object) -> str:
    projection = {
        "subject_id": claim["subject"]["id"],
        "predicate_id": claim["predicate_id"],
        "predicate_title": "Depends on",
        "value": claim["value"],
        "modality": claim["modality"],
        "polarity": claim["polarity"],
    }
    projection.update(overrides)
    return render_claim_text(projection)


def _assert_unknown_usage(row: dict[str, object], mission_id) -> None:
    assert row["measurement"] == "unknown"
    assert row["work_id"] == mission_id
    assert row["role_derived"] == "intake_support_judge"
    assert row["request_id"]
    for column in _NULL_USAGE:
        assert row[column] is None


def _assert_measured_usage(row: dict[str, object], mission_id, input_tokens: int, output_tokens: int) -> None:
    assert row["measurement"] == "measured"
    assert row["input_tokens"] == input_tokens
    assert row["output_tokens"] == output_tokens
    assert row["work_id"] == mission_id
    assert row["role_derived"] == "intake_support_judge"
    assert row["request_id"]
    assert row["cache_tokens"] is None
    assert row["tokens"] is None
    assert row["price_usd"] is None


def _assert_stored_bindings(receipt, *, claim_text: str, changed_claim_text: str, quote: str, context: str) -> None:
    """The stored receipt matches the call bindings and fails when any one changes."""
    model_id = _research_model()
    prompt_version = prompt_sha256()
    rule_version = _sealed_rule_version()
    source_revision = text_sha256(context)
    span = receipt.source_span
    assert changed_claim_text != claim_text
    assert receipt.model_id == receipt.model_version == model_id
    assert receipt.prompt_version == prompt_version
    assert receipt.rule_version == rule_version
    assert receipt.source_revision == receipt.context_sha256 == source_revision
    assert receipt.passage_sha256 == text_sha256(quote)
    assert receipt.claim_text_sha256 == text_sha256(claim_text)
    original = dict(
        claim_text=claim_text, quote=quote, model_id=model_id, model_version=model_id,
        prompt_version=prompt_version, rule_version=rule_version,
        source_span=span, source_revision=source_revision,
    )
    assert verify_receipt(receipt, **original)
    changed = {
        "quote": {**original, "quote": quote + "x"},
        "claim_text": {**original, "claim_text": changed_claim_text},
        "source_span": {**original, "source_span": SourceSpan(start=span.start + 1, end=span.end + 1)},
        "source_revision": {**original, "source_revision": text_sha256(context + "x")},
        "model_id": {**original, "model_id": model_id + "-other"},
        "model_version": {**original, "model_version": model_id + "-other"},
        "prompt_version": {**original, "prompt_version": "0" * 64},
        "rule_version": {**original, "rule_version": "1" * 64},
    }
    for name, kwargs in changed.items():
        assert verify_receipt(receipt, **kwargs) is False, name


def test_echo_write_needs_no_budget_completion_or_usage(service, http_intake):
    workspace_id, mission_id = http_intake
    _start_and_evidence(service, workspace_id, mission_id)
    complete = _scripted("contradicts")
    with use_judge_hook(JudgeHook(echo_caller=True, complete=complete)):
        assert _command(service, workspace_id, claim_command(OWNER, "owner"))[0] == 200
    assert complete.seen == []
    assert _usage_count(service, workspace_id) == 0
    links = _stored_links(service, workspace_id, 2)
    assert len(links) == 1
    link = links[0]
    assert link.relation == "supports"
    receipt = link.support_receipt
    assert receipt is not None
    assert receipt.verdict == "supports"
    assert receipt.usage_id is None
    assert receipt.source_span == SourceSpan(start=link.start_byte, end=link.end_byte)


@pytest.mark.parametrize(
    ("verdict", "expected_status"),
    [
        ("supports", "supported"),
        ("contradicts", "contested"),
        ("neither", "unsupported"),
        ("abstain", "unsupported"),
        ("timeout", "unsupported"),
        ("evidence-unavailable", "unsupported"),
    ],
)
def test_budget_scripted_verdict_derives_status_and_stores_receipt(
    service, http_intake, verdict: str, expected_status: str,
):
    """Budget + scripted verdict => status and stored receipt across all six verdicts.

    GET head and pinned revision verify every state/binding against DB receipt and computed inputs;
    only supports agrees. Includes nonzero byte span with Unicode context.
    """
    workspace_id, mission_id = http_intake
    assert _command(
        service, workspace_id,
        envelope(workspace_id, mission_id).command.model_dump(mode="json"),
    )[0] == 200
    context_text = "prefix: aé🙂 suffix"
    ev_cmd = evidence_command(OWNER, "owner", text=context_text)
    assert _command(service, workspace_id, ev_cmd)[0] == 200

    _seed_budget(service, workspace_id, mission_id)
    complete = _scripted(verdict)
    passage = "aé🙂"
    span = {
        "artifact_id": "artifact-added",
        "start_byte": 8,
        "end_byte": 15,
        "quote": passage,
    }
    cmd = claim_command(
        OWNER, "owner",
        evidence=[span],
        evidence_links=[{"span": span, "relation": "supports"}],
    )
    with use_judge_hook(JudgeHook(complete=complete)):
        status, body = _command(service, workspace_id, cmd)
    assert status == 200
    assert len(complete.seen) == 1
    assert _usage_count(service, workspace_id) == 1

    events = _domain_events(service, workspace_id)
    claim_event = events[-1][1]
    assert claim_event["type"] == "record_intake_claim"
    assert claim_event["evidence_status"] == expected_status

    links = _stored_links(service, workspace_id, 2)
    assert len(links) == 1
    link = links[0]
    assert link.relation == "supports"  # Caller relation
    receipt = link.support_receipt
    assert receipt is not None
    assert receipt.verdict == verdict
    assert receipt.measurement == "measured"
    assert receipt.input_tokens == 7
    assert receipt.output_tokens == 3
    assert receipt.usage_id is not None
    assert receipt.source_span == SourceSpan(start=8, end=15)
    assert receipt.source_span.start > 0

    usage = _usage_row(service, receipt.usage_id)
    assert usage is not None
    assert usage["role_derived"] == "intake_support_judge"
    assert usage["measurement"] == "measured"
    assert usage["input_tokens"] == 7
    assert usage["output_tokens"] == 3

    # Computed inputs
    claim_payload_data = cmd["payload"]["claim"]
    claim_text = render_claim_text({
        "subject_id": claim_payload_data["subject"]["id"],
        "predicate_id": claim_payload_data["predicate_id"],
        "predicate_title": "Depends on",
        "value": claim_payload_data["value"],
        "modality": claim_payload_data["modality"],
        "polarity": claim_payload_data["polarity"],
    })
    expected_claim_sha256 = text_sha256(claim_text)
    expected_passage_sha256 = text_sha256(passage)
    expected_context_sha256 = text_sha256(context_text)

    # GET head and pinned revision (both HTTP and read_intake_view)
    head_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/{RUN_ID}",
        headers=_owner_headers(workspace_id),
    )
    assert head_resp.status_code == 200
    head_view = IntakeRecordView.model_validate(head_resp.json())

    pinned_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/{RUN_ID}?revision=3",
        headers=_owner_headers(workspace_id),
    )
    assert pinned_resp.status_code == 200
    pinned_view = IntakeRecordView.model_validate(pinned_resp.json())

    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        direct_head = read_intake_view(cur, workspace_id, RUN_ID)
        direct_pinned = read_intake_view(cur, workspace_id, f"{RUN_ID}@3")

    for view in (head_view, pinned_view, direct_head, direct_pinned):
        assert len(view.evidence_links) == 1
        assert len(view.support_judgments) == 1
        judgment = view.support_judgments[0]

        # Verify against DB receipt
        assert judgment.claim_id == link.claim_id
        assert judgment.artifact_id == link.artifact_id
        assert judgment.model_id == receipt.model_id
        assert judgment.start_byte == receipt.source_span.start
        assert judgment.end_byte == receipt.source_span.end
        assert judgment.caller_relation == link.relation
        assert judgment.verdict == receipt.verdict
        assert judgment.source_revision == receipt.source_revision
        assert judgment.model_version == receipt.model_version
        assert judgment.prompt_version == receipt.prompt_version
        assert judgment.rule_version == receipt.rule_version
        assert judgment.passage_sha256 == receipt.passage_sha256
        assert judgment.claim_text_sha256 == receipt.claim_text_sha256
        assert judgment.context_sha256 == receipt.context_sha256

        # Verify against computed inputs
        assert judgment.start_byte == 8
        assert judgment.end_byte == 15
        assert judgment.start_byte > 0
        assert judgment.caller_relation == "supports"
        assert judgment.verdict == verdict
        assert judgment.claim_text_sha256 == expected_claim_sha256
        assert judgment.passage_sha256 == expected_passage_sha256
        assert judgment.context_sha256 == expected_context_sha256
        assert judgment.source_revision == expected_context_sha256

        # Disagreement: only supports agrees
        if verdict == "supports":
            assert not judgment.disagreement
        else:
            assert judgment.disagreement
        assert judgment.disagreement == (judgment.caller_relation != judgment.verdict)


def test_no_budget_refuses_claim_write(service, http_intake):
    """No budget => forbidden; no revision, usage, or completion."""
    workspace_id, mission_id = http_intake
    _start_and_evidence(service, workspace_id, mission_id)
    complete = _scripted("supports")
    with use_judge_hook(JudgeHook(complete=complete)):
        status, body = _command(service, workspace_id, claim_command(OWNER, "owner"))
    assert status == 403
    assert body["error"]["code"] == "forbidden"
    assert complete.seen == []
    assert _usage_count(service, workspace_id) == 0

    with connection(service, workspace_id) as conn:
        assert conn.execute(
            "SELECT current_revision FROM omp_work.intake_heads WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM omp_work.intake_revisions WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone() == (2,)


def test_admission_copies_stored_receipts_without_rejudging(service, http_intake):
    """Admission copies stored receipts without rejudging."""
    workspace_id, mission_id = http_intake
    _start_and_evidence(service, workspace_id, mission_id)
    _seed_budget(service, workspace_id, mission_id)
    complete = _scripted("supports")
    with use_judge_hook(JudgeHook(complete=complete)):
        assert _command(service, workspace_id, claim_command(OWNER, "owner"))[0] == 200
    assert len(complete.seen) == 1
    assert _usage_count(service, workspace_id) == 1
    proposed_receipt = _stored_links(service, workspace_id, 2)[0].support_receipt
    assert proposed_receipt is not None

    proposal = claim_command(OWNER, "owner")
    admission = deepcopy(proposal)
    admission["payload"]["expected_revision"] = 3
    admission["payload"]["claim"].update(
        state="admitted", truth="true", admitted_by=dict(id=str(OWNER), kind="owner"),
    )
    with use_judge_hook(JudgeHook(complete=complete)):
        status, body = _command(service, workspace_id, admission)
    assert status == 200
    assert len(complete.seen) == 1
    assert _usage_count(service, workspace_id) == 1

    events = _domain_events(service, workspace_id)
    assert events[-1][1]["evidence_status"] == "supported"

    admitted_links = _stored_links(service, workspace_id, 3)
    assert len(admitted_links) == 1
    assert admitted_links[0].support_receipt == proposed_receipt


def test_admission_with_missing_receipts_reports_unsupported(service, http_intake):
    """Missing receipts never report support on admission."""
    workspace_id, mission_id = http_intake
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        seeded = revision_body(workspace_id, revision=0)
        seeded["snapshot"]["state"] = "reasoning"
        for artifact in seeded["snapshot"]["artifacts"]:
            artifact["content_hash"] = hashlib.sha256(seeded["artifact_bytes"][artifact["id"]]).hexdigest()
        seeded["supersessions"] = {}
        seeded["snapshot"]["ontology"]["predicates"][0]["authority"].append("model")
        proposed_claim = seeded["snapshot"]["claims"][0]
        span = proposed_claim["evidence"][0]
        seeded["evidence_links"] = [
            dict(claim_id=proposed_claim["id"], relation="supports", **span),
        ]
        seeded["binding"]["rule_bundle_hash"] = "1" * 64
        seeded["binding"]["schema_hash"] = "2" * 64
        seeded["binding"]["contract_hash"] = "3" * 64
        seeded_snapshot = nsi.IntakeSnapshot.model_validate(seeded["snapshot"])
        c_hash, s_hash = revision_hashes(seeded_snapshot, seeded["artifact_bytes"])
        seeded["binding"]["claim_set_hash"] = c_hash
        seeded["binding"]["source_hash"] = s_hash

        locked = lock_revision(
            cur, workspace_id=workspace_id, run_id=RUN_ID, expected_revision=-1,
            mission_id=mission_id, operation_id=uuid4(),
        )
        append_revision(cur, locked, seeded, operation_id=uuid4())

    assert _stored_links(service, workspace_id, 0)[0].support_receipt is None

    _seed_budget(service, workspace_id, mission_id)
    complete = _scripted("supports")

    admitted_claim = deepcopy(proposed_claim)
    admitted_claim.update(
        state="admitted", truth="true", admitted_by=dict(id=str(OWNER), kind="owner"),
    )
    admit_cmd = {
        "type": "record_intake_claim",
        "payload": {
            "run_id": RUN_ID,
            "expected_revision": 1,
            "claim": admitted_claim,
            "evidence_links": [dict(span=span, relation="supports")],
        },
    }
    with use_judge_hook(JudgeHook(complete=complete)):
        status, body = _command(service, workspace_id, admit_cmd)
    assert status == 200
    assert complete.seen == []  # Did not re-judge

    events = _domain_events(service, workspace_id)
    assert events[-1][1]["evidence_status"] == "unsupported"

    admitted_links = _stored_links(service, workspace_id, 1)
    assert len(admitted_links) == 1
    assert admitted_links[0].support_receipt is None

    # Legacy link without receipt produces no judgments on GET head and pinned revision
    head_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/{RUN_ID}",
        headers=_owner_headers(workspace_id),
    )
    assert head_resp.status_code == 200
    head_view = IntakeRecordView.model_validate(head_resp.json())
    assert len(head_view.evidence_links) == 1
    assert head_view.support_judgments == ()

    pinned_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/{RUN_ID}?revision=1",
        headers=_owner_headers(workspace_id),
    )
    assert pinned_resp.status_code == 200
    pinned_view = IntakeRecordView.model_validate(pinned_resp.json())
    assert len(pinned_view.evidence_links) == 1
    assert pinned_view.support_judgments == ()

    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        direct_head = read_intake_view(cur, workspace_id, RUN_ID)
        direct_pinned = read_intake_view(cur, workspace_id, f"{RUN_ID}@1")
        assert len(direct_head.evidence_links) == 1
        assert direct_head.support_judgments == ()
        assert len(direct_pinned.evidence_links) == 1
        assert direct_pinned.support_judgments == ()


def test_forced_timeout_persists_timeout_receipt(service, http_intake):
    """Forced timeout persists timeout receipt with unknown usage (null tokens)."""
    workspace_id, mission_id = http_intake
    _start_and_evidence(service, workspace_id, mission_id)
    _seed_budget(service, workspace_id, mission_id)

    seen: list[tuple[str, str]] = []

    def complete_timeout(prompt: str, model_id: str) -> Completion:
        seen.append((prompt, model_id))
        raise httpx.ReadTimeout("simulated timeout")

    with use_judge_hook(JudgeHook(complete=complete_timeout)):
        status, body = _command(service, workspace_id, claim_command(OWNER, "owner"))
    assert status == 200
    assert len(seen) == 1

    events = _domain_events(service, workspace_id)
    assert events[-1][1]["evidence_status"] == "unsupported"

    links = _stored_links(service, workspace_id, 2)
    assert len(links) == 1
    link = links[0]
    assert link.relation == "supports"
    receipt = link.support_receipt
    assert receipt is not None
    assert receipt.verdict == "timeout"
    assert receipt.measurement == "unknown"
    assert receipt.input_tokens is None
    assert receipt.output_tokens is None
    assert receipt.usage_id is not None
    assert receipt.source_span == SourceSpan(start=link.start_byte, end=link.end_byte)

    assert _usage_count(service, workspace_id) == 1
    usage = _usage_row(service, receipt.usage_id)
    assert usage is not None
    assert usage["measurement"] == "unknown"
    assert usage["input_tokens"] is None
    assert usage["output_tokens"] is None
    assert usage["tokens"] is None
    assert usage["price_usd"] is None


def test_receipt_binds_nonzero_artifact_byte_span(service, http_intake):
    """Receipt binds nonzero artifact byte span."""
    workspace_id, mission_id = http_intake
    assert _command(
        service, workspace_id,
        envelope(workspace_id, mission_id).command.model_dump(mode="json"),
    )[0] == 200
    ev_cmd = evidence_command(OWNER, "owner", text="prefix: aé🙂 suffix")
    assert _command(service, workspace_id, ev_cmd)[0] == 200

    _seed_budget(service, workspace_id, mission_id)
    complete = _scripted("supports")

    span = {
        "artifact_id": "artifact-added",
        "start_byte": 8,
        "end_byte": 15,
        "quote": "aé🙂",
    }
    cmd = claim_command(
        OWNER, "owner",
        evidence=[span],
        evidence_links=[{"span": span, "relation": "supports"}],
    )
    with use_judge_hook(JudgeHook(complete=complete)):
        status, body = _command(service, workspace_id, cmd)
    assert status == 200

    links = _stored_links(service, workspace_id, 2)
    assert len(links) == 1
    link = links[0]
    assert link.start_byte == 8
    assert link.end_byte == 15
    assert link.support_receipt is not None
    assert link.support_receipt.source_span == SourceSpan(start=8, end=15)
    assert link.support_receipt.source_span.start > 0

    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        view = read_intake_view(cur, workspace_id, RUN_ID)
    assert len(view.support_judgments) == 1
    judgment = view.support_judgments[0]
    assert judgment.start_byte == 8
    assert judgment.end_byte == 15
    assert judgment.start_byte > 0


def test_receiptless_legacy_link_yields_no_invented_judgments(service, http_intake):
    """Receiptless legacy links yield no invented judgments while keeping evidence_links unchanged."""
    workspace_id, mission_id = http_intake
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        seeded = revision_body(workspace_id, revision=0)
        seeded["snapshot"]["state"] = "reasoning"
        for artifact in seeded["snapshot"]["artifacts"]:
            artifact["content_hash"] = hashlib.sha256(seeded["artifact_bytes"][artifact["id"]]).hexdigest()
        seeded["supersessions"] = {}
        seeded["snapshot"]["ontology"]["predicates"][0]["authority"].append("owner")
        claim = seeded["snapshot"]["claims"][0]
        span = claim["evidence"][0]
        # Legacy link with no support_receipt
        seeded["evidence_links"] = [
            dict(claim_id=claim["id"], relation="supports", **span),
        ]
        seeded["binding"]["rule_bundle_hash"] = "1" * 64
        seeded["binding"]["schema_hash"] = "2" * 64
        seeded["binding"]["contract_hash"] = "3" * 64
        seeded_snapshot = nsi.IntakeSnapshot.model_validate(seeded["snapshot"])
        c_hash, s_hash = revision_hashes(seeded_snapshot, seeded["artifact_bytes"])
        seeded["binding"]["claim_set_hash"] = c_hash
        seeded["binding"]["source_hash"] = s_hash

        locked = lock_revision(
            cur, workspace_id=workspace_id, run_id=RUN_ID, expected_revision=-1,
            mission_id=mission_id, operation_id=uuid4(),
        )
        append_revision(cur, locked, seeded, operation_id=uuid4())

    stored_link = _stored_links(service, workspace_id, 0)[0]
    assert stored_link.support_receipt is None

    # GET head
    head_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/{RUN_ID}",
        headers=_owner_headers(workspace_id),
    )
    assert head_resp.status_code == 200
    head_view = IntakeRecordView.model_validate(head_resp.json())
    assert len(head_view.evidence_links) == 1
    assert head_view.evidence_links[0].relation == "supports"
    assert head_view.support_judgments == ()

    # GET pinned revision
    pinned_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/{RUN_ID}?revision=1",
        headers=_owner_headers(workspace_id),
    )
    assert pinned_resp.status_code == 200
    pinned_view = IntakeRecordView.model_validate(pinned_resp.json())
    assert len(pinned_view.evidence_links) == 1
    assert pinned_view.evidence_links[0].relation == "supports"
    assert pinned_view.support_judgments == ()

    # read_intake_view direct
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        direct_head = read_intake_view(cur, workspace_id, RUN_ID)
        direct_pinned = read_intake_view(cur, workspace_id, f"{RUN_ID}@1")
        assert len(direct_head.evidence_links) == 1
        assert direct_head.support_judgments == ()
        assert len(direct_pinned.evidence_links) == 1
        assert direct_pinned.support_judgments == ()


def test_http_post_timeout_persists_unknown_usage(service, http_intake, monkeypatch):
    """A patched Client.post ReadTimeout is stored, not turned into support or zeros."""
    workspace_id, mission_id = http_intake
    _start_and_evidence(service, workspace_id, mission_id)
    _seed_budget(service, workspace_id, mission_id)
    prior_url = os.environ.get("OMP_INTAKE_JUDGE_URL")
    original_post = httpx.Client.post
    with monkeypatch.context() as scoped:
        seen = _patch_judge_post(scoped, _timeout_reply)
        with use_judge_hook(JudgeHook(complete=complete_from_env)):
            assert current_hook().complete is complete_from_env
            assert not current_hook().echo_caller
            status, _body = _command(service, workspace_id, claim_command(OWNER, "owner"))
        _assert_echo_hook_restored()
    assert httpx.Client.post is original_post
    assert os.environ.get("OMP_INTAKE_JUDGE_URL") == prior_url
    assert status == 200
    assert len(seen) == 1
    assert seen[0]["model"] == _research_model()
    assert "prompt" in seen[0]

    events = _domain_events(service, workspace_id)
    assert events[-1][1]["type"] == "record_intake_claim"
    assert events[-1][1]["evidence_status"] == "unsupported"

    link = _stored_links(service, workspace_id, 2)[0]
    assert link.relation == "supports"
    receipt = link.support_receipt
    assert receipt is not None
    assert receipt.verdict == "timeout"
    assert receipt.measurement == "unknown"
    assert receipt.input_tokens is None
    assert receipt.output_tokens is None
    assert receipt.usage_id is not None

    rows = _usage_rows(service, workspace_id)
    assert len(rows) == 1
    assert rows[0]["usage_id"] == receipt.usage_id
    _assert_unknown_usage(rows[0], mission_id)

    for judgments in (
        _http_judgments(service, workspace_id),
        _http_judgments(service, workspace_id, revision=3),
    ):
        assert len(judgments) == 1
        assert judgments[0].caller_relation == "supports"
        assert judgments[0].verdict == "timeout"
        assert judgments[0].disagreement is True
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        projected = read_intake_view(cur, workspace_id, RUN_ID).support_judgments
    assert len(projected) == 1
    assert projected[0].caller_relation == "supports"
    assert projected[0].verdict == "timeout"
    assert projected[0].disagreement is True


def test_http_measured_links_bind_usage_replay_and_receipt(service, http_intake, monkeypatch):
    """Measured replies store exact O8 counts. Two links get distinct ids. Replay adds nothing.

    Each link is its own claim write. One command settles judge usage under that
    command's operation id, so a second link on the same command would conflict.
    """
    workspace_id, mission_id = http_intake
    context = "alpha beta"
    _start_and_evidence(service, workspace_id, mission_id, text=context)
    _seed_budget(service, workspace_id, mission_id)
    left = {"artifact_id": "artifact-added", "start_byte": 0, "end_byte": 5, "quote": "alpha"}
    right = {"artifact_id": "artifact-added", "start_byte": 6, "end_byte": 10, "quote": "beta"}
    first = claim_command(
        OWNER, "owner",
        evidence=[left],
        evidence_links=[{"span": left, "relation": "supports"}],
    )
    second = claim_command(
        OWNER, "owner",
        claim_id="claim-2",
        expected_revision=3,
        evidence=[right],
        evidence_links=[{"span": right, "relation": "supports"}],
    )
    pending = [
        {"text": '{"verdict": "supports"}', "input_tokens": 11, "output_tokens": 5},
        {"text": '{"verdict": "contradicts"}', "input_tokens": 13, "output_tokens": 2},
    ]

    def respond():
        return _JudgeReply(pending.pop(0))

    prior_url = os.environ.get("OMP_INTAKE_JUDGE_URL")
    original_post = httpx.Client.post
    operation_id = uuid4()
    with monkeypatch.context() as scoped:
        seen = _patch_judge_post(scoped, respond)
        with use_judge_hook(JudgeHook(complete=complete_from_env)):
            status, body = _command(service, workspace_id, first, operation_id=operation_id)
            assert status == 200, body
            assert len(seen) == 1
            calls = len(seen)
            revisions = _revision_count(service, workspace_id)
            head = _head_revision(service, workspace_id)
            usage = _usage_count(service, workspace_id)
            events = len(_domain_events(service, workspace_id))
            replay_status, replay = _command(
                service, workspace_id, first, operation_id=operation_id,
            )
            assert replay_status == 200
            assert replay["receipt"]["state"] == "replayed"
            assert replay["result"] == body["result"]
            assert len(seen) == calls
            assert _revision_count(service, workspace_id) == revisions
            assert _head_revision(service, workspace_id) == head
            assert _usage_count(service, workspace_id) == usage
            assert len(_domain_events(service, workspace_id)) == events
            status, body = _command(service, workspace_id, second)
            assert status == 200, body
        _assert_echo_hook_restored()
    assert httpx.Client.post is original_post
    assert os.environ.get("OMP_INTAKE_JUDGE_URL") == prior_url
    assert len(seen) == 2
    assert pending == []
    assert [call["model"] for call in seen] == [_research_model(), _research_model()]
    assert "alpha" in str(seen[0]["prompt"]) and "beta" in str(seen[1]["prompt"])

    links = {link.quote: link for link in _stored_links(service, workspace_id, 3)}
    assert set(links) == {"alpha", "beta"}
    expected = {
        "alpha": (first, 11, 5, "supports"),
        "beta": (second, 13, 2, "contradicts"),
    }
    rows = {row["usage_id"]: row for row in _usage_rows(service, workspace_id)}
    assert len(rows) == 2
    request_ids: set[str] = set()
    usage_ids: set[str] = set()
    for quote, (command, input_tokens, output_tokens, verdict) in expected.items():
        link = links[quote]
        receipt = link.support_receipt
        assert receipt is not None
        assert receipt.verdict == verdict
        assert receipt.measurement == "measured"
        assert receipt.input_tokens == input_tokens
        assert receipt.output_tokens == output_tokens
        assert receipt.usage_id in rows
        _assert_measured_usage(rows[receipt.usage_id], mission_id, input_tokens, output_tokens)
        request_ids.add(str(rows[receipt.usage_id]["request_id"]))
        usage_ids.add(str(receipt.usage_id))
        claim = command["payload"]["claim"]
        _assert_stored_bindings(
            receipt,
            claim_text=_claim_text(claim),
            changed_claim_text=_claim_text(claim, polarity="negative"),
            quote=quote,
            context=context,
        )
        assert receipt.source_span == SourceSpan(start=link.start_byte, end=link.end_byte)
    assert len(request_ids) == 2
    assert len(usage_ids) == 2

    def _by_span(judgments):
        return {item.start_byte: item for item in judgments}

    http_judgments = _by_span(_http_judgments(service, workspace_id))
    assert http_judgments[0].verdict == "supports" and http_judgments[0].disagreement is False
    assert http_judgments[6].verdict == "contradicts" and http_judgments[6].disagreement is True
    assert all(item.caller_relation == "supports" for item in http_judgments.values())
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        projected = _by_span(read_intake_view(cur, workspace_id, RUN_ID).support_judgments)
    assert projected[0].verdict == "supports" and projected[0].disagreement is False
    assert projected[6].verdict == "contradicts" and projected[6].disagreement is True


def test_http_tokenless_reply_stores_unknown_not_zero(service, http_intake, monkeypatch):
    """A reply with null token counts is unknown usage, never a fabricated zero."""
    workspace_id, mission_id = http_intake
    _start_and_evidence(service, workspace_id, mission_id)
    _seed_budget(service, workspace_id, mission_id)

    def respond():
        return _JudgeReply({
            "text": '{"verdict": "neither"}',
            "input_tokens": None,
            "output_tokens": None,
        })

    with monkeypatch.context() as scoped:
        seen = _patch_judge_post(scoped, respond)
        with use_judge_hook(JudgeHook(complete=complete_from_env)):
            status, _body = _command(service, workspace_id, claim_command(OWNER, "owner"))
        _assert_echo_hook_restored()
    assert status == 200
    assert len(seen) == 1
    assert _domain_events(service, workspace_id)[-1][1]["evidence_status"] == "unsupported"
    receipt = _stored_links(service, workspace_id, 2)[0].support_receipt
    assert receipt is not None
    assert receipt.verdict == "neither"
    assert receipt.measurement == "unknown"
    assert receipt.input_tokens is None
    assert receipt.output_tokens is None
    rows = _usage_rows(service, workspace_id)
    assert len(rows) == 1
    assert rows[0]["usage_id"] == receipt.usage_id
    _assert_unknown_usage(rows[0], mission_id)
    judgment = _http_judgments(service, workspace_id)[0]
    assert judgment.caller_relation == "supports"
    assert judgment.verdict == "neither"
    assert judgment.disagreement is True


def test_admission_copies_judged_receipt_bytes_without_recall(service, http_intake, monkeypatch):
    """Admission copies the judged receipt byte-for-byte and does not call the judge again."""
    workspace_id, mission_id = http_intake
    _start_and_evidence(service, workspace_id, mission_id)
    _seed_budget(service, workspace_id, mission_id)
    command = claim_command(OWNER, "owner")

    def respond():
        return _JudgeReply({
            "text": '{"verdict": "contradicts"}',
            "input_tokens": 9,
            "output_tokens": 4,
        })

    with monkeypatch.context() as scoped:
        seen = _patch_judge_post(scoped, respond)
        with use_judge_hook(JudgeHook(complete=complete_from_env)):
            assert _command(service, workspace_id, command)[0] == 200
            assert len(seen) == 1
            proposed = _stored_links(service, workspace_id, 2)[0].support_receipt
            assert proposed is not None
            before = _receipt_texts(service, workspace_id, 2)
            usage = _usage_count(service, workspace_id)
            judgment = _http_judgments(service, workspace_id)[0]
            assert judgment.disagreement is True
            assert judgment.verdict == "contradicts"
            assert _domain_events(service, workspace_id)[-1][1]["evidence_status"] == "contested"

            admission = deepcopy(command)
            admission["payload"]["expected_revision"] = 3
            admission["payload"]["claim"].update(
                state="admitted", truth="true", admitted_by=dict(id=str(OWNER), kind="owner"),
            )
            status, _body = _command(service, workspace_id, admission)
            assert status == 200
            assert len(seen) == 1
        _assert_echo_hook_restored()
    assert _usage_count(service, workspace_id) == usage
    admitted = _stored_links(service, workspace_id, 3)[0].support_receipt
    assert admitted is not None
    assert admitted.model_dump_json() == proposed.model_dump_json()
    assert _receipt_texts(service, workspace_id, 3) == before
    assert before[0] is not None
    assert before[0].encode() == _receipt_texts(service, workspace_id, 3)[0].encode()
    retained = _http_judgments(service, workspace_id)[0]
    assert retained.disagreement is True
    assert retained.verdict == "contradicts"
    assert retained.caller_relation == "supports"
    assert _domain_events(service, workspace_id)[-1][1]["evidence_status"] == "contested"


def test_echo_legacy_admission_copies_absent_receipts(service, http_intake, monkeypatch):
    """Echo admission of a receiptless proposal stores no receipt and calls no judge."""
    workspace_id, mission_id = http_intake
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        seeded = revision_body(workspace_id, revision=0)
        seeded["snapshot"]["state"] = "reasoning"
        for artifact in seeded["snapshot"]["artifacts"]:
            artifact["content_hash"] = hashlib.sha256(seeded["artifact_bytes"][artifact["id"]]).hexdigest()
        seeded["supersessions"] = {}
        seeded["snapshot"]["ontology"]["predicates"][0]["authority"].append("model")
        proposed_claim = seeded["snapshot"]["claims"][0]
        span = proposed_claim["evidence"][0]
        seeded["evidence_links"] = [
            dict(claim_id=proposed_claim["id"], relation="supports", **span),
        ]
        seeded["binding"]["rule_bundle_hash"] = "1" * 64
        seeded["binding"]["schema_hash"] = "2" * 64
        seeded["binding"]["contract_hash"] = "3" * 64
        seeded_snapshot = nsi.IntakeSnapshot.model_validate(seeded["snapshot"])
        c_hash, s_hash = revision_hashes(seeded_snapshot, seeded["artifact_bytes"])
        seeded["binding"]["claim_set_hash"] = c_hash
        seeded["binding"]["source_hash"] = s_hash
        locked = lock_revision(
            cur, workspace_id=workspace_id, run_id=RUN_ID, expected_revision=-1,
            mission_id=mission_id, operation_id=uuid4(),
        )
        append_revision(cur, locked, seeded, operation_id=uuid4())

    assert _receipt_texts(service, workspace_id, 0) == [None]
    assert current_hook() is not None and current_hook().echo_caller
    admitted_claim = deepcopy(proposed_claim)
    admitted_claim.update(
        state="admitted", truth="true", admitted_by=dict(id=str(OWNER), kind="owner"),
    )
    admit_cmd = {
        "type": "record_intake_claim",
        "payload": {
            "run_id": RUN_ID,
            "expected_revision": 1,
            "claim": admitted_claim,
            "evidence_links": [dict(span=span, relation="supports")],
        },
    }

    def respond():
        raise AssertionError("echo admission must not call the judge")

    with monkeypatch.context() as scoped:
        seen = _patch_judge_post(scoped, respond)
        status, _body = _command(service, workspace_id, admit_cmd)
    assert status == 200
    assert seen == []
    assert _usage_count(service, workspace_id) == 0
    assert _receipt_texts(service, workspace_id, 1) == [None]
    assert _stored_links(service, workspace_id, 1)[0].support_receipt is None
    assert _http_judgments(service, workspace_id) == ()
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        assert read_intake_view(cur, workspace_id, RUN_ID).support_judgments == ()


def test_execute_intake_without_judge_or_echo_is_forbidden(service, http_intake, monkeypatch):
    """execute_intake(judge=None) with echo disabled writes no revision or usage."""
    workspace_id, mission_id = http_intake
    _start_and_evidence(service, workspace_id, mission_id)
    _seed_budget(service, workspace_id, mission_id)
    revisions = _revision_count(service, workspace_id)
    events = len(_domain_events(service, workspace_id))
    assert _usage_count(service, workspace_id) == 0
    assert _head_revision(service, workspace_id) == 1
    original_post = httpx.Client.post
    prior_url = os.environ.get("OMP_INTAKE_JUDGE_URL")
    with monkeypatch.context() as scoped:
        seen = _patch_judge_post(scoped, _timeout_reply)
        with use_judge_hook(JudgeHook()):
            assert current_hook() is not None and not current_hook().echo_caller
            with connection(service, workspace_id) as conn, conn.cursor() as cur:
                with pytest.raises(WorkStoreError) as exc:
                    execute_intake(
                        cur, claim_envelope(workspace_id, OWNER, "owner"),
                        OWNER, "owner", judge=None,
                    )
                assert exc.value.code == "forbidden"
        _assert_echo_hook_restored()
    assert httpx.Client.post is original_post
    assert os.environ.get("OMP_INTAKE_JUDGE_URL") == prior_url
    assert seen == []
    assert _usage_count(service, workspace_id) == 0
    assert _revision_count(service, workspace_id) == revisions
    assert _head_revision(service, workspace_id) == 1
    assert len(_domain_events(service, workspace_id)) == events
    response = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/{RUN_ID}",
        headers=_owner_headers(workspace_id),
    )
    assert response.status_code == 200
    assert response.json()["snapshot"]["claims"] == []
