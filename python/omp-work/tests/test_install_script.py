from pathlib import Path
import os
import stat
import subprocess
import tempfile


def test_installer_generates_service_units_with_migration_preflight():
    repo_root = Path(__file__).resolve().parents[3]
    install_script = repo_root / "infra" / "work-ledger" / "install.sh"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()

        # Create fake PostgreSQL 18 executables
        postgres_script = fake_bin / "postgres"
        postgres_script.write_text(
            '#!/bin/sh\nif [ "$1" = "--version" ]; then echo \'postgres (PostgreSQL) 18.0\'; exit 0; fi\nexit 0\n'
        )
        postgres_script.chmod(postgres_script.stat().st_mode | stat.S_IXUSR)

        for name in ("initdb", "pg_ctl"):
            script = fake_bin / name
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(script.stat().st_mode | stat.S_IXUSR)

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        env = {
            **os.environ,
            "HOME": str(fake_home),
            "XDG_CONFIG_HOME": str(fake_home / ".config"),
            "XDG_STATE_HOME": str(fake_home / ".local" / "state"),
            "XDG_DATA_HOME": str(fake_home / ".local" / "share"),
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        }

        res = subprocess.run(
            ["/usr/bin/env", "bash", str(install_script)],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, (
            f"install.sh failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )

        unit_file = (
            fake_home / ".config" / "systemd" / "user" / "omp-work-service.service"
        )
        assert unit_file.exists(), f"Service unit not found at {unit_file}"

        content = unit_file.read_text()
        assert "ExecStartPre=" in content
        assert "ops migrate" in content
        assert "ExecStart=" in content
        assert "serve" in content

        # Preflight migration must precede serve command
        pre_idx = content.index("ExecStartPre=")
        start_idx = content.index("ExecStart=")
        assert pre_idx < start_idx, "ExecStartPre must precede ExecStart"
