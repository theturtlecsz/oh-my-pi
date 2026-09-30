"""Public-key check for an owner's answer to a tier 3 decision record.

The service holds no private key. Verification shells out to `ssh-keygen -Y
verify` against the owner_allowed_signers file.

Owner decision D47 (2026-09-30): flood creates and holds the owner signing key
on this host under ~/.config/omp/owner-signing, not on Chris's own device, so a
valid owner signature proves flood's approval. Removing that key and
owner_allowed_signers undoes this.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - ssh-keygen only, resolved via shutil.which, fixed argv, no shell
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from .models import RelayOwnerIntentPayload

NAMESPACE = "omp-work-decision"
PRINCIPAL = "owner"
_VERIFY_TIMEOUT_SECONDS = 10


def decision_signature_message(
    *,
    workspace_id: UUID,
    decision_id: UUID,
    action_class: str,
    answer: str,
    target_sha256: str | None = None,
    expires_at: datetime | None = None,
) -> bytes:
    payload = {
        "action_class": action_class,
        "answer": answer,
        "decision_id": str(decision_id),
        "workspace_id": str(workspace_id),
    }
    if target_sha256 is not None:
        payload["target_sha256"] = target_sha256
    if expires_at is not None:
        payload["expires_at"] = expires_at.astimezone(timezone.utc).isoformat()
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def relay_signature_message(workspace_id: UUID, payload: RelayOwnerIntentPayload) -> bytes:
    """Canonical relay bytes (decision 0019).

    Compact sorted JSON of the payload with nulls and ``owner_signature``
    omitted, plus ``workspace_id`` and ``purpose`` ``relay_owner_intent``.
    """
    body = {
        **payload.model_dump(
            mode="json", exclude_none=True, exclude={"owner_signature"}
        ),
        "workspace_id": str(workspace_id),
        "purpose": "relay_owner_intent",
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_owner_signature(allowed_signers: Path, message: bytes, signature: str) -> bool:
    if signature == "" or not Path(allowed_signers).is_file():
        return False
    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        return False
    sig_path: str | None = None
    try:
        fd, sig_path = tempfile.mkstemp(prefix="omp-owner-sig-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(signature)
        completed = subprocess.run(  # nosec B603 - absolute ssh-keygen from shutil.which, fixed argv, no shell; the message goes in on stdin and the signature in a temp file
            [
                ssh_keygen,
                "-Y",
                "verify",
                "-f",
                str(allowed_signers),
                "-I",
                PRINCIPAL,
                "-n",
                NAMESPACE,
                "-s",
                sig_path,
            ],
            input=message,
            capture_output=True,
            timeout=_VERIFY_TIMEOUT_SECONDS,
            shell=False,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        if sig_path is not None:
            try:
                os.unlink(sig_path)
            except OSError:
                pass
