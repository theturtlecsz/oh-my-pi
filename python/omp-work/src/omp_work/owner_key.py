"""Owner-key custody check (OMP-403).

``omp-work owner-key check`` fails when the owner allowed-signers file is
missing, group- or world-writable, or does not contain exactly one ``owner``
line; when ``--key`` is readable by ``--user`` (mode bits and parent-directory
search); or when a ``--scan`` file, other than a symlink and at most 64 KiB,
is an OpenSSH private key whose ``openssh-key-v1`` header public key is the
owner's.

Owner decision D47 (2026-09-30): flood creates and holds the owner signing key
on this host under ``~/.config/omp/owner-signing``, not on Chris's own device,
so an owner signature proves flood's approval. Removing that key and
``owner_allowed_signers`` undoes this. ``--scan`` still reports a matching
private key under any scanned directory; the operator does not scan the key's
own directory.
"""

from __future__ import annotations

import argparse
import base64
import os
import pwd
import stat
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "SIGNERS_NAME",
    "UnixIds",
    "check",
    "readable_by",
    "run",
]

SIGNERS_NAME = "owner_allowed_signers"
_MAX_SCAN_BYTES = 64 * 1024
_OPENSSH_MAGIC = b"openssh-key-v1\x00"
_BEGIN = b"-----BEGIN OPENSSH PRIVATE KEY-----"
_END = b"-----END OPENSSH PRIVATE KEY-----"


@dataclass(frozen=True)
class UnixIds:
    """A synthetic or resolved uid and the group set used for mode checks."""

    uid: int
    gids: frozenset[int]


def _signers_path(config_dir: Path) -> Path:
    return Path(config_dir) / SIGNERS_NAME


def _is_owner_line(line: str) -> bool:
    """True when ``owner`` is one of the line's comma-separated principals."""
    principal = line.split(None, 1)[0]
    return "owner" in principal.split(",")


def _owner_lines(text: str) -> list[str]:
    """Non-comment lines that name the ``owner`` principal."""
    found: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _is_owner_line(line):
            found.append(line)
    return found


def _owner_public_key(line: str) -> bytes | None:
    """The key blob on one owner line, or None when the line has no key."""
    fields = line.split()
    if len(fields) < 3 or not _is_owner_line(line):
        return None
    rest = fields[1:]
    # An options field (namespaces="...") sits between the principal and the type.
    if "=" in rest[0] and not rest[0].startswith(("ssh-", "ecdsa-", "sk-")):
        rest = rest[1:]
    if len(rest) < 2:
        return None
    try:
        return base64.b64decode(rest[1], validate=True)
    except (ValueError, TypeError):
        return None


def _signers_problems(path: Path) -> tuple[list[str], bytes | None]:
    """Problems for the allowed-signers file, and the owner blob when it is unique."""
    if not path.is_file():
        return [f"{SIGNERS_NAME} is missing"], None
    mode = stat.S_IMODE(path.stat().st_mode)
    problems: list[str] = []
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        problems.append(f"{SIGNERS_NAME} is group or world writable")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        problems.append(f"{SIGNERS_NAME} lacks exactly one owner line")
        return problems, None
    lines = _owner_lines(text)
    if len(lines) != 1:
        problems.append(f"{SIGNERS_NAME} lacks exactly one owner line")
        return problems, None
    return problems, _owner_public_key(lines[0])


def _mode_allows(mode: int, st_uid: int, st_gid: int, ids: UnixIds, owner_bit: int, group_bit: int, other_bit: int) -> bool:
    if st_uid == ids.uid:
        return bool(mode & owner_bit)
    if st_gid in ids.gids:
        return bool(mode & group_bit)
    return bool(mode & other_bit)


def readable_by(path: Path, ids: UnixIds) -> bool:
    """True when mode bits let ``ids`` read ``path`` and search every parent.

    Owner, group, and other bits are applied the way Unix permission checks
    are: the owner bits win when the uid matches, otherwise the group bits
    when the file's gid is in ``ids.gids``, otherwise the other bits. A parent
    must grant search (execute). Root is not a bypass; only the mode bits count.
    """
    try:
        current = Path(path).resolve()
        file_stat = current.stat()
    except OSError:
        return False
    if not _mode_allows(
        file_stat.st_mode, file_stat.st_uid, file_stat.st_gid, ids, stat.S_IRUSR, stat.S_IRGRP, stat.S_IROTH
    ):
        return False
    for parent in current.parents:
        try:
            parent_stat = parent.stat()
        except OSError:
            return False
        if not _mode_allows(
            parent_stat.st_mode,
            parent_stat.st_uid,
            parent_stat.st_gid,
            ids,
            stat.S_IXUSR,
            stat.S_IXGRP,
            stat.S_IXOTH,
        ):
            return False
    return True


