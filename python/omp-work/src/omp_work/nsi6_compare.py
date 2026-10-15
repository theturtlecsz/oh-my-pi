"""Model-backed NSI-6 comparison.

The intake runner is the only model boundary, and it receives ``evaluator_input``
alone. Original source hashes are checked before that call. Proof is evaluated
from the original scenario, then projected with ``nsi6_observation`` before the
unchanged ``nsi_eval.score``. Saved completions can be replayed without a model.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable

from pydantic import ValidationError

from omp_work.frozen_corpus import verify
from omp_work.nsi6_corpus import evaluator_input, load_corpus
from omp_work.nsi6_observation import project_observation
from omp_work.nsi_eval import (
    aggregate, evaluate_current, evaluate_typed, observe, report_pins, score, semantic_edits,
)
from omp_work.nsi_eval_postgres import disposable_provenance
from omp_work.nsi_route import corpus_binding
from omp_work.v1.canonical import sha256, text_sha256
from omp_work.v1.models import BoundedIntakeDraft

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_MANIFEST = ROOT / "evals/nsi6/v1/nsi6-manifest.json"
RUNNER = ROOT / "session-system/extensions/workflow/intake-runner.ts"
ARMS = ("current", "typed")
EVALUATORS = {"current": evaluate_current, "typed": evaluate_typed}
DRAFTING_SOURCES = (
    "session-system/extensions/workflow/intake-runner.ts",
    "session-system/extensions/workflow/nsi6-draft.md",
    "session-system/extensions/workflow/nsi6-repair.md",
    "session-system/extensions/workflow/nsi6-source.md",
    "session-system/skills/intake/SKILL.md",
    "session-system/skills/intake/references/typed-completeness.md",
    "python/omp-work/src/omp_work/nsi6_compare.py",
    "python/omp-work/src/omp_work/nsi6_observation.py",
)
_FENCE = re.compile(r"^```(?:json)?\s*\n([\s\S]*?)\n```$")
_FINDING_KINDS = {"contradiction", "missing_oracle", "unresolved_authority", "ambiguity", "other"}
_INCOMPLETE_RUNNER = {"failed", "cap_denied", "unmeasured"}
_REQUEST_KEYS = {"model", "max_usd", "cases"}
_CASE_KEYS = {"id", "arm", "input"}
_INPUT_KEYS = {"bounded", "nsi2_proof_scenario"}


def _require_budget(model: object, max_usd: object) -> None:
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model is required")
    if isinstance(max_usd, bool) or not isinstance(max_usd, (int, float)) or not math.isfinite(float(max_usd)) or float(max_usd) <= 0:
        raise ValueError("max-usd must be a finite positive number")


def _money_equal(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=0, abs_tol=1e-9)


def _over_cap(spent: float, cap: float) -> bool:
    return spent > cap and not _money_equal(spent, cap)


def _display(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return str(resolved)


def _refuse_overwrite(out: Path, manifest: Path, rollback_proof: Path | None) -> None:
    resolved = out.resolve()
    protected = [manifest.resolve(), ROOT / "evals/nsi6", ROOT / "docs/nsi6"]
    if rollback_proof is not None:
        protected.append(rollback_proof.resolve())
    for frozen in protected:
        if resolved == frozen or (frozen.is_dir() and resolved.is_relative_to(frozen)):
            raise ValueError("report cannot overwrite frozen corpus")


def runner_case_id(case_id: str, arm: str) -> str:
    return f"{case_id}/{arm}"


def split_runner_id(runner_id: str) -> tuple[str, str]:
    case_id, separator, arm = runner_id.rpartition("/")
    if not separator or arm not in ARMS or not case_id or "/" in case_id:
        raise ValueError(f"runner case id is not case/arm: {runner_id}")
    return case_id, arm


def _reject_labels(request: dict) -> None:
    """The runner request is the model boundary: evaluator input, id, and arm only."""
    if set(request) - _REQUEST_KEYS:
        raise ValueError("runner request carries fields outside the model boundary")
    for index, case in enumerate(request["cases"]):
        if set(case) - _CASE_KEYS:
            raise ValueError(f"cases[{index}]: labels are never accepted")
        raw = case["input"]
        if not isinstance(raw, dict) or set(raw) - _INPUT_KEYS:
            raise ValueError(f"cases[{index}].input: not an evaluator_input field")


def build_runner_request(document: dict, model: str, max_usd: float) -> tuple[dict, list[tuple[dict, dict, dict | None]]]:
    """Validate original source and span hashes, then build a label-free request."""
    prepared = []
    cases = []
    for case in document["cases"]:
        raw = evaluator_input(case)
        failure = _original_failure(raw)
        prepared.append((case, raw, failure))
        if failure is not None:
            continue
        for arm in ARMS:
            cases.append({"id": runner_case_id(case["id"], arm), "arm": arm, "input": deepcopy(raw)})
    request = {"model": model, "max_usd": float(max_usd), "cases": cases}
    _reject_labels(request)
    return request, prepared


def _original_failure(raw: dict) -> dict | None:
    def check(value: dict) -> dict:
        draft = value["bounded"]["draft"]
        BoundedIntakeDraft.model_validate(draft)
        return {"readiness": "NEEDS_SPEC", "findings": [], "questions": [], "criterion_ids": []}

    result = observe(check, raw)
    if result["status"] == "ok":
        return None
    return result


def _strip_fence(text: str) -> str:
    match = _FENCE.match(text.strip())
    return match.group(1) if match else text.strip()


def completion_for_draft(draft: dict, findings: list | None = None) -> str:
    """A runner reply whose quotes are the draft's own source slices."""
    encoded = draft["source"]["text"].encode()
    spans = []
    for span in draft["source"]["spans"]:
        spans.append({"id": span["id"], "quote": encoded[span["start"]:span["end"]].decode()})
    payload = {
        "draft": {
            "archetype": draft.get("archetype", "small_code_change"),
            "spans": spans,
            "goal": draft["goal"],
            "constraints": draft.get("constraints", []),
            "unknowns": draft.get("unknowns", []),
            "acceptance_criteria": draft.get("acceptance_criteria", []),
        },
        "findings": findings or [],
    }
    return json.dumps(payload)


