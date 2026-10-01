"""Release a merge credential only for one signed, unexpired merge approval.

``load_answered_decision`` reads the decision view and the first applied
``answer_decision`` event the store recorded for it. ``release_merge_credential``
returns the credential path only when that record is an owner-signed approval
of ``merge_protected_branch`` for the exact target and the approval is still
inside its expiry. Any other record, including a malformed one, returns None.
The credential file is only checked to exist; its contents are never read.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import psycopg

from omp_work.v1.decision_records import find_decision
from omp_work.v1.owner_signature import decision_signature_message, verify_owner_signature

__all__ = [
    "load_answered_decision",
    "release_merge_credential",
]

# First applied answer for this decision on the workspace aggregate.
_ANSWER_EVENT = """
SELECT payload
FROM omp_audit.domain_events
WHERE workspace_id = %s
  AND aggregate_id = %s
  AND outcome = 'applied'
  AND event_type = 'answer_decision'
  AND payload->>'decision_id' = %s
ORDER BY sequence ASC
LIMIT 1
"""

_MERGE = "merge_protected_branch"
_APPROVE = "approve"


def load_answered_decision(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
    decision_id: UUID,
) -> dict[str, object] | None:
    """The answered view and its first applied answer event, or None."""
    view = find_decision(cur, workspace_id, decision_id)
    if not isinstance(view, dict) or view.get("status") != "answered":
        return None
    cur.execute(_ANSWER_EVENT, (workspace_id, workspace_id, str(decision_id)))
    row = cur.fetchone()
    if row is None:
        return None
    event = _object(row.get("payload"))
    if event is None:
        return None
    return {"view": view, "answer_event": event}


def release_merge_credential(
    *,
    decision: dict[str, object] | None,
    workspace_id: UUID,
    target_sha256: str,
    allowed_signers: Path,
    credential_path: Path,
) -> Path | None:
    """The credential path when ``decision`` approves this exact merge target.

    Returns None for every other record. Malformed input returns None and
    does not raise. The credential file is not opened.
    """
    try:
        if not isinstance(decision, dict):
            return None
        view = decision.get("view")
        event = decision.get("answer_event")
        if not isinstance(view, dict) or not isinstance(event, dict):
            return None
        if view.get("action_class") != _MERGE:
            return None
        view_target = view.get("target_sha256")
        if view_target != target_sha256 or not isinstance(view_target, str):
            return None
        answer = event.get("answer")
        if answer != _APPROVE or not isinstance(answer, str):
            return None
        expires_at = _aware_expiry(event.get("expires_at"))
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            return None
        signature = event.get("owner_signature")
        if not isinstance(signature, str) or signature.strip() == "":
            return None
        path = Path(credential_path)
        if not path.is_file():
            return None
        raw_id = view.get("decision_id")
        if not isinstance(raw_id, str):
            return None
        action_class = view.get("action_class")
        if not isinstance(action_class, str):
            return None
        message = decision_signature_message(
            workspace_id=workspace_id,
            decision_id=UUID(raw_id),
            action_class=action_class,
            answer=answer,
            target_sha256=view_target,
            expires_at=expires_at,
        )
        if not verify_owner_signature(allowed_signers, message, signature):
            return None
        return path
    except Exception:
        return None


def _aware_expiry(value: object) -> datetime | None:
    """An aware UTC datetime parsed from an ISO string, or None."""
    if not isinstance(value, str) or value.strip() == "":
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed.astimezone(timezone.utc)


def _object(value: object) -> dict[str, object] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, dict):
        return value
    return None
