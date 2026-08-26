#!/usr/bin/env bash
# update.sh — merge one pinned upstream commit into the fork and prove the tree.
#
# usage: bash session-system/update.sh <full-40-hex-upstream-commit>
#
# Behavior:
#   * refuses anything but exactly one full 40-hex commit id (never a branch, never `main`)
#   * refuses a dirty worktree (tracked files)
#   * fetches `upstream`, verifies the object resolves to exactly the supplied commit
#   * target NOT an ancestor of HEAD  -> `git merge --no-ff <commit>` and stop.
#     A conflicted or otherwise failed merge exits immediately: resolve by hand, commit,
#     then re-run. No install, native refresh, verifier, or gate runs on this path.
#   * target IS an ancestor of HEAD   -> frozen install, native refresh, then the full
#     OMP-156 gate list (verifier + TS/Rust/Python/PostgreSQL) verbatim and in order.
#     Success additionally requires the tracked tree to still be clean afterwards.
#
# This script never pushes, never installs live links, and never uses a moving merge
# ref. Cutover to the live checkout is a separate, separately-gated procedure.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -ne 1 ]; then
	echo "update.sh: exactly one argument required — a full 40-hex upstream commit" >&2
	exit 2
fi
TARGET="$1"
if [ "$TARGET" = "main" ] || [ "$TARGET" = "upstream/main" ]; then
	echo "update.sh: moving refs are refused — pass a full 40-hex commit" >&2
	exit 2
fi
if ! printf '%s' "$TARGET" | grep -Eq '^[0-9a-f]{40}$'; then
	echo "update.sh: '$TARGET' is not a full 40-hex commit id" >&2
	exit 2
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
	echo "update.sh: worktree has uncommitted tracked changes — commit or stash first" >&2
	exit 1
fi

git fetch upstream

RESOLVED="$(git rev-parse --verify --quiet "${TARGET}^{commit}" || true)"
if [ "$RESOLVED" != "$TARGET" ]; then
	echo "update.sh: '$TARGET' does not resolve to a commit after fetching upstream" >&2
	exit 1
fi

if ! git merge-base --is-ancestor "$TARGET" HEAD; then
	BRANCH="$(git branch --show-current)"
	if [ "$BRANCH" = "main" ]; then
		echo "update.sh: refusing to merge on 'main' — run from an integration branch; main only fast-forwards during gated cutover" >&2
		exit 1
	fi
	echo "update.sh: merging $TARGET (--no-ff); a conflict stops here — resolve, commit, re-run"
	git merge --no-ff "$TARGET"
	echo "update.sh: merge committed at $(git rev-parse --short HEAD) — re-run to execute the gates"
	exit 0
fi

echo "update.sh: $TARGET is an ancestor — running frozen install, natives, and gates"
bun install --frozen-lockfile
bash session-system/refresh-natives.sh

run_gate() { # run_gate <label> <command...>
	local label="$1"
	shift
	echo "=== gate $label: $*"
	"$@"
}

run_gate 1 bun scripts/verify-upstream-handoff.ts \
	--base ae2d3d6ea16a47aa5208bd123dcc4cfcc8756472 \
	--fork 79b037e420943010e03727d7cdb22f05e64507b7 \
	--target b4e8e856ad40294167679a3f88417c07429fe59b \
	--sources docs/upstream-18.0.6-fork-sources.tsv \
	--matrix docs/upstream-18.0.6-fork-matrix.tsv \
	--changelog docs/upstream-18.0.6-changelog.tsv \
	--handoff docs/upstream-18.0.6-upgrade.md \
	--allow-pending
run_gate 2 bun test session-system/tests packages/work-client/test scripts/verify-upstream-handoff.test.ts
run_gate 3 ./node_modules/.bin/tsc --noEmit -p session-system
run_gate 4 bun run check:ts
run_gate 5 cargo fmt --all -- --check
run_gate 6 cargo clippy --workspace --exclude brush-core --no-deps -- -D warnings
run_gate 7 bun run test:ts
run_gate 8 bun run test:scripts
run_gate 9 bun run test:py
run_gate 10 cargo nextest run --workspace --exclude brush-core --status-level=fail --final-status-level=fail

echo "=== gate 11: OMP_WORK_POSTGRES_INTEGRATION=1 bun run test:session:smoke"
SMOKE_LOG="$(mktemp)"
trap 'rm -f "$SMOKE_LOG"' EXIT
OMP_WORK_POSTGRES_INTEGRATION=1 bun run test:session:smoke | tee "$SMOKE_LOG"
if ! grep -q 'PASS' "$SMOKE_LOG"; then
	echo "update.sh: gate 11 did not print PASS" >&2
	exit 1
fi

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
	echo "update.sh: gates dirtied tracked state — refusing success" >&2
	git status --porcelain --untracked-files=no >&2
	exit 1
fi
echo "update.sh: all gates passed at $(git rev-parse --short HEAD) — cutover is a separate gated step"
