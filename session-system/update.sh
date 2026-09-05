#!/usr/bin/env bash
# update.sh — merge one pinned upstream commit into the fork and prove the tree.
#
# usage: bash session-system/update.sh <full-40-hex-upstream-commit>
#
# Behavior:
#   * refuses anything but exactly one full 40-hex commit id (never a branch, never `main`)
#   * refuses a dirty worktree (tracked files)
#   * refuses while any live same-owner process maps code from this checkout (/proc scan)
#     (kernel-shielded same-owner processes — maps unreadable by ptrace policy,
#     e.g. systemd --user, ssh/gpg agents — are skipped with a warning:
#     owner-accepted carve-out, OMP-157 2026-08-26)
#   * fetches `upstream`, verifies the object resolves to exactly the supplied commit
#   * target NOT an ancestor of HEAD  -> requires a passing standing-guardrail review
#     record for exactly this candidate (docs/upstream/reviews/<first-12-hex>/review.json
#     pinning this target with fork == current HEAD, verified by
#     scripts/verify-upstream-handoff.ts), then `git merge --no-ff <commit>` and stop.
#     No review record, a mismatched record, or a failing review refuses the merge —
#     no incorporation path bypasses the guardrail (OMP-229).
#     A conflicted or otherwise failed merge exits immediately: resolve by hand, commit,
#     then re-run. No install, native refresh, verifier, or gate runs on this path.
#   * target IS an ancestor of HEAD   -> frozen install, native refresh, then the full
#     gate list (guardrail verifier + inventory + TS/Rust/Python/PostgreSQL) in order.
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