def rebuild_draft(completion: str, source_text: str) -> tuple[dict, list]:
    """Derive a bounded draft from one saved completion and the frozen source text."""
    try:
        payload = json.loads(_strip_fence(completion))
    except json.JSONDecodeError as exc:
        raise ValueError(f"reply is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("draft"), dict):
        raise ValueError("reply is missing a draft object")
    body = payload["draft"]
    spans_in = body.get("spans", [])
    if not isinstance(spans_in, list):
        raise ValueError("spans must be a list")
    raw_bytes = source_text.encode()
    spans = []
    for span in spans_in:
        if not isinstance(span, dict) or not isinstance(span.get("id"), str) or not span["id"]:
            raise ValueError("span id must be a non-empty string")
        quote = span.get("quote")
        if not isinstance(quote, str) or quote == "":
            raise ValueError(f"span {span['id']}: quote must be a non-empty string")
        encoded = quote.encode()
        start = raw_bytes.find(encoded)
        if start < 0:
            raise ValueError(f"span {span['id']}: quote is not an exact substring of the source text")
        spans.append({
            "id": span["id"], "start": start, "end": start + len(encoded),
            "exact_text_sha256": text_sha256(quote),
        })
    draft = {
        "archetype": body.get("archetype", "small_code_change"),
        "source": {"text": source_text, "sha256": text_sha256(source_text), "spans": spans},
        "goal": body["goal"],
        "constraints": body.get("constraints", []),
        "unknowns": body.get("unknowns", []),
        "acceptance_criteria": body.get("acceptance_criteria", []),
    }
    try:
        normalized = BoundedIntakeDraft.model_validate(draft).model_dump(mode="json")
    except ValidationError as exc:
        raise ValueError(exc.errors()[0]["msg"]) from exc
    return normalized, _findings(payload.get("findings", []), normalized)


def _findings(findings: object, draft: dict) -> list:
    if not isinstance(findings, list):
        raise ValueError("findings must be a list")
    claim_ids = {draft["goal"]["id"]}
    for group in ("constraints", "unknowns", "acceptance_criteria"):
        claim_ids.update(claim["id"] for claim in draft[group])
    cleaned = []
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("kind") not in _FINDING_KINDS:
            raise ValueError("finding kind or statement is invalid")
        statement = finding.get("statement")
        refs = finding.get("claim_ids", [])
        if not isinstance(statement, str) or not statement or not isinstance(refs, list):
            raise ValueError("finding kind or statement is invalid")
        if not all(isinstance(item, str) and item in claim_ids for item in refs):
            raise ValueError("finding names unknown claim id")
        cleaned.append({"kind": finding["kind"], "statement": statement, "claim_ids": list(refs)})
    return cleaned


def _empty_transcript(model: str, max_usd: float) -> dict:
    return {
        "model": model, "cap_usd": float(max_usd), "spent_usd": 0,
        "requests": 0, "denied_requests": 0, "cases": [],
    }


def call_intake_runner(request: dict) -> dict:
    """JSON stdin/stdout CLI of intake-runner.ts. No ledger client and no gold."""
    if not request["cases"]:
        return _empty_transcript(request["model"], request["max_usd"])
    timeout = max(120, 90 * len(request["cases"]))
    proc = subprocess.run(  # nosec B603 - fixed bun argv, JSON over stdin, no shell
        ["bun", str(RUNNER)], input=json.dumps(request).encode(),
        capture_output=True, timeout=timeout, check=False,
    )
    stdout = proc.stdout.decode()
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"intake runner returned non-JSON: {stdout[:500]}") from exc
    if proc.returncode != 0 or "cases" not in payload:
        detail = payload.get("error") if isinstance(payload, dict) else None
        raise RuntimeError(detail or proc.stderr.decode()[:500] or f"intake runner exited {proc.returncode}")
    return payload


