"""Trusted jail entrypoint. Candidate loading stays exclusively in strategy_protocol."""

from __future__ import annotations

import json
import resource
import sys
from pathlib import Path

from omp_work.research.strategy_protocol import MAX_PROPOSAL_BYTES, run_candidate


def main() -> None:
    invocation = json.loads(Path("/work/input.json").read_bytes())
    resource.setrlimit(resource.RLIMIT_AS, (invocation["memory_bytes"],) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (invocation["cpu_seconds"],) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_PROPOSAL_BYTES,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    result = run_candidate(
        invocation["source"], invocation["steps"], invocation["opened"],
        seed=invocation["seed"], width=invocation["width"],
        allowed_actions=invocation["allowed_actions"], resource_caps=invocation["resource_caps"],
    )
    if result["status"] == "failed":
        response = {"failure": result["reason"]}
    else:
        # Controller revalidates against its original snapshot even if candidate
        # mutated the jail's copy before run_candidate performed validation.
        response = {"proposal": result["proposal"], "seed": result["seed"]}
    encoded = json.dumps(response, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_PROPOSAL_BYTES:
        encoded = b'{"failure":"output_too_large"}'
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    main()
