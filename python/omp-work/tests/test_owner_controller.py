from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from omp_work.v1.owner_controller import (
    DESIGNATION_NAME,
    designated_controller,
    designation_message,
    write_designation,
)
from omp_work.v1.owner_signature import NAMESPACE

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None,
    reason="ssh-keygen is not installed",
)

_WORKSPACE_ID = UUID("11111111-1111-1111-1111-111111111111")
_OTHER_WORKSPACE_ID = UUID("33333333-3333-3333-3333-333333333333")
_CONTROLLER_ID = UUID("22222222-2222-2222-2222-222222222222")
_EXPECTED_MESSAGE = (
    b'{"controller_actor_id":"22222222-2222-2222-2222-222222222222",'
    b'"purpose":"designate_owner_controller",'
    b'"workspace_id":"11111111-1111-1111-1111-111111111111"}'
)


def _generate_key(tmp_path: Path, name: str) -> Path:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def _allowed_signers(directory: Path, public_key: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "owner_allowed_signers"
    path.write_text(f"owner {public_key.read_text().strip()}\n", encoding="utf-8")
    return path


def _sign(key: Path, message: bytes, *, namespace: str = NAMESPACE) -> str:
    completed = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", namespace],
        input=message,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("ascii")


def test_message_bytes_are_sorted_compact_json() -> None:
    assert designation_message(_WORKSPACE_ID, _CONTROLLER_ID) == _EXPECTED_MESSAGE


def test_signed_designation_names_the_controller(tmp_path: Path) -> None:
    key = _generate_key(tmp_path, "owner")
    _allowed_signers(tmp_path, Path(f"{key}.pub"))
    signature = _sign(key, designation_message(_WORKSPACE_ID, _CONTROLLER_ID))

    write_designation(tmp_path, _WORKSPACE_ID, _CONTROLLER_ID, signature)

    assert designated_controller(tmp_path, _WORKSPACE_ID) == _CONTROLLER_ID
    assert (tmp_path / DESIGNATION_NAME).stat().st_mode & 0o777 == 0o600


def test_tampered_signature_returns_none(tmp_path: Path) -> None:
    key = _generate_key(tmp_path, "owner")
    _allowed_signers(tmp_path, Path(f"{key}.pub"))
    signature = _sign(key, designation_message(_WORKSPACE_ID, _CONTROLLER_ID))
    write_designation(tmp_path, _WORKSPACE_ID, _CONTROLLER_ID, signature)

    path = tmp_path / DESIGNATION_NAME
    stored = json.loads(path.read_text(encoding="utf-8"))
    signature = stored["owner_signature"]
    # Flip one base64 character inside the signature body (not the PEM armor).
    body_at = signature.index("\n") + 12
    stored["owner_signature"] = (
        signature[:body_at]
        + ("A" if signature[body_at] != "A" else "B")
        + signature[body_at + 1 :]
    )
    path.write_text(json.dumps(stored), encoding="utf-8")

    assert designated_controller(tmp_path, _WORKSPACE_ID) is None


def test_other_workspace_does_not_match(tmp_path: Path) -> None:
    key = _generate_key(tmp_path, "owner")
    _allowed_signers(tmp_path, Path(f"{key}.pub"))
    signature = _sign(key, designation_message(_WORKSPACE_ID, _CONTROLLER_ID))
    write_designation(tmp_path, _WORKSPACE_ID, _CONTROLLER_ID, signature)

    assert designated_controller(tmp_path, _OTHER_WORKSPACE_ID) is None


def test_signature_by_another_key_is_not_a_designation(tmp_path: Path) -> None:
    owner = _generate_key(tmp_path, "owner")
    other = _generate_key(tmp_path, "other")
    _allowed_signers(tmp_path, Path(f"{owner}.pub"))
    signature = _sign(other, designation_message(_WORKSPACE_ID, _CONTROLLER_ID))

    with pytest.raises(ValueError):
        write_designation(tmp_path, _WORKSPACE_ID, _CONTROLLER_ID, signature)
    assert not (tmp_path / DESIGNATION_NAME).exists()
    assert designated_controller(tmp_path, _WORKSPACE_ID) is None


def test_missing_designation_returns_none(tmp_path: Path) -> None:
    key = _generate_key(tmp_path, "owner")
    _allowed_signers(tmp_path, Path(f"{key}.pub"))

    assert designated_controller(tmp_path, _WORKSPACE_ID) is None


def test_redesignation_replaces_the_controller(tmp_path: Path) -> None:
    key = _generate_key(tmp_path, "owner")
    _allowed_signers(tmp_path, Path(f"{key}.pub"))
    write_designation(
        tmp_path,
        _WORKSPACE_ID,
        _CONTROLLER_ID,
        _sign(key, designation_message(_WORKSPACE_ID, _CONTROLLER_ID)),
    )

    replacement = UUID("44444444-4444-4444-4444-444444444444")
    write_designation(
        tmp_path,
        _WORKSPACE_ID,
        replacement,
        _sign(key, designation_message(_WORKSPACE_ID, replacement)),
    )

    assert designated_controller(tmp_path, _WORKSPACE_ID) == replacement


def test_client_credential_lacks_work_approve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client credential can stop but never approve: work.approve absent."""
    from omp_work.__main__ import main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    workspace_id = UUID("55555555-5555-5555-5555-555555555555")

    assert (
        main(
            [
                "ops",
                "capabilities",
                "client",
                "--name",
                "monitor",
                "--workspace-id",
                str(workspace_id),
            ]
        )
        == 0
    )
    data = json.loads(
        (tmp_path / "omp" / "work-ledger" / "capabilities" / "monitor.json").read_text()
    )
    assert data["actor_kind"] == "client"
    assert "work.stop" in data["scopes"]
    assert "work.approve" not in data["scopes"]


def test_controller_cli_message_designate_show(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`ops controller` emits the message, keeps a bad-signature file absent,
    and writes/reads the designation."""
    from omp_work.__main__ import main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config_dir = tmp_path / "omp" / "work-ledger"
    workspace_id = _WORKSPACE_ID
    key = _generate_key(tmp_path, "owner")
    _allowed_signers(config_dir, Path(f"{key}.pub"))

    assert (
        main(
            [
                "ops",
                "controller",
                "message",
                "--workspace-id",
                str(workspace_id),
                "--actor-id",
                str(_CONTROLLER_ID),
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.encode() == _EXPECTED_MESSAGE + b"\n"

    # A bad signature exits 1 and leaves no designation file behind.
    bad = tmp_path / "bad.sig"
    bad.write_text(_sign(_generate_key(tmp_path, "other"), designation_message(workspace_id, _CONTROLLER_ID)))
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "ops",
                "controller",
                "designate",
                "--workspace-id",
                str(workspace_id),
                "--actor-id",
                str(_CONTROLLER_ID),
                "--signature-file",
                str(bad),
            ]
        )
    assert excinfo.value.code == 1
    assert not (config_dir / DESIGNATION_NAME).exists()

    good = tmp_path / "good.sig"
    good.write_text(_sign(key, designation_message(workspace_id, _CONTROLLER_ID)))
    assert (
        main(
            [
                "ops",
                "controller",
                "designate",
                "--workspace-id",
                str(workspace_id),
                "--actor-id",
                str(_CONTROLLER_ID),
                "--signature-file",
                str(good),
            ]
        )
        == 0
    )
    assert (config_dir / DESIGNATION_NAME).stat().st_mode & 0o777 == 0o600
    capsys.readouterr()

    assert (
        main(
            [
                "ops",
                "controller",
                "show",
                "--workspace-id",
                str(workspace_id),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "controller_actor_id": str(_CONTROLLER_ID)
    }
