"""Local NSI-6 cutover state. Missing state selects current; invalid state refuses."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Literal

from pydantic import Field, model_validator

from omp_work.operations.config import OperationsConfig
from omp_work.contracts.nsi.v1.compat import evaluate_typed_bounded_intake
from omp_work.v1.canonical import sha256
from omp_work.v1.models import BoundedIntakeDraft, IntakeBlockingQuestion, StrictModel, hex64
from omp_work.v1.semantics import evaluate_bounded_intake
from omp_work.v1.store_shared import WorkStoreError

ROOT = Path(__file__).resolve().parents[4]
CORPUS = ROOT / "evals/nsi6/v1"


def byte_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def implementation_binding() -> dict[str, str]:
    """Bind source/schema bytes, never operational state or release bookkeeping."""
    source = ROOT / "python/omp-work/src/omp_work"
    paths = set((source / "v1").glob("*.py"))
    paths.update((source / "contracts/nsi/v1").glob("*.py"))
    paths.update((source / "contracts/nsi/v1").glob("*.json"))
    paths.update(source / name for name in (
        "nsi_route.py", "nsi_rollout.py", "nsi_eval.py", "nsi_eval_postgres.py",
        "nsi6_corpus.py", "frozen_corpus.py", "intake_repository.py", "operations/config.py",
        "operations/database.py",
    ))
    paths.add(source / "mission_intake_rules.py")
    paths.update((ROOT / "packages/work-client/src").glob("intake*.ts"))
    paths.add(ROOT / "packages/work-client/src/index.ts")
    paths.update(ROOT / f"session-system/extensions/workflow/{name}.ts" for name in ("work", "host", "backend"))
    return {str(path.relative_to(ROOT)): byte_digest(path) for path in sorted(paths)}


def corpus_binding() -> dict[str, str]:
    return {str(path.relative_to(CORPUS)): byte_digest(path)
            for path in sorted([CORPUS / "nsi6-manifest.json", *(CORPUS / "inputs").glob("*.json")])}


class EvidenceFile(StrictModel):
    path: str = Field(min_length=1)
    sha256: hex64


class ReviewRecord(StrictModel):
    kind: Literal["nsi6-review"] = "nsi6-review"
    version: Literal[1] = 1
    reviewer: Literal["Chris"]
    approved: Literal[True]
    reference: str = Field(min_length=1, pattern=r"\S")
    reason: str = Field(min_length=1, pattern=r"\S")
    report: EvidenceFile
    implementation: dict[str, hex64]
    corpus: dict[str, hex64]
    review_sha256: hex64

    @model_validator(mode="after")
    def sealed(self) -> ReviewRecord:
        if self.review_sha256 != sha256(self.model_dump(mode="json", exclude={"review_sha256"})):
            raise ValueError("review digest drift")
        return self


class RouteState(StrictModel):
    kind: Literal["nsi6-route-state"] = "nsi6-route-state"
    version: Literal[1] = 1
    mode: Literal["current", "typed"]
    report: EvidenceFile | None = None
    review: EvidenceFile | None = None
    implementation: dict[str, hex64] = Field(default_factory=dict)
    corpus: dict[str, hex64] = Field(default_factory=dict)
    prior_binding: dict[str, object] | None = None
    state_sha256: hex64

    @model_validator(mode="after")
    def sealed(self) -> RouteState:
        if self.state_sha256 != sha256(self.model_dump(mode="json", exclude={"state_sha256"})):
            raise ValueError("route state digest drift")
        if self.mode == "typed" and (self.report is None or self.review is None
                                     or not self.implementation or not self.corpus):
            raise ValueError("typed route requires promotion binding")
        return self


def seal_state(**fields) -> RouteState:
    body = {"kind": "nsi6-route-state", "version": 1, "report": None, "review": None,
            "implementation": {}, "corpus": {}, "prior_binding": None, **fields}
    return RouteState.model_validate({**body, "state_sha256": sha256(body)})


def read_state(path: Path, *, check_binding: bool = True) -> RouteState:
    try:
        state = RouteState.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        return seal_state(mode="current")
    if check_binding and state.mode == "typed":
        if state.implementation != implementation_binding() or state.corpus != corpus_binding():
            raise ValueError("stale typed implementation/corpus binding")
        for evidence in (state.report, state.review):
            if evidence is None or byte_digest(Path(evidence.path)) != evidence.sha256:
                raise ValueError("stale typed report/review binding")
        review = ReviewRecord.model_validate_json(Path(state.review.path).read_bytes())
        if (review.implementation != state.implementation or review.corpus != state.corpus
                or review.report != state.report):
            raise ValueError("invalid typed review binding")
    return state


def route_mode(config: OperationsConfig) -> Literal["current", "typed"]:
    try:
        return read_state(config.config_dir / "nsi-route.json").mode
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise WorkStoreError("intake_not_ready", (f"NSI-6 route state invalid: {error}",)) from error


def evaluate_intake(config: OperationsConfig, draft: BoundedIntakeDraft) -> tuple[tuple[IntakeBlockingQuestion, ...], int]:
    evaluator = evaluate_typed_bounded_intake if route_mode(config) == "typed" else evaluate_bounded_intake
    return evaluator(draft)


def replace_state(path: Path, state: RouteState) -> None:
    """Flush then atomically replace one local state file; clean failed writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(state.model_dump(mode="json"), stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
