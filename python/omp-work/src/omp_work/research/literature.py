"""Literature and evidence-research domain (R08, OMP-316).

The catalog is supplied by the caller. This module does not fetch, crawl,
run a command, or write a file. A material claim is literature-supported
only when a full-text passage from a source that is not retracted supports
it and no passage contests it. Abstract-only support, a retracted source,
metadata-only support, a contradiction, and no support stay unresolved.
Lines that tell the campaign to change scope or run a tool are reported
and leave the scope as it was given.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from omp_work.report_evidence import (
    EvidenceMatrix,
    Passage,
    SourceDoc,
    StubClassifier,
    build_evidence_matrix,
)

Relation = Literal[
    "preprint",
    "paper",
    "correction",
    "replication",
    "dataset",
    "code",
    "report",
]
Coverage = Literal["full_text", "abstract_only", "metadata"]
SynthesisStatus = Literal["literature-supported", "unresolved"]
Disclosure = Literal["contested", "abstract_only", "retracted", "metadata"]
QualityKind = Literal["abstract_only", "metadata", "retracted", "figure_omitted"]

_RELATIONS = frozenset({
    "preprint",
    "paper",
    "correction",
    "replication",
    "dataset",
    "code",
    "report",
})
_COVERAGES = frozenset({"full_text", "abstract_only", "metadata"})
# Reports of one study share a family. A correction, dataset, code bundle,
# or write-up is not another study.
_INDEPENDENT_RELATIONS = frozenset({"preprint", "paper", "replication"})
_INSTRUCTION_MARKERS = ("change the campaign scope to", "run this tool:")
_FIGURE_OMITTED = "[figure omitted]"
_QUERY_PUNCTUATION = ".,;:!?\"'()[]{}"

__all__ = [
    "ClaimSynthesis",
    "Coverage",
    "Disclosure",
    "LiteratureProfile",
    "QualityFinding",
    "QualityKind",
    "Relation",
    "Source",
    "SourceReading",
    "SynthesisStatus",
    "apply_sources",
    "contradictions",
    "discover",
    "ignored_instructions",
    "independent_studies",
    "matrix_for",
    "profile",
    "quality_findings",
    "read_source",
    "synthesis",
]


@dataclass(frozen=True)
class Source:
    """One catalog record. ``text`` and ``coverage`` are what was stored."""

    source_id: str
    family_id: str
    relation: Relation
    version: str
    coverage: Coverage
    text: str
    doi: str | None = None
    retracted: bool = False

    def __post_init__(self) -> None:
        if not self.source_id or not self.family_id or not self.version:
            raise ValueError("source_id, family_id, and version are required")
        if self.relation not in _RELATIONS:
            raise ValueError(f"unknown relation {self.relation!r}")
        if self.coverage not in _COVERAGES:
            raise ValueError(f"unknown coverage {self.coverage!r}")
        if not isinstance(self.retracted, bool):
            raise TypeError("retracted must be a bool")


@dataclass(frozen=True)
class SourceReading:
    """Stored text and coverage. Reading does not widen coverage."""

    text: str
    coverage: Coverage


@dataclass(frozen=True)
class QualityFinding:
    """One limit on what a source can establish."""

    source_id: str
    kind: QualityKind
    detail: str


@dataclass(frozen=True)
class ClaimSynthesis:
    """Literature label for one material claim, with the inspected passages."""

    claim_id: str
    text: str
    matrix_status: str
    status: SynthesisStatus
    disclosure: Disclosure | None
    passages: tuple[str, ...]


@dataclass(frozen=True)
class LiteratureProfile:
    """Campaign view. Source versions are the versions that were supplied."""

    question: str
    scope: str
    independent_studies: int
    quality: tuple[QualityFinding, ...]
    ignored: tuple[str, ...]
    statuses: tuple[tuple[str, str], ...]
    synthesis: tuple[ClaimSynthesis, ...]
    versions: tuple[tuple[str, str], ...]


def _index(sources: Sequence[Source]) -> dict[str, Source]:
    indexed: dict[str, Source] = {}
    for source in sources:
        if source.source_id in indexed:
            raise ValueError(f"duplicate source_id {source.source_id!r}")
        indexed[source.source_id] = source
    return indexed


def _query_tokens(query: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for raw in query.split():
        token = raw.strip(_QUERY_PUNCTUATION).casefold()
        if len(token) >= 4:
            tokens.append(token)
    return tuple(tokens)


def _contains(source: Source, token: str) -> bool:
    if token in source.text.casefold():
        return True
    return source.doi is not None and token in source.doi.casefold()


def discover(catalog: Sequence[Source], query: str) -> list[Source]:
    """Keep catalog rows whose text or DOI contains every long query token.

    A token shorter than 4 characters does not filter. Matching is
    case-insensitive. The catalog is not fetched or copied.
    """
    tokens = _query_tokens(query)
    if not tokens:
        return list(catalog)
    return [source for source in catalog if all(_contains(source, token) for token in tokens)]


def read_source(source: Source) -> SourceReading:
    """Return the stored text and coverage. Coverage is not recomputed."""
    return SourceReading(text=source.text, coverage=source.coverage)


def independent_studies(sources: Sequence[Source]) -> int:
    """Count distinct families of non-retracted preprints, papers, and replications.

    A correction, dataset, code bundle, or report does not count. A retracted
    record does not count. Two records in one family count once.
    """
    families = {
        source.family_id
        for source in sources
        if source.relation in _INDEPENDENT_RELATIONS and not source.retracted
    }
    return len(families)


def ignored_instructions(sources: Sequence[Source]) -> list[str]:
    """Lines that try to change the campaign scope or run a tool."""
    found: list[str] = []
    for source in sources:
        for line in source.text.splitlines():
            if any(marker in line for marker in _INSTRUCTION_MARKERS):
                found.append(line)
    return found


def apply_sources(scope: str, sources: Sequence[Source]) -> tuple[str, list[str]]:
    """Return ``scope`` unchanged and the ignored instruction lines.

    The lines are not executed and nothing is written.
    """
    return scope, ignored_instructions(sources)


def quality_findings(sources: Sequence[Source]) -> list[QualityFinding]:
    """List abstract-only access, metadata, retraction, and omitted figures."""
    findings: list[QualityFinding] = []
    for source in sources:
        if source.coverage == "abstract_only":
            findings.append(QualityFinding(source.source_id, "abstract_only", "abstract_only"))
        elif source.coverage == "metadata":
            findings.append(QualityFinding(source.source_id, "metadata", "metadata"))
        if source.retracted:
            findings.append(QualityFinding(source.source_id, "retracted", "retracted"))
        if _FIGURE_OMITTED in source.text:
            findings.append(QualityFinding(source.source_id, "figure_omitted", _FIGURE_OMITTED))
    return findings


def _source_docs(sources: Sequence[Source]) -> dict[str, SourceDoc]:
    docs: dict[str, SourceDoc] = {}
    for source in _index(sources).values():
        docs[source.source_id] = SourceDoc(
            source_id=source.source_id,
            passages=(
                Passage(
                    passage_id=f"{source.source_id}-body",
                    source_id=source.source_id,
                    locator="body",
                    text=source.text,
                ),
            ),
        )
    return docs


def matrix_for(
    draft: str,
    sources: Sequence[Source],
    *,
    all_sources: bool = False,
) -> EvidenceMatrix:
    """Build the literature matrix from one body passage per source.

    Citations are the ``[^source_id]`` markers the evidence matrix parses.
    ``all_sources`` defaults to false, so an uncited claim is the only one
    that draws on the whole catalog.
    """
    return build_evidence_matrix(
        report_id="literature",
        draft=draft,
        sources=_source_docs(sources),
        classifier=StubClassifier(),
        all_sources=all_sources,
    )


def contradictions(matrix: EvidenceMatrix) -> list[str]:
    """Claim ids whose matrix status is contested."""
    return [
        claim.id
        for claim in matrix.material_claims()
        if matrix.statuses[claim.id] == "contested"
    ]


def _literature_status(
    matrix_status: str,
    supporters: Sequence[Source],
) -> tuple[SynthesisStatus, Disclosure | None]:
    # A contesting passage wins even when another passage supports.
    if matrix_status == "contested":
        return "unresolved", "contested"
    if matrix_status != "supported":
        return "unresolved", None
    if any(source.coverage == "full_text" and not source.retracted for source in supporters):
        return "literature-supported", None
    if supporters and all(source.coverage == "abstract_only" for source in supporters):
        return "unresolved", "abstract_only"
    if any(source.retracted for source in supporters):
        return "unresolved", "retracted"
    if any(source.coverage == "metadata" for source in supporters):
        return "unresolved", "metadata"
    return "unresolved", None


def synthesis(matrix: EvidenceMatrix, sources: Sequence[Source]) -> tuple[ClaimSynthesis, ...]:
    """Turn matrix statuses into literature-supported or unresolved labels."""
    indexed = _index(sources)
    records: list[ClaimSynthesis] = []
    for claim in matrix.material_claims():
        matrix_status = matrix.statuses[claim.id]
        supporting = matrix.supporting_passages(claim.id)
        contradicting = matrix.contradicting_passages(claim.id)
        passages: list[str] = []
        seen: set[str] = set()
        for passage in (*supporting, *contradicting):
            if passage.passage_id in seen:
                continue
            seen.add(passage.passage_id)
            passages.append(passage.text)
        supporters: list[Source] = []
        for passage in supporting:
            source = indexed.get(passage.source_id)
            if source is None:
                raise ValueError(f"matrix passage cites unknown source {passage.source_id!r}")
            supporters.append(source)
        status, disclosure = _literature_status(matrix_status, supporters)
        records.append(
            ClaimSynthesis(
                claim_id=claim.id,
                text=claim.text,
                matrix_status=matrix_status,
                status=status,
                disclosure=disclosure,
                passages=tuple(passages),
            )
        )
    return tuple(records)


def profile(
    *,
    question: str,
    scope: str,
    draft: str,
    sources: Sequence[Source],
) -> LiteratureProfile:
    """Assemble the literature campaign profile without changing source versions."""
    kept_scope, ignored = apply_sources(scope, sources)
    matrix = matrix_for(draft, sources)
    records = synthesis(matrix, sources)
    return LiteratureProfile(
        question=question,
        scope=kept_scope,
        independent_studies=independent_studies(sources),
        quality=tuple(quality_findings(sources)),
        ignored=tuple(ignored),
        statuses=tuple((record.claim_id, record.status) for record in records),
        synthesis=records,
        versions=tuple((source.source_id, source.version) for source in sources),
    )
