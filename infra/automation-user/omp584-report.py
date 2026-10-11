"""Read-only OMP-584 rollout evidence report. Only the requested output is written."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import statistics
import sys


BASELINE = 3.0


def integer(value: object) -> bool:
    return type(value) is int


def cap_proof(starts: list[dict]) -> dict:
    cap = {"proven": False, "kind": None, "value": None, "evidence": []}
    if not starts:
        return cap
    slots = starts[0].get("slots")
    if integer(slots) and slots >= 1 and all(
        integer(e.get("slots")) and e["slots"] == slots
        and integer(e.get("occupied_slots")) and e["occupied_slots"] >= slots
        for e in starts
    ):
        return {"proven": True, "kind": "slots", "value": slots,
                "evidence": [{k: e[k] for k in ("task_id", "at", "slots", "occupied_slots")} for e in starts]}
    providers = starts[0].get("providers")
    if not isinstance(providers, dict) or not providers or not all(
        isinstance(name, str) and name.strip() for name in providers
    ):
        return cap
    for event in starts:
        current = event.get("providers")
        if not isinstance(current, dict) or current.keys() != providers.keys():
            return cap
        for limits in current.values():
            if not isinstance(limits, dict):
                return cap
            load, maximum = limits.get("load"), limits.get("max_parallel")
            if not integer(load) or not integer(maximum) or load < maximum:
                return cap
    return {"proven": True, "kind": "providers",
            "value": {name: limits["max_parallel"] for name, limits in providers.items()},
            "evidence": [{k: e[k] for k in ("task_id", "at", "providers")} for e in starts]}


def report(flood_dir: Path, rollout_path: Path) -> tuple[dict, int]:
    result = {
        "input_hashes": {"flood_state_sha256": None, "rollout_sha256": None},
        "cutoff": {"activated_at": None, "flood_sha256": None, "repo_commit": None, "baseline": BASELINE},
        "plans": [], "plan_count": 0, "independent_plan_count": 0,
        "starts": [], "start_count": 0, "median": None,
        "cap": cap_proof([]), "verdicts": {"plans": "pending", "starts": "pending"},
        "status": "pending", "error": None,
    }
    errors = []

    def read_input(path: Path, hash_key: str):
        try:
            raw = path.read_bytes()
            result["input_hashes"][hash_key] = hashlib.sha256(raw).hexdigest()
            return json.loads(raw)
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
            return None

    rollout = read_input(rollout_path, "rollout_sha256")
    state = read_input(flood_dir / "flood-state.json", "flood_state_sha256")
    valid_rollout = isinstance(rollout, dict) and all(
        isinstance(rollout.get(key), str) and rollout[key].strip()
        for key in ("activated_at", "flood_sha256", "repo_commit")
    ) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", rollout["activated_at"])
    if valid_rollout:
        result["cutoff"].update({key: rollout[key] for key in ("activated_at", "flood_sha256", "repo_commit")})
    else:
        errors.append("rollout must contain activated_at (YYYY-MM-DDTHH:MM:SSZ), flood_sha256 and repo_commit")
    valid_state = isinstance(state, dict) and isinstance(state.get("tasks"), dict)
    if not valid_state:
        errors.append("state must contain a tasks object")

    flood = None
    try:
        # Disable bytecode caching before executing the imported module: DIR is read-only evidence.
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location("omp584_flood", flood_dir / "flood.py")
        flood = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = flood
        spec.loader.exec_module(flood)
        for name in ("parse_front_matter", "is_owner"):
            if not callable(getattr(flood, name, None)):
                raise ValueError(f"flood.py missing {name}")
    except Exception as exc:
        errors.append(f"flood import: {exc}")
        flood = None

    def included(event: dict) -> bool:
        if not isinstance(event.get("at"), str) or not isinstance(event.get("code_sha256"), str):
            raise ValueError("start requires string at and code_sha256")
        return event["at"] > rollout["activated_at"] and event["code_sha256"] == rollout["flood_sha256"]

    if valid_rollout and valid_state:
        for task_id, task in state["tasks"].items():
            try:
                if not isinstance(task, dict):
                    raise ValueError("malformed task: must be an object")
                history = task.get("history", [])
                if not isinstance(history, list) or not all(isinstance(e, dict) for e in history):
                    raise ValueError("malformed history: must be a list of objects")
                # Implement starts count even for tasks that never promoted a plan.
                for event in history:
                    if event.get("phase") == "implement-start":
                        try:
                            if included(event):
                                if not integer(event.get("running_implementers")):
                                    raise ValueError("running_implementers must be an integer")
                                result["starts"].append({**event, "task_id": task_id})
                        except Exception as exc:
                            errors.append(f"{task_id}: {exc}")
                slices = task.get("slices")
                if not isinstance(slices, list) or not slices:
                    continue
                plan_starts = [e for e in history if e.get("phase") == "plan-start"]
                reviews = [e for e in history if e.get("phase") == "plan-review"]
                if not plan_starts:
                    continue
                success = reviews[-1] if reviews else {}
                verdict = success.get("verdict")
                if not isinstance(verdict, str) or verdict.upper() != "PASS":
                    raise ValueError("missing successful final plan-review")
                matching = [e for e in plan_starts if "attempt" in e and success.get("attempt") is not None
                            and e["attempt"] == success["attempt"]]
                if len(matching) != 1:
                    raise ValueError("ambiguous successful plan-review: requires exactly one plan-start attempt")
                start = matching[0]
                if not isinstance(start.get("at"), str) or not re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", start["at"]
                ) or not isinstance(start.get("code_sha256"), str):
                    raise ValueError("malformed plan-start: requires timestamp at and string code_sha256")
                if not included(start):
                    continue
                plan = {"id": task_id, "attempt": success["attempt"],
                    "code_sha256": start["code_sha256"], "slices": len(slices), "roots": None,
                    "implementer_slices": None, "implementer_roots": None}
                result["plans"].append(plan)
                if not all(isinstance(s, str) and s.strip() for s in slices) or len(set(slices)) != len(slices):
                    raise ValueError("malformed slices: ids must be nonblank and unique")
                if flood is None:
                    continue
                roots = implementer_slices = implementer_roots = 0
                for slice_id in slices:
                    path = flood_dir / "tasks" / f"{slice_id}.md"
                    try:
                        text = path.read_text(encoding="utf-8")
                    except FileNotFoundError:
                        raise ValueError(f"missing slice file: {slice_id}") from None
                    meta, _ = flood.parse_front_matter(text)
                    root = meta.get("depends_on") == [] and any(
                        line.strip() == "depends_on: []" for line in text.splitlines()
                    )
                    owner = flood.is_owner(meta)
                    roots += root
                    implementer_slices += not owner
                    implementer_roots += root and not owner
                plan.update(roots=roots, implementer_slices=implementer_slices,
                            implementer_roots=implementer_roots)
            except Exception as exc:
                errors.append(f"{task_id}: {exc}")
    result["plans"].sort(key=lambda plan: plan["id"])
    result["plan_count"] = len(result["plans"])
    result["independent_plan_count"] = sum(
        plan["roots"] is not None and plan["roots"] >= 2 for plan in result["plans"]
    )
    if result["plan_count"] >= 10:
        result["verdicts"]["plans"] = "met" if result["independent_plan_count"] * 2 > result["plan_count"] else "unmet"
    result["start_count"] = len(result["starts"])
    if result["starts"]:
        result["median"] = statistics.median(e["running_implementers"] for e in result["starts"])
    result["cap"] = cap_proof(result["starts"])
    if result["start_count"] >= 10:
        result["verdicts"]["starts"] = "met" if result["median"] > BASELINE or result["cap"]["proven"] else "unmet"
    if errors:
        result.update(status="failure", error="; ".join(errors))
        return result, 2
    return result, 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flood-dir", type=Path, required=True)
    parser.add_argument("--rollout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result, code = report(args.flood_dir, args.rollout)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
