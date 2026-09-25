"""R03 recovery process tests — mechanical seal by Run Owner (C19 path completed without paid explore)."""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ROOT))

from omp_work.contracts.v1.recovery import (  # noqa: E402
    CloseoutRecord,
    RecoveryError,
    apply_commit,
    acknowledge,
    close,
    recover_unacked_commit,
    execution_status_is_not_scientific_success,
)

V1 = Path(__file__).resolve().parents[1] / "src" / "omp_work" / "contracts" / "v1"
FLAG = Path(__file__).with_name("R03_RECOVERY_IMPLEMENTED.flag")


def test_v1_files_and_decision_0002_present():
    assert (V1 / "contract.json").is_file()
    assert (V1 / "api-schema.json").is_file()
    assert (V1 / "decisions/0002-evidence-completion-and-recovery.md").is_file()
    text = (V1 / "decisions/0002-evidence-completion-and-recovery.md").read_text()
    assert "complet" in text.lower() or "recover" in text.lower()


def test_commit_then_recover_unacked_with_untrusted_evidence_fails_closed():
    r = CloseoutRecord(id="c1", state="open", revision=0)
    r = apply_commit(r)
    assert r.state == "committed"
    r2 = recover_unacked_commit(r, evidence_trusted=False)
    assert r2.state == "failed"
    try:
        close(r2)
        assert False, "must not close from failed/unacked-untrusted"
    except RecoveryError:
        pass


def test_commit_recover_ack_close_with_trusted_evidence():
    r = apply_commit(CloseoutRecord(id="c2", state="open", revision=0))
    r = recover_unacked_commit(r, evidence_trusted=True)
    assert r.state == "acknowledged" and r.ack_token
    r = close(r)
    assert r.state == "closed"


def test_cannot_skip_ack_to_close_from_committed():
    r = apply_commit(CloseoutRecord(id="c3", state="open", revision=0))
    try:
        close(r)
        assert False, "close from committed must fail"
    except RecoveryError:
        pass


def test_untrusted_evidence_blocks_scientific_success_on_close():
    try:
        execution_status_is_not_scientific_success("closed", "untrusted")
        assert False
    except RecoveryError:
        pass


def test_implementation_flag_present():
    assert FLAG.is_file(), "R03_RECOVERY_IMPLEMENTED.flag required after real process tests"
