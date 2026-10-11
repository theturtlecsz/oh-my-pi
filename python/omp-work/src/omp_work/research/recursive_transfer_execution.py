"""Recursive transfer execution orchestrator for baseline and candidate arms.

OMP-319-s03-s05-s05:
- ArmExecution exposes selection, invocation count, verified trial IDs,
  selected BoundStep, failures, and updated policy state.
- run_arm binds every opened step for the arm before policy invocation,
  requiring admitted trial identity from ReservationLedger.
- Only verified Observations enter GeneratedPolicy.
- MethodContext uses native controller fields and frozen.result.state,
  suffixing request_id by arm for independent usage.
- Never fabricates grants, policy, authority or scope.
- Invokes policy once with validated payload steps/opened and prepared seed/width.
- Binds the selected step after proposal.
- Execution/verification exceptions become failures with no fallback pick/score.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from omp_work.operations.artifacts import read_verified_bytes
from omp_work.research.custody import artifact_path
from omp_work.research.receipt import EvaluationProtocol, load_protocol
from omp_work.research.recursive import RecursionState
from omp_work.research.recursive_method import GeneratedPolicy, MethodContext, Observation
from omp_work.research.recursive_transfer_evidence import NativeEvidence
from omp_work.research.recursive_transfer_execution_evidence import BoundStep, bind_step
from omp_work.research.recursive_transfer_execution_inputs import ExecutionPayload, PreparedExecution
from omp_work.research.recursive_transfer_execution_reservation import ReservationLedger
from omp_work.research.recursive_transfer_freshness import PreparedStep
from omp_work.research.recursive_transfer_methods import FrozenMethod

__all__ = [
    "ArmExecution",
    "run_arm",
]


@dataclass(frozen=True)
class ArmExecution:
    """Outcome of running one transfer method arm."""

    selection: str | None
    invocation_count: int
    verified_trial_ids: tuple[str, ...]
    selected_bound_step: BoundStep | None
    failures: tuple[str, ...]
    state: RecursionState

    @property
    def selected_step(self) -> BoundStep | None:
        return self.selected_bound_step

    @property
    def bound_step(self) -> BoundStep | None:
        return self.selected_bound_step

    @property
    def policy_state(self) -> RecursionState:
        return self.state


def _load_protocol(native: NativeEvidence, protocol_sha256: str) -> EvaluationProtocol:
    """Load and verify evaluation protocol bytes from custody."""
    with native.store.transaction(native.workspace_id, native.actor_id) as cur:
        cur.execute(
            "SELECT manifest FROM omp_research.artifacts WHERE workspace_id=%s AND artifact_sha256=%s",
            (native.workspace_id, protocol_sha256),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError("missing_protocol")
        manifest = row["manifest"]
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("size_bytes"), int):
            raise ValueError("missing_protocol")
        size = manifest["size_bytes"]
    proto_bytes = read_verified_bytes(
        artifact_path(native.data_dir, native.workspace_id, protocol_sha256),
        protocol_sha256,
        size,
    )
    return load_protocol(proto_bytes, expected_sha256=protocol_sha256)


def run_arm(
    frozen: FrozenMethod,
    prepared: PreparedExecution,
    payload: ExecutionPayload,
    native: NativeEvidence,
    ledger: ReservationLedger,
    arm: Literal["baseline", "candidate"] | str,
) -> ArmExecution:
    """Execute one frozen arm against validated payload and reservation ledger.

    Refused reservation never reaches GeneratedPolicy.
    Every opened step for this arm is bound and verified before invocation,
    requiring admitted trial identity.
    Only verified Observations enter GeneratedPolicy.
    MethodContext uses native controller fields and frozen.result.state,
    with request_id suffixed by arm.
    """
    initial_state = frozen.result.state

    if arm not in ("baseline", "candidate"):
        return ArmExecution(
            selection=None,
            invocation_count=0,
            verified_trial_ids=(),
            selected_bound_step=None,
            failures=("invalid_trial_arm",),
            state=initial_state,
        )

    # Refused reservation never reaches GeneratedPolicy.
    if ledger.reservation.reasons:
        return ArmExecution(
            selection=None,
            invocation_count=0,
            verified_trial_ids=(),
            selected_bound_step=None,
            failures=tuple(ledger.reservation.reasons),
            state=initial_state,
        )

    if prepared.reasons:
        return ArmExecution(
            selection=None,
            invocation_count=0,
            verified_trial_ids=(),
            selected_bound_step=None,
            failures=tuple(prepared.reasons),
            state=initial_state,
        )

    if not prepared.eligible:
        return ArmExecution(
            selection=None,
            invocation_count=0,
            verified_trial_ids=(),
            selected_bound_step=None,
            failures=("ineligible_task",),
            state=initial_state,
        )

    if not ledger.comparison_allowed:
        return ArmExecution(None, 0, (), None, ("comparison_not_allowed",), initial_state)

    if native.operation_id is None or native.request_id is None:
        return ArmExecution(None, 0, (), None, ("missing_controller_identity",), initial_state)

    step_map: dict[str, PreparedStep] = {s.step_id: s for s in prepared.steps}
    admitted_ids: set[str] = set(ledger.newly_admitted_trial_ids)
    verified_ids: list[str] = []
    observations: dict[str, Observation] = {}
    opened_bound_steps: dict[str, BoundStep] = {}

    opened_ids = sorted(payload.opened)

    # 1. Bind every opened step for this arm before invocation, requiring admitted trial identity.
    for step_id in opened_ids:
        prep_step = step_map.get(step_id)
        if prep_step is None:
            return ArmExecution(
                selection=None,
                invocation_count=0,
                verified_trial_ids=tuple(dict.fromkeys(verified_ids)),
                selected_bound_step=None,
                failures=("missing_step",),
                state=initial_state,
            )

        trial_id = prep_step.baseline_trial_id if arm == "baseline" else prep_step.candidate_trial_id
        if trial_id is None:
            return ArmExecution(
                selection=None,
                invocation_count=0,
                verified_trial_ids=tuple(dict.fromkeys(verified_ids)),
                selected_bound_step=None,
                failures=("missing_trial_arm",),
                state=initial_state,
            )

        if str(trial_id) not in admitted_ids:
            return ArmExecution(
                selection=None,
                invocation_count=0,
                verified_trial_ids=tuple(dict.fromkeys(verified_ids)),
                selected_bound_step=None,
                failures=("unadmitted_trial",),
                state=initial_state,
            )

        try:
            bound = bind_step(native, frozen, prep_step, arm)
        except Exception as error:
            # Bad opened non-pick tar fails arm before its evidence reaches policy
            failure = str(error)
            return ArmExecution(
                selection=None,
                invocation_count=0,
                verified_trial_ids=tuple(dict.fromkeys(verified_ids)),
                selected_bound_step=None,
                failures=(failure,),
                state=initial_state,
            )

        verified_ids.append(str(trial_id))
        observations[step_id] = bound.observation
        opened_bound_steps[step_id] = bound

    # 2. Build MethodContext using native controller fields and frozen.result.state
    context = MethodContext(
        state=frozen.result.state,
        grant=native.grant,
        policy=native.policy,
        authority=native.authority,
        action=native.action,
        repositories=native.repositories,
        store=native.store,
        workspace_id=native.workspace_id,
        actor_id=native.actor_id,
        work_id=native.work_id,
        # Usage writes also key idempotency by operation_id. Both arm writes
        # need distinct derived identities while retaining native caller scope.
        operation_id=f"{native.operation_id}:{arm}",
        request_id=f"{native.request_id}:{arm}",
    )

    # 3. Determine protocol and construct GeneratedPolicy
    try:
        if opened_bound_steps:
            protocol = next(iter(opened_bound_steps.values())).evidence.protocol
        else:
            protocol = _load_protocol(native, frozen.study.protocol_sha256)
    except Exception as error:
        return ArmExecution(
            selection=None,
            invocation_count=0,
            verified_trial_ids=tuple(dict.fromkeys(verified_ids)),
            selected_bound_step=None,
            failures=(str(error),),
            state=initial_state,
        )

    try:
        policy = GeneratedPolicy(
            frozen.result,
            frozen.study,
            context=context,
            protocol=protocol,
            observations=observations,
        )
    except Exception as error:
        return ArmExecution(
            selection=None,
            invocation_count=0,
            verified_trial_ids=tuple(dict.fromkeys(verified_ids)),
            selected_bound_step=None,
            failures=(str(error),),
            state=initial_state,
        )

    # 4. Invoke policy once with validated payload steps/opened and prepared seed/width
    invocation_count = 1
    try:
        pick, _ = policy(payload.steps, set(payload.opened), seed=prepared.seed, width=prepared.width)
    except Exception as error:
        failures = policy.failures or (str(error),)
        return ArmExecution(
            selection=None,
            invocation_count=invocation_count,
            verified_trial_ids=tuple(dict.fromkeys(verified_ids)),
            selected_bound_step=None,
            failures=failures,
            state=policy.state,
        )

    # 5. Bind selected step too
    if pick is None:
        return ArmExecution(
            selection=None,
            invocation_count=invocation_count,
            verified_trial_ids=tuple(dict.fromkeys(verified_ids)),
            selected_bound_step=None,
            failures=(),
            state=policy.state,
        )

    selected_step = step_map.get(pick)
    if selected_step is None:
        return ArmExecution(
            selection=None,
            invocation_count=invocation_count,
            verified_trial_ids=tuple(dict.fromkeys(verified_ids)),
            selected_bound_step=None,
            failures=("missing_selected_step",),
            state=policy.state,
        )

    selected_trial_id = (
        selected_step.baseline_trial_id if arm == "baseline" else selected_step.candidate_trial_id
    )
    if selected_trial_id is None:
        return ArmExecution(
            selection=None,
            invocation_count=invocation_count,
            verified_trial_ids=tuple(dict.fromkeys(verified_ids)),
            selected_bound_step=None,
            failures=("missing_trial_arm",),
            state=policy.state,
        )

    if str(selected_trial_id) not in admitted_ids:
        return ArmExecution(
            selection=None,
            invocation_count=invocation_count,
            verified_trial_ids=tuple(dict.fromkeys(verified_ids)),
            selected_bound_step=None,
            failures=("unadmitted_trial",),
            state=policy.state,
        )

    try:
        selected_bound = bind_step(native, frozen, selected_step, arm)
    except Exception as error:
        # Execution/verification exceptions become failures; no fallback pick/score.
        failures = (str(error),)
        return ArmExecution(
            selection=None,
            invocation_count=invocation_count,
            verified_trial_ids=tuple(dict.fromkeys(verified_ids)),
            selected_bound_step=None,
            failures=failures,
            state=policy.state,
        )

    verified_ids.append(str(selected_trial_id))
    return ArmExecution(
        selection=pick,
        invocation_count=invocation_count,
        verified_trial_ids=tuple(dict.fromkeys(verified_ids)),
        selected_bound_step=selected_bound,
        failures=(),
        state=policy.state,
    )
