"""Owner-key custody: no OMP principal signs as owner or holds the owner key."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from omp_work.__main__ import main
from omp_work.owner_key import SIGNERS_NAME, UnixIds, check, readable_by
from omp_work.v1.owner_signature import NAMESPACE, decision_signature_message, verify_owner_signature

_WORKSPACE = "11111111-1111-1111-1111-111111111111"
_DECISION = "22222222-2222-2222-2222-222222222222"


def _message() -> bytes:
    from uuid import UUID

    return decision_signature_message(
        workspace_id=UUID(_WORKSPACE),
        decision_id=UUID(_DECISION),
        action_class="tier3",
        answer="approve",
    )


def _generate_key(directory: Path, name: str, *, passphrase: str = "") -> Path:
    key = directory / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-f", str(key), "-C", name],
        check=True,
        capture_output=True,
    )
    return key


def _sign(key: Path, message: bytes) -> str:
    completed = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", NAMESPACE],
        input=message,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("ascii")


def _write_signers(config_dir: Path, public_key: Path, *, mode: int = 0o600, extra: str = "") -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / SIGNERS_NAME
    body = f"owner {public_key.read_text(encoding='utf-8').strip()}\n{extra}"
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


pytestmark_ssh = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is not installed")


@pytestmark_ssh
@pytest.mark.parametrize("name", ["grokbot", "automation", "service"])
def test_principal_key_does_not_verify(tmp_path: Path, name: str) -> None:
    owner = _generate_key(tmp_path, "owner")
    other = _generate_key(tmp_path, name)
    extra = f"{name} {Path(f'{other}.pub').read_text(encoding='utf-8').strip()}\n"
    allowed = _write_signers(tmp_path / "cfg", Path(f"{owner}.pub"), extra=extra)
    message = _message()
    signature = _sign(other, message)
    assert verify_owner_signature(allowed, message, signature) is False


@pytestmark_ssh
def test_capability_token_does_not_verify(tmp_path: Path) -> None:
    owner = _generate_key(tmp_path, "owner")
    allowed = _write_signers(tmp_path / "cfg", Path(f"{owner}.pub"))
    message = _message()
    token = json.dumps(
        {
            "actor_id": "33333333-3333-3333-3333-333333333333",
            "actor_kind": "automation",
            "scopes": ["work.execute", "work.mutate"],
            "token": "capability-token-not-a-signature",
        },
        sort_keys=True,
    )
    assert verify_owner_signature(allowed, message, token) is False
    assert verify_owner_signature(allowed, message, "capability-token-not-a-signature") is False


def test_missing_signers(tmp_path: Path) -> None:
    problems = check(tmp_path)
    assert problems == [f"{SIGNERS_NAME} is missing"]


def test_group_or_world_writable_signers(tmp_path: Path) -> None:
    path = tmp_path / SIGNERS_NAME
    path.write_text("owner ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForModeOnly\n", encoding="utf-8")
    path.chmod(0o660)
    assert f"{SIGNERS_NAME} is group or world writable" in check(tmp_path)
    path.chmod(0o606)
    assert f"{SIGNERS_NAME} is group or world writable" in check(tmp_path)
    path.chmod(0o644)
    assert f"{SIGNERS_NAME} is group or world writable" not in check(tmp_path)


def test_owner_line_count(tmp_path: Path) -> None:
    path = tmp_path / SIGNERS_NAME
    path.write_text("# only a comment\ngrokbot ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINotTheOwner\n", encoding="utf-8")
    path.chmod(0o600)
    assert f"{SIGNERS_NAME} lacks exactly one owner line" in check(tmp_path)

    path.write_text(
        "owner ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOne\n"
        "owner ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITwo\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    assert f"{SIGNERS_NAME} lacks exactly one owner line" in check(tmp_path)


def test_writable_and_bad_line_are_both_reported(tmp_path: Path) -> None:
    path = tmp_path / SIGNERS_NAME
    path.write_text("grokbot ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINotTheOwner\n", encoding="utf-8")
    path.chmod(0o666)
    problems = check(tmp_path)
    assert f"{SIGNERS_NAME} is group or world writable" in problems
    assert f"{SIGNERS_NAME} lacks exactly one owner line" in problems


def _other_ids() -> UnixIds:
    return UnixIds(uid=os.getuid() + 1, gids=frozenset({os.getgid() + 1}))


def test_key_readable_by_mode_bits(tmp_path: Path) -> None:
    key = tmp_path / "owner_key"
    key.write_text("key\n", encoding="utf-8")
    owner = UnixIds(uid=os.getuid(), gids=frozenset({os.getgid()}))
    key.chmod(0o600)
    assert readable_by(key, owner) is True
    key.chmod(0o000)
    assert readable_by(key, owner) is False

    key.chmod(0o666)
    assert readable_by(key, _other_ids()) is False  # parent search blocks other


def _grant_search(key: Path, bit: int) -> list[tuple[Path, int]]:
    """Add ``bit`` on temp parents that lack it. Returns ``(path, old mode)``."""
    changed: list[tuple[Path, int]] = []
    temp_root = Path(os.environ.get("TMPDIR", "/tmp")).resolve()
    for parent in key.resolve().parents:
        info = parent.stat()
        if info.st_mode & bit:
            continue
        if temp_root not in parent.parents and parent != temp_root:
            pytest.skip(f"refusing to change search bits on {parent}")
        if info.st_uid != os.getuid():
            pytest.skip(f"cannot open search on {parent}")
        old = stat.S_IMODE(info.st_mode)
        parent.chmod(old | bit)
        changed.append((parent, old))
    return changed


def _restore_modes(changed: list[tuple[Path, int]]) -> None:
    for parent, old in reversed(changed):
        parent.chmod(old)


def test_other_read_follows_parent_search(tmp_path: Path) -> None:
    key = tmp_path / "owner_key"
    key.write_text("key\n", encoding="utf-8")
    key.chmod(0o666)
    other = _other_ids()
    assert readable_by(key, other) is False
    changed = _grant_search(key, stat.S_IXOTH)
    try:
        assert readable_by(key, other) is True
        key.chmod(0o600)
        assert readable_by(key, other) is False
    finally:
        _restore_modes(changed)


def test_group_read_follows_parent_search(tmp_path: Path) -> None:
    key = tmp_path / "owner_key"
    key.write_text("key\n", encoding="utf-8")
    key.chmod(0o640)
    group = UnixIds(uid=os.getuid() + 1, gids=frozenset({key.stat().st_gid}))
    assert readable_by(key, group) is False
    changed = _grant_search(key, stat.S_IXGRP)
    try:
        assert readable_by(key, group) is True
        key.parent.chmod(0o700)
        assert readable_by(key, group) is False
    finally:
        _restore_modes(changed)


def test_check_reports_readable_key_for_synthetic_ids(tmp_path: Path) -> None:
    signers = tmp_path / "cfg"
    signers.mkdir()
    allowed = signers / SIGNERS_NAME
    allowed.write_text("owner ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForModeOnly\n", encoding="utf-8")
    allowed.chmod(0o600)
    key = tmp_path / "id_ed25519"
    key.write_text("private\n", encoding="utf-8")
    key.chmod(0o600)
    owner = UnixIds(uid=os.getuid(), gids=frozenset({os.getgid()}))
    problems = check(signers, key=key, ids=owner)
    assert any(str(key) in problem and "readable" in problem for problem in problems)
    key.chmod(0o000)
    assert check(signers, key=key, ids=owner) == []


@pytestmark_ssh
def test_scan_finds_owner_private_key_only(tmp_path: Path) -> None:
    limit = 64 * 1024
    owner = _generate_key(tmp_path, "owner")
    other = _generate_key(tmp_path, "service")
    config = tmp_path / "cfg"
    _write_signers(config, Path(f"{owner}.pub"))
    scan = tmp_path / "scan"
    nested = scan / "nested"
    nested.mkdir(parents=True)
    owner_copy = nested / "id_ed25519"
    owner_copy.write_bytes(owner.read_bytes())
    (scan / "service_key").write_bytes(other.read_bytes())
    link = scan / "link"
    link.symlink_to(owner_copy)
    linked_dir = tmp_path / "elsewhere"
    linked_dir.mkdir()
    (linked_dir / "hidden").write_bytes(owner.read_bytes())
    (scan / "via-dir").symlink_to(linked_dir, target_is_directory=True)
    (scan / "huge").write_bytes(owner.read_bytes() + b"\n" + (b"x" * (limit + 1)))
    exact = scan / "exact"
    body = owner.read_bytes()
    body = body + b"\n" + (b" " * (limit - len(body) - 1))
    exact.write_bytes(body)

    problems = check(config, scan=(scan,))
    assert problems == sorted(
        [
            f"owner private key: {owner_copy}",
            f"owner private key: {exact}",
        ]
    )


@pytestmark_ssh
def test_encrypted_owner_key_is_found(tmp_path: Path) -> None:
    owner = _generate_key(tmp_path, "owner", passphrase="secret")
    config = tmp_path / "cfg"
    _write_signers(config, Path(f"{owner}.pub"))
    scan = tmp_path / "scan"
    scan.mkdir()
    copy = scan / "id_ed25519"
    copy.write_bytes(owner.read_bytes())
    problems = check(config, scan=(scan,))
    assert problems == [f"owner private key: {copy}"]


@pytestmark_ssh
def test_each_problem_and_clean_setup(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    owner = _generate_key(tmp_path, "owner")
    other = _generate_key(tmp_path, "automation")
    config = tmp_path / "cfg"
    _write_signers(config, Path(f"{owner}.pub"), mode=0o666)
    key = tmp_path / "exposed"
    key.write_bytes(owner.read_bytes())
    key.chmod(0o644)
    scan = tmp_path / "scan"
    scan.mkdir()
    planted = scan / "id_ed25519"
    planted.write_bytes(owner.read_bytes())
    (scan / "other").write_bytes(other.read_bytes())
    user = _current_user()
    code = main(
        [
            "owner-key",
            "check",
            "--config-dir",
            str(config),
            "--key",
            str(key),
            "--user",
            user,
            "--scan",
            str(scan),
        ]
    )
    captured = capsys.readouterr().out
    assert code == 1
    assert f"{SIGNERS_NAME} is group or world writable" in captured
    assert f"{key} is readable by {user}" in captured
    assert f"owner private key: {planted}" in captured
    assert f"owner private key: {scan / 'other'}" not in captured

    config.joinpath(SIGNERS_NAME).chmod(0o600)
    key.chmod(0o000)
    clean_scan = tmp_path / "clean-scan"
    clean_scan.mkdir()
    (clean_scan / "other").write_bytes(other.read_bytes())
    (clean_scan / "link").symlink_to(key)
    code = main(
        [
            "owner-key",
            "check",
            "--config-dir",
            str(config),
            "--key",
            str(key),
            "--user",
            user,
            "--scan",
            str(clean_scan),
        ]
    )
    assert code == 0
    assert capsys.readouterr().out == ""


def _current_user() -> str:
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name


def test_key_without_user_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["owner-key", "check", "--key", "/tmp/nope"])
    assert code == 2
    assert "together" in capsys.readouterr().out
