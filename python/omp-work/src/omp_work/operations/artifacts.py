from __future__ import annotations

import json
import os
from hashlib import sha256 as bytes_sha256
from pathlib import Path
from subprocess import CalledProcessError, run

from omp_work.v1.canonical import canonical_json, sha256


def _run(arguments: list[str]) -> None:
    try:
        run(arguments, capture_output=True, check=True)
    except CalledProcessError as error:
        raise RuntimeError("artifact cryptography failed") from error


def _install(temporary: Path, destination: Path, mode: int) -> None:
    if destination.exists():
        raise FileExistsError("immutable artifact already exists")
    temporary.chmod(mode)
    try:
        os.link(temporary, destination)
    except FileExistsError as error:
        raise FileExistsError("immutable artifact already exists") from error
    destination.chmod(mode)


def encrypt_file(
    source: Path, destination: Path, passphrase_file: Path, *, mode: int = 0o600
) -> str:
    temporary = destination.with_name(f".{destination.name}.next")
    try:
        _run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--symmetric",
                "--cipher-algo",
                "AES256",
                "--passphrase-file",
                str(passphrase_file),
                "--output",
                str(temporary),
                str(source),
            ]
        )
        _install(temporary, destination, mode)
        return bytes_sha256(destination.read_bytes()).hexdigest()
    finally:
        temporary.unlink(missing_ok=True)


def decrypt_file(
    source: Path, destination: Path, passphrase_file: Path, *, mode: int = 0o600
) -> None:
    temporary = destination.with_name(f".{destination.name}.next")
    try:
        _run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--decrypt",
                "--passphrase-file",
                str(passphrase_file),
                "--output",
                str(temporary),
                str(source),
            ]
        )
        _install(temporary, destination, mode)
    finally:
        temporary.unlink(missing_ok=True)


def resolve_artifact_path(
    relative: str, data_dir: Path, expected_root: Path | None = None
) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError("pagination_count_hash_gap")
    resolved = (data_dir / path).resolve()
    data_root = data_dir.resolve()
    try:
        resolved.relative_to(data_root)
        if expected_root is not None:
            resolved.relative_to(expected_root.resolve())
    except ValueError:
        raise RuntimeError("pagination_count_hash_gap") from None
    return resolved


def write_json_artifact(
    root: Path,
    staging: Path,
    name: str,
    payload: object,
    passphrase_file: Path,
    data_dir: Path | None = None,
    *,
    mode: int = 0o400,
    encrypt_fn=encrypt_file,
    decrypt_fn=decrypt_file,
) -> tuple[str, str, str]:
    digest = sha256(payload)
    encrypted = root / f"{name}-{digest}.json.gpg"
    plain = staging / f"{name}.json"
    plain.write_text(canonical_json(payload), encoding="utf-8")
    plain.chmod(0o600)
    try:
        try:
            ciphertext_hash = encrypt_fn(plain, encrypted, passphrase_file, mode=mode)
        except FileExistsError:
            plain.unlink(missing_ok=True)
            reused = staging / f"reuse-{name}.json"
            ciphertext_hash = bytes_sha256(encrypted.read_bytes()).hexdigest()
            read_json_artifact(
                encrypted,
                reused,
                passphrase_file,
                expected_plaintext_sha256=digest,
                expected_ciphertext_sha256=ciphertext_hash,
                data_dir=data_dir,
                expected_root=root,
                decrypt_fn=decrypt_fn,
            )
    finally:
        plain.unlink(missing_ok=True)
    relative_path = (
        str(encrypted.relative_to(data_dir)) if data_dir is not None else str(encrypted)
    )
    return relative_path, digest, ciphertext_hash


def read_json_artifact(
    encrypted: Path,
    destination: Path,
    passphrase_file: Path,
    *,
    expected_plaintext_sha256: str | None = None,
    expected_ciphertext_sha256: str | None = None,
    data_dir: Path | None = None,
    expected_root: Path | None = None,
    decrypt_fn=decrypt_file,
) -> object:
    if data_dir is not None:
        try:
            rel = encrypted.relative_to(data_dir)
            encrypted = resolve_artifact_path(str(rel), data_dir, expected_root)
        except ValueError:
            raise RuntimeError("pagination_count_hash_gap") from None
    elif expected_root is not None:
        try:
            encrypted.resolve().relative_to(expected_root.resolve())
        except ValueError:
            raise RuntimeError("pagination_count_hash_gap") from None

    if not encrypted.is_file() or encrypted.stat().st_mode & 0o777 != 0o400:
        raise RuntimeError("pagination_count_hash_gap")
    if (
        expected_ciphertext_sha256 is not None
        and bytes_sha256(encrypted.read_bytes()).hexdigest()
        != expected_ciphertext_sha256
    ):
        raise RuntimeError("pagination_count_hash_gap")
    destination.unlink(missing_ok=True)
    try:
        decrypt_fn(encrypted, destination, passphrase_file)
        try:
            payload = json.loads(destination.read_text(encoding="utf-8"))
        except Exception:
            raise RuntimeError("pagination_count_hash_gap") from None
        if (
            expected_plaintext_sha256 is not None
            and sha256(payload) != expected_plaintext_sha256
        ):
            raise RuntimeError("pagination_count_hash_gap")
        return payload
    finally:
        destination.unlink(missing_ok=True)
