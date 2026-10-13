"""Compile worker context through the configured context CLI boundary."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any

from omp_work.orchestrator.service import StageContext
from omp_work.v1.canonical import canonical_json
from omp_work.v1.service import WorkError


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
        result = subprocess.run(
            argv, input=canonical_json({
                "workflow": workflow, "stage": "implement",
                "attempt_id": str(ctx.lease.job_id), "cwd": str(worktree),
            }), cwd=worktree, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise WorkError("context_compile_failed")
        compiled = json.loads(result.stdout)
        if (
            not isinstance(compiled, dict)
            or any(not isinstance(compiled.get(key), str) or not compiled[key]
                   for key in ("text", "bundle_id", "bundle_sha256"))
            or compiled.get("stage") != "implement"
            or ("lesson_supply_ids" in compiled and (
                not isinstance(compiled["lesson_supply_ids"], list)
                or any(not isinstance(sid, str) or not sid for sid in compiled["lesson_supply_ids"])
            ))
        ):
            raise WorkError("context_compile_failed")
        lessons = [
            {"id": m.group(1), "version": int(m.group(2))}
            for m in re.finditer(r"PROCEDURE\s+([^\s@]+)@v(\d+)", compiled["text"])
        ]
        if lessons:
            compiled["lesson_versions"] = lessons
    except (OSError, ValueError) as error:
        raise WorkError("context_compile_failed") from error
    return str(candidate["candidate_id"]), compiled
