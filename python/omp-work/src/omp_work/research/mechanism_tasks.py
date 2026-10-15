"""Frozen mechanism-study task manifests and managed candidate/evaluator inputs (OMP-606-s03).

Two real, generator-independent domains share one search shape (the
``zlib_source_task`` pool topology, budget=5, width=1, unit node costs):

* development / fresh confirmation (simulation): CPU gradient descent on a
  seeded shifted quadratic. The sealed evaluator scores ``1/(1+d^2)`` from the
  candidate's coordinates, ``d`` being the distance to the minimizer.
* held-out (engineering): zlib compression of frozen production source. The
  sealed evaluator decompresses the candidate bytes, demands the exact original
  and computes ``raw/compressed`` itself.

Candidate-reported numbers are never read. Candidate tar and output envelopes
carry a binding to the task bytes; the evaluator refuses a mismatch. Everything
a run depends on is digested into the task manifest before scoring.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omp_work.operations.artifacts import bytes_sha256
from omp_work.research.receipt import EvaluationProtocol
from omp_work.research.zlib_source_task import POOL_NODES, ZlibNode, build_corpus
from omp_work.v1.canonical import canonical_json, sha256

__all__ = [
    "BUDGET",
    "CONFIRMATION_SEEDS",
    "DEVELOPMENT_SEEDS",
    "ENGINEERING_SEEDS",
    "WIDTH",
    "ManagedInputs",
    "MechanismTask",
    "assert_disjoint",
    "cohort_manifest",
    "confirmation_cohort",
    "development_cohort",
    "engineering_cohort",
    "engineering_task",
    "managed_inputs",
    "simulation_task",
]

BUDGET = 5
WIDTH = 1
DEVELOPMENT_SEEDS = (0, 1, 2)
CONFIRMATION_SEEDS = (3, 4)
ENGINEERING_SEEDS = (0, 1, 2, 3, 4)
TIMEOUT_SECONDS = 60
DIMENSIONS = 4
OUTPUT_ENVELOPE = "out/envelope.json"
OUTPUT_COMPRESSED = "out/compressed.bin"

# Simulation pool: same topology as the zlib pool, optimizer parameters per node.
_SIM_PARAMS: dict[str, dict[str, Any]] = {
    "a": {"lr": 0.002, "iters": 20},
    "b": {"lr": 0.05, "iters": 20},
    "c": {"lr": 0.1, "iters": 40},
    "a1": {"lr": 0.05, "iters": 60},
    "a2": {"lr": 0.2, "iters": 60},
    "a1x": {"lr": 0.1, "iters": 200},
    "a1y": {"lr": 0.15, "iters": 400},
}

_SIM_CANDIDATE = r'''
import json
import pathlib

problem = json.loads(pathlib.Path("problem.json").read_text(encoding="utf-8"))
params = json.loads(pathlib.Path("params.json").read_text(encoding="utf-8"))
weights, shift = problem["weights"], problem["shift"]
x = list(problem["start"])
for _ in range(int(params["iters"])):
    x = [xi - params["lr"] * 2.0 * w * (xi - s) for xi, w, s in zip(x, weights, shift)]
pathlib.Path("out").mkdir(exist_ok=True)
pathlib.Path("out/envelope.json").write_text(
    json.dumps({"binding": params["binding"], "x": x, "reported_score": 1.0}),
    encoding="utf-8",
)
print("METRIC score=1.0")
'''

_ZLIB_CANDIDATE = r'''
import hashlib
import json
import pathlib
import zlib

params = json.loads(pathlib.Path("params.json").read_text(encoding="utf-8"))
raw = pathlib.Path("source.bin").read_bytes()
strategy = zlib.Z_FILTERED if params["strategy"] == "filtered" else zlib.Z_DEFAULT_STRATEGY
comp = zlib.compressobj(params["level"], zlib.DEFLATED, 15, params["mem_level"], strategy)
packed = comp.compress(raw) + comp.flush()
pathlib.Path("out").mkdir(exist_ok=True)
pathlib.Path("out/compressed.bin").write_bytes(packed)
pathlib.Path("out/envelope.json").write_text(
    json.dumps(
        {
            "binding": params["binding"],
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "reported_ratio": 1000.0,
        }
    ),
    encoding="utf-8",
)
print("METRIC ratio=1000.0")
'''

_EVALUATOR_PRELUDE = r'''
import hashlib
import json
import math
import pathlib
import zlib

handoff = json.loads(pathlib.Path("input.json").read_text(encoding="utf-8"))
sealed = json.loads(pathlib.Path(handoff["confirmation"]).read_text(encoding="utf-8"))
outputs = handoff.get("outputs") or {}


def finish(valid, metric):
    pathlib.Path("score.json").write_text(
        json.dumps({"valid": valid, "metrics": {METRIC: metric}}), encoding="utf-8"
    )
    raise SystemExit(0)


def load_envelope():
    if "out/envelope.json" not in outputs:
        finish(False, 0.0)
    try:
        envelope = json.loads(pathlib.Path(outputs["out/envelope.json"]).read_bytes())
    except (OSError, ValueError):
        finish(False, 0.0)
    if not isinstance(envelope, dict) or envelope.get("binding") != sealed["binding"]:
        finish(False, 0.0)
    return envelope
'''

_SIM_EVALUATOR = (
    'METRIC = "score"\n'
    + _EVALUATOR_PRELUDE
    + r'''
envelope = load_envelope()
x = envelope.get("x")
shift = sealed["shift"]
if (
    not isinstance(x, list)
    or len(x) != len(shift)
    or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in x)
    or not all(math.isfinite(v) for v in x)
):
    finish(False, 0.0)
distance_sq = sum((v - s) ** 2 for v, s in zip(x, shift))
finish(True, 1.0 / (1.0 + distance_sq))
'''
)

_ZLIB_EVALUATOR = (
    'METRIC = "ratio"\nimport base64\n'
    + _EVALUATOR_PRELUDE
    + r'''
envelope = load_envelope()
original = base64.b64decode(sealed["source_b64"])
if envelope.get("source_sha256") != hashlib.sha256(original).hexdigest():
    finish(False, 0.0)
if "out/compressed.bin" not in outputs:
    finish(False, 0.0)
packed = pathlib.Path(outputs["out/compressed.bin"]).read_bytes()
if not packed:
    finish(False, 0.0)
inflater = zlib.decompressobj()
try:
    restored = inflater.decompress(packed, len(original) + 1)
    restored += inflater.flush()
except zlib.error:
    finish(False, 0.0)
if not inflater.eof or inflater.unused_data or restored != original:
    finish(False, 0.0)
finish(True, len(original) / len(packed))
'''
)


@dataclass(frozen=True, slots=True)
class MechanismTask:
    """One frozen task. ``manifest`` is fixed before any candidate is scored."""

    task_id: str
    domain: str
    cohort: str
    seed: int
    content_sha256: str
    evidence_class: str
    manifest: Mapping[str, Any]
    manifest_sha256: str
    sealed: bytes = field(repr=False)
    payload: bytes = field(repr=False)
    nodes: tuple[ZlibNode, ...] = field(repr=False)

    @property
    def real_evidence(self) -> bool:
        return self.evidence_class == "real"


@dataclass(frozen=True, slots=True)
class ManagedInputs:
    """Arguments of ``managed_trial.evaluate_candidate`` for one pool node."""

    protocol: EvaluationProtocol
    candidate_tar: bytes
    candidate_digest: str
    evaluator_script: bytes
    confirmation: bytes


def _tar(files: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in sorted(files):
            info = tarfile.TarInfo(name)
            info.size = len(files[name])
            info.mtime = 0
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(files[name]))
    return buffer.getvalue()


def _hex(data: bytes) -> str:
    return bytes_sha256(data).hexdigest()


def _node_params(task_domain: str, node: ZlibNode) -> dict[str, Any]:
    if task_domain == "simulation":
        return dict(_SIM_PARAMS[node.id])
    return {"level": node.level, "mem_level": node.mem_level, "strategy": node.strategy}


def _protocol(domain: str, sealed: bytes) -> EvaluationProtocol:
    metric = "score" if domain == "simulation" else "ratio"
    return EvaluationProtocol.model_validate(
        {
            "contract_version": "research-protocol.v1",
            "metrics": [{"name": metric, "direction": "higher"}],
            "primary": metric,
            "tolerance": {},
            "candidate_command": ["python3", "candidate.py"],
            "evaluator_command": ["python3", "evaluate"],
            "outputs": (
                [OUTPUT_ENVELOPE]
                if domain == "simulation"
                else [OUTPUT_ENVELOPE, OUTPUT_COMPRESSED]
            ),
            "requires": ["python3"],
            "confirmation_sha256": _hex(sealed),
            "timeout_seconds": TIMEOUT_SECONDS,
        }
    )


def _evaluator_bytes(domain: str) -> bytes:
    return (_SIM_EVALUATOR if domain == "simulation" else _ZLIB_EVALUATOR).encode()


def _candidate_files(task: MechanismTask, node: ZlibNode) -> dict[str, bytes]:
    binding = {"task_id": task.task_id, "task_sha256": task.content_sha256}
    params = {**_node_params(task.domain, node), "binding": binding}
    files = {"params.json": canonical_json(params).encode()}
    if task.domain == "simulation":
        files["candidate.py"] = _SIM_CANDIDATE.encode()
        problem = json.loads(task.sealed)
        files["problem.json"] = canonical_json(
            {"weights": problem["weights"], "shift": problem["shift"], "start": problem["start"]}
        ).encode()
    else:
        files["candidate.py"] = _ZLIB_CANDIDATE.encode()
        files["source.bin"] = task.payload
    return files


def _freeze(
    domain: str,
    cohort: str,
    seed: int,
    content: bytes,
    sealed_extra: Mapping[str, Any],
    payload: bytes,
    evidence_class: str,
    provenance: Mapping[str, Any],
) -> MechanismTask:
    content_sha256 = _hex(content)
    task_id = f"{domain}-{cohort}-s{seed}-{content_sha256[:12]}"
    sealed = canonical_json(
        {
            **sealed_extra,
            "binding": {"task_id": task_id, "task_sha256": content_sha256},
        }
    ).encode()
    nodes = POOL_NODES
    if any(node.cost != 1 for node in nodes):
        raise ValueError("pool nodes must carry unit cost")
    evaluator = _evaluator_bytes(domain)
    protocol = _protocol(domain, sealed)
    draft = MechanismTask(
        task_id=task_id,
        domain=domain,
        cohort=cohort,
        seed=seed,
        content_sha256=content_sha256,
        evidence_class=evidence_class,
        manifest={},
        manifest_sha256="",
        sealed=sealed,
        payload=payload,
        nodes=nodes,
    )
    topology = [{"id": n.id, "parent": n.parent, "cost": n.cost} for n in nodes]
    parameters = {n.id: _node_params(domain, n) for n in nodes}
    candidate_digests = {n.id: _hex(_tar(_candidate_files(draft, n))) for n in nodes}
    manifest = {
        "task_id": task_id,
        "domain": domain,
        "cohort": cohort,
        "seed": seed,
        "evidence_class": evidence_class,
        "content_sha256": content_sha256,
        "sealed_sha256": _hex(sealed),
        "tree": topology,
        "candidate_parameters": parameters,
        "candidate_tar_sha256": candidate_digests,
        "evaluator_sha256": _hex(evaluator),
        "protocol_sha256": _hex(protocol.protocol_bytes()),
        "search": {"budget": BUDGET, "width": WIDTH, "seed": seed},
        "provenance": dict(provenance),
    }
    return MechanismTask(
        task_id=task_id,
        domain=domain,
        cohort=cohort,
        seed=seed,
        content_sha256=content_sha256,
        evidence_class=evidence_class,
        manifest=manifest,
        manifest_sha256=sha256(manifest),
        sealed=sealed,
        payload=payload,
        nodes=nodes,
    )


def simulation_task(seed: int, cohort: str) -> MechanismTask:
    """Seeded shifted quadratic ``sum w_i (x_i - s_i)^2``; cohort namespaces the draw."""
    if cohort not in {"development", "confirmation"}:
        raise ValueError(f"unknown simulation cohort: {cohort}")
    allowed = DEVELOPMENT_SEEDS if cohort == "development" else CONFIRMATION_SEEDS
    if seed not in allowed:
        raise ValueError(f"seed {seed} is not in the {cohort} cohort")

    def unit(label: str, index: int) -> float:
        digest = hashlib.sha256(f"mechanism-sim:{cohort}:{seed}:{label}:{index}".encode()).digest()
        return int.from_bytes(digest[:8], "big") / 2**64

    spec = {
        "kind": "shifted-quadratic",
        "weights": [round(1.0 + 2.0 * unit("w", i), 6) for i in range(DIMENSIONS)],
        "shift": [round(10.0 * unit("s", i) - 5.0, 6) for i in range(DIMENSIONS)],
        "start": [0.0] * DIMENSIONS,
    }
    content = canonical_json(spec).encode()
    return _freeze(
        "simulation",
        cohort,
        seed,
        content,
        spec,
        b"",
        "real",
        {"generator": "mechanism-sim.v1", "cohort_seed": seed},
    )


def engineering_task(
    seed: int,
    source: bytes,
    provenance: Mapping[str, Any],
) -> MechanismTask:
    """Held-out zlib task over opaque ``source`` bytes.

    ``provenance`` is recorded verbatim; it counts as real evidence only when it
    names source files with their sha256 hashes. Anything else is a fixture.
    """
    files = provenance.get("files")
    real = bool(source) and isinstance(files, Mapping) and bool(files) and all(
        isinstance(h, str) and len(h) == 64 for h in files.values()
    )
    sealed_extra = {"source_b64": base64.b64encode(source).decode("ascii")}
    return _freeze(
        "engineering",
        "heldout",
        seed,
        source,
        sealed_extra,
        source,
        "real" if real else "fixture",
        provenance,
    )


def development_cohort() -> tuple[MechanismTask, ...]:
    return tuple(simulation_task(seed, "development") for seed in DEVELOPMENT_SEEDS)


def confirmation_cohort() -> tuple[MechanismTask, ...]:
    return tuple(simulation_task(seed, "confirmation") for seed in CONFIRMATION_SEEDS)


def engineering_cohort(repo_root: Path | str) -> tuple[MechanismTask, ...]:
    """Held-out cohort sampled with ``zlib_source_task.build_corpus``; never feed it to generation."""
    tasks: list[MechanismTask] = []
    for seed in ENGINEERING_SEEDS:
        corpus, hashes, tree_sha256 = build_corpus(repo_root, seed)
        tasks.append(
            engineering_task(seed, corpus, {"files": hashes, "tree_sha256": tree_sha256})
        )
    return tuple(tasks)


def assert_disjoint(*cohorts: Sequence[MechanismTask]) -> None:
    """Refuse cohorts that share a task id, seed-within-domain pair or content digest."""
    seen: dict[str, int] = {}
    for index, cohort in enumerate(cohorts):
        for task in cohort:
            for key in (task.content_sha256, task.task_id):
                if seen.setdefault(key, index) != index:
                    raise ValueError(f"cohorts overlap on {key}")


def cohort_manifest(tasks: Sequence[MechanismTask]) -> dict[str, Any]:
    """Cohort-level manifest whose digest covers every member manifest."""
    if not tasks:
        raise ValueError("cohort is empty")
    if any(not task.real_evidence for task in tasks):
        raise ValueError("fixture tasks cannot form a real-evidence cohort")
    members = {task.task_id: task.manifest_sha256 for task in tasks}
    body = {"cohort": tasks[0].cohort, "tasks": members}
    return {**body, "manifest_sha256": sha256(body)}


def managed_inputs(task: MechanismTask, node_id: str) -> ManagedInputs:
    """Inputs for ``evaluate_candidate`` running pool node ``node_id`` on ``task``."""
    node = next((n for n in task.nodes if n.id == node_id), None)
    if node is None:
        raise ValueError(f"unknown pool node: {node_id}")
    tar = _tar(_candidate_files(task, node))
    return ManagedInputs(
        protocol=_protocol(task.domain, task.sealed),
        candidate_tar=tar,
        candidate_digest=_hex(tar),
        evaluator_script=_evaluator_bytes(task.domain),
        confirmation=task.sealed,
    )
