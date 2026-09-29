"""Public-key check for an owner's answer to a tier 3 decision record.

The service holds no private key. Verification shells out to `ssh-keygen -Y
verify` against an allowed-signers file the owner supplies.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import UUID

NAMESPACE = "omp-work-decision"
PRINCIPAL = "owner"
_VERIFY_TIMEOUT_SECONDS = 10


def decision_signature_message(
    *,
    workspace_id: UUID,
    decision_id: UUID,
    action_class: str,
    answer: str,
) -> bytes:
    payload = {
        "action_class": action_class,
        "answer": answer,
        "decision_id": str(decision_id),
        "workspace_id": str(workspace_id),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


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
        completed = subprocess.run(
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
