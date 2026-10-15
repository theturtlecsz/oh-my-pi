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

    local parts_out
    # Classifier diagnostics pass through stderr; conflicts (exit 3) fail staging.
    parts_out=$(python3 "$OMP584_ASSETS/omp584-parts.py" "$OMP584_STAGE/flood.py") || return 2

    local prompt_patch=${OMP584_PROMPT_PATCH:-omp584-planner-prompt.patch}
    [[ -s "$OMP584_ASSETS/$prompt_patch" ]] || prompt_patch=omp584-prompt.patch

    local -A patch_files=(
        [dependencies]="omp584-dependencies.patch"
        [start-metrics]="omp584-start-metrics.patch"
        [planner-prompt]="$prompt_patch"
    )

    local -A states=()
    local part state
    while read -r part state; do
        [[ -n "$part" ]] || continue
        states["$part"]="$state"
    done <<< "$parts_out"

    local -a applied_parts=()
    local -a part_order=(dependencies start-metrics planner-prompt)
    local patch

    for part in "${part_order[@]}"; do
        state=${states["$part"]:-missing}
        if [[ "$state" == "installed" ]]; then
            echo "part $part: present" >&2
        elif [[ "$state" == "missing" ]]; then
            patch=${patch_files["$part"]}
            if ! git -C "$OMP584_STAGE" apply --check "$OMP584_ASSETS/$patch" 2>/dev/null; then
                echo "error: part $part is missing and its patch does not apply" >&2
                return 2
            fi
            git -C "$OMP584_STAGE" apply "$OMP584_ASSETS/$patch" || return 2
            applied_parts+=("$part")
            echo "part $part: applied" >&2
        else
            return 2
        fi
    done

    > "$OMP584_STAGE/applied-parts"
    for part in "${applied_parts[@]}"; do
        echo "$part" >> "$OMP584_STAGE/applied-parts"
    done

    parts_out=$(python3 "$OMP584_ASSETS/omp584-parts.py" "$OMP584_STAGE/flood.py") || return 2
    states=()
    while read -r part state; do
        [[ -n "$part" ]] || continue
        states["$part"]="$state"
    done <<< "$parts_out"
    for part in "${part_order[@]}"; do
        [[ "${states["$part"]:-missing}" == installed ]] || return 2
    done

    local scratch_dir="$OMP584_STAGE/legacy-scratch"
    mkdir -- "$scratch_dir" || return
    cp -p -- "$OMP584_STAGE/original-flood.py" "$scratch_dir/flood.py" || return
    python3 - "$scratch_dir/flood.py" <<'PYCODE' || return
import pathlib, sys
p = pathlib.Path(sys.argv[1])
p.write_bytes(p.read_bytes().replace(b'with_name("planner-independence.md")',
                                    b'with_name("omp584-planner-rules.md")'))
