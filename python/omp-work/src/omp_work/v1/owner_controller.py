"""Owner designation of the workspace controller (OMP-416).

A workspace names one controller actor. The designation is an owner signature
over :func:`designation_message`; verification reuses the same allowed-signers
file and namespace as owner decision answers (`verify_owner_signature`), with
the ``purpose`` field keeping the two messages distinct.

Owner decision D47 (2026-09-30): flood creates and holds the owner signing key
on this host under ~/.config/omp/owner-signing, so a valid owner signature
proves flood's approval. Removing that key and ``owner_allowed_signers``
undoes this.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

from .owner_signature import verify_owner_signature

__all__ = [
    "DESIGNATION_NAME",
    "PURPOSE",
    "designated_controller",
    "designation_message",
    "write_designation",
]

DESIGNATION_NAME = "owner-controller.json"
PURPOSE = "designate_owner_controller"


def designation_message(workspace_id: UUID, actor_id: UUID) -> bytes:
    """Compact sorted JSON naming the workspace and its designated controller."""
    payload = {
        "controller_actor_id": str(actor_id),
        "purpose": PURPOSE,
        "workspace_id": str(workspace_id),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _designation_path(config_dir: Path) -> Path:
    return Path(config_dir) / DESIGNATION_NAME


def _signers_path(config_dir: Path) -> Path:
    return Path(config_dir) / "owner_allowed_signers"


def designated_controller(config_dir: Path, workspace_id: UUID) -> UUID | None:
    """Return the designated controller actor, or None.

    Reads ``<config_dir>/owner-controller.json`` on every call (never cached).
    Returns the actor only when the file names this workspace and its
    ``owner_signature`` verifies against ``<config_dir>/owner_allowed_signers``
    over the designation message; a missing, malformed, mismatched, or
    unverifiable designation returns None.
    """
    config_dir = Path(config_dir)
    try:
        data = json.loads(_designation_path(config_dir).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("workspace_id") != str(workspace_id):
        return None
    actor = data.get("controller_actor_id")
    signature = data.get("owner_signature")
    if not isinstance(actor, str) or not isinstance(signature, str):
        return None
    try:
        actor_id = UUID(actor)
    except ValueError:
        return None
    if not verify_owner_signature(
        _signers_path(config_dir), designation_message(workspace_id, actor_id), signature
    ):
        return None
    return actor_id


def write_designation(
    config_dir: Path, workspace_id: UUID, actor_id: UUID, signature: str
) -> Path:
    """Verify ``signature`` over the designation, then write it atomically.

    Raises ValueError when the signature does not verify against
    ``<config_dir>/owner_allowed_signers``; nothing is written in that case.
    The written file is mode 0600.
    """
    config_dir = Path(config_dir)
    if not verify_owner_signature(
        _signers_path(config_dir), designation_message(workspace_id, actor_id), signature
    ):
        raise ValueError("owner designation signature is invalid")
    data = {
        "workspace_id": str(workspace_id),
        "controller_actor_id": str(actor_id),
        "owner_signature": signature,
    }
    path = _designation_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return path
