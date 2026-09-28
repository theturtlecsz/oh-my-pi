#!/bin/bash
# Independent verification for fixture f1: the workflow revise_work test must
# pass on the repaired tree. Copied to /tests/test.sh and run from /workspace.
set -uo pipefail
mkdir -p /logs/verifier
cd /workspace
if [ ! -d node_modules ]; then
	bun install --frozen-lockfile
fi
bun test session-system/tests/workflow-revise.test.ts
status=$?
if [ "$status" -eq 0 ]; then
	echo 1 > /logs/verifier/reward.txt
else
	echo 0 > /logs/verifier/reward.txt
fi
exit 0
