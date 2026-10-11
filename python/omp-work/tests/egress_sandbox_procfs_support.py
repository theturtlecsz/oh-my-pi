"""Per-launch fault injection for the real egress sandbox helper."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from omp_work import egress_sandbox_helper as helper


def bootstrap(interceptor: str) -> None:
    """Force the probe through the next-mount interceptor, then chain the shim."""
    real_run = subprocess.run
    real_ensure = helper._ensure_worker_unshare_supported

    def probe_run(argv, **kwargs):
        if "--mount-proc" in argv:
            kwargs["env"] = {**os.environ, "LD_PRELOAD": interceptor}
        return real_run(argv, **kwargs)

    def ensure(temp_dir: Path) -> dict[str, str]:
        with patch.object(subprocess, "run", side_effect=probe_run):
            env = real_ensure(temp_dir)
        wrapper = temp_dir / "shim" / "unshare"
        shim = temp_dir / "shim" / "shim.so"
        wrapper.write_text(wrapper.read_text().replace(str(shim), f"{shim}:{interceptor}"))
        return env

    # python -c keeps the helper module marker and config path in its cmdline.
    sys.argv = sys.argv[1:]
    with patch.object(helper, "_ensure_worker_unshare_supported", side_effect=ensure):
        helper.main()


def intercept_helper(monkeypatch, interceptor: Path) -> None:
    """Patch only helper launches; retain outer namespace flags and config argv."""
    real_popen = subprocess.Popen
    support_dir = str(Path(__file__).parent)
    code = (
        f"import sys; sys.path.insert(0, {support_dir!r}); "
        "from egress_sandbox_procfs_support import bootstrap; "
        f"bootstrap({str(interceptor)!r})"
    )

    def popen(argv, *args, **kwargs):
        if "omp_work.egress_sandbox_helper" in argv:
            argv = list(argv)
            module_index = argv.index("omp_work.egress_sandbox_helper")
            assert argv[module_index - 1] == "-m"
            argv[module_index - 1:module_index] = ["-c", code]
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
