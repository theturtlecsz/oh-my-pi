#!/usr/bin/env bash
# refresh-natives.sh — keep the source-linked native addon in sync with packages/natives.
#
# build:native needs bazelisk (absent on this host), so omp runs on the
# exact-version @oh-my-pi/pi-natives-linux-x64 npm binaries dropped into
# packages/natives/native/. The workspace loader skips version-sentinel
# validation (loader-state.js isWorkspaceLoad), so a stale drop-in fails
# NOTHING — this script is the only staleness gate. Marker lives under the
# gitignored packages/natives/npm/ so the tree stays clean.
set -euo pipefail
cd "$(dirname "$0")/.."

[ "$(uname -sm)" = "Linux x86_64" ] || { echo "refresh-natives: non-linux-x64 host — build with bun run build:native instead" >&2; exit 1; }

ver=$(jq -r .version packages/natives/package.json)
dest=packages/natives/native
marker=packages/natives/npm/.native-dropin-version

if [ -f "$dest/pi_natives.linux-x64-modern.node" ] && [ -f "$dest/pi_natives.linux-x64-baseline.node" ] && [ "$(cat "$marker" 2>/dev/null)" = "$ver" ]; then
	echo "natives OK ($ver)"
	exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
( cd "$tmp" && npm pack "@oh-my-pi/pi-natives-linux-x64@$ver" >/dev/null && tar xzf ./*.tgz )
cp "$tmp"/package/pi_natives.linux-x64-*.node "$dest"/
mkdir -p packages/natives/npm
echo "$ver" > "$marker"
echo "natives refreshed to $ver"
