"""ML and simulation experiment support for autoresearch (R09, OMP-316)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import statistics
import threading
from typing import Any

from omp_work.jobs.runner import BackendIncompatible, LocalJailBackend, RunRequest, RunResult
from omp_work.v1.canonical import canonical_json

__all__ = [
    "CandidateBinds",
    "RepeatedAnalysis",
    "SimulationManifest",
    "TrainingManifest",
    "adapt_metrics",
    "artifact_sha256",
    "candidate_binds",
    "check_splits",
    "classify_checkpoint",
    "manifest_bytes",
    "repeated_analysis",
    "run_manifest",
    "suggest",
]


def suggest(space: Mapping[str, Sequence[Any]], index: int) -> dict[str, Any]:
    """Suggest parameters for index by varying the last key fastest and wrapping with modulo."""
    sorted_keys = sorted(space.keys())
    if not sorted_keys:
        return {
            "optimizer": "optuna-bridge.v1",
            "space": dict(space),
            "index": index,
            "params": {},
        }

    lengths = [len(space[k]) for k in sorted_keys]
    total = math.prod(lengths)
    if total == 0:
        params = {k: space[k][0] for k in sorted_keys if len(space[k]) > 0}
        return {
            "optimizer": "optuna-bridge.v1",
            "space": dict(space),
            "index": index,
            "params": params,
        }

    wrapped_idx = index % total
    params: dict[str, Any] = {}
    current = wrapped_idx
    for k in reversed(sorted_keys):
        n = len(space[k])
        current, rem = divmod(current, n)
        params[k] = space[k][rem]

    return {
        "optimizer": "optuna-bridge.v1",
        "space": dict(space),
        "index": index,
        "params": params,
    }


def check_splits(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Check dataset splits for entity overlap, future leaks, and label-in-features leaks."""
    row_list = list(rows)
    codes: list[str] = []

    # 1. entity_overlap:<entity> when that entity is in both splits
    train_entities = {
        r["entity"] for r in row_list if r.get("split") == "train" and "entity" in r
    }
    test_entities = {
        r["entity"] for r in row_list if r.get("split") == "test" and "entity" in r
    }
    overlap = sorted(train_entities & test_entities, key=str)
    for entity in overlap:
        codes.append(f"entity_overlap:{entity}")

    # 2. future_leak:<entity> when that train row's time is greater than the minimum test time
    test_times = [
        r["time"]
        for r in row_list
        if r.get("split") == "test" and r.get("time") is not None
    ]
    if test_times:
        min_test_time = min(test_times)
        seen_leaks = set()
        for r in row_list:
            if (
                r.get("split") == "train"
                and r.get("time") is not None
                and r["time"] > min_test_time
            ):
                entity = r.get("entity")
                code = f"future_leak:{entity}"
                if code not in seen_leaks:
                    seen_leaks.add(code)
                    codes.append(code)

    # 3. label_in_features:<entity> when a feature value equals that row's label
    seen_label_leaks = set()
    for r in row_list:
        label = r.get("label")
        if label is None:
            continue
        features = r.get("features")
        feat_values: Iterable[Any]
        if isinstance(features, Mapping):
            feat_values = features.values()
        elif isinstance(features, (list, tuple, set, frozenset)):
            feat_values = features
        elif features is not None:
            feat_values = [features]
        else:
            feat_values = []

        if any(v == label for v in feat_values):
            entity = r.get("entity")
            code = f"label_in_features:{entity}"
            if code not in seen_label_leaks:
                seen_label_leaks.add(code)
                codes.append(code)

    return codes


