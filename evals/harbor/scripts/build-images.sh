#!/usr/bin/env bash
# One-command build of every image the f1 and f2 Harbor fixtures reference.
#
# Runs before `harbor run` (the OMP-250-s08 owner step). It needs no network
# credentials and spends no model tokens: it only builds local images from the
# repository.
#
# Usage:
#   bash evals/harbor/scripts/build-images.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
HARBOR_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
REPO_ROOT="$(cd "$HARBOR_DIR/../.." && pwd -P)"

# The agent image copies these host-built addons in. Never compile Rust here.
for addon_name in pi_natives.linux-x64-modern.node pi_natives.linux-x64-baseline.node; do
  addon_path="$REPO_ROOT/packages/natives/native/$addon_name"
  if [ ! -f "$addon_path" ]; then
    echo "build-images: missing prebuilt native addon $addon_path; build or copy it into packages/natives/native first" >&2
    exit 1
  fi
done

# The token has to exist before `harbor run`: the agent reads it from the
# host environment, and the workservice only installs this same bundle.
bash "$SCRIPT_DIR/provision-credentials.sh"

docker build -t omp-verifier:dev \
  -f "$HARBOR_DIR/docker/Dockerfile.verifier" "$HARBOR_DIR/docker"

docker build -t omp-workservice:dev \
  -f "$HARBOR_DIR/docker/Dockerfile.workservice" "$REPO_ROOT"

docker build -t omp-agent:dev \
  -f "$HARBOR_DIR/docker/Dockerfile.agent" "$REPO_ROOT"

docker tag omp-agent:dev omp-f1-agent:dev

images=(omp-verifier:dev omp-workservice:dev omp-agent:dev omp-f1-agent:dev)
for image in "${images[@]}"; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "build-images: $image missing after build" >&2
    exit 1
  fi
done

echo "built: ${images[*]}"