def _usage(row: dict) -> dict:
    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    tokens = usage.get("tokens") if isinstance(usage.get("tokens"), dict) else {}
    counts = {}
    for key in ("input", "output", "cache_read", "cache_write", "total"):
        value = tokens.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"usage token {key} is not a non-negative number")
        counts[key] = int(value)
    usd = usage.get("usd", 0)
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not math.isfinite(float(usd)) or usd < 0:
        raise ValueError("usage usd is not a non-negative finite number")
    return {
        "requests": int(usage.get("requests") or 0),
        "denied_requests": int(usage.get("denied_requests") or 0),
        "measured": bool(usage.get("measured")),
        "tokens": counts,
        "usd": float(usd),
    }


def _blank(status: str, diagnostics: list, tokens: int) -> dict:
    return {
        "status": status, "diagnostics": list(diagnostics), "findings": [], "questions": [],
        "criterion_ids": [], "safety_evidence": status in {"ok", "invalid", "refused"},
        "wall_seconds": 0.0, "model_tokens": tokens, "execution": "model comparison",
    }


def _skipped_proof() -> dict:
    return {
        "status": "skipped", "reason": "no NSI-2 scenario", "diagnostics": [],
        "safety_evidence": False, "model_tokens": 0, "wall_seconds": 0,
        "execution": "disposable provenance",
    }


def _assess_arm(raw: dict, arm: str, runner_row: dict | None, proof: dict, required: bool) -> dict:
    source_text = raw["bounded"]["draft"]["source"]["text"]
    if runner_row is None:
        usage = _usage({})
        observation = _blank("crashed", ["runner omitted this case"], 0)
        projected = project_observation(observation, proof if required else None, proof_required=required)
        return _arm_record(None, [], [], "failed", ["runner omitted this case"], usage, {}, projected, raw)

    usage = _usage(runner_row)
    status = runner_row.get("status")
    completions = list(runner_row.get("completions") or [])
    diagnostics = list(runner_row.get("diagnostics") or [])
    supplied = runner_row.get("draft")
    model_findings = list(runner_row.get("findings") or [])
    tokens = usage["tokens"]["total"]
    draft = None
    if status in {"ok", "unmeasured"} and completions:
        try:
            rebuilt, rebuilt_findings = rebuild_draft(completions[-1], source_text)
        except (ValueError, KeyError, TypeError) as exc:
            status = "invalid"
            diagnostics = [*diagnostics, str(exc)]
            model_findings = []
        else:
            if supplied is not None and semantic_edits(rebuilt, supplied):
                status = "invalid"
                diagnostics = [*diagnostics, "completion does not match generated draft"]
            elif list(runner_row.get("findings") or []) != rebuilt_findings:
                status = "invalid"
                diagnostics = [*diagnostics, "completion findings do not match the runner record"]
            else:
                draft = rebuilt
                model_findings = rebuilt_findings
    if draft is None:
        if status == "cap_denied":
            observation = _blank("skipped", diagnostics or ["cap exhausted"], tokens)
        elif status == "invalid":
            observation = _blank("invalid", diagnostics or ["malformed completion"], tokens)
        else:
            observation = _blank("crashed", diagnostics or [f"runner {status}"], tokens)
            status = "failed" if status not in _INCOMPLETE_RUNNER else status
    else:
        bound = draft["source"]
        original = raw["bounded"]["draft"]["source"]
        if bound["text"] != original["text"] or bound["sha256"] != original["sha256"]:
            # Keep the rewrite visible, but do not score it as a draft of this case.
            observation = _blank("invalid", ["generated draft replaced the original source"], tokens)
            status = "invalid"
            model_findings = []
        else:
            assessment = observe(EVALUATORS[arm], {"bounded": {"draft": draft}})
            assessment["model_tokens"] = tokens
            assessment["execution"] = "model comparison"
            observation = assessment
    projected = project_observation(observation, proof if required else None, proof_required=required)
    receipt = {"model": deepcopy(runner_row.get("model")), "hashes": deepcopy(runner_row.get("hashes"))}
    return _arm_record(draft, completions, model_findings, status, diagnostics, usage, receipt, projected)