@dataclass(frozen=True)
class TrainingManifest:
    """Canonical execution manifest for training runs."""

    kind: str
    data_sha256: str
    params: dict[str, Any]
    seed: int
    step: int
    command: tuple[str, ...]
    device: str
    confirmation_sha256: str | None

    def __init__(
        self,
        *args: Any,
        data_sha256: str = "",
        params: Mapping[str, Any] | None = None,
        seed: int = 0,
        step: int = 0,
        command: Sequence[str] = (),
        device: str = "cpu",
        confirmation_sha256: str | None = None,
        kind: str = "training",
        argv: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> None:
        actual_kind = kind
        actual_data = data_sha256
        actual_params = params
        actual_seed = seed
        actual_step = step
        actual_cmd = command
        actual_device = device
        actual_conf = confirmation_sha256

        if args:
            rem = args
            if args[0] in ("training", "simulation"):
                actual_kind = str(args[0])
                rem = args[1:]
            if len(rem) > 0:
                actual_data = rem[0]
            if len(rem) > 1:
                actual_params = rem[1]
            if len(rem) > 2:
                actual_seed = rem[2]
            if len(rem) > 3:
                actual_step = rem[3]
            if len(rem) > 4:
                actual_cmd = rem[4]
            if len(rem) > 5:
                actual_device = rem[5]
            if len(rem) > 6:
                actual_conf = rem[6]

        if argv is not None:
            actual_cmd = argv

        cmd_tuple = tuple(str(x) for x in (actual_cmd or ()))
        object.__setattr__(self, "kind", actual_kind)
        object.__setattr__(self, "data_sha256", str(actual_data))
        object.__setattr__(self, "params", dict(actual_params or {}))
        object.__setattr__(self, "seed", int(actual_seed))
        object.__setattr__(self, "step", int(actual_step))
        object.__setattr__(self, "command", cmd_tuple)
        object.__setattr__(self, "device", str(actual_device))
        object.__setattr__(self, "confirmation_sha256", actual_conf)

    @property
    def argv(self) -> tuple[str, ...]:
        return self.command

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "data_sha256": self.data_sha256,
            "params": dict(self.params),
            "seed": self.seed,
            "step": self.step,
            "command": list(self.command),
            "device": self.device,
            "confirmation_sha256": self.confirmation_sha256,
        }

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: object) -> bool:
        return hasattr(self, str(key))


@dataclass(frozen=True)
class SimulationManifest:
    """Canonical execution manifest for simulation runs."""

    kind: str
    data_sha256: str
    params: dict[str, Any]
    seed: int
    step: int
    command: tuple[str, ...]
    device: str
    confirmation_sha256: str | None

    def __init__(
        self,
        *args: Any,
        data_sha256: str = "",
        params: Mapping[str, Any] | None = None,
        seed: int = 0,
        step: int = 0,
        command: Sequence[str] = (),
        device: str = "cpu",
        confirmation_sha256: str | None = None,
        kind: str = "simulation",
        argv: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> None:
        actual_kind = kind
        actual_data = data_sha256
        actual_params = params
        actual_seed = seed
        actual_step = step
        actual_cmd = command
        actual_device = device
        actual_conf = confirmation_sha256

        if args:
            rem = args
            if args[0] in ("training", "simulation"):
                actual_kind = str(args[0])
                rem = args[1:]
            if len(rem) > 0:
                actual_data = rem[0]
            if len(rem) > 1:
                actual_params = rem[1]
            if len(rem) > 2:
                actual_seed = rem[2]
            if len(rem) > 3:
                actual_step = rem[3]
            if len(rem) > 4:
                actual_cmd = rem[4]
            if len(rem) > 5:
                actual_device = rem[5]
            if len(rem) > 6:
                actual_conf = rem[6]

        if argv is not None:
            actual_cmd = argv

        cmd_tuple = tuple(str(x) for x in (actual_cmd or ()))
        object.__setattr__(self, "kind", actual_kind)
        object.__setattr__(self, "data_sha256", str(actual_data))
        object.__setattr__(self, "params", dict(actual_params or {}))
        object.__setattr__(self, "seed", int(actual_seed))
        object.__setattr__(self, "step", int(actual_step))
        object.__setattr__(self, "command", cmd_tuple)
        object.__setattr__(self, "device", str(actual_device))
        object.__setattr__(self, "confirmation_sha256", actual_conf)

    @property
    def argv(self) -> tuple[str, ...]:
        return self.command

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "data_sha256": self.data_sha256,
            "params": dict(self.params),
            "seed": self.seed,
            "step": self.step,
            "command": list(self.command),
            "device": self.device,
            "confirmation_sha256": self.confirmation_sha256,
        }

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: object) -> bool:
        return hasattr(self, str(key))


def manifest_bytes(manifest: Any) -> bytes:
    """Canonical JSON bytes for a training or simulation manifest."""
    if hasattr(manifest, "to_dict"):
        payload = manifest.to_dict()
    elif isinstance(manifest, Mapping):
        payload = dict(manifest)
    else:
        payload = {
            "kind": getattr(manifest, "kind", ""),
            "data_sha256": getattr(manifest, "data_sha256", ""),
            "params": getattr(manifest, "params", {}),
            "seed": getattr(manifest, "seed", 0),
            "step": getattr(manifest, "step", 0),
            "command": list(getattr(manifest, "command", getattr(manifest, "argv", ()))),
            "device": getattr(manifest, "device", "cpu"),
            "confirmation_sha256": getattr(manifest, "confirmation_sha256", None),
        }
    return canonical_json(payload).encode("utf-8")


