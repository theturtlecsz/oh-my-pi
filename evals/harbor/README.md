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

## Build the fixture images first

`harbor run` builds its `main` service from the task's prebuilt
`[environment].docker_image` and starts the fixture's compose services from
local images, so build every image the f1 and f2 environments reference once
before the first trial. The command needs no network credentials and spends no
model tokens:

```bash
bash evals/harbor/scripts/build-images.sh
```

It builds `omp-workservice:dev` (PostgreSQL 18 plus the real
`python/omp-work` package), `omp-agent:dev` (the worker tree with the `omp`
shim; the image stays up so Harbor can `docker exec` into it),
`omp-verifier:dev`, and tags `omp-f1-agent:dev`. The same command writes a
credential bundle to `$HOME/.local/state/omp/harbor-fixtures` with the Python
standard library. Running the command again keeps that token; delete the
directory first when it should be replaced. It does not call a model and it
does not need network credentials. The workservice mounts that directory read-only at
`/run/omp/supplied` and refuses to start if the files are missing. It does not
generate a token. `python -m omp_harbor_eval.build_recipe
evals/harbor/fixtures/f1 evals/harbor/fixtures/f2` fails if a fixture
references an image with no recipe, lacks what Harbor needs for its main
service, or the worker image, credential mount, or database authentication
does not match this contract.

A Harbor trial uses `--agent-import-path omp_harbor_eval.harbor_agent:OmpRpcAgent`
and four variables: `OMP_HARBOR_BEARER`, `OMP_HARBOR_WORKSPACE_ID`,
`OMP_HARBOR_RUN_ID`, and `OMP_HARBOR_NONCE`. The bearer and workspace id
authorize the WorkService read. The run id and nonce are the evidence identity
the grader checks out of band. Source the bundle's `harbor.env` before
`harbor run` or `interactive_env`. `OMP_HARBOR_BEARER` is the `token` field
from `capabilities/owner.json`, which is the value the server compares to the
`Authorization` header. It is not the JSON file. `OMP_HARBOR_WORKSPACE_ID` is
the workspace id in that same file. `interactive_env` does not open the file;
both variables must already be set, or it exits before starting a container.

```bash
set -a
. "$HOME/.local/state/omp/harbor-fixtures/harbor.env"
set +a

OMP_HARBOR_RUN_ID=... OMP_HARBOR_NONCE=... \
  harbor run --agent-import-path omp_harbor_eval.harbor_agent:OmpRpcAgent -p evals/harbor/fixtures/<id>

python -m omp_harbor_eval.verify --evidence DIR --fixture ID --manifest-sha256 SHA --out FILE

python -m omp_harbor_eval.interactive_env --fixture ID --out DIR

python -m omp_harbor_eval.grader grade --help
python -m omp_harbor_eval.grader validate --help
```

`interactive_env` brings the fixture's workservice and worker up for one
interactive omp session, then writes `service-readback.json` and copies the
session directory into `DIR/session`. `verify` runs the fixture's independent
tests against the sealed bundle and grades that evidence. `grader` prints one
JSON object. This package does not call a model.