def _resolve_user(name: str) -> UnixIds | None:
    try:
        record = pwd.getpwnam(name)
    except KeyError:
        return None
    return UnixIds(record.pw_uid, frozenset(os.getgrouplist(name, record.pw_gid)))


def _pem_payload(data: bytes) -> bytes | None:
    start = data.find(_BEGIN)
    if start < 0:
        return None
    stop = data.find(_END, start + len(_BEGIN))
    if stop < 0:
        return None
    armor = b"".join(data[start + len(_BEGIN) : stop].split())
    try:
        return base64.b64decode(armor)
    except (ValueError, TypeError):
        return None


def _ssh_string(blob: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 4 > len(blob):
        raise ValueError("short")
    length = int.from_bytes(blob[offset : offset + 4], "big")
    offset += 4
    if length < 0 or offset + length > len(blob):
        raise ValueError("short")
    return blob[offset : offset + length], offset + length


def _header_public_keys(data: bytes) -> list[bytes]:
    """Public key blobs from an ``openssh-key-v1`` header, or an empty list."""
    blob = _pem_payload(data)
    if blob is None:
        blob = data if data.startswith(_OPENSSH_MAGIC) else None
    if blob is None or not blob.startswith(_OPENSSH_MAGIC):
        return []
    try:
        offset = len(_OPENSSH_MAGIC)
        _cipher, offset = _ssh_string(blob, offset)
        _kdf, offset = _ssh_string(blob, offset)
        _options, offset = _ssh_string(blob, offset)
        if offset + 4 > len(blob):
            return []
        count = int.from_bytes(blob[offset : offset + 4], "big")
        offset += 4
        if count < 1 or count > 32:
            return []
        keys: list[bytes] = []
        for _ in range(count):
            key, offset = _ssh_string(blob, offset)
            keys.append(key)
        return keys
    except ValueError:
        return []


def _scan_files(root: Path) -> Iterator[Path]:
    """Regular files under ``root``. Symlinks are not followed or reported."""
    if root.is_symlink():
        return
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        return
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            candidate = Path(dirpath) / name
            if candidate.is_symlink():
                continue
            try:
                info = candidate.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_size > _MAX_SCAN_BYTES:
                continue
            yield candidate


def _scan_problems(roots: Sequence[Path], owner_key: bytes | None) -> list[str]:
    if owner_key is None:
        return []
    found: list[str] = []
    seen: set[Path] = set()
    for root in roots:
        for candidate in _scan_files(Path(root)):
            resolved = candidate
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                data = candidate.read_bytes()
            except OSError:
                continue
            if owner_key in _header_public_keys(data):
                found.append(f"owner private key: {candidate}")
    found.sort()
    return found


def check(
    config_dir: Path,
    *,
    key: Path | None = None,
    user: str | None = None,
    ids: UnixIds | None = None,
    scan: Sequence[Path] = (),
) -> list[str]:
    """Return one string per custody problem. An empty list is a clean setup.

    ``ids`` supplies a synthetic uid and group set. When it is omitted and
    ``user`` is set, the ids are resolved from the password database. ``key``
    without a user or ids is itself a problem.
    """
    problems, owner_key = _signers_problems(_signers_path(config_dir))
    if key is not None:
        who = user if user else None
        resolved = ids if ids is not None else (_resolve_user(user) if user else None)
        if resolved is None:
            problems.append(f"unknown user: {user}" if user else "--key requires --user")
        elif readable_by(key, resolved):
            label = who if who else f"uid {resolved.uid}"
            problems.append(f"{key} is readable by {label}")
    problems.extend(_scan_problems(scan, owner_key))
    return problems


def add_parser(subcommands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subcommands.add_parser("owner-key")
    commands = parser.add_subparsers(dest="owner_key_command", required=True)
    check_parser = commands.add_parser("check")
    check_parser.add_argument("--config-dir", type=Path, default=None)
    check_parser.add_argument("--key", type=Path, default=None)
    check_parser.add_argument("--user", default=None)
    check_parser.add_argument("--scan", action="append", type=Path, default=[])


def run(args: argparse.Namespace) -> int:
    """Run ``owner-key check``. Prints each problem and returns 1 when any exist."""
    if getattr(args, "owner_key_command", None) != "check":
        return 2
    if (args.key is None) != (args.user is None):
        print("owner-key check: --key and --user must be given together")
        return 2
    config_dir = args.config_dir
    if config_dir is None:
        from .operations.config import OperationsConfig

        config_dir = OperationsConfig.defaults().config_dir
    problems = check(
        config_dir,
        key=args.key,
        user=args.user,
        scan=tuple(args.scan or ()),
    )
    for problem in problems:
        print(problem)
    return 1 if problems else 0
