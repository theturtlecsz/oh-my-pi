#!/usr/bin/env bash
# Qualify and commit only the two deployment targets; activation is a separate gate.
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
    omp584_check_targets || return 2
    OMP584_SOURCE_HEAD=$(git -C "$OMP584_FLOOD_DIR" rev-parse HEAD) || return 2
    # The canonical planner patch may also appear under its legacy alias.
    OMP584_PROMPT_PATCH=omp584-planner-prompt.patch
    [[ -s "$OMP584_ASSETS/$OMP584_PROMPT_PATCH" ]] || OMP584_PROMPT_PATCH=omp584-prompt.patch
    for artifact in omp584-dependencies.patch omp584-start-metrics.patch "$OMP584_PROMPT_PATCH" omp584-planner-rules.md; do
        [[ -s "$OMP584_ASSETS/$artifact" ]] || { echo "Missing/empty artifact: $artifact" >&2; return 2; }
    done
}

omp584_check_targets() {
    local root dirty target flags
    root=$(git -C "$OMP584_FLOOD_DIR" rev-parse --show-toplevel) || return
    [[ "$root" == "$OMP584_FLOOD_DIR" ]] || { echo 'Require checkout root' >&2; return 2; }
    case "$root/" in "$OMP584_ROOT/"*) echo 'Require external checkout' >&2; return 2 ;; esac
    case "$OMP584_ROOT/" in "$root/"*) echo 'Require external checkout' >&2; return 2 ;; esac
    for target in planner-independence.md flood.py; do
        [[ ! -L "$root/$target" && ( ! -e "$root/$target" || -f "$root/$target" ) ]] || {
            echo "Require regular target: $target" >&2; return 2;
        }
    done
    git -C "$root" ls-files --error-unmatch -- flood.py >/dev/null || return
    flags=$(git -C "$root" ls-files -v -- flood.py planner-independence.md) || return
    # Skip-worktree/assume-unchanged can hide operator edits from status.
    while IFS= read -r target; do
        [[ "$target" == H\ * ]] || { echo 'Hidden target index state' >&2; return 2; }
    done <<< "$flags"
    dirty=$(git --no-optional-locks -C "$root" status --porcelain --untracked-files=all --ignored -- \
        flood.py planner-independence.md) || return
    [[ -z "$dirty" ]] || { echo 'Dirty deployment targets' >&2; return 2; }
}

omp584_stage() {
    OMP584_STAGE=$(mktemp -d) || return
    cp -p -- "$OMP584_FLOOD_DIR/flood.py" "$OMP584_STAGE/original-flood.py" || return
    cp -p -- "$OMP584_STAGE/original-flood.py" "$OMP584_STAGE/flood.py" || return
    # Normalize the installed canonical spelling for the legacy patch composer.
    python3 - "$OMP584_STAGE/flood.py" <<'PYCODE' || return
import pathlib, sys
p = pathlib.Path(sys.argv[1])
p.write_bytes(p.read_bytes().replace(b'with_name("planner-independence.md")',
                                    b'with_name("omp584-planner-rules.md")'))
PYCODE
    cp -- "$OMP584_ASSETS/omp584-planner-rules.md" "$OMP584_STAGE/planner-independence.md" || return
    # The planner prompt patch loads the sibling rules under their legacy filename.
    ln -s planner-independence.md "$OMP584_STAGE/omp584-planner-rules.md" || return
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
        >"$OMP584_EVIDENCE_DIR/pytest.log" 2>&1 || return
    # Qualify the exact canonical deployment bytes, without recomposing them.
    python3 - "$OMP584_STAGE/flood.py" <<'PYCODE' || return
import pathlib, sys
p = pathlib.Path(sys.argv[1])
p.write_bytes(p.read_bytes().replace(b'with_name("omp584-planner-rules.md")',
                                    b'with_name("planner-independence.md")'))
PYCODE
    OMP584_FLOOD_SOURCE="$OMP584_STAGE" OMP584_TEST_EXACT=1 PYTHONDONTWRITEBYTECODE=1 \
        uv run --project "$OMP584_ROOT/python/omp-work" --extra dev pytest \
        "$OMP584_ROOT/python/omp-work/tests/test_flood_planner_rollout.py" \
        --basetemp "$pytest_temp/exact" -k test_combined_ -rs \
        >"$OMP584_EVIDENCE_DIR/pytest-exact.log" 2>&1
)

