"""M5 bounded source-proof pipeline. Caller inputs are not owner approval.

No trials are created: downstream evidence must already exist in native custody.
The selectable method digest is the generated source digest; s02's generator
implementation digest is shared by distinct recipes and cannot select an arm.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from omp_work.jobs.store import NativeJobStore
from omp_work.operations.config import OperationsConfig
from omp_work.research.dossier import verify_dossier, write_dossier
from omp_work.research.program import ProgramCoordinator, QuestionQueue, load_snapshot
from omp_work.research.receipt import managed_job_id
from omp_work.research.recursive import CallerGrant, RecursionState, RecursiveStudy
from omp_work.research.recursive_benefit import (
    ArmLink, ConfirmationComparison, CostAxes, ObservationWindow,
    OwnershipCosts, QualityBound, summarize_benefit,
)
from omp_work.research.recursive_maintenance import (
    FreshResult, MaintenancePolicy, MaintenanceRelease, maintenance_plan,
)
from omp_work.research.recursive_method import MethodContext, run_method
from omp_work.research.recursive_transfer import assemble_results, compare_methods
from omp_work.research.recursive_transfer_evidence import ConsumptionHistory, NativeEvidence
from omp_work.research.recursive_transfer_freshness import CohortTask
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_policy import ActionRequest, RepositoryRecord, StandingPolicy
from omp_work.v1.canonical import canonical_json, sha256


def load_packet(directory: str | Path, expected_sha256: str) -> dict:
    """Reject changed packet, source bytes, report, table or program snapshot."""
    root = Path(directory)
    packet = json.loads((root / "dossier.json").read_text())
    if sha256(packet) != expected_sha256:
        raise ValueError("packet_digest_mismatch")
    reasons = verify_dossier(root)
    if reasons:
        raise ValueError(",".join(reasons))
    program_source = root / "sources" / "program.json"
    if (root / "program.json").read_bytes() != program_source.read_bytes():
        raise ValueError("program_snapshot_altered")
    if packet.get("reevaluation"):
        load_packet(root / "reevaluation", packet["reevaluation"]["packet_sha256"])
    return packet


def _controller(raw: dict, history: ConsumptionHistory) -> tuple[NativeEvidence, tuple[MethodContext, ...]]:
    config_raw = dict(raw["controller"]["config"])
    for key in ("config_dir", "state_dir", "data_dir"):
        config_raw[key] = Path(config_raw[key])
    config = OperationsConfig(**config_raw)
    store = NativeJobStore(config)
    scope = raw["controller"]
    contexts = []
    for arm in ("baseline", "candidate"):
        control = scope[arm]
        grant = CallerGrant.model_validate(control["grant"])
        contexts.append(MethodContext(
            state=RecursionState.model_validate(control["state"]), grant=grant,
            policy=StandingPolicy(**control["policy"]),
            authority=ChangeAuthority(**control["authority"]), action=ActionRequest(**control["action"]),
            repositories=tuple(RepositoryRecord(**r) for r in control["repositories"]),
            store=store, workspace_id=scope["workspace_id"], actor_id=scope["actor_id"],
            work_id=scope["work_id"], operation_id=control["operation_id"], request_id=control["request_id"],
        ))
    context = contexts[1]
    native = NativeEvidence(
        store, context.workspace_id, context.actor_id, context.work_id, config.data_dir,
        grant=context.grant, policy=context.policy, authority=context.authority,
        action=context.action, repositories=context.repositories,
        operation_id=context.operation_id, request_id=context.request_id, history=history,
    )
    return native, tuple(contexts)


def _run(raw: dict, directory: Path, chosen: str | None, history: ConsumptionHistory) -> dict:
    native, contexts = _controller(raw, history)
    studies = tuple(RecursiveStudy.model_validate(raw[arm]["study"]) for arm in ("baseline", "candidate"))
    policy = MaintenancePolicy.model_validate(raw["maintenance_policy"])
    window = ObservationWindow(
        policy.window_start, policy.window_end, QualityBound(**raw["quality_bound"]), policy.cost_bounds,
    )
    if policy.authorization_decision_id != contexts[1].grant.decision_id:
        raise ValueError("maintenance_authorization_mismatch")
    if policy.frozen_at > min(study.window_start for study in studies):
        raise ValueError("bounds_not_preregistered")
    coordinator = ProgramCoordinator(
        raw["program_id"], {str(contexts[1].policy.policy_id): contexts[1].policy},
        str(contexts[1].policy.policy_id), QuestionQueue(), repos=contexts[1].repositories,
    )
    # Program admission and per-method s01 admission both apply; no authority
    # is inferred from this file, the dossier, or the maintenance question.
    coordinator.admit_campaign(raw["run_id"], "recursive-study", datetime.now(timezone.utc), contexts[1].action)
    results = tuple(run_method(
        study, study.recipe, json.loads(study.spec), raw[arm]["development_traces"], context=context,
    ) for arm, study, context in zip(("baseline", "candidate"), studies, contexts))
    selectable = [result.source_sha256 for result in results]
    if chosen is not None and (chosen not in selectable or selectable.count(chosen) != 1):
        raise ValueError("chosen_method_not_unique")
    selected = selectable.index(chosen) if chosen is not None else 1
    sources = {"input.json": canonical_json(raw).encode()}
    for arm, result in zip(("baseline", "candidate"), results):
        if result.source_bytes is not None:
            sources[f"{arm}.py"] = result.source_bytes
    report = None
    benefit = None
    plan = None
    executions = []
    usage = []
    fresh = []
    if not any(result.failures for result in results):
        # Generation operations have committed; invocation identities must be fresh.
        cohorts = tuple(CohortTask.model_validate(c) for c in raw["cohorts"])
        current = list(results)
        for cohort in cohorts:
            # Distinct tasks must not replay one proposal accounting identity.
            # Carry measured state forward so the shared s01 quota covers all.
            native.operation_id = f"{raw['controller']['execution_operation_id']}:{cohort.dataset_id}"
            native.request_id = f"{raw['controller']['execution_request_id']}:{cohort.dataset_id}"
            comparison = compare_methods(studies[1], *current, (cohort,), native)
            rows = [execution for domain in comparison.domains for task in domain.tasks for execution in task.executions]
            executions.extend(rows)
            for execution in rows:
                for index, arm in enumerate((execution.baseline, execution.candidate)):
                    current[index] = replace(current[index], state=arm.state)
        results = tuple(current)
        report = replace(assemble_results(executions), history=history)
        with native.store.transaction(native.workspace_id, native.actor_id) as cur:
            cur.execute("SELECT * FROM omp_jobs.usage_events WHERE workspace_id=%s AND work_id=%s",
                        (native.workspace_id, native.work_id))
            all_usage = [dict(row) for row in cur.fetchall()]
        # Retain costs for every referenced native attempt, including failed
        # or unqualified arms which cannot contribute a comparison score.
        trials = {trial_id for cohort in cohorts for step in cohort.steps
                  for trial_id in (step.baseline_trial_id, step.candidate_trial_id) if trial_id is not None}
        jobs = {managed_job_id(trial_id) for trial_id in trials}
        requests = tuple(context.request_id for context in contexts) + (raw["controller"]["execution_request_id"],)
        usage = [row for row in all_usage if str(row["job_id_derived"]) in jobs
                 or any(str(row["request_id"]).startswith(f"{request}:") for request in requests)]
        comparisons = []
        for execution in executions:
            if execution.phase != "confirmation" or not execution.baseline.selected_bound_step or not execution.candidate.selected_bound_step:
                continue
            # Use native receipt provenance and native observation time. Never
            # synthesize a successful receipt or infer costs from resource caps.
            with native.store.transaction(native.workspace_id, native.actor_id) as cur:
                cur.execute("SELECT observed_at FROM omp_research.observations WHERE workspace_id=%s AND trial_id=%s ORDER BY observed_at DESC LIMIT 1",
                            (native.workspace_id, execution.candidate.trial_id))
                observed_at = cur.fetchone()["observed_at"]
            links = []
            for arm in (execution.baseline, execution.candidate):
                ids = tuple(str(row["usage_id"]) for row in usage if str(row["job_id_derived"]) == arm.job_id)
                links.append(ArmLink(arm.source_sha256, arm.bundle_sha256, arm.receipt_sha256, ids))
            comparisons.append(ConfirmationComparison(
                execution.domain, execution.task_id, studies[1].evaluator_artifact_sha256,
                observed_at, baseline=links[0], candidate=links[1], result=execution,
                content_sha256=execution.content_sha256,
            ))
            if not execution.ok:
                continue
            fresh.append(FreshResult(
                observed_at=observed_at, release_id=results[1].artifact_sha256,
                artifact_sha256=results[1].artifact_sha256, task_id=execution.task_id,
                content_sha256=execution.content_sha256, protocol_sha256=execution.protocol_sha256,
                evaluator_component_sha256=studies[1].evaluator_component_sha256,
                evaluator_artifact_sha256=studies[1].evaluator_artifact_sha256,
                evaluator_version=studies[1].evaluator_artifact_sha256,
                primary=execution.candidate.selected_bound_step.metric,
            ))
        # Native usage rows have independently recorded times. The bound
        # comparison carries the observation time; omit usage timestamps here
        # rather than relabeling them as simultaneous trial observations.
        referenced = {usage_id for comparison in comparisons
                      for link in (comparison.baseline, comparison.candidate) for usage_id in link.usage_ids}
        events = [{**{key: value for key, value in row.items() if key != "recorded_at"},
                   "job_id": row["job_id_derived"],
                   # Native storage has a numeric aggregate even for unknown
                   # token counts; that aggregate is not a measured zero.
                   "tokens": None if row["measurement"] == "unknown" else row["tokens"]}
                  for row in usage if str(row["usage_id"]) in referenced]
        ownership = OwnershipCosts(**{key: CostAxes(**value) for key, value in raw["ownership_costs"].items()})
        benefit = summarize_benefit(comparisons, events, ownership, window)
        release = MaintenanceRelease.model_validate(raw["release"])
        if release.bundle_sha256 != results[1].artifact_sha256 or release.release_id != results[1].artifact_sha256:
            raise ValueError("release_generated_source_mismatch")
        if (policy.rollback_target.bundle_sha256 != results[0].artifact_sha256
                or policy.rollback_target.method_sha256 != results[0].source_sha256):
            raise ValueError("stable_replacement_not_bound_to_baseline")
        plan = maintenance_plan(
            studies[1].model_copy(update={"method_version": results[1].source_sha256}),
            release, fresh, benefit, raw["upstream_check"], policy,
        )
        coordinator.queue.enqueue(plan.question)
    evidence = {
        "evidence_class": "fixture_source_proof_only", "installed_qualified": False,
        "used_method_sha256": selectable[selected], "chosen_arm": ("baseline", "candidate")[selected],
        "methods": {arm: {"study": study.model_dump(mode="json"), "source_sha256": result.source_sha256,
                           "artifact_sha256": result.artifact_sha256, "failures": result.failures,
                           "denominator": result.denominator, "state": result.state.model_dump(mode="json")}
                    for arm, study, result in zip(("baseline", "candidate"), studies, results)},
        "comparison": {"status": report.status, "denominator": report.denominator,
                       "keep_both": report.keep_both, "failures": report.failures,
                       "transfer_domains": [domain.domain for domain in report.transfer]} if report else None,
        "transfer": [asdict(execution) for execution in executions],
        "costs": asdict(benefit) if benefit else None, "native_usage": usage,
        "maintenance": asdict(plan) if plan else None, "history": history.snapshot(),
        "eligible": bool(selected == 1 and report and not report.keep_both and report.transfer
                         and benefit and benefit.adoption_eligible and plan and plan.qualification_valid),
    }
    # asdict includes datetimes, Decimals and nested Pydantic state. Replace the
    # latter explicitly; canonical JSON owns all remaining primitive encoding.
    for item, execution in zip(evidence["transfer"], executions):
        for arm in ("baseline", "candidate"):
            item[arm]["state"] = getattr(execution, arm).state.model_dump(mode="json")
            bound = getattr(execution, arm).selected_bound_step
            if bound:
                item[arm]["selected_bound_step"]["evidence"]["receipt"] = bound.evidence.receipt.model_dump(mode="json")
                item[arm]["selected_bound_step"]["evidence"]["protocol"] = bound.evidence.protocol.model_dump(mode="json")
                item[arm]["selected_bound_step"]["observation"]["expected"] = bound.observation.expected.model_dump(mode="json")
    evidence = json.loads(canonical_json(evidence))
    coordinator.set_evidence(evidence)
    coordinator.conclude_campaign(raw["run_id"])
    coordinator.save_snapshot(directory)
    sources["program.json"] = (directory / "program.json").read_bytes()
    record = {
        "objective": "M5 recursive method study", "scope": "fixture source proof only; bounded local run",
        "decision": evidence, "questions": [asdict(q) for q in coordinator.queue.drain()],
        "sources": [{"id": key, "sha256": hashlib.sha256(value).hexdigest(), "coverage": "source proof only"}
                    for key, value in sources.items()],
        "experiments": [{"name": f"{row.domain}:{row.task_id}", "metrics": {"delta": row.delta} if row.delta is not None else {},
                         "receipt_sha256": row.candidate.receipt_sha256 or ""} for row in executions],
    }
    packet = write_dossier(directory, json.loads(canonical_json(record)), source_bytes=sources)
    digest = sha256(packet)
    load_packet(directory, digest)
    # Rerender from verified packet, then reload both packet and program.
    write_dossier(directory, packet)
    load_packet(directory, digest)
    restored = load_snapshot(directory)
    if restored.evidence != evidence:
        raise ValueError("program_reload_mismatch")
    return {"packet_sha256": digest, "used_method_sha256": selectable[selected], "eligible": evidence["eligible"]}


def run_study(input_path: str | Path, output_dir: str | Path, chosen_method_sha256: str | None = None) -> dict:
    """Compose s01–s05 against caller-bound local controller and native evidence.

    A drift rerun is another complete input file, with a different signed
    decision/window. Absent that explicit input, the QuestionItem stays pending.
    Neither a chosen digest nor a packet activates an installed default.
    """
    raw = json.loads(Path(input_path).read_text())
    history = ConsumptionHistory.restore(raw["controller"].get("history", {}))
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    result = _run(raw, directory, chosen_method_sha256, history)
    packet = load_packet(directory, result["packet_sha256"])
    plan = packet["decision"]["maintenance"]
    reevaluation = raw.get("reevaluation_input_path")
    if plan and plan["proposal"] == "reevaluate" and reevaluation:
        newer = json.loads(Path(reevaluation).read_text())
        old_grant = CallerGrant.model_validate(raw["controller"]["candidate"]["grant"])
        grant = CallerGrant.model_validate(newer["controller"]["candidate"]["grant"])
        if grant.decision_id == old_grant.decision_id or (grant.window_start, grant.window_end) == (old_grant.window_start, old_grant.window_end):
            result["reevaluation_pending"] = "separate_reevaluation_authorization_required"
            return result
        if not grant.window_start <= datetime.now(timezone.utc) < grant.window_end:
            result["reevaluation_pending"] = "outside_authorized_reevaluation_window"
            return result
        # Shared consumption history prevents relabeling old holdouts as fresh.
        result["reevaluation"] = _run(newer, directory / "reevaluation", chosen_method_sha256, history)
        packet["reevaluation"] = result["reevaluation"]
        packet = write_dossier(directory, packet)
        result["packet_sha256"] = sha256(packet)
        load_packet(directory, result["packet_sha256"])
    return result
