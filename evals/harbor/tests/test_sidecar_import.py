"""Sidecar import at shallow directory depth."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def test_sidecar_import() -> None:
    d = tempfile.mkdtemp(dir="/tmp")
    d_path = Path(d)
    try:
        shutil.copytree(SRC / "omp_harbor_eval", d_path / "omp_harbor_eval")
        assert len(Path(d_path / "omp_harbor_eval" / "verify.py").resolve().parents) <= 4
        env = dict(os.environ)
        env["PYTHONPATH"] = str(d_path)
        proc = subprocess.run(  # nosec B603
            [
                sys.executable,
                "-c",
                "import omp_harbor_eval.scripted_model as m; print(m.__file__)",
            ],
            cwd=str(d_path),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        printed_path = Path(proc.stdout.strip()).resolve()
        assert printed_path.is_relative_to(d_path.resolve())
    finally:
        shutil.rmtree(d, ignore_errors=True)
