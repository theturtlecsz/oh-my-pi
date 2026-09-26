from __future__ import annotations

from datetime import datetime
from uuid import UUID


def row_json(row: dict[str, object] | None) -> dict[str, object] | None:
    """One JSON-safe projection for result payloads: UUID→str, datetime→ISO."""
    if row is None:
        return None

    def convert(value: object) -> object:
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, (list, tuple)):
            return [convert(item) for item in value]
        return value

    return {key: convert(value) for key, value in row.items()}


class WorkStoreError(Exception):
    def __init__(self, code: str, diagnostics: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostics = diagnostics