def adapt_metrics(metrics: Mapping[str, Any]) -> dict[str, float]:
    """Validate and coerce metrics to finite floats under the same names."""
    result: dict[str, float] = {}
    for key, val in metrics.items():
        if isinstance(val, bool):
            val_float = float(val)
        else:
            try:
                val_float = float(val)
            except (TypeError, ValueError):
                raise ValueError("metric_not_finite") from None
        if not math.isfinite(val_float):
            raise ValueError("metric_not_finite")
        result[str(key)] = val_float
    return result


class MediansDict(dict[str, float]):
    baseline: float
    candidate: float

    def __init__(self, baseline: float, candidate: float) -> None:
        b = float(baseline)
        c = float(candidate)
        super().__init__(baseline=b, candidate=c)
        self.baseline = b
        self.candidate = c


class RepeatedAnalysis(dict[str, Any]):
    medians: MediansDict
    mean_gain: float
    median_gain: float
    misleading_aggregate: bool

    def __init__(
        self,
        medians: Mapping[str, float] | MediansDict,
        mean_gain: float,
        median_gain: float,
        misleading_aggregate: bool,
    ) -> None:
        if isinstance(medians, MediansDict):
            med_dict = medians
        else:
            med_dict = MediansDict(medians.get("baseline", 0.0), medians.get("candidate", 0.0))
        super().__init__(
            medians=med_dict,
            mean_gain=float(mean_gain),
            median_gain=float(median_gain),
            misleading_aggregate=bool(misleading_aggregate),
        )
        self.medians = med_dict
        self.mean_gain = float(mean_gain)
        self.median_gain = float(median_gain)
        self.misleading_aggregate = bool(misleading_aggregate)


def _extract_metric_values(seq: Sequence[Any]) -> list[float]:
    if not seq:
        return []
    if isinstance(seq[0], (int, float)):
        return [float(x) for x in seq]
    if isinstance(seq[0], Mapping):
        for k in ("score", "metric", "val", "value"):
            if k in seq[0]:
                return [float(x[k]) for x in seq]
        first_key = next(iter(seq[0]))
        return [float(x[first_key]) for x in seq]
    return [float(x) for x in seq]


def repeated_analysis(baseline: Sequence[Any], candidate: Sequence[Any]) -> RepeatedAnalysis:
    """Calculate medians, mean_gain, median_gain, and misleading_aggregate across repeated trials."""
    base_vals = _extract_metric_values(baseline)
    cand_vals = _extract_metric_values(candidate)

    if not base_vals or not cand_vals:
        return RepeatedAnalysis(
            medians=MediansDict(0.0, 0.0),
            mean_gain=0.0,
            median_gain=0.0,
            misleading_aggregate=False,
        )

    base_mean = statistics.mean(base_vals)
    cand_mean = statistics.mean(cand_vals)
    base_median = float(statistics.median(base_vals))
    cand_median = float(statistics.median(cand_vals))

    mean_gain = cand_mean - base_mean
    median_gain = cand_median - base_median
    misleading = bool(mean_gain > 0 and median_gain <= 0)

    return RepeatedAnalysis(
        medians=MediansDict(base_median, cand_median),
        mean_gain=mean_gain,
        median_gain=median_gain,
        misleading_aggregate=misleading,
    )


def classify_checkpoint(saved: Any, current: Any) -> str:
    """Classify a checkpoint resumption state as interrupted, incompatible, or resumed."""
    if saved is None:
        return "interrupted"

    saved_data = getattr(saved, "data_sha256", None)
    if saved_data is None and isinstance(saved, Mapping):
        saved_data = saved.get("data_sha256")

    curr_data = getattr(current, "data_sha256", None)
    if curr_data is None and isinstance(current, Mapping):
        curr_data = current.get("data_sha256")

    if saved_data != curr_data:
        return "incompatible"

    saved_params = getattr(saved, "params", None)
    if saved_params is None and isinstance(saved, Mapping):
        saved_params = saved.get("params", {})
    saved_params = dict(saved_params or {})

    curr_params = getattr(current, "params", None)
    if curr_params is None and isinstance(current, Mapping):
        curr_params = current.get("params", {})
    curr_params = dict(curr_params or {})

    saved_filtered = {k: v for k, v in saved_params.items() if k != "step"}
    curr_filtered = {k: v for k, v in curr_params.items() if k != "step"}

    if saved_filtered != curr_filtered:
        return "incompatible"

    return "resumed"


