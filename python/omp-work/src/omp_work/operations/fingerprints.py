"""Stable fingerprints binding a cutover manifest to the exact code, transform, and config that produced it."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path

from .config import OperationsConfig

_TRANSFORM_MODULES = ("importer.py", "legacy_artifacts.py")


def _sha256_json(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def code_fingerprint() -> str:
    root = Path(str(files("omp_work")))
    entries = [
        (str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]
    return _sha256_json(entries)


def service_runtime_fingerprint() -> str:
    from .database import migration_set_sha256

    return _sha256_json(
        {
            "code_fingerprint": code_fingerprint(),
            "migration_set_sha256": migration_set_sha256(),
        }
    )


def transform_sha256() -> str:
    from omp_work.integration.importer import TRANSFORMATION_VERSION

    root = Path(str(files("omp_work"))) / "integration"
    modules = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in _TRANSFORM_MODULES
    }
    return _sha256_json({"modules": modules, "version": TRANSFORMATION_VERSION})


def config_fingerprint(config: OperationsConfig) -> str:
    return _sha256_json(
        {
            "aws_region": config.aws_region,
            "bucket": config.bucket,
            "database": config.database,
            "host": config.host,
            "port": config.port,
            "prefix": config.prefix,
        }
    )
