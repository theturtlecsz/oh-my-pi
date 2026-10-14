#!/usr/bin/env bash
# Staging only. Installation is deliberately unwired until OMP-584-s03.
set -euo pipefail

OMP584_ASSETS=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
OMP584_ROOT=$(cd -- "$OMP584_ASSETS/../.." && pwd)

omp584_preflight() {
    OMP584_FLOOD_DIR= OMP584_EVIDENCE_DIR=
    while (($#)); do
        case "$1" in
            --flood-dir|--evidence-dir)
                if (($# < 2)) || [[ -z "$2" || "$2" == --* ]]; then
                    echo "Missing value for $1" >&2; return 2
                fi
                if [[ "$1" == --flood-dir ]]; then
                    [[ -z "$OMP584_FLOOD_DIR" ]] || { echo 'Duplicate --flood-dir' >&2; return 2; }
                    OMP584_FLOOD_DIR=$2
                else
                    [[ -z "$OMP584_EVIDENCE_DIR" ]] || { echo 'Duplicate --evidence-dir' >&2; return 2; }
                    OMP584_EVIDENCE_DIR=$2
                fi
                shift 2 ;;
            *) echo "Unknown argument: $1" >&2; return 2 ;;
        esac
    done
    [[ -n "$OMP584_FLOOD_DIR" && -n "$OMP584_EVIDENCE_DIR" ]] || {
        echo 'Required: --flood-dir DIR --evidence-dir DIR' >&2; return 2;
    }
    local tool artifact
    for tool in git python3 uv mktemp realpath; do
        command -v "$tool" >/dev/null || { echo "Missing tool: $tool" >&2; return 2; }
    done
    OMP584_FLOOD_DIR=$(realpath -e -- "$OMP584_FLOOD_DIR") || return 2
    OMP584_EVIDENCE_DIR=$(realpath -m -- "$OMP584_EVIDENCE_DIR") || return 2
    [[ -s "$OMP584_FLOOD_DIR/flood.py" ]] || { echo 'Missing/empty flood.py' >&2; return 2; }
    # Evidence must never write into the supplied checkout (or replace an ancestor).
    case "$OMP584_EVIDENCE_DIR/" in "$OMP584_FLOOD_DIR/"*) echo 'Evidence overlaps source' >&2; return 2 ;; esac
    case "$OMP584_FLOOD_DIR/" in "$OMP584_EVIDENCE_DIR/"*) echo 'Evidence overlaps source' >&2; return 2 ;; esac
    [[ ! -e "$OMP584_EVIDENCE_DIR" || -d "$OMP584_EVIDENCE_DIR" ]] || {
        echo 'Evidence must be a directory' >&2; return 2;
    }
    # The canonical planner patch may also appear under its legacy alias.
    OMP584_PROMPT_PATCH=omp584-planner-prompt.patch
    [[ -s "$OMP584_ASSETS/$OMP584_PROMPT_PATCH" ]] || OMP584_PROMPT_PATCH=omp584-prompt.patch
    for artifact in omp584-dependencies.patch omp584-start-metrics.patch "$OMP584_PROMPT_PATCH" omp584-planner-rules.md; do
        [[ -s "$OMP584_ASSETS/$artifact" ]] || { echo "Missing/empty artifact: $artifact" >&2; return 2; }
    done
}

omp584_stage() {
    OMP584_STAGE=$(mktemp -d)
    cp -- "$OMP584_FLOOD_DIR/flood.py" "$OMP584_STAGE/flood.py"
    cp -- "$OMP584_ASSETS/omp584-planner-rules.md" "$OMP584_STAGE/planner-independence.md"
    # The planner prompt patch loads the sibling rules under their legacy filename.
    ln -s planner-independence.md "$OMP584_STAGE/omp584-planner-rules.md"
    local patches=(omp584-dependencies.patch omp584-start-metrics.patch "$OMP584_PROMPT_PATCH")
    local i patch
    # Normalize exact installations in reverse composition order. git apply is
    # atomic per patch, has no fuzz, and rejects partial installations.
    for ((i=${#patches[@]}-1; i>=0; i--)); do
        patch=$OMP584_ASSETS/${patches[i]}
        if git -C "$OMP584_STAGE" apply --reverse --check "$patch" 2>/dev/null; then
            git -C "$OMP584_STAGE" apply --reverse "$patch" || return
        fi
    done
    for patch in "${patches[@]}"; do
        git -C "$OMP584_STAGE" apply --check "$OMP584_ASSETS/$patch" || return
        git -C "$OMP584_STAGE" apply "$OMP584_ASSETS/$patch" || return
    done
}

omp584_verify() (
    mkdir -p -- "$OMP584_EVIDENCE_DIR"
    python3 - "$OMP584_STAGE/flood.py" >"$OMP584_EVIDENCE_DIR/compile.log" 2>&1 <<'PY' || return
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
compile(path.read_bytes(), str(path), 'exec')
print('Candidate compiles without writing bytecode')
PY
    # Select only combined tests, never the tests which invoke this rollout.
    # Own pytest's temp tree: shared retention or inherited --basetemp can
    # otherwise delete the invoking test's fixtures during qualification.
    # Keep it outside the candidate tree whose source snapshots must stay fixed.
    local pytest_temp
    pytest_temp=$(mktemp -d) || return
    trap 'rm -rf -- "$pytest_temp"' EXIT
    OMP584_FLOOD_SOURCE="$OMP584_STAGE" PYTHONDONTWRITEBYTECODE=1 \
        uv run --project "$OMP584_ROOT/python/omp-work" --extra dev pytest \
        "$OMP584_ROOT/python/omp-work/tests/test_flood_planner_rollout.py" \
        "$OMP584_ROOT/python/omp-work/tests/test_flood_rollout_provenance.py" \
        --basetemp "$pytest_temp/pytest" \
        -k 'test_combined_ or not test_flood_planner_rollout' -rs \
        >"$OMP584_EVIDENCE_DIR/pytest.log" 2>&1
)

omp584_main() (
    omp584_preflight "$@" || exit $?
    trap 'if [[ -n "${OMP584_STAGE:-}" ]]; then rm -rf -- "$OMP584_STAGE"; fi' EXIT
    omp584_stage || exit $?
    omp584_verify || exit $?
    echo "Staging verified; installation unwired. Evidence: $OMP584_EVIDENCE_DIR"
)

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    omp584_main "$@"
fi