def _arm_record(draft, completions, findings, status, diagnostics, usage, receipt, projected) -> dict:
    return {
        "generated_draft": draft,
        "completions": completions,
        "model_findings": findings,
        "runner_status": status if status is not None else "failed",
        "runner_diagnostics": diagnostics,
        "usage": usage,
        "receipt": receipt,
        "observation": projected,
    }


def _cause(original: dict, generated: dict | None, findings: list) -> tuple[str, str]:
    if generated is None:
        return "missing generated draft", "no draft was assessed"
    if semantic_edits(generated, original):
        return "unsupported model repair", "generated draft differs from the frozen input draft"
    if findings:
        return "unsupported model repair", "model findings do not change bounded readiness"
    return "bounded rules accept the unchanged draft", "bounded rules score the frozen draft ready"


def _failure(case_id: str, arm: str | None, stage: str, status: str, diagnostics: list) -> dict:
    return {"id": case_id, "arm": arm, "stage": stage, "status": status, "diagnostics": list(diagnostics)}


def _paraphrase(rows: list[dict]) -> dict:
    agreements = {}
    groups = {row["paraphrase_group"] for row in rows} - {None, ""}
    for arm in ARMS:
        pairs = []
        for group in sorted(groups):
            pair = [row["arms"][arm]["observation"] for row in rows if row["paraphrase_group"] == group]

            def signature(obs):
                return (obs.get("readiness"), sorted(obs.get("criterion_ids", [])),
                        sorted(item["target"] for item in obs.get("findings", [])),
                        sorted(Counter(item["target"] for item in obs.get("questions", [])).items()))

            eligible = len(pair) == 2 and all(obs["status"] == "ok" for obs in pair)
            pairs.append({"group": group, "eligible": eligible, "agrees": eligible and signature(pair[0]) == signature(pair[1])})
        numerator = sum(item["agrees"] for item in pairs)
        agreements[arm] = {"pairs": pairs, "agreement": {
            "numerator": numerator, "denominator": len(pairs),
            "value": numerator / len(pairs) if pairs else None,
        }}
    return agreements


def _arm_summary(rows: list[dict], arm: str) -> dict:
    """Reuse aggregate(), then replace the replay-hardcoded usage and execution."""
    summary = aggregate(rows, arm)
    summary["model_tokens"] = sum(row["arms"][arm]["observation"]["model_tokens"] for row in rows)
    summary["usd"] = sum(row["arms"][arm]["usage"]["usd"] for row in rows)
    summary["execution"] = "model comparison"
    return summary


def comparison_pins() -> dict:
    pins = report_pins()
    pins["drafting_sources"] = {
        relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() for relative in DRAFTING_SOURCES
    }
    pins["frozen_bytes"] = corpus_binding()
    return pins


def _index_transcript(transcript: dict) -> dict[tuple[str, str], dict]:
    indexed = {}
    for row in transcript.get("cases", []):
        case_id, arm = split_runner_id(row["id"])
        if (case_id, arm) in indexed:
            raise ValueError(f"duplicate runner case: {row['id']}")
        indexed[(case_id, arm)] = row
    return indexed


