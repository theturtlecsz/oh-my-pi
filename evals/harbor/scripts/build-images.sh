#!/usr/bin/env bash
# One-command build script for all Docker images referenced by Harbor fixtures
# (f1, f2, etc.) and Harbor's main service.
#
# Requirements:
# - Zero model spend
# - No network credentials needed
#
# Usage:
#   bash evals/harbor/scripts/build-images.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
HARBOR_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
REPO_ROOT="$(cd "$HARBOR_DIR/../.." && pwd -P)"

echo "==> Building omp-verifier:dev..."
docker build -t omp-verifier:dev -f "$HARBOR_DIR/docker/Dockerfile.verifier" "$HARBOR_DIR/docker"

echo "==> Building omp-workservice:dev..."
docker build -t omp-workservice:dev -f "$HARBOR_DIR/docker/Dockerfile.workservice" "$REPO_ROOT"

echo "==> Building omp-agent:dev..."
docker build -t omp-agent:dev -f "$HARBOR_DIR/docker/Dockerfile.agent" "$REPO_ROOT"

echo "==> Tagging agent alias images..."
docker tag omp-agent:dev omp-f1-agent:dev
docker tag omp-agent:dev omp-f2-agent:dev

echo "==> Verifying built images..."
for img in omp-verifier:dev omp-workservice:dev omp-agent:dev omp-f1-agent:dev omp-f2-agent:dev; do
    if docker image inspect "$img" >/dev/null 2>&1; then
        echo "  [OK] $img"
    else
        echo "  [FAIL] $img not found in local Docker registry" >&2
        exit 1
    fi
done

echo "==> All Harbor images built and verified successfully."
