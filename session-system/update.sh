#!/usr/bin/env bash
# update.sh — pull upstream omp into the fork and prove the session system still fits.
# On merge conflict: resolve by hand, commit, then re-run to finish the checks.
set -euo pipefail
cd "$(dirname "$0")/.."
git fetch upstream
git merge upstream/main
bun install
bun test session-system/tests
git push origin main
echo "fork now at $(git rev-parse --short HEAD) — restart omp to load the new source."
