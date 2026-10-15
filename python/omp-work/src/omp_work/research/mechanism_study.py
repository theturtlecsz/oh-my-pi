"""Preregistered mechanism populations and the L3 admission gate (OMP-606-s06).

Two generators, adaptive-restart.v1 and root-first.v1, each emit one policy at
every frozen threshold 0.25, 0.5 and 0.75. Generator, spec, source and cohort
identities are digested before any campaign runs. Every population member is
executed on the matched task and caps. The best member is not an L2 result.

Simulation tasks may develop a proposal. A structural family change is admitted
only by ``recursive_confirmation`` over a fresh simulation confirmation cohort.
Held-out engineering tasks are transfer measurements. They are not written back
into the generator, and they cannot admit L3. Nothing here turns a generator on
by default.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from omp_work.jobs.campaign_setup import register_campaign_artifact
from omp_work.jobs.generated_campaign import GeneratedCampaign
from omp_work.jobs.research_campaign import run_research_campaign
from omp_work.jobs.store import NativeJobStore
from omp_work.research import mechanism, mechanism_recipe, mechanism_tasks, recursive_confirmation
from omp_work.research.mechanism_tasks import MechanismTask
from omp_work.research.recursive import RecursionState, RecursiveProposal, admit_proposal
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_policy import ActionRequest, RepositoryRecord, StandingPolicy
from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.client import WorkClient

__all__ = [
    "BASELINE_FAMILY",
    "CANDIDATE_FAMILY",
    "CAP_KEYS",
    "GENERATORS",
    "SPEC",
    "Admission",
    "CampaignContext",
    "FrozenPolicy",
    "FrozenStudy",
    "MemberRun",
    "StudyExecution",
    "assignments",
    "execute",
    "finish",
    "freeze_caps",
    "freeze_study",
    "gate_structural_admission",
    "population_average",
    "run_controlled",
    "validate_study",
]

GENERATORS = mechanism_recipe.FAMILIES
BASELINE_FAMILY = mechanism_recipe.ADAPTIVE_RESTART
CANDIDATE_FAMILY = mechanism_recipe.ROOT_FIRST
SPEC = {
    "name": "mechanism-study",
    "interface_version": 1,
    "input_keys": ["steps", "opened", "seed", "width"],
    "output": "step_id | None",
}
CAP_KEYS = (
    "node_budget",
    "selection_seconds",
    "selection_timeout",
    "selection_attempts",
    "width",
)
_INT_CAPS = frozenset({"selection_attempts", "width"})
PASS_RULE = "all-pairs-positive.v1"


def population_average(differences: Sequence[float]) -> float:
    """Mean of every threshold. The largest member is not the population result."""
    if len(differences) != len(mechanism_recipe.THRESHOLD_GRID):
        raise ValueError("population_incomplete")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
           for value in differences):
        raise ValueError("nonfinite_difference")
    return math.fsum(differences) / len(differences)


def freeze_caps(caps: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Canonical matched caps. Width and attempt counts stay integers."""
    raw = {
        "node_budget": mechanism_tasks.BUDGET,
        "selection_seconds": 30.0,
        "selection_timeout": 10.0,
        "selection_attempts": 4,
        "width": mechanism_tasks.WIDTH,
    }
    if caps:
        unknown = set(caps) - set(CAP_KEYS)
        if unknown:
            raise ValueError(f"unknown cap: {sorted(unknown)}")
        raw.update(caps)
    out: dict[str, Any] = {}
    for key in CAP_KEYS:
        value = raw[key]
        if key in _INT_CAPS:
            if type(value) is not int or value < (1 if key == "width" else 0):
                raise ValueError(f"{key} must be a nonnegative integer")
            out[key] = value
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError(f"{key} must be a finite nonnegative number")
        out[key] = float(value)
    return out


def _phase(task: MechanismTask) -> str:
    if task.cohort == "development":
        return "development"
    if task.cohort in {"confirmation", "heldout"}:
        return "confirmation"
    raise ValueError(f"unknown cohort: {task.cohort}")


def _metric(task: MechanismTask) -> str:
    if task.domain == "simulation":
        return "score"
    if task.domain == "engineering":
        return "ratio"
    raise ValueError(f"unknown domain: {task.domain}")


@dataclass(frozen=True, slots=True)
class FrozenPolicy:
    family: str
    threshold: float
    recipe_json: str
    recipe_sha256: str
    spec_sha256: str
    source: str
    source_sha256: str
    implementation_sha256: str

    @property
    def recipe(self) -> dict[str, Any]:
        return json.loads(self.recipe_json)

    @property
    def role(self) -> str:
        if self.family == BASELINE_FAMILY:
            return "baseline"
        if self.family == CANDIDATE_FAMILY:
            return "candidate"
        raise ValueError(f"unknown family: {self.family}")


