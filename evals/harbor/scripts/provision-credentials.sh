#!/usr/bin/env bash
# Write the host credential bundle the workservice mounts read-only.
#
# The Harbor agent reads OMP_HARBOR_BEARER and OMP_HARBOR_WORKSPACE_ID from
# the host environment before any container exists. This script creates that
# bundle once under $HOME/.local/state/omp/harbor-fixtures (override with
# OMP_HARBOR_CREDENTIALS_DIR). A later run keeps the same token. It uses only
# the Python standard library: no network, no model, and no omp_work process.
set -euo pipefail
umask 077

if [ -n "${OMP_HARBOR_CREDENTIALS_DIR:-}" ]; then
	DEST="$OMP_HARBOR_CREDENTIALS_DIR"
else
	if [ -z "${HOME:-}" ]; then
		echo "provision-credentials: HOME is required" >&2
		exit 1
	fi
	DEST="$HOME/.local/state/omp/harbor-fixtures"
fi

required=(
	credentials/postgres
	credentials/omp_work_migrator
	credentials/omp_work_app
	credentials/omp_work_importer
	credentials/omp_work_readonly
	credentials/omp_work_backup
	credentials/gpg-passphrase
	credentials/operator-actor-id
	credentials/workspace-id
	capabilities/owner.json
	harbor.env
)

if [ -d "$DEST" ]; then
	missing=0
	present=0
	for rel in "${required[@]}"; do
		if [ -s "$DEST/$rel" ]; then
			present=1
		else
			missing=1
		fi
	done
	if [ "$missing" -eq 0 ]; then
		exit 0
	fi
	if [ "$present" -eq 1 ]; then
		echo "provision-credentials: incomplete bundle at $DEST; remove it and rerun" >&2
		exit 1
	fi
fi

command -v python3 >/dev/null 2>&1 || {
	echo "provision-credentials: python3 is required" >&2
	exit 1
}

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
python3 - "$stage" "$DEST" <<'PY'
import json
import secrets
import shlex
import sys
from pathlib import Path
from uuid import uuid4

stage = Path(sys.argv[1])
dest = Path(sys.argv[2])
credentials = stage / "credentials"
capabilities = stage / "capabilities"
credentials.mkdir(mode=0o700)
capabilities.mkdir(mode=0o700)

def write_secret(path: Path, value: str) -> None:
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)

for role in (
    "postgres",
    "omp_work_migrator",
    "omp_work_app",
    "omp_work_importer",
    "omp_work_readonly",
    "omp_work_backup",
):
    write_secret(credentials / role, secrets.token_urlsafe(32))
write_secret(credentials / "gpg-passphrase", secrets.token_urlsafe(48))
workspace_id = str(uuid4())
owner_id = str(uuid4())
write_secret(credentials / "workspace-id", workspace_id)
write_secret(credentials / "operator-actor-id", owner_id)
token = secrets.token_urlsafe(32)
owner = {
    "actor_id": owner_id,
    "actor_kind": "owner",
    "scopes": [
        "work.approve",
        "work.close",
        "work.execute",
        "work.mutate",
        "work.read",
    ],
    "token": token,
    "workspaces": [workspace_id],
}
write_secret(capabilities / "owner.json", json.dumps(owner, indent=2, sort_keys=True))
env = "\n".join(
    [
        f"export OMP_HARBOR_BEARER={shlex.quote(token)}",
        f"export OMP_HARBOR_WORKSPACE_ID={shlex.quote(workspace_id)}",
        f"export OMP_HARBOR_CREDENTIALS_DIR={shlex.quote(str(dest))}",
    ]
)
write_secret(stage / "harbor.env", env)
PY
chmod 0700 "$stage" "$stage/credentials" "$stage/capabilities"
mkdir -p "$(dirname "$DEST")"
if [ -d "$DEST" ]; then
	rm -rf "$DEST"
fi
trap - EXIT
mv "$stage" "$DEST"
chmod 0700 "$DEST"