def compare(
    document: dict,
    *,
    model: str,
    max_usd: float,
    provenance: Callable[[dict], dict],
    transcript: dict | None = None,
    runner: Callable[[dict], dict] | None = None,
    manifest: Path | None = None,
    rollback_proof: bytes | None = None,
    corpus_record: dict | None = None,
) -> dict:
    """Score generated drafts with current and typed rules after original-input proof."""
    _require_budget(model, max_usd)
    started = time.monotonic()
    request, prepared = build_runner_request(document, model, float(max_usd))
    if transcript is None:
        transcript = (runner or call_intake_runner)(request) if request["cases"] else _empty_transcript(model, float(max_usd))
    if not isinstance(transcript, dict) or not isinstance(transcript.get("cases"), list):
        raise ValueError("runner transcript must be an object with cases")
    indexed = _index_transcript(transcript)
    failures = []
    rows = []
    for case, raw, original_failure in prepared:
        required = bool(raw.get("nsi2_proof_scenario"))
        # Proof always sees the frozen scenario, never the model draft.
        if required:
            proof = observe(provenance, raw)
            proof["execution"] = "disposable provenance"
        else:
            proof = _skipped_proof()
        if proof["status"] not in {"ok", "skipped"}:
            failures.append(_failure(case["id"], None, "proof", proof["status"], proof.get("diagnostics", [])))
        arms = {}
        for arm in ARMS:
            if original_failure is not None:
                observation = deepcopy(original_failure)
                observation["model_tokens"] = 0
                observation["execution"] = "model comparison"
                projected = project_observation(observation, proof if required else None, proof_required=required)
                record = _arm_record(None, [], [], "not_drafted", list(original_failure.get("diagnostics", [])), _usage({}), {}, projected)
                record["observation"] = projected
                failures.append(_failure(case["id"], arm, "source", projected["status"], projected.get("diagnostics", [])))
            else:
                record = _assess_arm(raw, arm, indexed.get((case["id"], arm)), proof, required)
                if (case["id"], arm) not in indexed:
                    failures.append(_failure(case["id"], arm, "runner", "failed", ["runner omitted this case"]))
                elif record["runner_status"] != "ok" or record["observation"]["status"] != "ok":
                    stage = "runner" if record["runner_status"] != "ok" else "assessment"
                    status = record["runner_status"] if stage == "runner" else record["observation"]["status"]
                    diagnostics = record["runner_diagnostics"] or record["observation"].get("diagnostics", [])
                    failures.append(_failure(case["id"], arm, stage, status, diagnostics))
            record["metrics"] = score(record["observation"], case["gold"])
            arms[arm] = record
        rows.append({
            "id": case["id"], "paraphrase_group": case.get("paraphrase_group"),
            "original_input": raw, "provenance": proof, "arms": arms,
        })
    unexpected = sorted(set(indexed) - {(case["id"], arm) for case, _, failure in prepared if failure is None for arm in ARMS})
    if unexpected:
        failures.append(_failure("*", None, "accounting", "invalid", [f"unexpected runner cases: {unexpected}"]))
    summed_usd = sum(row["usage"]["usd"] for row in transcript["cases"])
    summed_tokens = 0
    summed_requests = 0
    summed_denied = 0
    for row in transcript["cases"]:
        usage = _usage(row)
        part = usage["tokens"]
        if part["input"] + part["output"] + part["cache_read"] + part["cache_write"] != part["total"]:
            failures.append(_failure(row["id"], None, "accounting", "invalid", ["token total does not match parts"]))
        summed_tokens += part["total"]
        summed_requests += usage["requests"]
        summed_denied += usage["denied_requests"]
    reported_spent = float(transcript.get("spent_usd", summed_usd))
    if not _money_equal(reported_spent, summed_usd):
        failures.append(_failure("*", None, "accounting", "invalid", ["runner spent_usd does not match case usage"]))
    if transcript.get("requests") not in (None, summed_requests) or transcript.get("denied_requests") not in (None, summed_denied):
        failures.append(_failure("*", None, "accounting", "invalid", ["runner request counts do not match case usage"]))
    if _over_cap(summed_usd, float(max_usd)) or _over_cap(reported_spent, float(max_usd)):
        failures.append(_failure("*", None, "cap", "cap_denied", [f"spent {summed_usd} exceeds cap {max_usd}"]))
    if "cap_usd" in transcript and not _money_equal(float(transcript["cap_usd"]), float(max_usd)):
        failures.append(_failure("*", None, "accounting", "invalid", ["transcript cap does not match the requested cap"]))
    aggregate_report = {arm: _arm_summary(rows, arm) for arm in ARMS}
    residual = []
    for row in rows:
        original_draft = row["original_input"]["bounded"]["draft"]
        for arm in ARMS:
            metrics = row["arms"][arm]["metrics"]
            if not metrics["false_ready"]["numerator"]:
                continue
            cause, detail = _cause(original_draft, row["arms"][arm]["generated_draft"], row["arms"][arm]["model_findings"])
            residual.append({
                "id": row["id"], "arm": arm, "cause": cause, "detail": detail,
                "undetected_critical_behaviors": list(metrics["undetected_critical_behaviors"]),
            })
    proof_rows = [row["provenance"] for row in rows]
    proof_expected = sum(bool(raw.get("nsi2_proof_scenario")) for _case, raw, _failure in prepared)
    complete = _is_complete(rows, failures, summed_usd, float(max_usd), summed_denied)
    efforts = sorted({
        (row.get("model") or {}).get("effort")
        for row in transcript["cases"]
        if isinstance(row.get("model"), dict)
    })
    report = {
        "kind": "nsi6-model-comparison",
        "version": 1,
        "execution": "model comparison",
        "model_configuration": {
            "requested": model,
            "resolved": transcript.get("model", model),
            "cap_usd": float(max_usd),
            "efforts": [item for item in efforts if item is not None],
        },
        "cap_usd": float(max_usd),
        "spent_usd": summed_usd,
        "model_tokens": sum(aggregate_report[arm]["model_tokens"] for arm in ARMS),
        "requests": summed_requests,
        "denied_requests": summed_denied,
        "complete_evidence": complete,
        "failures": failures,
        "residual_false_ready": residual,
        "cases": rows,
        "aggregate": aggregate_report,
        "provenance": {
            "statuses": dict(Counter(item["status"] for item in proof_rows)),
            "outcomes": {row["id"]: row["provenance"] for row in rows if row["provenance"]["status"] != "skipped"},
            "valid_proofs": {
                "numerator": sum(bool(item.get("provenance_valid")) for item in proof_rows),
                "denominator": proof_expected,
                "value": (sum(bool(item.get("provenance_valid")) for item in proof_rows) / proof_expected) if proof_expected else None,
            },
            "model_tokens": sum(item.get("model_tokens", 0) for item in proof_rows),
            "execution": "disposable provenance",
            "wall_seconds": sum(item.get("wall_seconds", 0) for item in proof_rows),
        },
        "paraphrase_agreement": _paraphrase(rows),
        "transcript": transcript,
        "runner_request_sha256": sha256(request),
        "manifest": _display(manifest or DEFAULT_MANIFEST),
        "corpus": corpus_record,
        "pins": comparison_pins(),
        "definitions": {
            "model_boundary": "Only evaluator_input, the case id, and the arm cross to intake-runner.ts.",
            "proof": "disposable_provenance reads the original scenario, so a generated draft cannot erase tamper or staleness.",
            "false_ready": "Unchanged nsi_eval.score after project_observation. Non-ready gold stays in the denominator.",
            "residual_false_ready": "Every scored false-ready id and cause, including unsupported model repairs.",
            "complete_evidence": "Every case finished under the cap with measured usage. Exhausted, unmeasured, and crashed proof runs are not evidence.",
            "replay": "verify rebuilds drafts from saved completions and reruns assessment, proof, projection, and score without a model call.",
        },
        "rollback_proof": None if rollback_proof is None else {
            "sha256": hashlib.sha256(rollback_proof).hexdigest(),
            "bytes_hex": rollback_proof.hex(),
            "raw": json.loads(rollback_proof),
        },
        "wall_seconds": time.monotonic() - started,
        "sealed": False,
    }
    if report["complete_evidence"]:
        report["sealed"] = True
        report["report_sha256"] = sha256(report)
    return report