@dataclass(frozen=True, slots=True)
class FrozenStudy:
    """Identities fixed before execution. Engineering bytes are not in the spec."""

    spec_json: str
    spec_sha256: str
    caps_json: str
    policies: tuple[FrozenPolicy, ...]
    tasks: tuple[MechanismTask, ...]
    cohorts_json: str
    digest: str

    @property
    def caps(self) -> dict[str, Any]:
        return json.loads(self.caps_json)

    @property
    def cohort_sha256(self) -> dict[str, str]:
        return json.loads(self.cohorts_json)

    def policy(self, family: str, threshold: float) -> FrozenPolicy | None:
        for item in self.policies:
            if item.family == family and item.threshold == threshold:
                return item
        return None

    @property
    def default_activated(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class MemberRun:
    """One generated campaign. ``result`` may contain runner scores; they are not evidence."""

    role: str
    family: str
    threshold: float | None
    recipe_sha256: str
    source_sha256: str
    spec_sha256: str
    source: str
    task_id: str
    content_sha256: str
    manifest_sha256: str
    domain: str
    cohort: str
    phase: str
    seed: int
    protocol_sha256: str
    evaluator_sha256: str
    metric: str
    direction: str
    caps: dict[str, Any]
    dataset_id: str
    result: dict[str, Any]
    failure: str | None = None


@dataclass
class CampaignContext:
    client: WorkClient
    store: NativeJobStore
    key: str
    workspace_id: UUID
    actor_id: UUID
    work_id: UUID
    harness_dir: Path
    data_dir: Path
    datasets: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Admission:
    """Result of the confirmation gate. Default activation is not a field that can be set."""

    admitted: bool
    reason: str | None
    structural: bool
    pass_rule: str
    parent_family: str | None
    proposed_family: str | None
    cohort_count: int
    state: RecursionState

    @property
    def default_activated(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class StudyExecution:
    study: FrozenStudy
    runs: tuple[MemberRun, ...]
    admission: Admission
    completed: bool = True

    @property
    def default_activated(self) -> bool:
        return False


def _policies(spec: Mapping[str, Any]) -> tuple[FrozenPolicy, ...]:
    policies: list[FrozenPolicy] = []
    for family in GENERATORS:
        for item in mechanism.generation_population(spec, family):
            recipe = item["recipe"]
            policies.append(FrozenPolicy(
                family=item["family"],
                threshold=float(recipe["min_metric"]),
                recipe_json=canonical_json(recipe),
                recipe_sha256=item["recipe_sha256"],
                spec_sha256=item["spec_sha256"],
                source=item["source"],
                source_sha256=item["source_sha256"],
                implementation_sha256=item["implementation_sha256"],
            ))
    if len(policies) != len(GENERATORS) * len(mechanism_recipe.THRESHOLD_GRID):
        raise ValueError("population_incomplete")
    if len({item.implementation_sha256 for item in policies}) < 2:
        raise ValueError("generators_not_distinct")
    return tuple(policies)


def _cohorts(tasks: Sequence[MechanismTask]) -> dict[str, str]:
    grouped: dict[str, list[MechanismTask]] = {}
    for task in tasks:
        grouped.setdefault(task.cohort, []).append(task)
    if not grouped:
        raise ValueError("cohort is empty")
    mechanism_tasks.assert_disjoint(*grouped.values())
    return {
        name: mechanism_tasks.cohort_manifest(tuple(members))["manifest_sha256"]
        for name, members in grouped.items()
    }


def freeze_study(
    repo_root: Path | str | None = None,
    *,
    tasks: Sequence[MechanismTask] | None = None,
    caps: Mapping[str, Any] | None = None,
    spec: Mapping[str, Any] | None = None,
) -> FrozenStudy:
    """Digest generators, spec, sources and cohorts before any campaign starts."""
    spec_map = dict(SPEC if spec is None else spec)
    if not spec_map:
        raise ValueError("spec must be nonempty")
    frozen_caps = freeze_caps(caps)
    policies = _policies(spec_map)
    if tasks is None:
        if repo_root is None:
            raise ValueError("repo_root is required for the preregistered cohorts")
        chosen: tuple[MechanismTask, ...] = (
            *mechanism_tasks.development_cohort(),
            *mechanism_tasks.confirmation_cohort(),
            *mechanism_tasks.engineering_cohort(repo_root),
        )
    else:
        chosen = tuple(tasks)
    if any(not task.real_evidence for task in chosen):
        raise ValueError("fixture tasks cannot enter a mechanism study")
    cohort_sha = _cohorts(chosen)
    # Engineering content is recorded as a cohort. It is not copied into the spec.
    spec_json = canonical_json(spec_map)
    for task in chosen:
        if task.domain == "engineering" and task.content_sha256 in spec_json:
            raise ValueError("engineering_tuned_generator")
    digest = sha256({
        "spec": sha256(spec_map),
        "caps": frozen_caps,
        "policies": [
            {
                "family": item.family,
                "threshold": item.threshold,
                "recipe": item.recipe_sha256,
                "source": item.source_sha256,
                "implementation": item.implementation_sha256,
            }
            for item in policies
        ],
        "tasks": [task.manifest_sha256 for task in chosen],
        "cohorts": cohort_sha,
    })
    return FrozenStudy(
        spec_json=spec_json,
        spec_sha256=sha256(spec_map),
        caps_json=canonical_json(frozen_caps),
        policies=policies,
        tasks=chosen,
        cohorts_json=canonical_json(cohort_sha),
        digest=digest,
    )


def validate_study(study: FrozenStudy) -> None:
    """Recheck all preregistered identities at execution and reporting boundaries."""
    for task in study.tasks:
        for node in task.nodes:
            mechanism_tasks.managed_inputs(task, node.id)
    expected = freeze_study(tasks=study.tasks, caps=study.caps, spec=json.loads(study.spec_json))
    if study != expected:
        raise ValueError("study_changed")


def assignments(study: FrozenStudy) -> tuple[tuple[FrozenPolicy, MechanismTask], ...]:
    """Every policy on every frozen task, in generator order. No score ranking."""
    return tuple((policy, task) for task in study.tasks for policy in study.policies)


def _dataset(ctx: CampaignContext, task: MechanismTask) -> str:
    body = canonical_json({
        "task_id": task.task_id,
        "payload": {"content_sha256": task.content_sha256, "domain": task.domain, "cohort": task.cohort},
    }).encode()
    digest = hashlib.sha256(body).hexdigest()
    if digest not in ctx.datasets:
        register_campaign_artifact(
            ctx.client, ctx.workspace_id, body, source_ref=f"mechanism-dataset:{digest}",
        )
        ctx.datasets[digest] = digest
    return digest


def _member(
    *,
    role: str,
    family: str,
    threshold: float | None,
    recipe_sha256: str,
    source_sha256: str,
    spec_sha256: str,
    source: str,
    task: MechanismTask,
    caps: Mapping[str, Any],
    dataset_id: str,
    result: dict[str, Any],
    failure: str | None,
) -> MemberRun:
    return MemberRun(
        role=role,
        family=family,
        threshold=threshold,
        recipe_sha256=recipe_sha256,
        source_sha256=source_sha256,
        spec_sha256=spec_sha256,
        source=source,
        task_id=task.task_id,
        content_sha256=task.content_sha256,
        manifest_sha256=task.manifest_sha256,
        domain=task.domain,
        cohort=task.cohort,
        phase=_phase(task),
        seed=task.seed,
        protocol_sha256=hashlib.sha256(task.protocol_bytes).hexdigest(),
        evaluator_sha256=hashlib.sha256(task.evaluator_script).hexdigest(),
        metric=_metric(task),
        direction="higher",
        caps=dict(caps),
        dataset_id=dataset_id,
        result=result,
        failure=failure,
    )


def _run_campaign(
    ctx: CampaignContext,
    task: MechanismTask,
    recipe: Mapping[str, Any],
    source: str,
    caps: Mapping[str, Any],
) -> tuple[dict[str, Any], str | None]:
    campaign = GeneratedCampaign(
        task,
        dict(recipe),
        ctx.data_dir,
        source=source.encode("utf-8"),
        node_budget=float(caps["node_budget"]),
        selection_seconds=float(caps["selection_seconds"]),
        selection_timeout=float(caps["selection_timeout"]),
        selection_attempts=int(caps["selection_attempts"]),
        width=int(caps["width"]),
    )
    try:
        result = run_research_campaign(
            ctx.client, ctx.store, ctx.key, campaign.definition,
            harness_dir=ctx.harness_dir, actor_id=ctx.actor_id, generated=campaign,
        )
    except RuntimeError as exc:
        return {
            "trials": [], "decisions": [], "best": None, "spent": 0.0,
            "receipt_links": [], "selection_attempts": [], "node_attempts": [],
        }, str(exc)
    return dict(result), None


def run_policy(
    study: FrozenStudy,
    policy: FrozenPolicy,
    task: MechanismTask,
    ctx: CampaignContext,
) -> MemberRun:
    """Run one frozen population member. Caps are the study caps, not a per-arm choice."""
    validate_study(study)
    if task.manifest_sha256 not in {item.manifest_sha256 for item in study.tasks}:
        raise ValueError("task_not_frozen")
    if study.policy(policy.family, policy.threshold) != policy:
        raise ValueError("policy_not_frozen")
    result, failure = _run_campaign(ctx, task, policy.recipe, policy.source, study.caps)
    return _member(
        role=policy.role, family=policy.family, threshold=policy.threshold,
        recipe_sha256=policy.recipe_sha256, source_sha256=policy.source_sha256,
        spec_sha256=policy.spec_sha256, source=policy.source, task=task, caps=study.caps,
        dataset_id=_dataset(ctx, task), result=result, failure=failure,
    )


def execute(
    study: FrozenStudy,
    ctx: CampaignContext,
    *,
    task_ids: Sequence[str] | None = None,
) -> tuple[MemberRun, ...]:
    """Execute every population member on each selected frozen task."""
    validate_study(study)
    chosen = study.tasks
    if task_ids is not None:
        wanted = set(task_ids)
        chosen = tuple(task for task in study.tasks if task.task_id in wanted)
        if {task.task_id for task in chosen} != wanted:
            raise ValueError("unknown_task")
    runs: list[MemberRun] = []
    for task in chosen:
        for policy in study.policies:
            runs.append(run_policy(study, policy, task, ctx))
    return tuple(runs)


def run_controlled(
    task: MechanismTask,
    *,
    role: str,
    family: str,
    source: str,
    recipe: Mapping[str, Any],
    caps: Mapping[str, Any],
    ctx: CampaignContext,
) -> MemberRun:
    """Real generated campaign for a predeclared controlled contrast.

    This is not a population member and cannot be reported as one. Both arms of
    a contrast must be given the same caps by the caller; the report rejects a
    mismatch.
    """
    if role not in {"baseline", "candidate"}:
        raise ValueError("role must be baseline or candidate")
    frozen_caps = freeze_caps(caps)
    canonical = mechanism.canonical_recipe(recipe)
    result, failure = _run_campaign(ctx, task, canonical, source, frozen_caps)
    return _member(
        role=role, family=family, threshold=None,
        recipe_sha256=sha256(canonical),
        source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        spec_sha256="", source=source, task=task, caps=frozen_caps,
        dataset_id=_dataset(ctx, task), result=result, failure=failure,
    )


def _family_of(recipe_bytes: bytes) -> str:
    return mechanism_recipe.canonical_recipe(json.loads(recipe_bytes))["family"]


def _engineering_smuggled(request: object, study: FrozenStudy | None) -> bool:
    cohorts = getattr(request, "cohorts", ()) or ()
    for cohort in cohorts:
        if getattr(cohort, "domain", None) == "engineering":
            return True
    return False


def gate_structural_admission(
    state: RecursionState,
    proposal: RecursiveProposal,
    request: object,
    *,
    grant,
    policy: StandingPolicy,
    authority: ChangeAuthority,
    action: ActionRequest,
    repositories: Mapping[str, RepositoryRecord] | Sequence[RepositoryRecord],
    now: datetime,
    study: FrozenStudy | None = None,
) -> Admission:
    """Admit a structural generator change only through recursive confirmation.

    Booleans, report summaries and held-out engineering cohorts are not proof.
    A null or failed confirmation leaves the incumbent in place. The returned
    admission cannot activate a default.
    """
    structural = recursive_confirmation.structural_change(state, proposal)
    parent = _family_of(recursive_confirmation._incumbent(state, proposal))
    proposed = _family_of(proposal.recipe)
    cohort_count = len(getattr(request, "cohorts", ()) or ())
    if study is not None and proposed:
        blob = proposal.spec.decode("utf-8", "replace")
        if any(task.domain == "engineering" and task.content_sha256 in blob for task in study.tasks):
            return Admission(False, "engineering_tuned_generator", structural, PASS_RULE,
                             parent, proposed, cohort_count, state)
    if _engineering_smuggled(request, study):
        return Admission(False, "engineering_cannot_admit", structural, PASS_RULE,
                         parent, proposed, cohort_count, state)
    if structural and type(request) is recursive_confirmation.ConfirmationRequest:
        # Staging freezes the population. Admission still re-reads native custody.
        staged = recursive_confirmation.stage_proposal(state, proposal)
        if staged.pass_rule != PASS_RULE or request.plan.pass_rule != PASS_RULE:
            return Admission(False, "confirmation_rule_changed", structural, PASS_RULE,
                             parent, proposed, cohort_count, state)
    out = admit_proposal(
        state, proposal, grant, policy=policy, authority=authority, action=action,
        repositories=repositories, now=now, confirmation=request,
    )
    admitted = (
        out.terminal_reason is None
        and len(out.proposal_history) == len(state.proposal_history) + 1
    )
    return Admission(
        admitted, out.terminal_reason, structural, PASS_RULE, parent, proposed, cohort_count, out,
    )


def finish(study: FrozenStudy, runs: Sequence[MemberRun], admission: Admission) -> StudyExecution:
    """A study is complete when the comparison and the admission gate have both returned.

    A null comparison and a refused structural change still complete. The spec
    digest is unchanged by execution.
    """
    validate_study(study)
    return StudyExecution(study, tuple(runs), admission, True)