PYCODE

    local legacy_ok=1
    local legacy_patches=(omp584-dependencies.patch omp584-start-metrics.patch "$prompt_patch")
    local i p_name
    for ((i=${#legacy_patches[@]}-1; i>=0; i--)); do
        p_name=${legacy_patches[i]}
        if git -C "$scratch_dir" apply --reverse --check "$OMP584_ASSETS/$p_name" 2>/dev/null; then
            if ! git -C "$scratch_dir" apply --reverse "$OMP584_ASSETS/$p_name" 2>/dev/null; then
                legacy_ok=0
                break
            fi
        fi
    done
    if (( legacy_ok )); then
        for p_name in "${legacy_patches[@]}"; do
            if ! git -C "$scratch_dir" apply --check "$OMP584_ASSETS/$p_name" 2>/dev/null || \
               ! git -C "$scratch_dir" apply "$OMP584_ASSETS/$p_name" 2>/dev/null; then
                legacy_ok=0
                break
            fi
        done
    fi
    rm -rf -- "$scratch_dir"
    if (( legacy_ok )); then
        touch "$OMP584_STAGE/legacy-composable"
    fi
    return 0
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

    local -a pytest_files=(
        "$OMP584_ROOT/python/omp-work/tests/test_omp622_exact.py"
    )
    local -a pytest_k=()
    if [[ -f "$OMP584_STAGE/legacy-composable" ]]; then
        pytest_files+=(
            "$OMP584_ROOT/python/omp-work/tests/test_flood_planner_rollout.py"
            "$OMP584_ROOT/python/omp-work/tests/test_flood_rollout_provenance.py"
        )
        pytest_k=(-k 'test_combined_ or not test_flood_planner_rollout')
    fi
    OMP584_FLOOD_SOURCE="$OMP584_STAGE" PYTHONDONTWRITEBYTECODE=1 \
        uv run --project "$OMP584_ROOT/python/omp-work" --extra dev pytest \
        "${pytest_files[@]}" \
        --basetemp "$pytest_temp/pytest" \
        ${pytest_k[@]+"${pytest_k[@]}"} -rs \
        >"$OMP584_EVIDENCE_DIR/pytest.log" 2>&1 || return

    # Qualify the exact canonical deployment bytes, without recomposing them.
    python3 - "$OMP584_STAGE/flood.py" <<'PYCODE' || return
import pathlib, sys
p = pathlib.Path(sys.argv[1])
p.write_bytes(p.read_bytes().replace(b'with_name("omp584-planner-rules.md")',
                                    b'with_name("planner-independence.md")'))
PYCODE

    local -a exact_files=(
        "$OMP584_ROOT/python/omp-work/tests/test_omp622_exact.py"
    )
    local -a exact_k=()
    if [[ -f "$OMP584_STAGE/legacy-composable" ]]; then
        exact_files+=(
            "$OMP584_ROOT/python/omp-work/tests/test_flood_planner_rollout.py"
        )
        exact_k=(-k 'test_combined_ or not test_flood_planner_rollout')
    fi
    OMP584_FLOOD_SOURCE="$OMP584_STAGE" OMP584_TEST_EXACT=1 PYTHONDONTWRITEBYTECODE=1 \
        uv run --project "$OMP584_ROOT/python/omp-work" --extra dev pytest \
        "${exact_files[@]}" \
        --basetemp "$pytest_temp/exact" \
        ${exact_k[@]+"${exact_k[@]}"} -rs \
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
    if [[ -f "$OMP584_FLOOD_DIR/flood.log" ]]; then
        wc -c < "$OMP584_FLOOD_DIR/flood.log" > "$OMP584_STAGE/flood.log.offset" || exit
    else
        echo 0 > "$OMP584_STAGE/flood.log.offset"
    fi
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
    target_commit=$(git -C "$OMP584_FLOOD_DIR" rev-parse HEAD) || exit
    echo "$target_commit" > "$OMP584_STAGE/target_commit"
    # Update only target entries in caller index after the deployment commit.
    git -C "$OMP584_FLOOD_DIR" reset -q HEAD -- planner-independence.md flood.py || exit
)

omp584_activate() (
    local target_commit code_sha256 rules_sha256 offset=0
    [[ -s "$OMP584_STAGE/target_commit" ]] || {
        echo "Missing target commit for activation" >&2
        return 1
    }
    target_commit=$(cat "$OMP584_STAGE/target_commit")
    code_sha256=$(sha256sum "$OMP584_STAGE/flood.py" | awk '{print $1}')
    rules_sha256=$(sha256sum "$OMP584_STAGE/planner-independence.md" | awk '{print $1}')
    if [[ -f "$OMP584_STAGE/flood.log.offset" ]]; then
        offset=$(cat "$OMP584_STAGE/flood.log.offset")
    fi

    # Start the monotonic budget inside the matcher, after interpreter startup.
    # Always scan once: scheduling delays must not skip already-written proof.
    python3 - "$OMP584_FLOOD_DIR/flood.log" "$offset" "$code_sha256" "$rules_sha256" "$target_commit" \
               "$OMP584_STAGE/matched_record.json" "$OMP584_EVIDENCE_DIR/reload.log" \
               "${OMP584_POLL_TIMEOUT:-3}" <<'PY' || return 1
import sys, json, pathlib, time

log_path = pathlib.Path(sys.argv[1])
offset = int(sys.argv[2])
expected = {
    "event": "loaded",
    "code_sha256": sys.argv[3],
    "rules_sha256": sys.argv[4],
    "external_head": sys.argv[5],
}
record_out = pathlib.Path(sys.argv[6])
reload_out = pathlib.Path(sys.argv[7])
deadline = time.monotonic() + float(sys.argv[8])


def scan():
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            lines = f.readlines()
    except FileNotFoundError:
        return None, [], f"{log_path} not found (offset {offset})"
    except OSError as exc:
        return None, [], f"cannot read {log_path} at offset {offset}: {exc}"

    reason = f"no reload line after offset {offset} ({len(lines)} lines read)"
    current_reload_lines = None
    for line in lines:
        if "[reload] flood.py or flood.json changed: exec in place" in line:
            current_reload_lines = [line]
            reason = "reload observed, but no loaded record followed"
            continue
        if current_reload_lines is None:
            continue
        current_reload_lines.append(line)
        if " loaded " not in line:
            continue
        try:
            rec = json.loads(line.split(" loaded ", 1)[1])
        except json.JSONDecodeError as exc:
            reason = f"invalid loaded record: {exc}"
            continue
        if not isinstance(rec, dict):
            reason = f"loaded record is not an object: {rec!r}"
            continue
        mismatches = [
            f"{key}: expected {value!r}, got {rec.get(key)!r}"
            for key, value in expected.items() if rec.get(key) != value
        ]
        if not isinstance(rec.get("at"), str):
            mismatches.append(f"at: expected string, got {rec.get('at')!r}")
        if mismatches:
            reason = "last loaded record mismatch: " + "; ".join(mismatches)
            continue
        return rec, current_reload_lines, ""
    return None, [], reason


while True:
    matched_record, reload_lines, reason = scan()
    if matched_record is not None:
        record_out.write_text(json.dumps(matched_record, indent=2) + "\n")
        reload_out.write_text("".join(reload_lines))
        break
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        print("Activation timeout: matching reload and loaded record not observed in time; " + reason,
              file=sys.stderr)
        sys.exit(1)
    time.sleep(min(0.1, remaining))
PY

    # After wait verify both disk targets and committed blobs equal tested bytes; drift/timeout fails without successful rollout.json.
    cmp -s -- "$OMP584_STAGE/flood.py" "$OMP584_FLOOD_DIR/flood.py" || {
        echo "Disk target flood.py drifted after activation wait" >&2
        return 1
    }
    cmp -s -- "$OMP584_STAGE/planner-independence.md" "$OMP584_FLOOD_DIR/planner-independence.md" || {
        echo "Disk target planner-independence.md drifted after activation wait" >&2
        return 1
    }
    local blob_flood blob_rules
    blob_flood=$(git hash-object --no-filters -- "$OMP584_STAGE/flood.py") || return 1
    [[ "$(git -C "$OMP584_FLOOD_DIR" rev-parse "HEAD:flood.py")" == "$blob_flood" ]] || {
        echo "Committed blob flood.py drifted after activation wait" >&2
        return 1
    }
    blob_rules=$(git hash-object --no-filters -- "$OMP584_STAGE/planner-independence.md") || return 1
    [[ "$(git -C "$OMP584_FLOOD_DIR" rev-parse "HEAD:planner-independence.md")" == "$blob_rules" ]] || {
        echo "Committed blob planner-independence.md drifted after activation wait" >&2
        return 1
    }
    [[ "$(git -C "$OMP584_FLOOD_DIR" rev-parse HEAD)" == "$target_commit" ]] || {
        echo "HEAD commit drifted after activation wait" >&2
        return 1
    }

    local repo_commit
    repo_commit=$(git -C "$OMP584_ROOT" rev-parse HEAD) || return 1

    # Atomically write evidence-dir/rollout.json only after proof:
    # repo_commit, external_commit, flood_sha256, activated_at from matched runtime record,
    # sibling artifact and installed-file hashes, compile/pytest/reload log paths. Preserve matched record.
    python3 - "$OMP584_ROOT" "$OMP584_ASSETS" "$OMP584_PROMPT_PATCH" "$OMP584_FLOOD_DIR" \
              "$OMP584_EVIDENCE_DIR" "$repo_commit" "$target_commit" "$code_sha256" \
              "$OMP584_STAGE/matched_record.json" <<'PY' || return 1
import sys, json, hashlib, pathlib, os

root = pathlib.Path(sys.argv[1])
assets = pathlib.Path(sys.argv[2])
prompt_patch = sys.argv[3]
flood_dir = pathlib.Path(sys.argv[4])
evidence_dir = pathlib.Path(sys.argv[5])
repo_commit = sys.argv[6]
external_commit = sys.argv[7]
flood_sha256 = sys.argv[8]
matched_record_path = pathlib.Path(sys.argv[9])

matched_record = json.loads(matched_record_path.read_text())

def file_sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

sibling_hashes = {
    "omp584-dependencies.patch": file_sha256(assets / "omp584-dependencies.patch"),
    "omp584-start-metrics.patch": file_sha256(assets / "omp584-start-metrics.patch"),
    prompt_patch: file_sha256(assets / prompt_patch),
    "omp584-planner-rules.md": file_sha256(assets / "omp584-planner-rules.md"),
}

installed_hashes = {
    "flood.py": file_sha256(flood_dir / "flood.py"),
    "planner-independence.md": file_sha256(flood_dir / "planner-independence.md"),
}

rollout = {
    "repo_commit": repo_commit,
    "external_commit": external_commit,
    "flood_sha256": flood_sha256,
    "activated_at": matched_record["at"],
    "matched_record": matched_record,
    "record": matched_record,
    "sibling_hashes": sibling_hashes,
    "artifact_hashes": sibling_hashes,
    "installed_file_hashes": installed_hashes,
    "installed_hashes": installed_hashes,
    "compile_log": str(evidence_dir / "compile.log"),
    "pytest_log": str(evidence_dir / "pytest.log"),
    "reload_log": str(evidence_dir / "reload.log"),
    "log_paths": {
        "compile": str(evidence_dir / "compile.log"),
        "pytest": str(evidence_dir / "pytest.log"),
        "reload": str(evidence_dir / "reload.log"),
    },
}

tmp_out = evidence_dir / f"rollout.json.tmp.{os.getpid()}"
tmp_out.write_text(json.dumps(rollout, indent=2) + "\n")
tmp_out.replace(evidence_dir / "rollout.json")
PY
)

omp584_main() (
    omp584_preflight "$@" || exit $?
    trap 'if [[ -n "${OMP584_STAGE:-}" ]]; then rm -rf -- "$OMP584_STAGE"; fi' EXIT
    omp584_stage || exit $?
    # Empty applied-parts means every code part was already installed. Install
    # nothing only when the checkout is already the canonical rules deployment.
    # A missing applied-parts file keeps qualification, commit, and activation.
    if [[ -f "$OMP584_STAGE/applied-parts" && ! -s "$OMP584_STAGE/applied-parts" ]] \
        && [[ -f "$OMP584_FLOOD_DIR/planner-independence.md" ]] \
        && ! grep -F -q -- 'with_name("omp584-planner-rules.md")' "$OMP584_FLOOD_DIR/flood.py" \
        && cmp -s -- "$OMP584_FLOOD_DIR/planner-independence.md" "$OMP584_ASSETS/omp584-planner-rules.md"
    then
        echo 'omp584: all parts already installed; nothing to install'
        exit 0
    fi
    omp584_verify || exit $?
    omp584_install || exit $?
    echo "Deployment committed; activation verification pending. Evidence: $OMP584_EVIDENCE_DIR" >&2
    omp584_activate || exit $?
    echo "Rollout completed successfully. Evidence: $OMP584_EVIDENCE_DIR/rollout.json"
)

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    omp584_main "$@"
fi
