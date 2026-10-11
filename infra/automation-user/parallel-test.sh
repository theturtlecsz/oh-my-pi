#!/usr/bin/env bash
# Prove this automation user can run flood and agent sessions on its own.
# Exit 2 before any evidence is written when the process is root, is the owner,
# or this checkout lies under the owner's home. Otherwise run every step and
# record each exit code.
set -uo pipefail

usage() {
  printf '%s\n' 'usage: parallel-test.sh --owner O --repo R --flood-dir D --flood-config C --evidence-dir E' >&2
  exit 2
}

refuse() {
  printf 'parallel-test: %s\n' "$1" >&2
  exit 2
}

OWNER=""
REPO=""
FLOOD_DIR=""
FLOOD_CONFIG=""
EVIDENCE_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --owner) OWNER="${2-}"; shift 2 ;;
    --repo) REPO="${2-}"; shift 2 ;;
    --flood-dir) FLOOD_DIR="${2-}"; shift 2 ;;
    --flood-config) FLOOD_CONFIG="${2-}"; shift 2 ;;
    --evidence-dir) EVIDENCE_DIR="${2-}"; shift 2 ;;
    *) usage ;;
  esac
done

[[ -n "$OWNER" && -n "$REPO" && -n "$FLOOD_DIR" && -n "$FLOOD_CONFIG" && -n "$EVIDENCE_DIR" ]] || usage

if [[ "$EUID" -eq 0 || "$UID" -eq 0 ]]; then
  refuse "refusing to run as root"
fi

owner_uid="$(id -u -- "$OWNER" 2>/dev/null)" || refuse "unknown owner ${OWNER}"
if [[ "$EUID" -eq "$owner_uid" || "$UID" -eq "$owner_uid" ]]; then
  refuse "refusing to run as owner ${OWNER}"
fi

owner_home="$(getent passwd "$OWNER" | awk -F: 'NR == 1 { print $6; exit }')"
if [[ -z "$owner_home" ]]; then
  owner_home="$(python3 -c 'import pwd,sys; print(pwd.getpwnam(sys.argv[1]).pw_dir)' "$OWNER" 2>/dev/null || true)"
fi
[[ -n "$owner_home" ]] || refuse "owner ${OWNER} has no home directory"

script_path="$(readlink -f -- "${BASH_SOURCE[0]}")"
script_dir="$(dirname -- "$script_path")"
# Ignore the caller's GIT_DIR so the checkout is the tree that holds this script.
checkout="$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$checkout" ]]; then
  checkout="$script_dir"
fi
checkout="$(readlink -f -- "$checkout")"
owner_home="$(readlink -f -- "$owner_home")"
if [[ "$owner_home" != "/" && ( "$checkout" == "$owner_home" || "$checkout" == "$owner_home"/* ) ]]; then
  refuse "checkout ${checkout} lies under owner home ${owner_home}"
fi

mkdir -p -- "$EVIDENCE_DIR" || exit 1

dir="$script_dir"
verify_rc=0
github_rc=0
omp_rc=0
claude_rc=0
flood_rc=0

python3 "$dir/verify-restrictions.py" --owner "$OWNER" >"$EVIDENCE_DIR/verify.log" 2>&1 || verify_rc=$?

GH_TOKEN="$(gh auth token 2>>"$EVIDENCE_DIR/github.log")" python3 "$dir/github-probe.py" --repo "$REPO" --branch main >>"$EVIDENCE_DIR/github.log" 2>&1 || github_rc=$?

omp -p "Reply with exactly: OK" >"$EVIDENCE_DIR/omp.log" 2>&1 || omp_rc=$?
if [[ "$omp_rc" -eq 0 ]] && [[ "$(cat -- "$EVIDENCE_DIR/omp.log")" != *OK* ]]; then
  printf '%s\n' 'omp output did not contain OK' >>"$EVIDENCE_DIR/omp.log"
  omp_rc=1
fi

claude -p "Reply with exactly: OK" >"$EVIDENCE_DIR/claude.log" 2>&1 || claude_rc=$?
if [[ "$claude_rc" -eq 0 ]] && [[ "$(cat -- "$EVIDENCE_DIR/claude.log")" != *OK* ]]; then
  printf '%s\n' 'claude output did not contain OK' >>"$EVIDENCE_DIR/claude.log"
  claude_rc=1
fi

timeout 3600 python3 "$FLOOD_DIR/flood.py" run --config "$FLOOD_CONFIG" >"$EVIDENCE_DIR/flood.log" 2>&1 || flood_rc=$?

printf '{"verify":%d,"github":%d,"omp":%d,"claude":%d,"flood":%d}\n' \
  "$verify_rc" "$github_rc" "$omp_rc" "$claude_rc" "$flood_rc" \
  >"$EVIDENCE_DIR/summary.json"

final=0
for rc in "$verify_rc" "$github_rc" "$omp_rc" "$claude_rc" "$flood_rc"; do
  if [[ "$rc" -ne 0 ]]; then
    final=1
    break
  fi
done
exit "$final"