def _is_complete(rows: list[dict], failures: list[dict], spent: float, cap: float, denied: int) -> bool:
    if denied or _over_cap(spent, cap) or any(item["stage"] in {"accounting", "cap"} for item in failures):
        return False
    for row in rows:
        if row["provenance"]["status"] == "crashed":
            return False
        for arm in ARMS:
            record = row["arms"][arm]
            if record["runner_status"] == "not_drafted":
                continue
            if record["runner_status"] in _INCOMPLETE_RUNNER or not record["usage"]["measured"]:
                return False
            if record["observation"]["status"] in {"crashed", "skipped"}:
                return False
    return True


def replay_transcript(
    document: dict,
    transcript: dict,
    provenance: Callable[[dict], dict],
    *,
    model: str,
    max_usd: float,
    manifest: Path | None = None,
    rollback_proof: bytes | None = None,
    corpus_record: dict | None = None,
) -> dict:
    """Rerun draft rebuild, assessment, proof, projection, and score. No model call."""
    return compare(
        document, model=model, max_usd=max_usd, provenance=provenance, transcript=transcript,
        manifest=manifest, rollback_proof=rollback_proof, corpus_record=corpus_record,
    )


def _inputs_dir(manifest: Path) -> Path:
    return manifest.parent / ("inputs" if (manifest.parent / "inputs").is_dir() else "nsi6")


