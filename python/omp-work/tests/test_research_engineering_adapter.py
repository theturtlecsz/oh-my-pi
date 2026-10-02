"""Tests for research engineering candidate freeze and materialize adapter (R07, OMP-315)."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
import subprocess  # nosec B404
import tarfile

import pytest

from omp_work.research.engineering import freeze_candidate, materialize


@pytest.fixture
def git_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Create a temporary git repository with two distinct commits."""
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)  # nosec B603
    subprocess.run(["git", "config", "user.name", "Test Committer"], cwd=repo, check=True, capture_output=True)  # nosec B603
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)  # nosec B603

    file_a = repo / "hello.txt"
    file_a.write_text("hello world\n", encoding="utf-8")
    sub_dir = repo / "nested"
    sub_dir.mkdir()
    (sub_dir / "inner.txt").write_text("inner content\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)  # nosec B603
    subprocess.run(["git", "commit", "-m", "first commit"], cwd=repo, check=True, capture_output=True)  # nosec B603
    rev1 = subprocess.run(  # nosec B603
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    file_b = repo / "second.txt"
    file_b.write_text("second file\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)  # nosec B603
    subprocess.run(["git", "commit", "-m", "second commit"], cwd=repo, check=True, capture_output=True)  # nosec B603
    rev2 = subprocess.run(  # nosec B603
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    return repo, rev1, rev2


def test_freeze_candidate_deterministic(git_repo: tuple[Path, str, str]) -> None:
    """Same commit archived twice yields the exact same bytes and digest."""
    repo, rev1, _ = git_repo
    tar1, sha1 = freeze_candidate(repo, rev1)
    tar2, sha2 = freeze_candidate(repo, rev1)

    assert isinstance(tar1, bytes)
    assert isinstance(sha1, str)
    assert len(sha1) == 64
    assert sha1 == hashlib.sha256(tar1).hexdigest()
    assert tar1 == tar2
    assert sha1 == sha2


def test_freeze_candidate_other_commit(git_repo: tuple[Path, str, str]) -> None:
    """Different commits yield different bytes and digests."""
    repo, rev1, rev2 = git_repo
    tar1, sha1 = freeze_candidate(repo, rev1)
    tar2, sha2 = freeze_candidate(repo, rev2)

    assert sha1 != sha2
    assert tar1 != tar2


def test_materialize_yields_commit_files(git_repo: tuple[Path, str, str], tmp_path: Path) -> None:
    """Materialize extracts exactly the commit's files and directories."""
    repo, rev1, rev2 = git_repo
    tar1, sha1 = freeze_candidate(repo, rev1)

    dest1 = tmp_path / "dest1"
    out1 = materialize(tar1, sha1, dest1)
    assert out1 == dest1
    assert (dest1 / "hello.txt").read_text(encoding="utf-8") == "hello world\n"
    assert (dest1 / "nested" / "inner.txt").read_text(encoding="utf-8") == "inner content\n"
    assert not (dest1 / "second.txt").exists()

    tar2, sha2 = freeze_candidate(repo, rev2)
    dest2 = tmp_path / "dest2"
    materialize(tar2, sha2, dest2)
    assert (dest2 / "hello.txt").read_text(encoding="utf-8") == "hello world\n"
    assert (dest2 / "nested" / "inner.txt").read_text(encoding="utf-8") == "inner content\n"
    assert (dest2 / "second.txt").read_text(encoding="utf-8") == "second file\n"


def test_materialize_refuses_tampered_bytes(git_repo: tuple[Path, str, str], tmp_path: Path) -> None:
    """Tampered archive bytes fail the digest check before writing anything."""
    repo, rev1, _ = git_repo
    tar1, sha1 = freeze_candidate(repo, rev1)

    tampered = bytearray(tar1)
    tampered[100] ^= 0xFF
    dest = tmp_path / "dest_tampered"

    with pytest.raises(ValueError, match="digest mismatch"):
        materialize(bytes(tampered), sha1, dest)

    assert not dest.exists()


def test_materialize_refuses_dotdot_member(tmp_path: Path) -> None:
    """Archive with a '..' member is refused and nothing is written outside dest."""
    dest = tmp_path / "dest_dotdot"
    outside_file = tmp_path / "outside_marker.txt"

    buf = io.BytesIO()
    with tarfile.open(mode="w", fileobj=buf) as tf:
        ti = tarfile.TarInfo(name="../outside_marker.txt")
        content = b"escape outside"
        ti.size = len(content)
        tf.addfile(ti, io.BytesIO(content))

    data = buf.getvalue()
    digest = hashlib.sha256(data).hexdigest()

    with pytest.raises(ValueError, match="refused '..' member"):
        materialize(data, digest, dest)

    assert not outside_file.exists()
    assert not dest.exists()


def test_materialize_refuses_symlink_member(tmp_path: Path) -> None:
    """Archive with a symlink member is refused before writing."""
    dest = tmp_path / "dest_symlink"

    buf = io.BytesIO()
    with tarfile.open(mode="w", fileobj=buf) as tf:
        ti = tarfile.TarInfo(name="escape_symlink")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd"
        tf.addfile(ti)

    data = buf.getvalue()
    digest = hashlib.sha256(data).hexdigest()

    with pytest.raises(ValueError, match="refused symlink member"):
        materialize(data, digest, dest)

    assert not dest.exists()


def test_materialize_refuses_hardlink_member(tmp_path: Path) -> None:
    """Archive with a hardlink member is refused before writing."""
    dest = tmp_path / "dest_hardlink"

    buf = io.BytesIO()
    with tarfile.open(mode="w", fileobj=buf) as tf:
        ti = tarfile.TarInfo(name="escape_hardlink")
        ti.type = tarfile.LNKTYPE
        ti.linkname = "some_target"
        tf.addfile(ti)

    data = buf.getvalue()
    digest = hashlib.sha256(data).hexdigest()

    with pytest.raises(ValueError, match="refused hardlink member"):
        materialize(data, digest, dest)

    assert not dest.exists()


def test_materialize_refuses_device_member(tmp_path: Path) -> None:
    """Archive with a character device member is refused before writing."""
    dest = tmp_path / "dest_device"

    buf = io.BytesIO()
    with tarfile.open(mode="w", fileobj=buf) as tf:
        ti = tarfile.TarInfo(name="char_dev")
        ti.type = tarfile.CHRTYPE
        tf.addfile(ti)

    data = buf.getvalue()
    digest = hashlib.sha256(data).hexdigest()

    with pytest.raises(ValueError, match="refused device member"):
        materialize(data, digest, dest)

    assert not dest.exists()


def test_materialize_refuses_absolute_member(tmp_path: Path) -> None:
    """Archive with an absolute path member is refused before writing."""
    dest = tmp_path / "dest_abs"

    buf = io.BytesIO()
    with tarfile.open(mode="w", fileobj=buf) as tf:
        ti = tarfile.TarInfo(name="/absolute/path.txt")
        content = b"root"
        ti.size = len(content)
        tf.addfile(ti, io.BytesIO(content))

    data = buf.getvalue()
    digest = hashlib.sha256(data).hexdigest()

    with pytest.raises(ValueError, match="refused absolute member"):
        materialize(data, digest, dest)

    assert not dest.exists()
