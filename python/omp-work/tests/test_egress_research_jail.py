"""Read-only bind remounts keep flags a user namespace refuses to drop.

The cases fake mountinfo. They do not mount, and they do not need root.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omp_work import egress_research_jail as jail


def _flags(*names: str) -> int:
    flags = jail._MS_BIND | jail._MS_REMOUNT | jail._MS_RDONLY
    for name in names:
        flags |= jail._REMOUNT_KEEP[name]
    return flags


def _mountinfo_line(point: str, options: str, optional: str = "-") -> str:
    # Superblock options after "-" omit the locked flags. The remount must
    # read field 6, not those options.
    return f"10 1 0:1 / {point} {options} {optional} ext4 /dev/root rw"


def test_rbind_readonly_keeps_locked_flags_on_each_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "research-root" / "usr"
    destination = os.path.normpath(str(target))
    child = destination + "/lib/extra"
    spaced = destination + "/my lib"
    decoy = destination + "-other"
    text = "\n".join(
        (
            _mountinfo_line("/usr", "rw,nosuid,nodev,noexec,relatime", "master:1 -"),
            _mountinfo_line(destination, "rw,nosuid,relatime", "master:1 -"),
            _mountinfo_line(child, "rw,nodev,noexec,relatime", "shared:2 -"),
            _mountinfo_line(destination + r"/my\040lib", "rw,nosuid,nodev"),
            _mountinfo_line(decoy, "rw,nodev,noexec"),
            "",
        )
    )
    calls: list[tuple[str, str, str | None, int]] = []

    def fake_mount(source: str, dest: str, fstype: str | None, flags: int) -> None:
        calls.append((source, dest, fstype, flags))

    monkeypatch.setattr(jail, "_mount", fake_mount)
    monkeypatch.setattr(jail, "_read_mountinfo", lambda: text)

    jail._rbind_readonly("/usr", target)

    assert calls[0] == ("/usr", destination, None, jail._MS_BIND | jail._MS_REC)
    assert [call[1] for call in calls[1:]] == [child, spaced, destination]
    assert calls[1][3] == _flags("nodev", "noexec", "relatime")
    assert calls[2][3] == _flags("nosuid", "nodev")
    assert calls[3][3] == _flags("nosuid", "relatime")
    assert all(call[3] & jail._MS_REC == 0 for call in calls[1:])
    assert decoy not in {call[1] for call in calls}
    assert "/usr" not in {call[1] for call in calls[1:]}


def test_bind_ro_file_keeps_nosuid_from_mountinfo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "secret.txt"
    source.write_text("secret", encoding="utf-8")
    target = tmp_path / "jail" / "secret.txt"
    text = (
        _mountinfo_line(
            os.path.normpath(str(target)), "rw,nosuid,relatime", "master:1 -"
        )
        + "\n"
    )
    calls: list[tuple[str, str, str | None, int]] = []

    def fake_mount(source_path: str, dest: str, fstype: str | None, flags: int) -> None:
        calls.append((source_path, dest, fstype, flags))

    monkeypatch.setattr(jail, "_mount", fake_mount)
    monkeypatch.setattr(jail, "_read_mountinfo", lambda: text)

    jail._bind_ro_file(str(source), target)

    assert calls[0][:3] == (str(source), str(target), None)
    assert calls[0][3] == jail._MS_BIND
    assert calls[1] == ("none", str(target), None, _flags("nosuid", "relatime"))
