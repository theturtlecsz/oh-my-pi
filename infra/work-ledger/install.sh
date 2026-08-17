#!/usr/bin/env bash
set -euo pipefail
image='postgres:18.3-bookworm@sha256:80630f83606d8db77d30b3851b16a9f78be2d0d4dda6f7b82a1fdca5ebe3acba'
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
config=${XDG_CONFIG_HOME:-$HOME/.config}/omp/work-ledger
state=${XDG_STATE_HOME:-$HOME/.local/state}/omp/work-ledger
share=${XDG_DATA_HOME:-$HOME/.local/share}/omp/work-ledger
for path in "$config" "$state/postgres" "$share/wal"; do install -d -m 700 "$path"; done
docker pull "$image"
[ "$(docker image inspect --format '{{index .RepoDigests 0}}' "$image")" = "$image" ] || { echo 'pinned postgres image mismatch' >&2; exit 1; }
python3 - "$root" "$config" "$state" "$share" <<'PY'
from pathlib import Path
import sys
root, config, state, share = sys.argv[1:]
unit = f'''[Unit]
Description=OMP Work Ledger PostgreSQL
After=docker.service

[Service]
Type=simple
ExecStartPre=/usr/bin/docker network inspect omp-work-internal
ExecStart=/usr/bin/docker run --rm --name omp-work-postgres --network omp-work-internal -p 127.0.0.1:54321:5432 -v {state}/postgres:/var/lib/postgresql/data -v {share}/wal:/var/lib/postgresql/wal -v {config}/credentials:/run/omp-credentials:ro -e POSTGRES_PASSWORD_FILE=/run/omp-credentials/postgres -e POSTGRES_INITDB_ARGS=--data-checksums postgres:18.3-bookworm@sha256:80630f83606d8db77d30b3851b16a9f78be2d0d4dda6f7b82a1fdca5ebe3acba -c password_encryption=scram-sha-256 -c wal_level=replica -c archive_mode=on -c archive_command='test ! -f /var/lib/postgresql/wal/%f && cp %p /var/lib/postgresql/wal/%f || cmp %p /var/lib/postgresql/wal/%f'
ExecStop=/usr/bin/docker stop -t 30 omp-work-postgres
Restart=on-failure

[Install]
WantedBy=default.target
'''
units = {
    "omp-work-postgres.service": unit,
    "omp-work-service.service": f"""[Unit]
Description=OMP Work Ledger WorkService (loopback)
After=omp-work-postgres.service

[Service]
Type=simple
ExecStart={root}/python/omp-work/.venv/bin/python -m omp_work serve --capabilities-dir {config}/capabilities
Restart=on-failure

[Install]
WantedBy=default.target
""",
    "omp-work-backup.service": f"""[Service]
Type=oneshot
ExecStart={root}/python/omp-work/.venv/bin/omp-work ops backup create
""",
    "omp-work-wal.service": f"""[Service]
Type=oneshot
ExecStart={root}/python/omp-work/.venv/bin/omp-work ops backup wal
""",
    "omp-work-restore-drill.service": f"""[Service]
Type=oneshot
ExecStart={root}/python/omp-work/.venv/bin/omp-work ops restore drill --source latest --reason monthly
""",
    "omp-work-backup.timer": """[Timer]
OnCalendar=daily
Persistent=true
[Install]
WantedBy=timers.target
""",
    "omp-work-wal.timer": """[Timer]
OnBootSec=5m
OnUnitActiveSec=5m
Persistent=true
[Install]
WantedBy=timers.target
""",
    "omp-work-restore-drill.timer": """[Timer]
OnCalendar=monthly
Persistent=true
[Install]
WantedBy=timers.target
""",
}
out = Path.home() / ".config/systemd/user"
out.mkdir(parents=True, exist_ok=True)
for name, content in units.items():
    (out / name).write_text(content)
PY
docker network inspect omp-work-internal >/dev/null 2>&1 || docker network create --internal omp-work-internal >/dev/null
