#!/usr/bin/env python3
"""Verify security restrictions for the automation user."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import pwd
import subprocess
import sys
from pathlib import Path
from typing import Any

BANNED_GROUPS = frozenset(
    {"wheel", "sudo", "docker", "adm", "root", "lxd", "libvirt", "disk"}
)

SENSITIVE_OWNER_PATHS = (
    ".config/omp/work-ledger/capabilities",
    ".config/omp-work",
    ".config/gh",
    ".git-credentials",
    ".netrc",
    ".ssh",
    ".aws",
    ".azure",
    ".kube",
    ".config/gcloud",
    ".omp/agent",
    ".claude",
    ".docker/config.json",
)


def check_not_owner(owner_name: str) -> dict[str, Any]:
    uid = os.getuid()
    euid = os.geteuid()
    if uid == 0 or euid == 0:
        return {"ok": False, "detail": f"Process running as root (uid={uid}, euid={euid})"}

    try:
        owner_entry = pwd.getpwnam(owner_name)
        owner_uid = owner_entry.pw_uid
    except KeyError:
        return {"ok": False, "detail": f"Owner user '{owner_name}' not found in passwd database"}

    if uid == owner_uid or euid == owner_uid:
        return {
            "ok": False,
            "detail": f"Process running as owner '{owner_name}' (uid={uid}, euid={euid})",
        }

    return {
        "ok": True,
        "detail": f"Process uid={uid} is neither owner '{owner_name}' (uid={owner_uid}) nor 0",
    }


def check_groups() -> dict[str, Any]:
    try:
        proc = subprocess.run(["id", "-Gn"], capture_output=True, text=True, check=True)
    except Exception as e:
        return {"ok": False, "detail": f"Failed to execute 'id -Gn': {e}"}

    groups = set(proc.stdout.strip().split())
    forbidden = sorted(groups & BANNED_GROUPS)
    if forbidden:
        return {"ok": False, "detail": f"User is in restricted group(s): {', '.join(forbidden)}"}

    return {"ok": True, "detail": "User is not in any restricted groups"}


def check_owner_credentials(owner_home: Path | None) -> dict[str, Any]:
    if owner_home is None:
        return {"ok": False, "detail": "Owner home directory could not be determined"}
    if not owner_home.exists():
        return {"ok": False, "detail": f"Owner home directory '{owner_home}' does not exist"}

    try:
        os.listdir(owner_home)
        return {"ok": False, "detail": f"Owner home '{owner_home}' is listable"}
    except PermissionError:
        pass
    except OSError:
        pass

    for rel_path in SENSITIVE_OWNER_PATHS:
        target = owner_home / rel_path

        try:
            os.listdir(target)
            return {"ok": False, "detail": f"Owner path '{rel_path}' is listable"}
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            pass
        except OSError:
            pass

        try:
            with open(target, "rb") as f:
                f.read(1)
            return {"ok": False, "detail": f"Owner path '{rel_path}' is readable"}
        except (PermissionError, FileNotFoundError, IsADirectoryError):
            pass
        except OSError:
            pass

        if rel_path == ".config/omp/work-ledger/capabilities":
            owner_json = target / "owner.json"
            try:
                with open(owner_json, "rb") as f:
                    f.read(1)
                return {"ok": False, "detail": f"Owner file '{rel_path}/owner.json' is readable"}
            except (PermissionError, FileNotFoundError, IsADirectoryError):
                pass
            except OSError:
                pass

    return {
        "ok": True,
        "detail": "Owner home is not listable and all checked owner paths are unreadable/missing",
    }


def check_sudo_named_only(admin_commands_file: Path | None) -> dict[str, Any]:
    try:
        proc_true = subprocess.run(["sudo", "-n", "true"], capture_output=True, text=True)
        if proc_true.returncode == 0:
            return {"ok": False, "detail": "'sudo -n true' succeeded (must fail)"}
    except Exception as e:
        return {"ok": False, "detail": f"Failed to execute 'sudo -n true': {e}"}

    if admin_commands_file is None or not admin_commands_file.exists():
        return {"ok": False, "detail": f"Admin commands file '{admin_commands_file}' not found"}

    try:
        expected_commands = [
            line.strip()
            for line in admin_commands_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except Exception as e:
        return {"ok": False, "detail": f"Failed to read admin commands file: {e}"}

    try:
        proc_l = subprocess.run(["sudo", "-n", "-l"], capture_output=True, text=True)
        if proc_l.returncode != 0:
            return {"ok": False, "detail": f"'sudo -n -l' failed with exit code {proc_l.returncode}"}
    except Exception as e:
        return {"ok": False, "detail": f"Failed to execute 'sudo -n -l': {e}"}

    lines = proc_l.stdout.splitlines()

    for line in lines:
        stripped = line.strip()
        if "(ALL) ALL" in stripped or "(ALL : ALL) ALL" in stripped:
            return {"ok": False, "detail": f"sudo privileges contain ALL: '{stripped}'"}
        if "NOPASSWD: ALL" in stripped:
            return {"ok": False, "detail": "sudo privileges contain NOPASSWD: ALL"}

    nopasswd_cmds: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "NOPASSWD:" in stripped:
            cmd_part = stripped.split("NOPASSWD:", 1)[1].strip()
            for cmd in cmd_part.split(","):
                c = cmd.strip()
                if c:
                    if c == "ALL" or c == "(ALL) ALL":
                        return {"ok": False, "detail": "sudo privileges contain ALL command"}
                    nopasswd_cmds.append(c)

    if sorted(nopasswd_cmds) != sorted(expected_commands):
        return {
            "ok": False,
            "detail": f"NOPASSWD commands ({nopasswd_cmds}) do not match admin list ({expected_commands})",
        }

    return {
        "ok": True,
        "detail": f"NOPASSWD commands match admin list exactly ({len(nopasswd_cmds)} commands)",
    }


def check_docker_socket(docker_socket_path: Path) -> dict[str, Any]:
    if docker_socket_path.exists():
        if os.access(docker_socket_path, os.W_OK):
            return {"ok": False, "detail": f"Docker socket '{docker_socket_path}' is writable"}
        return {"ok": True, "detail": f"Docker socket '{docker_socket_path}' is not writable"}
    return {"ok": True, "detail": f"Docker socket '{docker_socket_path}' does not exist"}


def check_no_cloud_credentials() -> dict[str, Any]:
    home = Path(os.environ.get("HOME", Path.home())).resolve()
    cloud_paths = [
        home / ".aws",
        home / ".azure",
        home / ".kube" / "config",
        home / ".config" / "gcloud",
    ]
    found_paths = [str(p) for p in cloud_paths if p.exists()]
    if found_paths:
        return {
            "ok": False,
            "detail": f"Cloud credential path(s) found in home: {', '.join(found_paths)}",
        }

    found_vars = []
    for var in os.environ:
        if var.startswith("AWS_") or var.startswith("AZURE_"):
            found_vars.append(var)
        elif var in {"ARM_CLIENT_SECRET", "KUBECONFIG", "GOOGLE_APPLICATION_CREDENTIALS"}:
            found_vars.append(var)

    if found_vars:
        return {
            "ok": False,
            "detail": f"Cloud credential environment variable(s) found: {', '.join(sorted(found_vars))}",
        }

    return {"ok": True, "detail": "No cloud credential paths or environment variables found"}


def check_ledger_principal(
    client_config_path: Path, owner_home: Path | None
) -> dict[str, Any]:
    if not client_config_path.exists():
        return {"ok": False, "detail": f"Client config '{client_config_path}' does not exist"}

    try:
        config_data = json.loads(client_config_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "detail": f"Failed to parse client config '{client_config_path}': {e}"}

    bearer_str = config_data.get("bearer_file")
    if not bearer_str:
        return {"ok": False, "detail": f"Client config '{client_config_path}' missing 'bearer_file' field"}

    bearer_file = Path(bearer_str).resolve()

    if owner_home is not None:
        resolved_owner = owner_home.resolve()
        try:
            bearer_file.relative_to(resolved_owner)
            return {
                "ok": False,
                "detail": f"Bearer file '{bearer_file}' is inside owner home '{resolved_owner}'",
            }
        except ValueError:
            pass

    if not bearer_file.exists():
        return {"ok": False, "detail": f"Bearer file '{bearer_file}' does not exist"}

    try:
        bearer_data = json.loads(bearer_file.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "detail": f"Failed to parse bearer file '{bearer_file}': {e}"}

    actor_kind = bearer_data.get("actor_kind")
    if actor_kind == "owner":
        return {
            "ok": False,
            "detail": f"Bearer file '{bearer_file}' actor_kind is 'owner' (must not be owner)",
        }
    if not actor_kind:
        return {"ok": False, "detail": f"Bearer file '{bearer_file}' missing 'actor_kind' field"}

    return {
        "ok": True,
        "detail": f"Bearer file is outside owner home and has valid actor_kind '{actor_kind}'",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify security restrictions for the automation user."
    )
    parser.add_argument(
        "--owner",
        required=True,
        help="Name of the owner user account",
    )
    parser.add_argument(
        "--owner-home",
        type=Path,
        default=None,
        help="Owner home path (default: owner's pwd home)",
    )
    parser.add_argument(
        "--admin-commands",
        type=Path,
        default=None,
        help="Path to file listing allowed admin commands (one per line)",
    )
    parser.add_argument(
        "--client-config",
        type=Path,
        default=None,
        help="Path to client.json (default: $XDG_CONFIG_HOME/omp-work/client.json)",
    )
    parser.add_argument(
        "--docker-socket",
        type=Path,
        default=Path("/var/run/docker.sock"),
        help="Path to docker socket (default: /var/run/docker.sock)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    owner_home: Path | None = None
    if args.owner_home:
        owner_home = Path(args.owner_home).resolve()
    else:
        try:
            owner_home = Path(pwd.getpwnam(args.owner).pw_dir).resolve()
        except KeyError:
            owner_home = None

    admin_commands_file: Path | None = None
    if args.admin_commands:
        admin_commands_file = Path(args.admin_commands).resolve()
    else:
        for loc in [
            Path(f"/etc/{getpass.getuser()}/admin-commands"),
            Path("/etc/ompbot/admin-commands"),
        ]:
            if loc.exists():
                admin_commands_file = loc
                break

    client_config: Path
    if args.client_config:
        client_config = Path(args.client_config).resolve()
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            client_config = Path(xdg) / "omp-work" / "client.json"
        else:
            client_config = (
                Path(os.environ.get("HOME", Path.home())) / ".config" / "omp-work" / "client.json"
            )

    docker_socket = Path(args.docker_socket).resolve()

    results = {
        "not_owner": check_not_owner(args.owner),
        "groups": check_groups(),
        "owner_credentials_unreadable": check_owner_credentials(owner_home),
        "sudo_named_only": check_sudo_named_only(admin_commands_file),
        "docker_socket": check_docker_socket(docker_socket),
        "no_cloud_credentials": check_no_cloud_credentials(),
        "ledger_principal": check_ledger_principal(client_config, owner_home),
    }

    print(json.dumps(results, indent=2))
    return 0 if all(c["ok"] for c in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
