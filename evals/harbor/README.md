# omp-harbor-eval

Host-side evidence, fixtures, and grader for Harbor evaluation runs. The scored
experiment is `repair`. Fixture f2's kill is `harness_crash_injection` and is
unscored. M3 controls stay with their owners. No service-evidence qualification
is claimed.

The score is `grade` over a sealed evidence directory. Harbor's in-container
verifier is not the score. `seal` writes `manifest.json` (every other file's
sha256) and returns the sha256 of those manifest bytes. That digest is passed
out of band; `load_evidence` trusts it, then checks each file hash. A missing,
extra, or altered file, or a run id, nonce, or fixture digest that does not
match the graded fixture, is `invalid_evidence`. An outcome of `harness_error`
is `harness_defect`.

A Harbor trial uses `--agent-import-path omp_harbor_eval.harbor_agent:OmpRpcAgent`
and four variables: `OMP_HARBOR_BEARER`, `OMP_HARBOR_WORKSPACE_ID`,
`OMP_HARBOR_RUN_ID`, and `OMP_HARBOR_NONCE`. The bearer and workspace id
authorize the WorkService read. The run id and nonce are the evidence identity
the grader checks out of band.

```bash
OMP_HARBOR_BEARER=... OMP_HARBOR_WORKSPACE_ID=... OMP_HARBOR_RUN_ID=... OMP_HARBOR_NONCE=... \
  harbor run --agent-import-path omp_harbor_eval.harbor_agent:OmpRpcAgent -p evals/harbor/fixtures/<id>

python -m omp_harbor_eval.verify --evidence DIR --fixture ID --manifest-sha256 SHA --out FILE

OMP_HARBOR_BEARER=... OMP_HARBOR_WORKSPACE_ID=... \
  python -m omp_harbor_eval.interactive_env --fixture ID --out DIR

python -m omp_harbor_eval.grader grade --help
python -m omp_harbor_eval.grader validate --help
```

`interactive_env` brings the fixture's workservice and worker up for one
interactive omp session, then writes `service-readback.json` and copies the
session directory into `DIR/session`. `verify` runs the fixture's independent
tests against the sealed bundle and grades that evidence. `grader` prints one
JSON object. This package does not call a model.