# Separate replacement seam permits fault injection without touching services.
omp584_replace() {
    mv -f -- "$1" "$2"
}

omp584_install() (
    omp584_check_targets || exit $?
    [[ "$(git -C "$OMP584_FLOOD_DIR" rev-parse HEAD)" == "$OMP584_SOURCE_HEAD" ]] || {
        echo 'Checkout changed during qualification' >&2; exit 2;
    }
    cmp -s -- "$OMP584_STAGE/original-flood.py" "$OMP584_FLOOD_DIR/flood.py" || exit 2
    local transaction target scratch index_path original_head blob committed=0 writes_started=0
    local -a scratches=()
    transaction=$(mktemp -d) || exit
    original_head=$OMP584_SOURCE_HEAD
    trap '
        rc=$?
        if (( writes_started && ! committed )) && [[ "$(git -C "$OMP584_FLOOD_DIR" rev-parse HEAD)" == "$original_head" ]]; then
            for target in planner-independence.md flood.py; do
                if [[ -f "$transaction/$target" ]]; then
                    cp -p -- "$transaction/$target" "$OMP584_FLOOD_DIR/$target" || rc=1
                elif [[ -f "$transaction/$target.absent" ]]; then
                    rm -f -- "$OMP584_FLOOD_DIR/$target" || rc=1
                fi
            done
        fi
        for scratch in "${scratches[@]}"; do rm -f -- "$scratch"; done
        rm -rf -- "$transaction"
        exit "$rc"
    ' EXIT
    # Snapshot presence, bytes, modes and index before any target write.
    for target in planner-independence.md flood.py; do
        if [[ -e "$OMP584_FLOOD_DIR/$target" ]]; then
            cp -p -- "$OMP584_FLOOD_DIR/$target" "$transaction/$target" || exit
        else
            touch "$transaction/$target.absent" || exit
        fi
    done
    index_path=$(git -C "$OMP584_FLOOD_DIR" rev-parse --path-format=absolute --git-path index) || exit
    cp -p -- "$index_path" "$transaction/index" || exit
    omp584_check_targets || exit $?
    [[ "$(git -C "$OMP584_FLOOD_DIR" rev-parse HEAD)" == "$original_head" ]] || exit 2
    writes_started=1
    # Isolated index keeps even failed commit hooks away from caller staging.
    for target in planner-independence.md flood.py; do
        scratch=$(mktemp "$OMP584_FLOOD_DIR/$target.omp584-new.XXXXXX") || exit
        scratches+=("$scratch")
        cp -p -- "$OMP584_STAGE/$target" "$scratch" || exit
        if [[ -f "$transaction/$target" ]]; then
            chmod --reference="$transaction/$target" "$scratch" || exit
        fi
        omp584_replace "$scratch" "$OMP584_FLOOD_DIR/$target" || exit
    done
    GIT_INDEX_FILE="$transaction/index" git -C "$OMP584_FLOOD_DIR" add -- \
        planner-independence.md flood.py || exit
    for target in planner-independence.md flood.py; do
        blob=$(git hash-object --no-filters -- "$OMP584_STAGE/$target") || exit
        [[ "$(GIT_INDEX_FILE="$transaction/index" git -C "$OMP584_FLOOD_DIR" rev-parse ":$target")" == "$blob" ]] || {
            echo "Git filtering changed tested bytes: $target" >&2; exit 2;
        }
    done
    GIT_INDEX_FILE="$transaction/index" git -C "$OMP584_FLOOD_DIR" commit --only \
        -m 'OMP-584: install qualified planner independence and start metrics' -- \
        planner-independence.md flood.py || exit
    committed=1
    # Update only target entries in caller index after the deployment commit.
    git -C "$OMP584_FLOOD_DIR" reset -q HEAD -- planner-independence.md flood.py || exit
)

omp584_main() (
    omp584_preflight "$@" || exit $?
    trap 'if [[ -n "${OMP584_STAGE:-}" ]]; then rm -rf -- "$OMP584_STAGE"; fi' EXIT
    omp584_stage || exit $?
    omp584_verify || exit $?
    omp584_install || exit $?
    echo "Deployment committed; activation verification pending. Evidence: $OMP584_EVIDENCE_DIR" >&2
    exit 1
)

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    omp584_main "$@"
fi
