"""Wire the label-blind support judge into claim writes (OMP-604-s06-s02-s01).

Echo claim writes require no budget, call no model completion, and record no
usage: the stored link retains the caller relation and an echo support receipt
binding the artifact byte span.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from omp_work.intake_repository import read_revision
from omp_work.intake_support import SourceSpan
from omp_work.intake_support_judge import Completion, JudgeHook, use_judge_hook
from omp_work.operations.jobs_migrate import apply_jobs_migrations
from test_nsi2_claims import (
    claim_command,
    envelope,
    evidence_command,
    http_intake,  # noqa: F401
)
from test_nsi2_repository import connection
from test_workflow_service import OWNER, _command, service  # noqa: F401

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


def _start_and_evidence(service, workspace_id, mission_id) -> None:
    assert _command(
        service, workspace_id,
        envelope(workspace_id, mission_id).command.model_dump(mode="json"),
    )[0] == 200
    assert _command(service, workspace_id, evidence_command(OWNER, "owner"))[0] == 200


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
