"""The existing cmdline scanner must reject a readable own-helper leak."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
from unittest.mock import patch

import pytest

from egress_sandbox_procfs_support import intercept_helper
import test_egress_sandbox as sandbox_tests
from omp_work import egress_sandbox_helper as helper


_HELPER_PID_ENV = "OMP_TEST_OWN_HELPER_PID"
_READABLE_MARKER = b"own-helper-readable"


def bootstrap(_interceptor: str) -> None:
    """Run the real helper with host proc inherited only for this fault."""
    real_popen = subprocess.Popen
    os.environ[_HELPER_PID_ENV] = str(os.getpid())

    def worker_popen(argv, *args, **kwargs):
        if "--mount-proc" in argv:
            # Keep the PID namespace, lifecycle flags and setpriv capability drop.
            argv = list(argv)
            argv.remove("--mount-proc")
        return real_popen(argv, *args, **kwargs)

    # The launch support retains the module name in the process's actual argv.
    sys.argv = sys.argv[1:]
    with (
        patch.object(helper, "_mask_helper_cmdlines"),
        patch.object(helper, "_ensure_worker_unshare_supported", return_value={}),
        patch.object(subprocess, "Popen", side_effect=worker_popen),
    ):
        helper.main()


@pytest.mark.skipif(sandbox_tests._unshare_probe_fails(), reason="unshare probe failed")
def test_existing_scanner_detects_readable_own_helper(monkeypatch, tmp_path: Path) -> None:
    real_run = sandbox_tests.run_sandboxed
    results: list[tuple[int, bytes]] = []

    def run_scanner(argv, *args, **kwargs):
        output = bytearray()
        rc = real_run(argv, *args, **kwargs, sockets_root=tmp_path / "sockets", output=output)
        results.append((rc, bytes(output)))
        return rc

    with monkeypatch.context() as control:
        control.setattr(sandbox_tests, "run_sandboxed", run_scanner)
        sandbox_tests.test_no_proc_cmdline_names_egress_sandbox_helper()
    assert results[0][0] == 0, results[0][1].decode(errors="replace")
    results.clear()

    # Reuse the support's outer launch interception and argv preservation,
    # selecting this fault's bootstrap instead of its mount-error bootstrap.
    with monkeypatch.context() as fault:
        original_popen = subprocess.Popen

        def helper_popen(argv, *args, **kwargs):
            if "omp_work.egress_sandbox_helper" in argv:
                argv = list(argv)
                code_index = argv.index("-c") + 1
                argv[code_index] = argv[code_index].replace(
                    "from egress_sandbox_procfs_support import bootstrap",
                    "from test_egress_sandbox_helper_leak import bootstrap",
                )
            return original_popen(argv, *args, **kwargs)

        fault.setattr(subprocess, "Popen", helper_popen)
        intercept_helper(fault, Path(__file__))

        def run_leaking_scanner(argv, *args, **kwargs):
            proof = (
                "import os\n"
                "from pathlib import Path\n"
                f"pid = int(os.environ[{_HELPER_PID_ENV!r}])\n"
                "cmdline = Path(f'/proc/{pid}/cmdline').read_bytes()\n"
                "assert ('egress' + '_sandbox_helper').encode() in cmdline\n"
                f"print({_READABLE_MARKER.decode()!r}, flush=True)\n"
            )
            command = f"python3 -c {shlex.quote(proof)} && exec {shlex.join(argv)}"
            return run_scanner(["sh", "-c", command], *args, **kwargs)

        fault.setattr(sandbox_tests, "run_sandboxed", run_leaking_scanner)
        with pytest.raises(AssertionError):
            sandbox_tests.test_no_proc_cmdline_names_egress_sandbox_helper()

    assert len(results) == 1
    rc, output = results[0]
    assert _READABLE_MARKER in output, output.decode(errors="replace")
    assert rc == 1, output.decode(errors="replace")