# OMP-157: refuse to mutate a checkout whose code any live process maps.
# Overwriting mapped files replaces text pages under running processes and
# crashed the broker and every session in OMP-156. The /proc scan is the only
# proof that covers the executing session itself; there is no bypass.
assert_tree_unmapped() {
	local root pid cmd procdir status
	root="$(pwd -P)"
	if [ ! -r /proc/self/maps ]; then
		echo "update.sh: /proc mappings unavailable — refusing to mutate $root" >&2
		echo "update.sh: run the upgrade from a session already on the stable build, then retry" >&2
		exit 1
	fi
	for procdir in /proc/[0-9]*; do
		# [ -O ]: owned by the invoking user (id -u). Root/system processes are
		# skipped rather than failed: they cannot map this user-owned checkout,
		# and their maps are expectedly unreadable.
		[ -O "$procdir" ] || continue
		pid="${procdir#/proc/}"
		if [ ! -r "$procdir/maps" ]; then
			[ -d "$procdir" ] || continue # vanished between glob and read
			echo "update.sh: warning: skipping kernel-shielded same-owner process $pid — mappings not inspectable" >&2
			continue
		fi
		status=0
		grep -qF " $root/" "$procdir/maps" 2>/dev/null || status=$?
		if [ "$status" -eq 0 ]; then
			cmd="$(tr '\0' ' ' <"$procdir/cmdline" 2>/dev/null || true)"
			cmd="${cmd% }"
			echo "update.sh: refusing to mutate $root — live process $pid maps code from this checkout: ${cmd:-unknown}" >&2
			echo "update.sh: run the upgrade from a session already on the stable build, then retry" >&2
			exit 1
		elif [ "$status" -gt 1 ]; then
			# Read denied at read() time (ptrace policy; maps mode is always
			# 0444 so -r never catches this). Vanished or now root-owned ->
			# the root/system carve-out. Still-owned kernel-shielded process
			# (session infrastructure, or anything self-shielded via
			# PR_SET_DUMPABLE=0) -> skip with a warning: owner-accepted
			# reduced guarantee (OMP-157, 2026-08-26). Inspectability is
			# process state, not class: any process may self-shield, and a
			# shielded mapper is the accepted blind spot.
			if [ -d "$procdir" ] && [ -O "$procdir" ]; then
				echo "update.sh: warning: skipping kernel-shielded same-owner process $pid — mappings not inspectable" >&2
			fi
		fi
	done
}
assert_tree_unmapped

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
	# OMP-229: the standing guardrail gates every incorporation. The review record
	# must exist for exactly this candidate, pin the reviewed fork commit, and pass
	# the full compatibility review before any merge happens. Committing the review
	# record itself legitimately advances HEAD past the fork pin, so HEAD may
	# differ from the pin only by changes under docs/upstream/ (guardrail
	# bookkeeping); any other divergence invalidates the review.
	REVIEW_DIR="docs/upstream/reviews/$(printf '%s' "$TARGET" | cut -c1-12)"
	REVIEW_JSON="$REVIEW_DIR/review.json"
	if [ ! -f "$REVIEW_JSON" ]; then
		echo "update.sh: no review record at $REVIEW_JSON — run the standing guardrail review first (docs/upstream-guardrail.md)" >&2
		exit 1
	fi
	REVIEW_TARGET="$(sed -n 's/.*"target"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{40\}\)".*/\1/p' "$REVIEW_JSON" | head -n 1)"
	REVIEW_FORK="$(sed -n 's/.*"fork"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{40\}\)".*/\1/p' "$REVIEW_JSON" | head -n 1)"
	if [ "$REVIEW_TARGET" != "$TARGET" ]; then
		echo "update.sh: review record targets '${REVIEW_TARGET:-none}', not $TARGET — refusing to merge" >&2
		exit 1
	fi
	HEAD_COMMIT="$(git rev-parse HEAD)"
	if [ "$REVIEW_FORK" != "$HEAD_COMMIT" ]; then
		if [ -z "$REVIEW_FORK" ] || ! git rev-parse --verify --quiet "${REVIEW_FORK}^{commit}" >/dev/null; then
			echo "update.sh: review record pins unknown fork commit '${REVIEW_FORK:-none}' — re-run the review against the current fork state" >&2
			exit 1
		fi
		DRIFT="$(git diff --name-only "$REVIEW_FORK" HEAD -- | grep -v '^docs/upstream/' || true)"
		if [ -n "$DRIFT" ]; then
			echo "update.sh: HEAD $HEAD_COMMIT diverges from the review's fork pin '$REVIEW_FORK' outside docs/upstream/ — re-run the review against the current fork state" >&2
			printf 'update.sh: diverging path: %s\n' $DRIFT >&2
			exit 1
		fi
	fi
	if ! bun scripts/verify-upstream-handoff.ts --record "$REVIEW_JSON" --allow-pending; then
		echo "update.sh: standing guardrail review failed — resolve the incompatibility report before merging" >&2
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

run_gate 1 bun scripts/verify-upstream-handoff.ts --record docs/upstream/baseline.json --allow-pending
run_gate 2 bun scripts/upstream-inventory.ts
run_gate 3 bun test session-system/tests packages/work-client/test scripts/verify-upstream-handoff.test.ts
run_gate 4 ./node_modules/.bin/tsc --noEmit -p session-system
run_gate 5 bun run check:ts
run_gate 6 cargo fmt --all -- --check
run_gate 7 cargo clippy --workspace --exclude brush-core --no-deps -- -D warnings
run_gate 8 bun run test:ts
run_gate 9 bun run test:scripts
run_gate 10 bun run test:py
run_gate 11 cargo nextest run --workspace --exclude brush-core --status-level=fail --final-status-level=fail

echo "=== gate 12: OMP_WORK_POSTGRES_INTEGRATION=1 bun run test:session:smoke"
SMOKE_LOG="$(mktemp)"
trap 'rm -f "$SMOKE_LOG"' EXIT
OMP_WORK_POSTGRES_INTEGRATION=1 bun run test:session:smoke | tee "$SMOKE_LOG"
if ! grep -q 'PASS' "$SMOKE_LOG"; then
	echo "update.sh: gate 12 did not print PASS" >&2
	exit 1
fi

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
	echo "update.sh: gates dirtied tracked state — refusing success" >&2
	git status --porcelain --untracked-files=no >&2
	exit 1
fi
echo "update.sh: all gates passed at $(git rev-parse --short HEAD) — cutover is a separate gated step"
