from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from omp_knowledge.learning.generation import GenerationResult


class StubGenerator:
    """Scripted ``LessonGenerator`` double for tests.

    Each call to :meth:`generate` consumes the next script entry. A
    :class:`GenerationResult` is returned as-is; an exception instance is raised.
    Calls are recorded in :attr:`calls` so a test can assert what the seam
    received. When the script runs out, the last entry is reused.
    """

    def __init__(self, script: Sequence[GenerationResult | Exception]) -> None:
        if not script:
            raise ValueError("StubGenerator requires at least one script entry")
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def generate(self, trace: dict[str, Any]) -> GenerationResult:
        index = min(len(self.calls), len(self._script) - 1)
        self.calls.append(trace)
        entry = self._script[index]
        if isinstance(entry, Exception):
            raise entry
        return entry


__all__ = ["StubGenerator"]
