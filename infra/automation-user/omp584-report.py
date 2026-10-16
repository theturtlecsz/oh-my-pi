"""Read-only OMP-584 rollout evidence report. Only the requested output is written."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import statistics
import subprocess  # nosec B404 - fixed git argv, no shell
import sys


BASELINE = 3.0
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def integer(value: object) -> bool:
    return type(value) is int


def cap_evidence(event: dict) -> dict:
    """One saturated start as evidence: its identity, attempt and observation time."""
    return {"id": event["id"], "attempt": event["attempt"], "at": event["at"]}


def cap_proof(starts: list[dict]) -> dict:
    """Name the limiting cap and its value, and count the starts that saw it saturated.

    Positive uniform slots with any start at occupied_slots >= slots win, naming the slot count.
    Otherwise each nonblank provider whose max_parallel is uniform over the starts carrying it is
    proven independently when any of those starts reports load >= max_parallel. The evidence lists
    only the saturated starts (id, attempt, at). No saturated start leaves the proof false. A cap
    whose value disagrees between starts names no single value and cannot prove.
    """
    proof = {"proven": False, "kind": None, "value": None,
             "start_count": len(starts), "saturated_count": 0, "evidence": []}
    if not starts:
        return proof
    slots = {event["slots"] for event in starts}
    if len(slots) == 1 and (value := next(iter(slots))) >= 1:
        saturated = [event for event in starts if event["occupied_slots"] >= value]
        if saturated:
            proof.update(proven=True, kind="slots", value=value,
                         saturated_count=len(saturated),
                         evidence=[cap_evidence(event) for event in saturated])
            return proof
    limits: dict[str, set] = {}
    carried: dict[str, int] = {}
    saturated_by: dict[str, list[dict]] = {}
    for event in starts:
        for name, observed in event["providers"].items():
            if not name.strip():
                continue
            limits.setdefault(name, set()).add(observed["max_parallel"])
            carried[name] = carried.get(name, 0) + 1
            if observed["load"] >= observed["max_parallel"]:
                saturated_by.setdefault(name, []).append(event)
    value: dict[str, int] = {}
    start_count: dict[str, int] = {}
    saturated_count: dict[str, int] = {}
    evidence: dict[str, list[dict]] = {}
    for name, rows in saturated_by.items():
        if len(limits[name]) != 1:
            continue
        value[name] = next(iter(limits[name]))
        start_count[name] = carried[name]
        saturated_count[name] = len(rows)
        evidence[name] = [cap_evidence(event) for event in rows]
    if value:
        proof.update(proven=True, kind="providers", value=value,
                     start_count=start_count, saturated_count=saturated_count, evidence=evidence)
    return proof


def content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_bytes(flood_dir: Path, *args: str) -> bytes | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        # Internal callers supply fixed commands and hex-validated revisions; flood_dir is one -C operand.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
        completed = subprocess.run(  # nosec B603 - absolute git path, argv list, no shell
            [git, "-C", str(flood_dir), *args], capture_output=True, check=False)
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def added_region_lines(before: bytes, after: bytes) -> list[str]:
    """Non-blank lines the activating flood commit added to flood.py. That is the OMP-584 change."""
    old = before.decode("utf-8", "surrogateescape").splitlines()
    new = after.decode("utf-8", "surrogateescape").splitlines()
    added = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new).get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(new[j1:j2])
    return [line for line in added if line.strip()]


def region_remains(source: bytes, lines: list[str]) -> bool:
    if not lines:
        return False
    pending = 0
    for line in source.decode("utf-8", "surrogateescape").splitlines():
        if line == lines[pending]:
            pending += 1
            if pending == len(lines):
                return True
    return False


def accepted_code_hashes(flood_dir: Path, rollout: dict) -> set[str]:
    """Whole-file hashes whose flood.py still contains the activating commit's OMP-584 lines.

    rollout.flood_sha256 is one file. Later commits change other lines and keep the region.
    external_commit is that flood commit; each start's code_sha256 is looked up in flood.git.
    """
    accepted = {rollout["flood_sha256"]}
    commit = rollout.get("external_commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
        return accepted
    activated = git_bytes(flood_dir, "show", f"{commit}:flood.py")
    parent = git_bytes(flood_dir, "show", f"{commit}^:flood.py")
    if activated is None or parent is None or content_sha256(activated) != rollout["flood_sha256"]:
        return accepted
    region = added_region_lines(parent, activated)
    if not region:
        return accepted
    listing = git_bytes(flood_dir, "rev-list", "--all", "--", "flood.py")
    blobs: dict[str, bytes] = {}
    if listing is not None:
        for rev in listing.decode().split():
            if not re.fullmatch(r"[0-9a-fA-F]{7,40}", rev):
                continue
            blob = git_bytes(flood_dir, "show", f"{rev}:flood.py")
            if blob is not None:
                blobs.setdefault(content_sha256(blob), blob)
    disk = flood_dir / "flood.py"
    if disk.is_file():
        data = disk.read_bytes()
        blobs.setdefault(content_sha256(data), data)
    for digest, blob in blobs.items():
        if region_remains(blob, region):
            accepted.add(digest)
    return accepted


def matching_plan_start(history: list, success: dict) -> dict:
    """Plan-start for a PASS review. A re-run keeps the last start of that attempt before the review."""
    def matches(event: dict) -> bool:
        return (event.get("phase") == "plan-start" and "attempt" in event
                and success.get("attempt") is not None and event["attempt"] == success["attempt"])

    anywhere = [event for event in history if matches(event)]
    if len(anywhere) == 1:
        return anywhere[0]
    review_at = success.get("at")
    if len(anywhere) > 1 and isinstance(review_at, str) and TIMESTAMP.fullmatch(review_at):
        review_index = next(i for i in range(len(history) - 1, -1, -1) if history[i] is success)
        before = [
            event for i, event in enumerate(history)
            if i < review_index and matches(event)
            and isinstance(event.get("at"), str) and TIMESTAMP.fullmatch(event["at"]) and event["at"] < review_at
        ]
        if before:
            return before[-1]
    raise ValueError("ambiguous successful plan-review: requires exactly one plan-start attempt")


def validate_matched_start(event: dict) -> None:
    for key in ("running_implementers", "slots", "occupied_slots"):
        if not integer(event.get(key)):
            raise ValueError(f"malformed implement-start: {key} must be an integer, not boolean")
    for key in ("load", "max_parallel"):
        if key in event and not integer(event[key]):
            raise ValueError(f"malformed implement-start: {key} must be an integer, not boolean")
    providers = event.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("malformed implement-start: providers must be an object")
    for prov_name, limits in providers.items():
        if not isinstance(limits, dict):
            raise ValueError(f"malformed implement-start: provider {prov_name} must be an object")
        for key in ("load", "max_parallel"):
            if not integer(limits.get(key)):
                raise ValueError(f"malformed implement-start: provider {prov_name} {key} must be an integer, not boolean")
    if event.get("attempt") is not None and not integer(event["attempt"]):
        raise ValueError("malformed implement-start: attempt must be an integer, not boolean")


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

    accepted = accepted_code_hashes(flood_dir, rollout) if valid_rollout else set()

    def included(event: dict) -> bool:
        if not isinstance(event.get("at"), str) or not isinstance(event.get("code_sha256"), str):
            raise ValueError("malformed start: requires string at and code_sha256")
        if not TIMESTAMP.fullmatch(event["at"]):
            return False
        return event["at"] > rollout["activated_at"] and event["code_sha256"] in accepted

    has_malformed_start = False
    cap_events = []
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
                                try:
                                    validate_matched_start(event)
                                    result["starts"].append({
                                        "id": task_id,
                                        "attempt": event.get("attempt"),
                                        "at": event["at"],
                                        "running_implementers": event["running_implementers"],
                                        "code_sha256": event["code_sha256"],
                                    })
                                    cap_events.append({"id": task_id, "attempt": event.get("attempt"),
                                        "at": event["at"], "slots": event["slots"],
                                        "occupied_slots": event["occupied_slots"],
                                        "providers": event["providers"]})
                                except Exception:
                                    has_malformed_start = True
                                    raise
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
                start = matching_plan_start(history, success)
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
    result["starts"].sort(key=lambda s: (s["at"], s["id"], -1 if s["attempt"] is None else s["attempt"]))
    result["start_count"] = len(result["starts"])
    if has_malformed_start:
        result["median"] = None
    elif result["starts"]:
        result["median"] = statistics.median(e["running_implementers"] for e in result["starts"])
    result["cap"] = cap_proof(cap_events) if not has_malformed_start else cap_proof([])
    if result["start_count"] < 10:
        result["verdicts"]["starts"] = "pending"
    elif result["median"] is not None and result["median"] > BASELINE:
        result["verdicts"]["starts"] = "met"
    elif result["median"] is not None and result["median"] <= BASELINE:
        result["verdicts"]["starts"] = "met" if result["cap"]["proven"] else "unmet"
    else:
        result["verdicts"]["starts"] = "pending"
    if errors:
        result.update(status="failure", error="; ".join(errors))
        return result, 2
    if result["plan_count"] < 10 or result["start_count"] < 10:
        return result, 2
    if all(verdict == "met" for verdict in result["verdicts"].values()):
        result["status"] = "ok"
        return result, 0
    result["status"] = "unmet"
    return result, 1


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
