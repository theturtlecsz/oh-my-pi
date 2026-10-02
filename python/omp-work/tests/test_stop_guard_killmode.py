"""OMP-537-s01: tests for stop guard KillMode inspection, override, and preflight."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omp_work.__main__ import main
from omp_work.operations.stop import (
    GUARD_DROP_IN_NAME,
    KILLMODE_OVERRIDE_CONTENT,
    KILLMODE_OVERRIDE_DROP_IN_NAME,
    install_guards,
)


def test_unit_killmode_control_group_with_drop_in_process_overridden(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = "worker.service"
    unit_file = tmp_path / unit
    unit_file.write_text("[Service]\nKillMode=control-group\n", encoding="utf-8")

    drop_in_dir = tmp_path / f"{unit}.d"
    drop_in_dir.mkdir(parents=True, exist_ok=True)
    drop_in_file = drop_in_dir / "killmode-process.conf"
    drop_in_file.write_text("[Service]\nKillMode=process\n", encoding="utf-8")

    written = install_guards([unit], systemd_dir=tmp_path, interval=5)
    captured = capsys.readouterr()

    zz_path = drop_in_dir / KILLMODE_OVERRIDE_DROP_IN_NAME
    assert zz_path.is_file()
    assert zz_path.read_text(encoding="utf-8") == KILLMODE_OVERRIDE_CONTENT

    expected_conf = drop_in_dir / GUARD_DROP_IN_NAME
    expected_watcher = tmp_path / "omp-agent-stop.service"
    assert written == [expected_conf, zz_path, expected_watcher]

    expected_line = (
        f"stop: {unit}: overriding KillMode=process from killmode-process.conf "
        "with KillMode=control-group\n"
    )
    assert expected_line in captured.out


def test_unit_file_killmode_process_without_drop_in_overridden(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = "worker.service"
    unit_file = tmp_path / unit
    unit_file.write_text("[Service]\nKillMode=process\n", encoding="utf-8")

    written = install_guards([unit], systemd_dir=tmp_path, interval=5)
    captured = capsys.readouterr()

    drop_in_dir = tmp_path / f"{unit}.d"
    zz_path = drop_in_dir / KILLMODE_OVERRIDE_DROP_IN_NAME
    assert zz_path.is_file()
    assert zz_path.read_text(encoding="utf-8") == KILLMODE_OVERRIDE_CONTENT

    expected_conf = drop_in_dir / GUARD_DROP_IN_NAME
    expected_watcher = tmp_path / "omp-agent-stop.service"
    assert written == [expected_conf, zz_path, expected_watcher]

    expected_line = (
        f"stop: {unit}: overriding KillMode=process from {unit} "
        "with KillMode=control-group\n"
    )
    assert expected_line in captured.out


def test_unit_file_killmode_none_overridden(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = "worker.service"
    unit_file = tmp_path / unit
    unit_file.write_text("[Service]\nKillMode=none\n", encoding="utf-8")

    written = install_guards([unit], systemd_dir=tmp_path, interval=5)
    captured = capsys.readouterr()

    drop_in_dir = tmp_path / f"{unit}.d"
    zz_path = drop_in_dir / KILLMODE_OVERRIDE_DROP_IN_NAME
    assert zz_path.is_file()
    assert zz_path.read_text(encoding="utf-8") == KILLMODE_OVERRIDE_CONTENT

    expected_line = (
        f"stop: {unit}: overriding KillMode=none from {unit} "
        "with KillMode=control-group\n"
    )
    assert expected_line in captured.out


def test_no_killmode_or_mixed_no_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Unit 1: no KillMode set
    u1 = "no-km.service"
    (tmp_path / u1).write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")

    # Unit 2: KillMode=mixed in unit file
    u2 = "mixed.service"
    (tmp_path / u2).write_text("[Service]\nKillMode=mixed\n", encoding="utf-8")

    # Unit 3: Unit file has KillMode=process, but drop-in overrides with mixed
    u3 = "proc-to-mixed.service"
    (tmp_path / u3).write_text("[Service]\nKillMode=process\n", encoding="utf-8")
    d3 = tmp_path / f"{u3}.d"
    d3.mkdir()
    (d3 / "10-mixed.conf").write_text("[Service]\nKillMode=mixed\n", encoding="utf-8")

    # Unit 4: Unit file has KillMode=process, but drop-in resets with empty value
    u4 = "proc-reset.service"
    (tmp_path / u4).write_text("[Service]\nKillMode=process\n", encoding="utf-8")
    d4 = tmp_path / f"{u4}.d"
    d4.mkdir()
    (d4 / "10-reset.conf").write_text("[Service]\nKillMode=\n", encoding="utf-8")

    # Unit 5: Unit file has KillMode=process, but drop-in sets control-group
    u5 = "proc-to-cg.service"
    (tmp_path / u5).write_text("[Service]\nKillMode=process\n", encoding="utf-8")
    d5 = tmp_path / f"{u5}.d"
    d5.mkdir()
    (d5 / "10-cg.conf").write_text("[Service]\nKillMode=control-group\n", encoding="utf-8")

    units = [u1, u2, u3, u4, u5]
    written = install_guards(units, systemd_dir=tmp_path, interval=5)
    captured = capsys.readouterr()

    for u in units:
        assert not (tmp_path / f"{u}.d" / KILLMODE_OVERRIDE_DROP_IN_NAME).exists()

    assert not any(p.name == KILLMODE_OVERRIDE_DROP_IN_NAME for p in written)
    assert "overriding KillMode" not in captured.out


def test_preflight_late_drop_in_rejects_and_writes_nothing(
    tmp_path: Path,
) -> None:
    u1 = "valid.service"
    (tmp_path / u1).write_text("[Service]\nKillMode=process\n", encoding="utf-8")

    u2 = "late.service"
    d2 = tmp_path / f"{u2}.d"
    d2.mkdir(parents=True, exist_ok=True)
    late_drop_in = d2 / "zzz-late.conf"
    late_drop_in.write_text("[Service]\nKillMode=process\n", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        install_guards([u1, u2], systemd_dir=tmp_path)

    err_msg = str(excinfo.value)
    assert u2 in err_msg
    assert "zzz-late.conf" in err_msg

    # Nothing must be written for either unit, nor the watcher
    assert not (tmp_path / f"{u1}.d").exists()
    assert not (d2 / GUARD_DROP_IN_NAME).exists()
    assert not (d2 / KILLMODE_OVERRIDE_DROP_IN_NAME).exists()
    assert not (tmp_path / "omp-agent-stop.service").exists()


def test_cli_late_drop_in_returns_code_2_and_prints_unit_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    u1 = "valid.service"
    (tmp_path / u1).write_text("[Service]\nKillMode=process\n", encoding="utf-8")

    u2 = "late.service"
    d2 = tmp_path / f"{u2}.d"
    d2.mkdir(parents=True, exist_ok=True)
    late_drop_in = d2 / "zzz-late.conf"
    late_drop_in.write_text("[Service]\nKillMode=process\n", encoding="utf-8")

    code = main([
        "stop",
        "install-guards",
        "--unit",
        u1,
        "--unit",
        u2,
        "--systemd-dir",
        str(tmp_path),
    ])
    assert code == 2

    captured = capsys.readouterr()
    assert u2 in captured.err
    assert "zzz-late.conf" in captured.err

    # Confirm no files written
    assert not (tmp_path / f"{u1}.d").exists()
    assert not (d2 / GUARD_DROP_IN_NAME).exists()
    assert not (d2 / KILLMODE_OVERRIDE_DROP_IN_NAME).exists()
    assert not (tmp_path / "omp-agent-stop.service").exists()


def test_late_drop_in_without_killmode_does_not_reject(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    u = "late-other.service"
    d = tmp_path / f"{u}.d"
    d.mkdir(parents=True, exist_ok=True)
    (d / "zzz-late.conf").write_text("[Service]\nEnvironment=FOO=BAR\n", encoding="utf-8")

    written = install_guards([u], systemd_dir=tmp_path)
    assert (d / GUARD_DROP_IN_NAME).is_file()
    assert not (d / KILLMODE_OVERRIDE_DROP_IN_NAME).exists()


def test_second_run_same_files_same_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = "flood.service"
    unit_file = tmp_path / unit
    unit_file.write_text("[Service]\nKillMode=process\n", encoding="utf-8")

    written_first = install_guards([unit], systemd_dir=tmp_path, interval=5)
    captured_first = capsys.readouterr()

    zz_path = tmp_path / f"{unit}.d" / KILLMODE_OVERRIDE_DROP_IN_NAME
    assert zz_path.is_file()
    assert zz_path.read_text(encoding="utf-8") == KILLMODE_OVERRIDE_CONTENT

    written_second = install_guards([unit], systemd_dir=tmp_path, interval=5)
    captured_second = capsys.readouterr()

    assert written_first == written_second
    assert captured_first.out == captured_second.out
    assert zz_path.read_text(encoding="utf-8") == KILLMODE_OVERRIDE_CONTENT


def test_killmode_in_non_service_section_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = "custom.service"
    unit_file = tmp_path / unit
    unit_file.write_text("[Unit]\nKillMode=process\n[Service]\nExecStart=/bin/true\n", encoding="utf-8")

    written = install_guards([unit], systemd_dir=tmp_path)
    captured = capsys.readouterr()

    assert not (tmp_path / f"{unit}.d" / KILLMODE_OVERRIDE_DROP_IN_NAME).exists()
    assert "overriding KillMode" not in captured.out
