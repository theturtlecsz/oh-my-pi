#!/bin/bash
# Reference solution: restore the two dropped fields in
# session-system/extensions/workflow/work.ts so revise_work keeps the previous
# revision's scope and acceptance criteria for every field the caller did not
# amend. The container receives this repository at /workspace and this
# directory at /solution.
set -euo pipefail
cd /workspace
git apply /solution/solution.patch
