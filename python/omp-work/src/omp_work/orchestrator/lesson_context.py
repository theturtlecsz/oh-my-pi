"""Compile worker context through the configured context CLI boundary."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess  # nosec B404 - runs the operator-configured context CLI
from typing import Any

from omp_work.orchestrator.service import StageContext
from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.service import WorkError


_HEX64 = re.compile(r"[0-9a-f]{64}")


def _validated(compiled: Any) -> dict[str, Any]:
    """Check compiler stdout once: text identity and one-to-one lesson supply pairing."""
    if (
        not isinstance(compiled, dict)
        or "error" in compiled
        or any(not isinstance(compiled.get(key), str) or not compiled[key]
               for key in ("text", "bundle_id", "bundle_sha256"))
        or compiled.get("stage") != "implement"
        or sha256(compiled["text"]) != compiled["bundle_sha256"]
    ):
        raise WorkError("context_compile_failed")
    ids, supplies = compiled.get("lesson_supply_ids"), compiled.get("lesson_supplies")
    if ids is None and supplies is None:
        return compiled
    if (
        not isinstance(ids, list) or not isinstance(supplies, list)
        or any(not isinstance(sid, str) or not sid for sid in ids)
        or any(not isinstance(rec, dict) or not isinstance(rec.get("supply_id"), str)
               or not rec["supply_id"] for rec in supplies)
    ):
        raise WorkError("context_compile_failed")
    supplied = [rec["supply_id"] for rec in supplies]
    if len(set(ids)) != len(ids) or len(set(supplied)) != len(supplied) or set(ids) != set(supplied):
        raise WorkError("context_compile_failed")
    return compiled


def record_delivery(ctx: StageContext, compiled: dict[str, Any], output: str | bytes, data: dict[str, Any]) -> None:
    """Persist lesson supplies through the knowledge CLI once the worker reports delivery.

    ``output`` is the worker's JSONL stdout. Without a ``context-delivery`` event that
    matches the compiled text and carries a 64-hex prompt hash, nothing is delivered.
    """
    supplies = compiled.get("lesson_supplies")
    if not supplies:
        return
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    expected = sha256(compiled["text"])
    event = None
    for line in output.splitlines():
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "context-delivery":
            event = parsed
            break
    prompt_hash = None if event is None else event.get("prompt_hash")
    if (
        event is None or event.get("compiled_context_hash") != expected
        or not isinstance(prompt_hash, str) or _HEX64.fullmatch(prompt_hash) is None
        or not ctx.config.context_command or ctx.config.learning_state_dir is None
    ):
        raise WorkError("context_delivery_failed")
    argv = [*ctx.config.context_command, "deliver", "--learning-state-dir", str(ctx.config.learning_state_dir)]
    try:
        result = subprocess.run(  # nosec B603 - argv list, no shell; command comes from operator config
            argv, input=canonical_json({
                "worker_job_id": str(ctx.lease.job_id), "compiled_context_hash": expected,
                "prompt_hash": prompt_hash, "supplies": supplies,
            }), capture_output=True, text=True, check=False,
        )
    except OSError as error:
        raise WorkError("context_delivery_failed") from error
    if result.returncode != 0:
        raise WorkError("context_delivery_failed")
    data["prompt_hash"] = prompt_hash


def compile_context(ctx: StageContext, worktree: Path, work_key: str) -> tuple[str, dict[str, Any]] | None:
    """Return the workflow candidate and complete compiler result, when configured."""
    if not ctx.config.context_command:
        return None
    workflow = ctx.service.read(ctx.principal, ctx.workspace_id, "workflow", work_key)
    # Direct service reads expose SQL NULL for receipts without an independence
    # claim. Omit that absent claim so the CLI's EvidenceReceipt default applies;
    # retain explicit false/true claims and leave the stored read unchanged.
    workflow = {**workflow, "receipts": [
        {key: value for key, value in receipt.items()
         if not (key == "independent" and value is None)}
        for receipt in workflow.get("receipts", ())
    ]}
    candidate = workflow["item"].get("candidate")
    if candidate is None:
        raise WorkError("invalid_request", diagnostics=("context candidate missing",))
    argv = [*ctx.config.context_command, "stage", "--settings", str(ctx.config.context_settings_path)]
    if ctx.config.learning_state_dir is not None:
        argv.extend(["--learning-state-dir", str(ctx.config.learning_state_dir)])
    try:
        result = subprocess.run(  # nosec B603 - argv list, no shell; command comes from operator config
            argv, input=canonical_json({
                "workflow": workflow, "stage": "implement",
                "attempt_id": str(ctx.lease.job_id), "cwd": str(worktree),
            }), cwd=worktree, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise WorkError("context_compile_failed")
        compiled = _validated(json.loads(result.stdout))
        lessons = [
            {"id": m.group(1), "version": int(m.group(2))}
            for m in re.finditer(r"PROCEDURE\s+([^\s@]+)@v(\d+)", compiled["text"])
        ]
        if lessons:
            compiled["lesson_versions"] = lessons
    except (OSError, ValueError) as error:
        raise WorkError("context_compile_failed") from error
    return str(candidate["candidate_id"]), compiled