def _stabilize(value, parent: str | None = None):
    if isinstance(value, dict):
        skip = {"wall_seconds", "report_sha256"}
        if parent in {"proof", "provenance"}:
            skip.add("raw")
        return {key: _stabilize(item, key) for key, item in value.items() if key not in skip}
    if isinstance(value, list):
        return [_stabilize(item, parent) for item in value]
    return value


def _diff_paths(left, right, prefix: str = "") -> list[str]:
    if type(left) is not type(right):
        return [prefix or "<root>"]
    if isinstance(left, dict):
        paths = []
        for key in sorted(set(left) | set(right)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in left or key not in right:
                paths.append(path)
            else:
                paths.extend(_diff_paths(left[key], right[key], path))
            if len(paths) > 8:
                break
        return paths
    if isinstance(left, list):
        if len(left) != len(right):
            return [f"{prefix} length {len(left)} != {len(right)}"]
        paths = []
        for index, (item, other) in enumerate(zip(left, right)):
            paths.extend(_diff_paths(item, other, f"{prefix}[{index}]"))
            if len(paths) > 8:
                break
        return paths
    if left != right:
        return [prefix or "<root>"]
    return []


def _check_seal(report: dict) -> None:
    if report.get("complete_evidence"):
        if report.get("sealed") is not True or "report_sha256" not in report:
            raise ValueError("complete evidence is not sealed")
        seal = report["report_sha256"]
        body = {key: value for key, value in report.items() if key != "report_sha256"}
        if sha256(body) != seal:
            raise ValueError("seal mismatch")
        return
    if report.get("sealed") or "report_sha256" in report:
        raise ValueError("incomplete run cannot carry a seal")


def _check_accounting(report: dict) -> None:
    transcript = report["transcript"]
    summed = 0.0
    tokens = 0
    requests = 0
    denied = 0
    for row in transcript["cases"]:
        usage = _usage(row)
        part = usage["tokens"]
        if part["input"] + part["output"] + part["cache_read"] + part["cache_write"] != part["total"]:
            raise ValueError("token total does not match parts")
        summed += usage["usd"]
        tokens += part["total"]
        requests += usage["requests"]
        denied += usage["denied_requests"]
    if not _money_equal(summed, float(report["spent_usd"])):
        raise ValueError("spent_usd does not match case usage")
    # Proof evaluation is not a model call, so arm totals are the transcript totals.
    if tokens != report["model_tokens"]:
        raise ValueError("model token total does not match case usage")
    if requests != report["requests"] or denied != report["denied_requests"]:
        raise ValueError("request counts do not match case usage")
    if _over_cap(float(report["spent_usd"]), float(report["cap_usd"])):
        raise ValueError("spent_usd exceeds the run cap")
    if not _money_equal(float(report["cap_usd"]), float(report["model_configuration"]["cap_usd"])):
        raise ValueError("cap does not match the model configuration")


def _check_inputs(report: dict, document: dict) -> None:
    expected = [case["id"] for case in document["cases"]]
    actual = [row["id"] for row in report["cases"]]
    if actual != expected:
        raise ValueError("report cases do not match the frozen corpus")
    for case, row in zip(document["cases"], report["cases"], strict=True):
        if row["original_input"] != evaluator_input(case):
            raise ValueError(f"original input does not match the frozen corpus: {case['id']}")
    request, _prepared = build_runner_request(document, report["model_configuration"]["requested"], report["cap_usd"])
    if sha256(request) != report["runner_request_sha256"]:
        raise ValueError("runner request does not match frozen inputs")


def verify_report(path: Path, *, provenance: Callable[[dict], dict] | None = None) -> dict:
    """Validate seal, frozen inputs, cap accounting, and recomputed assessments."""
    report = json.loads(Path(path).read_bytes())
    if not isinstance(report, dict) or report.get("kind") != "nsi6-model-comparison" or report.get("version") != 1:
        raise ValueError("not an NSI-6 model comparison report")
    _check_seal(report)
    manifest = ROOT / report["manifest"]
    directory = _inputs_dir(manifest)
    corpus_record = verify(directory, manifest).model_dump(mode="json")
    document = load_corpus(directory, manifest)
    if report.get("corpus") != corpus_record:
        raise ValueError("frozen corpus pin does not match the manifest")
    _check_inputs(report, document)
    _check_accounting(report)
    rollback = None
    if report.get("rollback_proof") is not None:
        rollback = bytes.fromhex(report["rollback_proof"]["bytes_hex"])
        if hashlib.sha256(rollback).hexdigest() != report["rollback_proof"]["sha256"]:
            raise ValueError("rollback proof hash mismatch")
        if json.loads(rollback) != report["rollback_proof"]["raw"]:
            raise ValueError("rollback proof bytes mismatch")
    model = report["model_configuration"]["requested"]
    cap = report["cap_usd"]

    def replay(source: Callable[[dict], dict]) -> dict:
        return replay_transcript(
            document, report["transcript"], source, model=model, max_usd=cap,
            manifest=manifest, rollback_proof=rollback, corpus_record=corpus_record,
        )

    if provenance is None:
        with disposable_provenance() as provenance:
            replayed = replay(provenance)
    else:
        replayed = replay(provenance)
    left, right = _stabilize(report), _stabilize(replayed)
    if left != right:
        raise ValueError("recomputed assessments or metrics differ: " + ", ".join(_diff_paths(left, right)))
    if not report["complete_evidence"]:
        raise ValueError("incomplete run is not complete evidence")
    return report


def _read_rollback(path: Path | None) -> bytes | None:
    if path is None:
        return None
    payload = path.read_bytes()
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("rollback proof must be a JSON object")
    return payload


def run(
    manifest: Path,
    out: Path,
    model: str,
    max_usd: float,
    rollback_proof: Path | None = None,
    *,
    runner: Callable[[dict], dict] | None = None,
) -> dict:
    _require_budget(model, max_usd)
    manifest = Path(manifest)
    out = Path(out)
    _refuse_overwrite(out, manifest, rollback_proof)
    if not manifest.is_file():
        raise ValueError(f"manifest not found: {manifest}")
    directory = _inputs_dir(manifest)
    document = load_corpus(directory, manifest)
    corpus_record = verify(directory, manifest).model_dump(mode="json")
    rollback = _read_rollback(rollback_proof)
    with ExitStack() as stack:
        try:
            provenance = stack.enter_context(disposable_provenance())
        except Exception as exc:
            message = f"PostgreSQL fixture startup: {type(exc).__name__}: {exc}"

            def provenance(_raw, message=message):
                raise RuntimeError(message)
        report = compare(
            document, model=model, max_usd=float(max_usd), provenance=provenance, runner=runner,
            manifest=manifest, rollback_proof=rollback, corpus_record=corpus_record,
        )
    with out.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report


def _positive_usd(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("max-usd must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("max-usd must be a finite positive number")
    return number


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("run")
    command.add_argument("--model", required=True)
    command.add_argument("--max-usd", required=True, type=_positive_usd)
    command.add_argument("--out", required=True, type=Path)
    command.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    command.add_argument("--rollback-proof", type=Path)
    verify_command = commands.add_parser("verify")
    verify_command.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            run(args.manifest, args.out, args.model, args.max_usd, args.rollback_proof)
        else:
            verify_report(args.path)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