class CandidateBinds(dict[str, Any]):
    argv: tuple[str, ...]
    ro_files: tuple[str, ...]
    env: dict[str, str]

    def __init__(
        self,
        argv: Sequence[str] = (),
        ro_files: Sequence[str | Path] = (),
        env: Mapping[str, str] | None = None,
    ) -> None:
        argv_tup = tuple(str(x) for x in argv)
        ro_tup = tuple(str(x) for x in ro_files)
        env_d = {str(k): str(v) for k, v in (env or {}).items()}
        super().__init__(argv=argv_tup, ro_files=ro_tup, env=env_d)
        self.argv = argv_tup
        self.ro_files = ro_tup
        self.env = env_d

    def __contains__(self, item: object) -> bool:
        if super().__contains__(item):
            return True
        item_str = str(item)
        if any(item_str == x or item_str in x for x in self.argv):
            return True
        if any(item_str == x or item_str in x for x in self.ro_files):
            return True
        if any(
            item_str == k or item_str in k or item_str == v or item_str in v
            for k, v in self.env.items()
        ):
            return True
        return False


def candidate_binds(
    confirmation_path: str | Path | None = None,
    *,
    argv: Sequence[str] = (),
    ro_files: Sequence[str | Path] = (),
    env: Mapping[str, str] | None = None,
) -> CandidateBinds:
    """Return sandbox binds for a candidate, guaranteeing confirmation_path is absent."""
    conf_str = str(confirmation_path) if confirmation_path is not None else None
    conf_path = Path(confirmation_path).resolve() if confirmation_path is not None else None

    clean_ro: list[str] = []
    for f in ro_files:
        p_str = str(f)
        if conf_str is not None and (p_str == conf_str or Path(f).resolve() == conf_path):
            continue
        clean_ro.append(p_str)

    clean_argv: list[str] = []
    for a in argv:
        a_str = str(a)
        if conf_str is not None and conf_str in a_str:
            continue
        clean_argv.append(a_str)

    clean_env: dict[str, str] = {}
    for k, v in (env or {}).items():
        k_str = str(k)
        v_str = str(v)
        if conf_str is not None and (conf_str in k_str or conf_str in v_str):
            continue
        clean_env[k_str] = v_str

    return CandidateBinds(
        argv=tuple(clean_argv),
        ro_files=tuple(clean_ro),
        env=clean_env,
    )


def artifact_sha256(data: bytes | str) -> str:
    """Hex-encoded SHA-256 digest of artifact bytes or UTF-8 text."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def run_manifest(
    manifest: Any,
    workdir: Path | str,
    *,
    cancel: threading.Event | None = None,
    backend: LocalJailBackend | None = None,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    ro_files: Sequence[str | Path] = (),
) -> RunResult:
    """Build a RunRequest and execute the manifest in LocalJailBackend."""
    command = getattr(manifest, "command", None)
    if command is None:
        command = getattr(manifest, "argv", None)
    if command is None and isinstance(manifest, Mapping):
        command = manifest.get("command", manifest.get("argv", ()))
    argv = tuple(str(x) for x in (command or ()))

    device = getattr(manifest, "device", None)
    if device is None and isinstance(manifest, Mapping):
        device = manifest.get("device", "cpu")
    device_str = str(device or "cpu").lower()

    if device_str == "gpu":
        capabilities = frozenset({"gpu"})
    elif device_str == "cpu":
        capabilities = frozenset({"cpu"})
    else:
        capabilities = frozenset({device_str})

    if timeout is None and hasattr(manifest, "timeout"):
        timeout = getattr(manifest, "timeout")

    req_workdir = Path(workdir).resolve()

    request = RunRequest(
        argv=argv,
        workdir=req_workdir,
        ro_files=ro_files,
        env=env,
        timeout=timeout,
        capabilities=capabilities,
    )

    if backend is None:
        backend = LocalJailBackend()

    return backend.run(request, cancel=cancel)
