from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import CommandEnvelope


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def command_sha256(envelope: CommandEnvelope) -> str:
    return sha256({
        "api_version": envelope.api_version,
        "workspace_id": str(envelope.workspace_id),
        "command": envelope.command.model_dump(mode="json"),
    })
