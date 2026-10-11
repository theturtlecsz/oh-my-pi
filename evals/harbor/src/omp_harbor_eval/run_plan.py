"""Harbor run plan: arms, price estimate and budget gate."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from decimal import Decimal, ROUND_CEILING
import json
from pathlib import Path
import re
import sys
from typing import Any, NamedTuple

from omp_work.action_tiers import tier_of
from omp_work.v1.models import ItemBudget

from .corpus import REPO_ROOT, verify as corpus_verify
from .evidence import canonical_json
from .fixtures import Terminal, parse_terminal

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ALLOWED_TASKS = frozenset({"all", "long_horizon"})


class ModelPrice(NamedTuple):
    input: Decimal
    output: Decimal
    cache_read: Decimal
    exact: bool


def _load_catalog(catalog: Mapping[str, Any] | str | Path | None) -> Mapping[str, Any]:
    if catalog is None:
        cat_path = REPO_ROOT / "packages/catalog/src/models.json"
        if not cat_path.is_file():
            raise ValueError(f"default catalog not found at {cat_path}")
        return json.loads(cat_path.read_text(encoding="utf-8"))
    if isinstance(catalog, (str, Path)):
        cat_file = Path(catalog)
        if not cat_file.is_file():
            raise ValueError(f"catalog file not found: {cat_file}")
        return json.loads(cat_file.read_text(encoding="utf-8"))
    if isinstance(catalog, Mapping):
        return catalog
    raise ValueError(f"invalid catalog type: {type(catalog).__name__}")


def _lookup_model(catalog: Mapping[str, Any], provider: str, model_id: str) -> dict[str, Any]:
    prov_dict = catalog.get(provider)
    if isinstance(prov_dict, Mapping) and model_id in prov_dict:
        entry = prov_dict[model_id]
        if isinstance(entry, Mapping):
            return dict(entry)
    combo_key = f"{provider}/{model_id}"
    if combo_key in catalog and isinstance(catalog[combo_key], Mapping):
        return dict(catalog[combo_key])
    if model_id in catalog and isinstance(catalog[model_id], Mapping):
        return dict(catalog[model_id])
    raise ValueError(f"model not found in catalog: {provider}/{model_id}")


def model_price(
    catalog: Mapping[str, Any] | str | Path | None,
    provider: str,
    model_id: str,
) -> ModelPrice:
    """Resolve upper-bound token price and exact flag for a model in catalog."""
    cat = _load_catalog(catalog)
    model_data = _lookup_model(cat, provider, model_id)

    base_cost = model_data.get("cost")
    if not isinstance(base_cost, Mapping):
        raise ValueError(f"model {provider}/{model_id} has no cost object")

    raw_base_inp = base_cost.get("input")
    raw_base_out = base_cost.get("output")
    if raw_base_inp is None or not isinstance(raw_base_inp, (int, float, Decimal)) or raw_base_inp <= 0:
        raise ValueError(f"model {provider}/{model_id} base input price must be positive: {raw_base_inp}")
    if raw_base_out is None or not isinstance(raw_base_out, (int, float, Decimal)) or raw_base_out <= 0:
        raise ValueError(f"model {provider}/{model_id} base output price must be positive: {raw_base_out}")

    time_based = base_cost.get("timeBased") or model_data.get("timeBased")
    has_time_based = isinstance(time_based, Mapping) and bool(time_based)

    multiplier = Decimal(1)
    if has_time_based:
        off_peak_val = time_based.get("offPeakMultiplier", 1)
        try:
            off_peak_dec = Decimal(str(off_peak_val))
        except Exception:
            off_peak_dec = Decimal(1)
        multiplier = max(Decimal(1), off_peak_dec)

    base_lc = base_cost.get("longContext") or model_data.get("longContext")
    has_long_context = isinstance(base_lc, Mapping) and bool(base_lc)

    cards: list[Mapping[str, Any]] = [base_cost]
    if has_long_context:
        cards.append(base_lc)

    if has_time_based:
        rates = time_based.get("effectiveRates")
        if isinstance(rates, Sequence):
            for rate in rates:
                if isinstance(rate, Mapping):
                    cards.append(rate)
                    rate_lc = rate.get("longContext")
                    if isinstance(rate_lc, Mapping) and bool(rate_lc):
                        has_long_context = True
                        cards.append(rate_lc)

    exact = (
        (not has_long_context)
        and (not has_time_based)
        and all("cacheRead" in c and c["cacheRead"] is not None for c in cards)
    )

    card_rates: list[tuple[Decimal, Decimal, Decimal]] = []
    for c in cards:
        inp = Decimal(str(c["input"])) * multiplier
        out = Decimal(str(c["output"])) * multiplier
        if "cacheRead" in c and c["cacheRead"] is not None:
            cr = Decimal(str(c["cacheRead"])) * multiplier
        else:
            cr = inp
        card_rates.append((inp, out, cr))

    max_inp = max(r[0] for r in card_rates)
    max_out = max(r[1] for r in card_rates)
    max_cr = max(r[2] for r in card_rates)

    return ModelPrice(input=max_inp, output=max_out, cache_read=max_cr, exact=exact)


def usd(
    p: ModelPrice | tuple[Decimal, ...],
    inp: int | float | Decimal,
    cached: int | float | Decimal,
    out: int | float | Decimal,
) -> Decimal:
    """Calculate trial USD cost per 1M tokens."""
    d_inp = Decimal(str(inp))
    d_cached = Decimal(str(cached))
    d_out = Decimal(str(out))
    p_inp = p.input if hasattr(p, "input") else Decimal(str(p[0]))
    p_out = p.output if hasattr(p, "output") else Decimal(str(p[1]))
    p_cr = p.cache_read if hasattr(p, "cache_read") else Decimal(str(p[2]))

    num = (d_inp - d_cached) * p_inp + d_cached * p_cr + d_out * p_out
    return num / Decimal(1_000_000)


class Arm:
    """Evaluation arm configuration."""

    def __init__(
        self,
        id: str,
        command: str,
        terminal: Terminal | Mapping[str, Any],
        *,
        status: str = "available",
        reason: str | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(id, str) or _ID_RE.fullmatch(id) is None:
            raise ValueError(f"unsafe arm id: {id!r}")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"arm {id}: command must be a non-empty string")

        placeholders = re.findall(r"\{([^}]+)\}", command)
        for ph in placeholders:
            if ph not in ("instruction", "key"):
                raise ValueError(f"arm {id}: invalid placeholder '{{{ph}}}', only {{instruction}} or {{key}} permitted")

        if isinstance(terminal, Terminal):
            parsed_terminal = terminal
        else:
            parsed_terminal = parse_terminal(terminal, label=f"arm {id}.terminal")

        if "/execute " in command:
            if parsed_terminal.agent_end or not parsed_terminal.pointer:
                raise ValueError(f"arm {id}: command with '/execute ' requires a pointer terminal")
        else:
            if not parsed_terminal.agent_end:
                raise ValueError(f"arm {id}: plain command requires agent_end terminal")

        if status not in ("available", "unavailable"):
            raise ValueError(f"arm {id}: status must be 'available' or 'unavailable', got {status!r}")

        if status == "unavailable":
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"arm {id}: unavailable arm requires a reason")
        else:
            if reason is not None and not isinstance(reason, str):
                raise ValueError(f"arm {id}: reason must be a string if provided")

        self.id = id
        self.command = command
        self.terminal = parsed_terminal
        self.status = status
        self.reason = reason if status == "unavailable" else (reason or None)
        self.settings = dict(settings) if settings is not None else {}

    def signature(self) -> str:
        """Deterministic string signature for arm equality comparison."""
        body = {
            "command": self.command,
            "agent_end": self.terminal.agent_end,
            "pointer": self.terminal.pointer,
            "accepted": list(self.terminal.accepted),
            "settings": self.settings,
        }
        return canonical_json(body).decode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        term_dict: dict[str, Any]
        if self.terminal.agent_end:
            term_dict = {"agent_end": True}
        else:
            term_dict = {"pointer": self.terminal.pointer, "in": list(self.terminal.accepted)}
        result: dict[str, Any] = {
            "id": self.id,
            "command": self.command,
            "terminal": term_dict,
            "status": self.status,
            "settings": self.settings,
        }
        if self.reason is not None:
            result["reason"] = self.reason
        return result


class ExperimentModel:
    """Model specification within an experiment."""

    def __init__(
        self,
        provider: str,
        model_id: str = "",
        thinking: Any = None,
        **kwargs: Any,
    ) -> None:
        resolved_id = model_id or kwargs.get("id", "")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("model provider must be a non-empty string")
        if not isinstance(resolved_id, str) or not resolved_id.strip():
            raise ValueError("model id must be a non-empty string")
        self.provider = provider
        self.id = resolved_id
        self.thinking = thinking

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {"provider": self.provider, "id": self.id}
        if self.thinking is not None:
            res["thinking"] = self.thinking
        return res


class Experiment:
    """Experiment specification grouping arms, models, and task scope."""

    def __init__(
        self,
        id: str,
        arms: Sequence[str | Arm],
        models: Sequence[ExperimentModel | Mapping[str, Any]],
        tasks: str = "all",
        repetitions: int = 1,
    ) -> None:
        if not isinstance(id, str) or _ID_RE.fullmatch(id) is None:
            raise ValueError(f"unsafe experiment id: {id!r}")
        if not arms:
            raise ValueError(f"experiment {id}: arms must not be empty")

        arm_ids: list[str] = []
        for arm in arms:
            if isinstance(arm, Arm):
                arm_ids.append(arm.id)
            elif isinstance(arm, str) and arm.strip():
                arm_ids.append(arm.strip())
            else:
                raise ValueError(f"experiment {id}: invalid arm reference {arm!r}")

        if not models:
            raise ValueError(f"experiment {id}: models must not be empty")

        parsed_models: list[ExperimentModel] = []
        for m in models:
            if isinstance(m, ExperimentModel):
                parsed_models.append(m)
            elif isinstance(m, Mapping):
                parsed_models.append(
                    ExperimentModel(
                        provider=m.get("provider", ""),
                        model_id=m.get("id", ""),
                        thinking=m.get("thinking"),
                    )
                )
            else:
                raise ValueError(f"experiment {id}: invalid model item {m!r}")

        if tasks not in _ALLOWED_TASKS:
            raise ValueError(f"experiment {id}: tasks must be 'all' or 'long_horizon', got {tasks!r}")

        if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions < 1:
            raise ValueError(f"experiment {id}: repetitions must be an integer >= 1, got {repetitions!r}")

        self.id = id
        self.arms = arm_ids
        self.models = parsed_models
        self.tasks = tasks
        self.repetitions = repetitions

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "arms": list(self.arms),
            "models": [m.to_dict() for m in self.models],
            "tasks": self.tasks,
            "repetitions": self.repetitions,
        }


class Plan:
    """Harbor evaluation plan."""

    def __init__(
        self,
        plan_id: str,
        corpus_manifest: str,
        corpus_sha256: str,
        max_input_tokens: int,
        max_output_tokens: int,
        trial_timeout_s: int | float,
        arms: Sequence[Arm],
        experiments: Sequence[Experiment],
        manifest: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(plan_id, str) or _ID_RE.fullmatch(plan_id) is None:
            raise ValueError(f"unsafe plan_id: {plan_id!r}")
        if not isinstance(corpus_manifest, str) or not corpus_manifest.strip():
            raise ValueError("corpus_manifest must be a non-empty string")
        if not isinstance(corpus_sha256, str) or not re.fullmatch(r"^[0-9a-f]{64}$", corpus_sha256):
            raise ValueError("corpus_sha256 must be a 64-hex SHA-256 string")
        if isinstance(max_input_tokens, bool) or not isinstance(max_input_tokens, int) or max_input_tokens <= 0:
            raise ValueError("max_input_tokens must be a positive integer")
        if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        if isinstance(trial_timeout_s, bool) or not isinstance(trial_timeout_s, (int, float)) or trial_timeout_s <= 0:
            raise ValueError("trial_timeout_s must be a positive number")

        if not arms:
            raise ValueError("plan arms must not be empty")

        seen_arm_ids: set[str] = set()
        seen_arm_signatures: dict[str, str] = {}
        arm_by_id: dict[str, Arm] = {}
        for a in arms:
            if not isinstance(a, Arm):
                raise ValueError(f"arms items must be Arm instances, got {type(a).__name__}")
            if a.id in seen_arm_ids:
                raise ValueError(f"duplicate arm id: {a.id}")
            seen_arm_ids.add(a.id)

            sig = a.signature()
            if sig in seen_arm_signatures:
                prior = seen_arm_signatures[sig]
                raise ValueError(f"arms must differ: arm {a.id!r} is identical to {prior!r}")
            seen_arm_signatures[sig] = a.id
            arm_by_id[a.id] = a

        if not experiments:
            raise ValueError("plan experiments must not be empty")

        seen_exp_ids: set[str] = set()
        for exp in experiments:
            if not isinstance(exp, Experiment):
                raise ValueError(f"experiments items must be Experiment instances, got {type(exp).__name__}")
            if exp.id in seen_exp_ids:
                raise ValueError(f"duplicate experiment id: {exp.id}")
            seen_exp_ids.add(exp.id)

            for arm_ref in exp.arms:
                if arm_ref not in arm_by_id:
                    raise ValueError(f"experiment {exp.id} references unknown arm: {arm_ref}")

        self.plan_id = plan_id
        self.corpus_manifest = corpus_manifest
        self.corpus_sha256 = corpus_sha256
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.trial_timeout_s = trial_timeout_s
        self.arms = list(arms)
        self.arms_by_id = arm_by_id
        self.experiments = list(experiments)
        self.manifest = dict(manifest) if manifest is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "corpus_manifest": self.corpus_manifest,
            "corpus_sha256": self.corpus_sha256,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "trial_timeout_s": self.trial_timeout_s,
            "arms": [a.to_dict() for a in self.arms],
            "experiments": [e.to_dict() for e in self.experiments],
        }


def _extract_requested_effort(thinking: Any) -> str | None:
    if thinking is None:
        return None
    if isinstance(thinking, str):
        return thinking
    if isinstance(thinking, Mapping):
        effort = thinking.get("effort")
        if effort is not None:
            return str(effort)
        default_lvl = thinking.get("defaultLevel")
        if default_lvl is not None:
            return str(default_lvl)
    return None


def _get_catalog_efforts(catalog: Mapping[str, Any], provider: str, model_id: str) -> list[str]:
    try:
        model_data = _lookup_model(catalog, provider, model_id)
    except Exception:
        return []
    thinking = model_data.get("thinking")
    if isinstance(thinking, Mapping):
        efforts = thinking.get("efforts")
        if isinstance(efforts, Sequence):
            return [str(e) for e in efforts]
    return []


class ScheduledTrial:
    """One scheduled trial row."""

    def __init__(
        self,
        experiment_id: str,
        task_id: str,
        arm: Arm,
        model: ExperimentModel,
        repetition: int,
        status: str,
        reason: str | None = None,
    ) -> None:
        self.experiment_id = experiment_id
        self.task_id = task_id
        self.arm = arm
        self.arm_id = arm.id
        self.model = model
        self.repetition = repetition
        self.status = status
        self.reason = reason

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "experiment_id": self.experiment_id,
            "task_id": self.task_id,
            "arm_id": self.arm_id,
            "model": self.model.to_dict(),
            "repetition": self.repetition,
            "status": self.status,
        }
        if self.reason is not None:
            res["reason"] = self.reason
        return res


def schedule(plan: Plan, catalog: Mapping[str, Any] | str | Path | None = None) -> list[ScheduledTrial]:
    """Generate trials by experiment x task x arm x model x repetition."""
    cat = _load_catalog(catalog)

    if plan.manifest is None:
        raise ValueError("plan has no loaded corpus manifest")
    manifest_tasks = plan.manifest.get("tasks", {})
    if not isinstance(manifest_tasks, Mapping):
        raise ValueError("corpus manifest tasks must be a dictionary")

    # Validate all models across all experiments are priced in catalog
    for exp in plan.experiments:
        for m in exp.models:
            model_price(cat, m.provider, m.id)

    trials: list[ScheduledTrial] = []

    for exp in plan.experiments:
        exp_arms = [plan.arms_by_id[arm_id] for arm_id in exp.arms]
        task_entries: list[tuple[str, Mapping[str, Any]]] = []
        for tid, tdata in sorted(manifest_tasks.items()):
            if isinstance(tdata, Mapping):
                if exp.tasks == "long_horizon":
                    if tdata.get("long_horizon") is True:
                        task_entries.append((tid, tdata))
                else:
                    task_entries.append((tid, tdata))

        exp_available_count = 0

        for tid, _ in task_entries:
            for arm in exp_arms:
                for model in exp.models:
                    req_effort = _extract_requested_effort(model.thinking)
                    cat_efforts = _get_catalog_efforts(cat, model.provider, model.id)

                    effort_unlisted = req_effort is not None and req_effort not in cat_efforts

                    if arm.status == "unavailable":
                        row_status = "unavailable"
                        row_reason = arm.reason
                    elif effort_unlisted:
                        row_status = "unavailable"
                        row_reason = f"effort {req_effort} not listed"
                    else:
                        row_status = "available"
                        row_reason = None

                    for rep in range(1, exp.repetitions + 1):
                        trial = ScheduledTrial(
                            experiment_id=exp.id,
                            task_id=tid,
                            arm=arm,
                            model=model,
                            repetition=rep,
                            status=row_status,
                            reason=row_reason,
                        )
                        trials.append(trial)
                        if row_status == "available":
                            exp_available_count += 1

        if exp_available_count == 0:
            raise ValueError(
                f"experiment {exp.id}: zero available trials (tasks={exp.tasks}, tasks_matched={len(task_entries)})"
            )

    return trials


class Estimate:
    """Price and token estimate for an evaluation plan."""

    def __init__(
        self,
        plan_id: str,
        usd_upper_bound: Decimal,
        tokens: int,
        exact: bool,
        trial_counts: dict[str, dict[str, int]],
        trials: list[ScheduledTrial],
    ) -> None:
        self.plan_id = plan_id
        self.usd_upper_bound = usd_upper_bound
        self.tokens = tokens
        self.exact = exact
        self.trial_counts = trial_counts
        self.trials = trials

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "usd_upper_bound": str(self.usd_upper_bound),
            "tokens": self.tokens,
            "exact": self.exact,
            "trial_counts": self.trial_counts,
        }


def estimate(plan: Plan, catalog: Mapping[str, Any] | str | Path | None = None) -> Estimate:
    """Compute upper-bound USD and tokens for available trials."""
    cat = _load_catalog(catalog)
    trials = schedule(plan, cat)

    total_usd = Decimal(0)
    total_tokens = 0
    all_exact = True
    trial_counts: dict[str, dict[str, int]] = {}

    for trial in trials:
        exp_map = trial_counts.setdefault(trial.experiment_id, {"available": 0, "unavailable": 0})
        exp_map[trial.status] += 1

        if trial.status == "available":
            p = model_price(cat, trial.model.provider, trial.model.id)
            if not p.exact:
                all_exact = False
            trial_cost = usd(p, plan.max_input_tokens, 0, plan.max_output_tokens)
            total_usd += trial_cost
            total_tokens += plan.max_input_tokens + plan.max_output_tokens

    usd_upper_bound = total_usd.quantize(Decimal("0.01"), rounding=ROUND_CEILING)

    return Estimate(
        plan_id=plan.plan_id,
        usd_upper_bound=usd_upper_bound,
        tokens=total_tokens,
        exact=all_exact,
        trial_counts=trial_counts,
        trials=trials,
    )


class GateResult:
    """Result of budget gate evaluation."""

    def __init__(
        self,
        status: str,
        tier: int,
        reason: str | None = None,
        budget: ItemBudget | None = None,
        estimate: Estimate | None = None,
    ) -> None:
        self.status = status
        self.decision = status
        self.outcome = status
        self.tier = tier
        self.reason = reason
        self.budget = budget
        self.estimate = estimate

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.status == other
        if isinstance(other, GateResult):
            return self.status == other.status and self.tier == other.tier
        return False

    def __str__(self) -> str:
        return self.status

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "status": self.status,
            "tier": self.tier,
        }
        if self.reason is not None:
            res["reason"] = self.reason
        return res


def gate(estimate: Estimate, budget: Any) -> GateResult:
    """Evaluate plan estimate against ItemBudget."""
    if budget is None:
        return GateResult(
            status="refused/no_budget",
            tier=tier_of("no_budget"),
            reason="budget is None",
            estimate=estimate,
        )

    parsed_budget: ItemBudget | None = None
    if isinstance(budget, ItemBudget):
        parsed_budget = budget
    elif isinstance(budget, Mapping):
        try:
            parsed_budget = ItemBudget.model_validate(budget)
        except Exception as exc:
            return GateResult(
                status="refused/no_budget",
                tier=tier_of("no_budget"),
                reason=f"invalid budget mapping: {exc}",
                estimate=estimate,
            )
    elif isinstance(budget, (str, Path)):
        p = Path(budget)
        try:
            if p.is_file():
                raw_text = p.read_text(encoding="utf-8")
            else:
                raw_text = str(budget)
            parsed_data = json.loads(raw_text)
            if not isinstance(parsed_data, Mapping):
                return GateResult(
                    status="refused/no_budget",
                    tier=tier_of("no_budget"),
                    reason="budget JSON is not an object",
                    estimate=estimate,
                )
            parsed_budget = ItemBudget.model_validate(parsed_data)
        except Exception as exc:
            return GateResult(
                status="refused/no_budget",
                tier=tier_of("no_budget"),
                reason=f"cannot parse budget JSON: {exc}",
                estimate=estimate,
            )
    else:
        return GateResult(
            status="refused/no_budget",
            tier=tier_of("no_budget"),
            reason=f"unsupported budget type: {type(budget).__name__}",
            estimate=estimate,
        )

    budget_usd = Decimal(parsed_budget.usd)
    if estimate.usd_upper_bound > budget_usd or estimate.tokens > parsed_budget.tokens:
        action = "spend_beyond_threshold"
        return GateResult(
            status="refused/spend_beyond_threshold",
            tier=tier_of(action),
            reason=(
                f"spend exceeds budget: usd {estimate.usd_upper_bound} > {budget_usd} "
                f"or tokens {estimate.tokens} > {parsed_budget.tokens}"
            ),
            budget=parsed_budget,
            estimate=estimate,
        )

    return GateResult(
        status="within_budget",
        tier=1,
        reason="estimate within budget limits",
        budget=parsed_budget,
        estimate=estimate,
    )


def load_plan(
    plan: str | Path | Mapping[str, Any],
    catalog: Mapping[str, Any] | str | Path | None = None,
) -> Plan:
    """Load plan document, verify corpus manifest and SHA-256, and validate schedule."""
    base_dir: Path | None = None
    if isinstance(plan, (str, Path)):
        plan_path = Path(plan)
        base_dir = plan_path.parent
        if not plan_path.is_file():
            raise ValueError(f"plan file not found: {plan_path}")
        try:
            doc = json.loads(plan_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"cannot parse plan file: {exc}") from exc
    elif isinstance(plan, Mapping):
        doc = dict(plan)
    else:
        raise ValueError(f"plan must be a path or mapping, got {type(plan).__name__}")

    for req in (
        "plan_id",
        "corpus_manifest",
        "corpus_sha256",
        "max_input_tokens",
        "max_output_tokens",
        "trial_timeout_s",
        "arms",
        "experiments",
    ):
        if req not in doc:
            raise ValueError(f"plan missing required key: {req}")

    manifest_ref = doc["corpus_manifest"]
    manifest_path = Path(manifest_ref)
    if not manifest_path.is_absolute():
        candidates = []
        if base_dir is not None:
            candidates.append(base_dir / manifest_ref)
        candidates.extend([REPO_ROOT / manifest_ref, Path.cwd() / manifest_ref])
        for cand in candidates:
            if cand.is_file():
                manifest_path = cand.resolve()
                break

    if not manifest_path.is_file():
        raise ValueError(f"corpus manifest file missing: {manifest_ref}")

    try:
        manifest_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"cannot read corpus manifest {manifest_path.name}: {exc}") from exc

    expected_sha = doc["corpus_sha256"]
    manifest_sha = manifest_doc.get("corpus_sha256")
    if expected_sha != manifest_sha:
        raise ValueError(f"corpus_sha256 mismatch: plan={expected_sha}, manifest={manifest_sha}")

    drift = corpus_verify(manifest_path)
    if drift:
        raise ValueError(f"corpus manifest drift: {drift}")

    arms_raw = doc["arms"]
    if not isinstance(arms_raw, Sequence) or not arms_raw:
        raise ValueError("plan arms must be a non-empty list")
    arms: list[Arm] = []
    for item in arms_raw:
        if isinstance(item, Arm):
            arms.append(item)
        elif isinstance(item, Mapping):
            arms.append(
                Arm(
                    id=item.get("id", ""),
                    command=item.get("command", ""),
                    terminal=item.get("terminal", {}),
                    status=item.get("status", "available"),
                    reason=item.get("reason"),
                    settings=item.get("settings"),
                )
            )
        else:
            raise ValueError(f"invalid arm item: {item!r}")

    exps_raw = doc["experiments"]
    if not isinstance(exps_raw, Sequence) or not exps_raw:
        raise ValueError("plan experiments must be a non-empty list")
    experiments: list[Experiment] = []
    for item in exps_raw:
        if isinstance(item, Experiment):
            experiments.append(item)
        elif isinstance(item, Mapping):
            experiments.append(
                Experiment(
                    id=item.get("id", ""),
                    arms=item.get("arms", []),
                    models=item.get("models", []),
                    tasks=item.get("tasks", "all"),
                    repetitions=item.get("repetitions", 1),
                )
            )
        else:
            raise ValueError(f"invalid experiment item: {item!r}")

    plan_obj = Plan(
        plan_id=doc["plan_id"],
        corpus_manifest=str(manifest_path),
        corpus_sha256=expected_sha,
        max_input_tokens=doc["max_input_tokens"],
        max_output_tokens=doc["max_output_tokens"],
        trial_timeout_s=doc["trial_timeout_s"],
        arms=arms,
        experiments=experiments,
        manifest=manifest_doc,
    )

    # Validate schedule to detect zero-trial experiments or unpriced models
    schedule(plan_obj, catalog)

    return plan_obj


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omp_harbor_eval.run_plan")
    subparsers = parser.add_subparsers(dest="command", required=True)

    estimate_parser = subparsers.add_parser("estimate")
    estimate_parser.add_argument("--plan", required=True, help="Path to plan JSON")
    estimate_parser.add_argument("--catalog", default=None, help="Path to models.json")

    gate_parser = subparsers.add_parser("gate")
    gate_parser.add_argument("--plan", required=True, help="Path to plan JSON")
    gate_parser.add_argument("--budget", required=True, help="Path to budget JSON or JSON string")
    gate_parser.add_argument("--catalog", default=None, help="Path to models.json")

    args = parser.parse_args(argv)

    if args.command == "estimate":
        try:
            p = load_plan(args.plan, catalog=args.catalog)
            est = estimate(p, catalog=args.catalog)
            print(json.dumps(est.to_dict(), indent=2))
            return 0
        except Exception as exc:
            sys.stderr.write(f"refused: {exc}\n")
            return 1

    if args.command == "gate":
        try:
            p = load_plan(args.plan, catalog=args.catalog)
            est = estimate(p, catalog=args.catalog)
            res = gate(est, args.budget)
            print(json.dumps(res.to_dict(), indent=2))
            if res.status == "within_budget":
                return 0
            return 1
        except Exception as exc:
            sys.stderr.write(f"refused: {exc}\n")
            return 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
