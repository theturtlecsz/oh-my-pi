"""Deterministic fleet context compiler (OMP-311 / FK-5).

``compile_bundle`` turns one immutable :class:`CompileRequest` plus a
:class:`TokenCounter` into a byte-stable :class:`CompiledBundle`. Nothing here
reads the clock or the environment: the same request and counter always produce
the same text, the same overall hash, and the same per-section hashes, no matter
what order the caller supplied the items in.

Only ``current`` items render. Every other status is withheld as an
:class:`Exclusion` instead of being silently dropped, and a mandatory item that
cannot fit the token budget fails the compile rather than rendering partial
context.
"""

from __future__ import annotations

from collections.abc import Sequence

from omp_work.v1.canonical import sha256
from omp_work.v1.models import StrictModel

from .models import (
    SECTIONS,
    CompileRequest,
    ContextItem,
    Exclusion,
    Section,
    TokenCounter,
)

_HEADER = "FLEET CONTEXT v1"
_IDENTITY_MARKER = "--- identity ---"


class BudgetInsufficientError(Exception):
    """The mandatory items alone do not fit the request's token budget."""

    def __init__(self, *, budget: int, required: int) -> None:
        self.budget = budget
        self.required = required
        super().__init__(
            f"budget_insufficient: mandatory context needs {required} tokens, "
            f"budget is {budget}"
        )


class CompiledBundle(StrictModel):
    """The compiled, hash-stable bundle handed to a worker stage."""

    text: str
    sha256: str
    section_sha256: dict[str, str]
    included: tuple[ContextItem, ...]
    exclusions: tuple[Exclusion, ...]
    tokens: int
    counts: dict[str, int]
    counter_profile: str


def _sort_key(item: ContextItem) -> tuple[bool, float, str, str, str, str, str, str]:
    """Deterministic admission order: mandatory first, then best score, then
    source and ref as the contract's tie-breakers. The remaining fields extend
    the key to a total order so a shuffled request still compiles to identical
    bytes and identical exclusion order."""
    score = item.score if item.score is not None else 0.0
    return (
        not item.mandatory,
        -score,
        item.source,
        item.ref,
        item.section,
        item.status,
        item.text,
        item.detail,
    )


def _section_lines(section: Section, items: Sequence[ContextItem]) -> list[str]:
    lines = [f"## {section}"]
    for item in items:
        lines.append(f"- {item.source}#{item.ref}: {item.text}")
    return lines


def _render(
    request: CompileRequest, included: Sequence[ContextItem], counter_profile: str
) -> tuple[str, dict[str, str]]:
    """Render the full bundle text and the per-section text hashes.

    All four section headers always render, so the text keeps a stable prefix
    even when a section is empty.
    """
    by_section: dict[Section, list[ContextItem]] = {section: [] for section in SECTIONS}
    for item in included:
        by_section[item.section].append(item)

    lines: list[str] = [_HEADER, ""]
    section_sha256: dict[str, str] = {}
    for section in SECTIONS:
        section_lines = _section_lines(section, by_section[section])
        section_sha256[section] = sha256("\n".join(section_lines))
        lines.extend(section_lines)
        lines.append("")

    identity = request.identity
    lines.append(_IDENTITY_MARKER)
    lines.append(f"work_id: {identity.work_id}")
    lines.append(f"work_key: {identity.work_key}")
    lines.append(f"revision_id: {identity.revision_id}")
    lines.append(f"stage: {identity.stage}")
    lines.append(f"attempt_id: {identity.attempt_id}")
    lines.append(f"candidate_id: {identity.candidate_id}")
    lines.append(f"token_budget: {request.token_budget}")
    lines.append(f"encoding: {request.encoding}")
    lines.append(f"counter_profile: {counter_profile}")
    return "\n".join(lines), section_sha256


def _counts(items: Sequence[ContextItem], counter: TokenCounter) -> dict[str, int]:
    """Content-addressed token counts for every distinct included item text."""
    texts: list[str] = []
    keys: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = sha256(item.text)
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
        texts.append(item.text)
    if not texts:
        return {}
    values = counter.count(texts)
    if len(values) != len(texts):
        raise ValueError(
            f"token counter returned {len(values)} counts for {len(texts)} texts"
        )
    return {key: int(value) for key, value in zip(keys, values)}


def compile_bundle(request: CompileRequest, counter: TokenCounter) -> CompiledBundle:
    """Compile a request into a deterministic, budget-bounded bundle.

    Non-current items never render and are reported as exclusions. While the
    rendered text exceeds the budget the last optional item is dropped (also an
    exclusion); if only mandatory items remain and they still exceed the budget,
    :class:`BudgetInsufficientError` is raised.
    """
    ordered = sorted(request.items, key=_sort_key)

    exclusions: list[Exclusion] = []
    candidates: list[ContextItem] = []
    for item in ordered:
        if item.status != "current":
            exclusions.append(
                Exclusion(
                    section=item.section,
                    source=item.source,
                    ref=item.ref,
                    reason=item.status,
                    detail=item.detail,
                )
            )
            continue
        candidates.append(item)

    text, section_sha256 = _render(request, candidates, counter.profile)
    while counter.count([text])[0] > request.token_budget:
        drop_index = next(
            (index for index in range(len(candidates) - 1, -1, -1) if not candidates[index].mandatory),
            None,
        )
        if drop_index is None:
            required = counter.count([text])[0]
            raise BudgetInsufficientError(budget=request.token_budget, required=required)
        dropped = candidates.pop(drop_index)
        exclusions.append(
            Exclusion(
                section=dropped.section,
                source=dropped.source,
                ref=dropped.ref,
                reason="budget",
                detail=dropped.detail,
            )
        )
        text, section_sha256 = _render(request, candidates, counter.profile)

    tokens = counter.count([text])[0]
    return CompiledBundle(
        text=text,
        sha256=sha256(text),
        section_sha256=section_sha256,
        included=tuple(candidates),
        exclusions=tuple(exclusions),
        tokens=tokens,
        counts=_counts(candidates, counter),
        counter_profile=counter.profile,
    )


__all__ = ["BudgetInsufficientError", "CompiledBundle", "compile_bundle"]
