from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from omp_work.research import dossier as d

SRC = b"source bytes"
PRIV = "ab" * 32
RECEIPT = "11" * 32


def _record(**over):
    rec = {
        "objective": "Does X beat Y?",
        "sources": [{"id": "s1", "sha256": hashlib.sha256(SRC).hexdigest(), "coverage": "full"}],
        "experiments": [{"name": "e1", "metrics": {"accuracy": 0.91}, "receipt_sha256": RECEIPT}],
        "private_dependencies": [PRIV],
        "decision": "positive",
    }
    rec.update(over)
    return rec


@pytest.fixture
def made(tmp_path):
    root = tmp_path / "a"
    d.write_dossier(root, _record(), source_bytes={"s1": SRC})
    priv = tmp_path / "priv"
    priv.mkdir()
    (priv / PRIV).write_bytes(b"x")
    return root, priv


def test_write_report_csv_and_copy_verifies(made, tmp_path):
    root, _ = made
    for rel in ("dossier.json", "report.md", "tables/results.csv", "sources/s1"):
        assert (root / rel).is_file()
    report = (root / "report.md").read_text()
    assert "s1" in report and "0.91" in report and RECEIPT in report
    assert d.measurements(root) == {"e1": {"accuracy": 0.91}}
    copy = tmp_path / "b"
    shutil.copytree(root, copy)
    assert d.verify_dossier(copy) == []


def test_reproduce_statuses(made, tmp_path):
    root, priv = made
    ok = d.reproduce(root, private_dir=priv, rerun=lambda n: {"accuracy": 0.91})
    assert ok == {"sources": "reproduced", "e1": "reproduced"}
    assert d.reproduce(root, private_dir=tmp_path / "none")["e1"] == f"missing_private_dependency:{PRIV}"
    assert d.reproduce(root, private_dir=priv, rerun=lambda n: {"accuracy": 0.5})["e1"] == "not_reproduced"
    empty = tmp_path / "e"
    d.write_dossier(empty, _record(private_dependencies=[]), source_bytes={"s1": SRC})
    assert d.reproduce(empty)["e1"] == "reproduced"


def test_prose_edit_detected_and_measurements_survive(made):
    root, _ = made
    report = root / "report.md"
    report.write_text(report.read_text() + "\nclaim\n")
    assert d.verify_dossier(root) == ["report_altered"]
    assert d.measurements(root)["e1"]["accuracy"] == 0.91


@pytest.mark.parametrize("decision", ["negative", "inconclusive"])
def test_non_positive_decisions_verify_clean(tmp_path, decision):
    d.write_dossier(tmp_path / "x", _record(decision=decision), source_bytes={"s1": SRC})
    assert d.verify_dossier(tmp_path / "x") == []


def test_bad_path_and_hash_mismatch_write_nothing(tmp_path):
    with pytest.raises(ValueError, match="bad_path"):
        d.write_dossier(tmp_path / "p", _record(plots=["../escape.png"]), plot_bytes={"../escape.png": b"x"})
    assert not (tmp_path / "escape.png").exists()
    with pytest.raises(ValueError, match="artifact_hash_mismatch"):
        d.write_dossier(tmp_path / "m", _record(), source_bytes={"s1": b"other"})
    assert not (tmp_path / "m" / "sources" / "s1").exists()


def test_corrupt_and_missing_source(made):
    root, _ = made
    (root / "sources" / "s1").write_bytes(b"bad")
    assert d.verify_dossier(root) == ["artifact_hash_mismatch:s1"]
    (root / "sources" / "s1").unlink()
    assert d.verify_dossier(root) == ["missing_artifact:s1"]


def test_plot_and_manuscript_bytes_stored(tmp_path):
    root = tmp_path / "p"
    out = d.write_dossier(
        root,
        _record(plots=["plots/a.png"], manuscript={"path": "m.md"}),
        source_bytes={"s1": SRC},
        plot_bytes={"plots/a.png": b"png"},
        manuscript_bytes=b"ms",
    )
    assert (root / "plots" / "a.png").read_bytes() == b"png"
    assert out["plots"][0]["sha256"] == hashlib.sha256(b"png").hexdigest()
    assert out["manuscript"]["status"] == "not_generated"
    assert d.verify_dossier(root) == []


def test_cli_exit_codes(made, tmp_path):
    root, priv = made
    copy = tmp_path / "c"
    shutil.copytree(root, copy)
    assert d.main(["verify", str(copy)]) == 0
    assert d.main(["reproduce", str(copy), "--private-dir", str(priv)]) == 0
    assert d.main(["reproduce", str(copy)]) == 1
    (copy / "report.md").write_text("edited\n")
    assert d.main(["verify", str(copy)]) == 1
