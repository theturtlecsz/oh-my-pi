#!/bin/sh
# Run the pinned launcher from a directory without candidate Bun/dotenv configuration.
set -eu
if [ "$#" -lt 3 ]; then
  echo 'usage: bin/omp /runtime-state /workspace MANIFEST_SHA256 [--service | omp arguments...]' >&2
  exit 2
fi
runtime_state=$1
case "$runtime_state" in /*) ;; *) echo 'runtime state must be absolute' >&2; exit 2 ;; esac
release_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$release_root"
# Bun 1.4 can create XDG cache directories while importing the verifier. Keep
# bootstrap read-only until prepareState admits/initializes the runtime directory.
exec env -i \
  BUN_RUNTIME_TRANSPILER_CACHE_PATH=0 \
  HOME="$runtime_state/home" \
  XDG_CONFIG_HOME="$runtime_state/config" \
  XDG_STATE_HOME="$runtime_state/state" \
  XDG_DATA_HOME="$runtime_state/data" \
  XDG_CACHE_HOME="$runtime_state/cache" \
  PATH=/usr/local/bin:/usr/bin:/bin \
  TERM="${TERM:-dumb}" LANG="${LANG:-C.UTF-8}" \
  "$release_root/bin/bun" "$release_root/source/session-system/runtime/run.ts" "$release_root" "$@"
