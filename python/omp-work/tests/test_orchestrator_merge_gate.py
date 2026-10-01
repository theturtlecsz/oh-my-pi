"""OMP-417-s04-s05: a merge credential is released only for a signed approval.

Unit rows are the events the store writes: the create_decision event, and the
answer_decision event taken from the second value of decision_records.answer_decision
on a fake cursor. Those rows are then read back through load_answered_decision.
The PostgreSQL test creates and answers through the real store.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from omp_work.orchestrator.merge_gate import (
    load_answered_decision,
    release_merge_credential,
)
from omp_work.v1.decision_records import answer_decision, create_decision
from omp_work.v1.models import CommandEnvelope, OperationState
from omp_work.v1.owner_signature import decision_signature_message
from omp_work.v1.store import PostgresWorkStore
from test_workflow_service import _grant

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None,
    reason="ssh-keygen is not installed",
)

_TARGET_A = "ab" * 32
_TARGET_B = "cd" * 32
_FUTURE = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)
_PAST_EXPIRY = datetime(2020, 6, 1, tzinfo=timezone.utc)
_CLOCK_BEFORE_PAST_EXPIRY = datetime(2020, 1, 1, tzinfo=timezone.utc)
_CLOCK = datetime(2026, 1, 1, tzinfo=timezone.utc)
_PROJECT = UUID("00000000-0000-7000-8000-000000000417")
_NAMESPACE = "omp-work-decision"


class _Cursor:
    """Rows shaped as omp_audit.domain_events, plus the clock the store reads."""

    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.now = _CLOCK
        self._query = ""
        self._params: tuple[object, ...] = ()

    def execute(self, query: str, params: object = None) -> None:
        self._query = " ".join(query.split())
        self._params = tuple(params) if isinstance(params, (tuple, list)) else ()

    def fetchall(self) -> list[dict[str, object]]:
        if "payload->>'decision_id'" in self._query:
            found = self._answer_row()
            return [] if found is None else [found]
        return list(self.rows)

    def fetchone(self) -> dict[str, object] | None:
        if "AS created_at" in self._query:
            return {"created_at": self.now}
        if "AS now" in self._query:
            return {"now": self.now}
        if "payload->>'decision_id'" in self._query:
            return self._answer_row()
        return None

    def _answer_row(self) -> dict[str, object] | None:
        if len(self._params) < 3:
            return None
        aggregate_id = self._params[1]
        decision_id = str(self._params[2])
        matched: list[dict[str, object]] = []
        for row in self.rows:
            if row.get("event_type") != "answer_decision":
                continue
            if row.get("outcome") != "applied":
                continue
            if row.get("aggregate_id") != aggregate_id:
                continue
            payload = _payload_dict(row.get("payload"))
            if payload is None or str(payload.get("decision_id")) != decision_id:
                continue
            matched.append(row)
        if not matched:
            return None
        matched.sort(key=lambda item: int(item["sequence"]))
        return {"payload": matched[0]["payload"]}


def _payload_dict(value: object) -> dict[str, object] | None:
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict):
        return value
    return None


def _body(
    decision_id: UUID,
    *,
    action_class: str,
    target_sha256: str,
) -> dict[str, object]:
    return {
        "decision_id": str(decision_id),
        "project_id": str(_PROJECT),
        "mission_id": "OMP-417",
        "question": "Merge this target?",
        "why_it_matters": "The approval must name the commit it covers.",
        "risk_of_delay": "The merge waits until the owner answers.",
        "options": ["approve", "reject"],
        "evidence_refs": ["receipt:merge-gate"],
        "default_if_any": "reject",
        "risk_of_each_choice": {
            "approve": "The named target is merged.",
            "reject": "The merge does not run.",
        },
        "action_class": action_class,
        "target_sha256": target_sha256,
        "resume_state": "merge-approved",
    }


def _envelope(workspace_id: UUID, command: dict[str, object]) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(workspace_id),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": command,
        }
    )


def _generate_key(tmp_path: Path, name: str) -> Path:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def _signers(path: Path, key: Path) -> Path:
    public = Path(f"{key}.pub").read_text(encoding="utf-8").strip()
    path.write_text(f"owner {public}\n", encoding="utf-8")
    return path


def _sign(key: Path, message: bytes) -> str:
    completed = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", _NAMESPACE],
        input=message,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("ascii")


def _message(
    workspace_id: UUID,
    decision_id: UUID,
    *,
    action_class: str,
    answer: str,
    target_sha256: str,
    expires_at: datetime,
) -> bytes:
    return decision_signature_message(
        workspace_id=workspace_id,
        decision_id=decision_id,
        action_class=action_class,
        answer=answer,
        target_sha256=target_sha256,
        expires_at=expires_at,
    )


def _record(
    cursor: _Cursor,
    workspace_id: UUID,
    event_type: str,
    payload: dict[str, object],
) -> None:
    cursor.rows.append(
        {
            "sequence": len(cursor.rows) + 1,
            "workspace_id": workspace_id,
            "aggregate_id": workspace_id,
            "event_type": event_type,
            "outcome": "applied",
            "payload": payload,
            "occurred_at": cursor.now,
        }
    )


def _open(
    cursor: _Cursor,
    workspace_id: UUID,
    decision_id: UUID,
    *,
    action_class: str,
    target_sha256: str,
) -> None:
    _result, event = create_decision(
        cursor,
        _envelope(
            workspace_id,
            {
                "type": "create_decision",
                "payload": _body(
                    decision_id,
                    action_class=action_class,
                    target_sha256=target_sha256,
                ),
            },
        ),
    )
    _record(cursor, workspace_id, "create_decision", event)


def _answer(
    cursor: _Cursor,
    workspace_id: UUID,
    decision_id: UUID,
    *,
    answer: str,
    signature: str,
    expires_at: datetime,
    signers: Path,
) -> dict[str, object]:
    _result, event = answer_decision(
        cursor,
        _envelope(
            workspace_id,
            {
                "type": "answer_decision",
                "payload": {
                    "decision_id": str(decision_id),
                    "answer": answer,
                    "owner_signature": signature,
                    "expires_at": expires_at.isoformat(),
                },
            },
        ),
        signers,
    )
    return event


def _load(cursor: _Cursor, workspace_id: UUID, decision_id: UUID):
    return load_answered_decision(cursor, workspace_id, decision_id)


def _release(decision, workspace_id: UUID, target: str, signers: Path, credential: Path):
    return release_merge_credential(
        decision=decision,
        workspace_id=workspace_id,
        target_sha256=target,
        allowed_signers=signers,
        credential_path=credential,
    )


def test_signed_approval_releases_only_target_a(tmp_path: Path) -> None:
    workspace_id = uuid4()
    decision_id = uuid4()
    cursor = _Cursor()
    owner = _generate_key(tmp_path, "owner")
    signers = _signers(tmp_path / "owner_allowed_signers", owner)
    credential = tmp_path / "merge-credential"
    credential.write_bytes(b"credential\n")
    _open(
        cursor,
        workspace_id,
        decision_id,
        action_class="merge_protected_branch",
        target_sha256=_TARGET_A,
    )
    signature = _sign(
        owner,
        _message(
            workspace_id,
            decision_id,
            action_class="merge_protected_branch",
            answer="approve",
            target_sha256=_TARGET_A,
            expires_at=_FUTURE,
        ),
    )
    event = _answer(
        cursor,
        workspace_id,
        decision_id,
        answer="approve",
        signature=signature,
        expires_at=_FUTURE,
        signers=signers,
    )
    assert event["owner_signature"] == signature
    _record(cursor, workspace_id, "answer_decision", event)
    loaded = _load(cursor, workspace_id, decision_id)
    assert loaded is not None
    assert loaded["view"]["status"] == "answered"
    assert loaded["view"]["target_sha256"] == _TARGET_A
    assert loaded["answer_event"]["owner_signature"] == signature

    assert _release(loaded, workspace_id, _TARGET_A, signers, credential) == credential
    assert _release(loaded, workspace_id, _TARGET_B, signers, credential) is None
    assert (
        _release(loaded, workspace_id, _TARGET_A, signers, tmp_path / "missing-credential")
        is None
    )


def test_unsigned_declined_expired_other_key_pending_and_other_class_do_not_release(
    tmp_path: Path,
) -> None:
    workspace_id = uuid4()
    cursor = _Cursor()
    owner = _generate_key(tmp_path, "owner")
    other = _generate_key(tmp_path, "other")
    owner_signers = _signers(tmp_path / "owner_allowed_signers", owner)
    other_signers = _signers(tmp_path / "other_allowed_signers", other)
    credential = tmp_path / "merge-credential"
    credential.write_bytes(b"credential\n")

    def approve(
        decision_id: UUID,
        *,
        action_class: str = "merge_protected_branch",
        answer: str = "approve",
        expires_at: datetime = _FUTURE,
        key: Path = owner,
        signers: Path = owner_signers,
        now: datetime = _CLOCK,
    ) -> dict[str, object]:
        cursor.now = now
        _open(
            cursor,
            workspace_id,
            decision_id,
            action_class=action_class,
            target_sha256=_TARGET_A,
        )
        signature = _sign(
            key,
            _message(
                workspace_id,
                decision_id,
                action_class=action_class,
                answer=answer,
                target_sha256=_TARGET_A,
                expires_at=expires_at,
            ),
        )
        event = _answer(
            cursor,
            workspace_id,
            decision_id,
            answer=answer,
            signature=signature,
            expires_at=expires_at,
            signers=signers,
        )
        return event

    unsigned_id = uuid4()
    unsigned_event = approve(unsigned_id)
    unsigned_event = {
        field: value for field, value in unsigned_event.items() if field != "owner_signature"
    }
    _record(cursor, workspace_id, "answer_decision", unsigned_event)

    declined_id = uuid4()
    _record(
        cursor,
        workspace_id,
        "answer_decision",
        approve(declined_id, answer="reject"),
    )

    expired_id = uuid4()
    _record(
        cursor,
        workspace_id,
        "answer_decision",
        approve(
            expired_id,
            expires_at=_PAST_EXPIRY,
            now=_CLOCK_BEFORE_PAST_EXPIRY,
        ),
    )

    other_key_id = uuid4()
    _record(
        cursor,
        workspace_id,
        "answer_decision",
        approve(other_key_id, key=other, signers=other_signers),
    )

    pending_id = uuid4()
    cursor.now = _CLOCK
    _open(
        cursor,
        workspace_id,
        pending_id,
        action_class="merge_protected_branch",
        target_sha256=_TARGET_A,
    )

    other_class_id = uuid4()
    _record(
        cursor,
        workspace_id,
        "answer_decision",
        approve(other_class_id, action_class="contract_hash"),
    )

    def released(decision_id: UUID):
        return _release(
            _load(cursor, workspace_id, decision_id),
            workspace_id,
            _TARGET_A,
            owner_signers,
            credential,
        )

    assert released(unsigned_id) is None
    assert released(declined_id) is None
    assert released(expired_id) is None
    assert released(other_key_id) is None
    assert _load(cursor, workspace_id, pending_id) is None
    assert released(pending_id) is None
    assert released(other_class_id) is None


def test_malformed_decision_returns_none_without_raising(tmp_path: Path) -> None:
    workspace_id = uuid4()
    decision_id = uuid4()
    cursor = _Cursor()
    owner = _generate_key(tmp_path, "owner")
    signers = _signers(tmp_path / "owner_allowed_signers", owner)
    credential = tmp_path / "merge-credential"
    credential.write_bytes(b"credential\n")
    _open(
        cursor,
        workspace_id,
        decision_id,
        action_class="merge_protected_branch",
        target_sha256=_TARGET_A,
    )
    event = _answer(
        cursor,
        workspace_id,
        decision_id,
        answer="approve",
        signature=_sign(
            owner,
            _message(
                workspace_id,
                decision_id,
                action_class="merge_protected_branch",
                answer="approve",
                target_sha256=_TARGET_A,
                expires_at=_FUTURE,
            ),
        ),
        expires_at=_FUTURE,
        signers=signers,
    )
    _record(cursor, workspace_id, "answer_decision", event)
    loaded = _load(cursor, workspace_id, decision_id)
    assert loaded is not None
    view = dict(loaded["view"])
    answer_event = dict(loaded["answer_event"])

    def released(decision) -> Path | None:
        return _release(decision, workspace_id, _TARGET_A, signers, credential)

    assert released(None) is None
    assert released({}) is None
    assert released({"view": view}) is None
    assert released({"view": "nope", "answer_event": answer_event}) is None

    naive = {"view": view, "answer_event": dict(answer_event)}
    naive["answer_event"]["expires_at"] = "2099-01-01T12:00:00"
    assert released(naive) is None

    garbage = {"view": view, "answer_event": dict(answer_event)}
    garbage["answer_event"]["expires_at"] = "not-a-date"
    assert released(garbage) is None

    bad_id = {"view": dict(view), "answer_event": answer_event}
    bad_id["view"]["decision_id"] = "not-a-uuid"
    assert released(bad_id) is None

    blank = {"view": view, "answer_event": dict(answer_event)}
    blank["answer_event"]["owner_signature"] = "   "
    assert released(blank) is None


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1"
    or shutil.which("ssh-keygen") is None,
    reason="requires OMP_WORK_POSTGRES_INTEGRATION=1 and ssh-keygen",
)
def test_postgres_signed_merge_releases_target_a_only(service, tmp_path: Path) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _grant(service, workspace_id)
    actor_id = uuid4()
    decision_id = uuid4()
    owner = _generate_key(tmp_path, "owner")
    signers_path = service.config.config_dir / "owner_allowed_signers"
    service.config.config_dir.mkdir(parents=True, exist_ok=True)
    _signers(signers_path, owner)
    credential = tmp_path / "merge-credential"
    credential.write_bytes(b"credential\n")
    signature = _sign(
        owner,
        _message(
            workspace_id,
            decision_id,
            action_class="merge_protected_branch",
            answer="approve",
            target_sha256=_TARGET_A,
            expires_at=_FUTURE,
        ),
    )
    try:
        create_receipt, _created = store.execute(
            _envelope(
                workspace_id,
                {
                    "type": "create_decision",
                    "payload": _body(
                        decision_id,
                        action_class="merge_protected_branch",
                        target_sha256=_TARGET_A,
                    ),
                },
            ),
            actor_id=actor_id,
            actor_kind="owner",
            required_scope="work.mutate",
        )
        assert create_receipt.state == OperationState.APPLIED
        answer_receipt, _answered = store.execute(
            _envelope(
                workspace_id,
                {
                    "type": "answer_decision",
                    "payload": {
                        "decision_id": str(decision_id),
                        "answer": "approve",
                        "owner_signature": signature,
                        "expires_at": _FUTURE.isoformat(),
                    },
                },
            ),
            actor_id=actor_id,
            actor_kind="owner",
            required_scope="work.approve",
        )
        assert answer_receipt.state == OperationState.APPLIED
        with store._transaction(workspace_id, actor_id) as cur:
            loaded = load_answered_decision(cur, workspace_id, decision_id)
        assert loaded is not None
        assert loaded["view"]["action_class"] == "merge_protected_branch"
        assert loaded["view"]["target_sha256"] == _TARGET_A
        assert loaded["answer_event"]["answer"] == "approve"
        assert loaded["answer_event"]["owner_signature"] == signature
        assert (
            _release(loaded, workspace_id, _TARGET_A, signers_path, credential) == credential
        )
        assert _release(loaded, workspace_id, _TARGET_B, signers_path, credential) is None
    finally:
        if signers_path.exists():
            signers_path.unlink()
