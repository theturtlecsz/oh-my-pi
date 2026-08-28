from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True)
class OperationsConfig:
    config_dir: Path
    state_dir: Path
    data_dir: Path
    database: str = "omp_work"
    host: str = "127.0.0.1"
    port: int = 54321
    aws_profile: str = "default"
    aws_region: str = "us-east-1"
    bucket: str = "omp-work-ledger-037842804132-us-east-1"
    prefix: str = "work-ledger/v1"
    endpoint_url: str | None = None

    @classmethod
    def defaults(cls) -> OperationsConfig:
        home = Path.home()
        return cls(
            config_dir=Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
            / "omp"
            / "work-ledger",
            state_dir=Path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
            / "omp"
            / "work-ledger",
            data_dir=Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
            / "omp"
            / "work-ledger",
            port=int(os.environ.get("OMP_WORK_POSTGRES_PORT", "54321")),
            prefix=os.environ.get("OMP_WORK_S3_PREFIX", "work-ledger/v1"),
            endpoint_url=os.environ.get("OMP_WORK_S3_ENDPOINT"),
        )

    @property
    def credentials_dir(self) -> Path:
        return self.config_dir / "credentials"

    def secret_path(self, name: str) -> Path:
        return self.credentials_dir / name

    def read_secret(self, name: str) -> str:
        path = self.secret_path(name)
        try:
            metadata = path.stat()
        except FileNotFoundError as error:
            raise ValueError(f"missing credential file: {path}") from error
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ValueError(
                f"credential file must be a regular file owned by the current user: {path}"
            )
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError(f"unsafe credential file permissions: {path}")
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"empty credential file: {path}")
        return value

    def actor_id(self) -> UUID:
        return UUID(self.read_secret("operator-actor-id"))

    def workspace_id(self) -> UUID:
        return UUID(self.read_secret("workspace-id"))

    def connection_kwargs(self, role: str) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": role,
            "password": self.read_secret(role),
        }
