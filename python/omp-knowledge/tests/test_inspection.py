from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from omp_work.v1.models import EvidenceKind, EvidenceReceipt

from omp_knowledge.inspection import (
    InspectionReport,
    build_report,
    create_server,
    format_cli_output,
)
from omp_knowledge.inspection.cli import main as cli_main
from omp_knowledge.learning.corrections import correct
from omp_knowledge.learning.models import (
    Attribution,
    Claim,
    Lesson,
    Precondition,
    SourceIdentity,
)
from omp_knowledge.learning.policy import REASON_INVALID_CITATION, NativeReceipts
from omp_knowledge.learning.proposals import create_proposal
from omp_knowledge.learning.store import LearningStore
from omp_knowledge.learning.uses import record_outcome, record_use, supply


class DictReceiptReader:
    """Dict-backed fake reader conforming to NativeReceipts protocol."""

    def __init__(self, receipts: dict[str | UUID, EvidenceReceipt] | None = None) -> None:
        self._receipts: dict[str, EvidenceReceipt] = {}
        if receipts:
            for k, v in receipts.items():
                self._receipts[str(k)] = v

    def add(self, receipt: EvidenceReceipt) -> None:
        self._receipts[str(receipt.receipt_id)] = receipt

    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        key = str(receipt_id)
        if key not in self._receipts:
            raise KeyError(f"Receipt {receipt_id} not found in reader")
        return self._receipts[key]


def make_receipt(
    *,
    receipt_id: UUID | None = None,
    candidate_id: UUID | None = None,
    kind: EvidenceKind = EvidenceKind.VERIFICATION,
    verdict: str | None = "PASS",
    payload: dict[str, Any] | None = None,
) -> EvidenceReceipt:
    rid = receipt_id or uuid4()
    cid = candidate_id or uuid4()
    p = payload if payload is not None else {"exit_code": 0}
    return EvidenceReceipt(
        receipt_id=rid,
        work_id=uuid4(),
        revision_id=uuid4(),
        candidate_id=cid,
        kind=kind,
        payload=p,
        payload_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        issuer="test-runner",
        issued_at=datetime.now(timezone.utc),
        verdict=verdict,
        independent=True,
    )


def make_attribution(
    model: str = "qwen-2.5",
    profile: str = "local-qwen",
    workspace_id: str | None = None,
) -> Attribution:
    ws = workspace_id or str(uuid4())
    return Attribution(
        model=model,
        profile=profile,
        source=SourceIdentity(
            workspace_id=ws,
            event_id=str(uuid4()),
            event_sequence=1,
            aggregate_id=str(uuid4()),
        ),
    )


def test_absent_store_gives_missing_and_creates_no_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An absent store gives 'missing', not an error or a new file."""
    state_root = tmp_path / "empty_root"
    state_root.mkdir()

    report = build_report(state_root)
    assert report.sources == "missing"
    assert report.sources.get("status") == "missing"
    assert report.evidence == "missing"
    assert report.evidence.get("status") == "missing"
    assert report.procedures == "missing"
    assert report.procedures.get("status") == "missing"
    assert report.acceptance == "missing"
    assert report.acceptance.get("status") == "missing"

    # Assert no files were created in state_root
    assert list(state_root.iterdir()) == []

    # Assert to_dict() returns missing for each section
    data = report.to_dict()
    assert data["sources"] == "missing"
    assert data["evidence"] == "missing"
    assert data["procedures"] == "missing"
    assert data["acceptance"] == "missing"

    # Assert CLI output in JSON mode gives missing
    rc = cli_main(["show", "--state-root", str(state_root), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    cli_json = json.loads(out)
    assert cli_json["procedures"] == "missing"

    # Assert CLI with --section procedures --json gives "missing"
    rc_proc = cli_main(["show", "--state-root", str(state_root), "--section", "procedures", "--json"])
    assert rc_proc == 0
    out_proc = capsys.readouterr().out
    assert json.loads(out_proc) == "missing"

    # Assert text formatting prints missing
    text_out = format_cli_output(report)
    assert "=== Procedures ===\n  missing" in text_out


def test_populated_learning_store_inspection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """On a populated learning store, CLI JSON lists accepted/rejected proposals, versions, receipts, and cleanup."""
    state_root = tmp_path / "populated_root"
    state_root.mkdir()
    db_file = state_root / "learning.sqlite"

    store = LearningStore(str(db_file))
    ws_id = str(uuid4())

    supp_receipt = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="PASS")
    verif_receipt = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="PASS")
    counter_receipt = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="NEEDS_FIX")
    reader = DictReceiptReader(
        {
            supp_receipt.receipt_id: supp_receipt,
            verif_receipt.receipt_id: verif_receipt,
            counter_receipt.receipt_id: counter_receipt,
        }
    )

    attr = make_attribution(workspace_id=ws_id)

    # 1. Accept proposal with title containing script (also checks XSS escaping later)
    lesson_accepted = Lesson(
        title="Safe Procedure <script>alert(1)</script>",
        steps=("step 1", "step 2"),
        preconditions=(Precondition(key="repository", op="eq", value="ssh://git@github.com/theturtlecsz/oh-my-pi"),),
        claims=(
            Claim(
                text="Accepted claim",
                receipt_ids=(supp_receipt.receipt_id,),
            ),
        ),
    )
    rec_accepted = create_proposal(
        store,
        unit_id=uuid4(),
        lesson=lesson_accepted,
        attribution=attr,
        reader=reader,
    )
    assert rec_accepted.status == "accepted"
    proc_id = rec_accepted.procedure_id
    assert proc_id is not None

    # 2. Reject proposal with invalid citation
    unknown_id = str(uuid4())
    lesson_rejected = Lesson(
        title="Bad Citation Procedure",
        steps=("step 1",),
        claims=(
            Claim(
                text="Rejected claim",
                receipt_ids=(unknown_id,),
            ),
        ),
    )
    rec_rejected = create_proposal(
        store,
        unit_id=uuid4(),
        lesson=lesson_rejected,
        attribution=attr,
        reader=reader,
    )
    assert rec_rejected.status == "rejected"
    assert rec_rejected.reason == REASON_INVALID_CITATION

    # 3. Supply and record use
    cand_id = uuid4()
    use_receipt = make_receipt(candidate_id=cand_id, kind=EvidenceKind.VERIFICATION, verdict="PASS")
    reader.add(use_receipt)
    verif_receipt = make_receipt(candidate_id=cand_id, kind=EvidenceKind.VERIFICATION, verdict="PASS")
    reader.add(verif_receipt)

    supply_res = supply(
        store,
        workspace_id=ws_id,
        work_key="OMP-312-TASK",
        context={"repository": "ssh://git@github.com/theturtlecsz/oh-my-pi"},
    )
    assert len(supply_res.supply_ids) >= 1
    supply_id = supply_res.supply_ids[0]
    use_record = record_use(
        store,
        reader,
        supply_id=supply_id,
        candidate_id=cand_id,
        receipt_ids=[use_receipt.receipt_id],
    )

    # 4. Record outcome
    record_outcome(
        store,
        reader,
        use_id=use_record.use_id,
        receipt_id=verif_receipt.receipt_id,
    )

    # 5. Narrow correction to produce version 2 and a pending cleanup row
    correct_res = correct(
        store,
        reader,
        procedure_id=proc_id,
        receipt_id=counter_receipt.receipt_id,
        action="narrow",
        preconditions=[Precondition(key="repository", op="eq", value="ssh://git@github.com/theturtlecsz/oh-my-pi")],
        attribution=attr,
    )
    assert correct_res.status == "accepted"
    assert correct_res.to_version == 2

    # Measure store file bytes and mtime before inspection
    bytes_before = db_file.read_bytes()
    sha_before = hashlib.sha256(bytes_before).hexdigest()
    mtime_before = os.path.getmtime(db_file)

    # Build report
    report = build_report(state_root)

    # The store file's bytes and mtime must remain unchanged
    bytes_after = db_file.read_bytes()
    sha_after = hashlib.sha256(bytes_after).hexdigest()
    mtime_after = os.path.getmtime(db_file)
    assert sha_after == sha_before
    assert mtime_after == mtime_before

    # Verify procedures in report
    procs = report.procedures
    assert isinstance(procs, list)
    assert len(procs) == 1
    p0 = procs[0]
    assert p0["id"] == proc_id
    assert p0["current_version"] == 2
    assert len(p0["versions"]) == 2
    assert str(supp_receipt.receipt_id) in p0["supporting_receipt_ids"]
    assert str(verif_receipt.receipt_id) in p0["outcome_receipt_ids"]

    # Verify evidence in report
    ev_list = report.evidence
    assert isinstance(ev_list, list)
    assert len(ev_list) == 1
    ev0 = ev_list[0]
    assert ev0["procedure_id"] == proc_id
    assert str(supp_receipt.receipt_id) in ev0["procedure_support"]
    assert str(use_receipt.receipt_id) in ev0["uses"]
    assert any(o["receipt_id"] == str(verif_receipt.receipt_id) and o["verdict"] == "PASS" for o in ev0["outcomes"])
    assert any(c["receipt_id"] == str(counter_receipt.receipt_id) for c in ev0["corrections"])

    # Verify acceptance in report
    acc = report.acceptance
    assert isinstance(acc, dict)
    props = acc["proposals"]
    assert len(props) == 2
    accepted_props = [p for p in props if p["status"] == "accepted"]
    rejected_props = [p for p in props if p["status"] == "rejected"]
    assert len(accepted_props) == 1
    assert len(rejected_props) == 1
    assert rejected_props[0]["reason"] == REASON_INVALID_CITATION

    # Verify pending cleanup in cleanup_queue
    cleanup = acc["cleanup_queue"]
    assert len(cleanup["pending"]) >= 1
    assert any(q["procedure_id"] == proc_id for q in cleanup["pending"])

    # Test CLI invocation --json
    rc = cli_main(["show", "--state-root", str(state_root), "--json"])
    assert rc == 0
    cli_data = json.loads(capsys.readouterr().out)
    assert len(cli_data["procedures"]) == 1
    assert cli_data["procedures"][0]["id"] == proc_id
    assert len(cli_data["acceptance"]["cleanup_queue"]["pending"]) >= 1


def test_structural_snapshots_inspection(tmp_path: Path) -> None:
    """Structural snapshots are collected from structural-publications.sqlite."""
    state_root = tmp_path / "struct_root"
    state_root.mkdir()
    struct_db = state_root / "structural-publications.sqlite"

    conn = sqlite3.connect(str(struct_db))
    conn.execute(
        """
        CREATE TABLE structural_snapshots (
            workspace_id TEXT NOT NULL,
            repository_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            namespace TEXT NOT NULL,
            state TEXT NOT NULL,
            projection_sha256 TEXT NOT NULL,
            coverage_sha256 TEXT NOT NULL,
            graph_sha256 TEXT NOT NULL,
            projection_json TEXT NOT NULL,
            coverage_json TEXT NOT NULL,
            staged_at TEXT NOT NULL,
            published_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (workspace_id, repository_id, snapshot_id)
        )
        """
    )
    ws_id = str(uuid4())
    repo_id = str(uuid4())
    snap_id = "snap-test-01"
    conn.execute(
        """
        INSERT INTO structural_snapshots (
            workspace_id, repository_id, snapshot_id, namespace, state,
            projection_sha256, coverage_sha256, graph_sha256, projection_json,
            coverage_json, staged_at, published_at, updated_at
        ) VALUES (?, ?, ?, 'default', 'published', 'sha1', 'sha2', 'sha3', '{}', '{}', '2026-09-27T00:00:00Z', '2026-09-27T01:00:00Z', '2026-09-27T01:00:00Z')
        """,
        (ws_id, repo_id, snap_id),
    )
    conn.commit()
    conn.close()

    report = build_report(state_root)
    assert report.sources != "missing"
    snaps = report.sources["structural_snapshots"]
    assert len(snaps) == 1
    assert snaps[0]["workspace"] == ws_id
    assert snaps[0]["repository"] == repo_id
    assert snaps[0]["snapshot"] == snap_id
    assert snaps[0]["state"] == "published"
    assert snaps[0]["published_at"] == "2026-09-27T01:00:00Z"


def test_web_server_ephemeral_port_and_endpoints(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Over HTTP against an ephemeral port, /api/procedures matches CLI JSON, / escapes <script>, and POST is 405."""
    state_root = tmp_path / "web_root"
    state_root.mkdir()
    db_file = state_root / "learning.sqlite"

    store = LearningStore(str(db_file))
    ws_id = str(uuid4())
    supp_receipt = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="PASS")
    reader = DictReceiptReader({supp_receipt.receipt_id: supp_receipt})
    attr = make_attribution(workspace_id=ws_id)

    # Title containing <script> to verify HTML escaping
    lesson = Lesson(
        title="Exploit <script>alert('pwn')</script>",
        steps=("step a", "step b"),
        claims=(Claim(text="Claim text", receipt_ids=(supp_receipt.receipt_id,)),),
    )
    create_proposal(
        store,
        unit_id=uuid4(),
        lesson=lesson,
        attribution=attr,
        reader=reader,
    )

    # Obtain CLI JSON for procedures
    cli_main(["show", "--state-root", str(state_root), "--section", "procedures", "--json"])
    cli_procedures_json = json.loads(capsys.readouterr().out)

    # Start HTTP server on ephemeral port (port 0)
    server, port = create_server(state_root, host="127.0.0.1", port=0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    base_url = f"http://127.0.0.1:{port}"
    try:
        # 1. GET / (HTML Dashboard)
        with urllib.request.urlopen(f"{base_url}/") as resp:
            assert resp.status == 200
            content_type = resp.headers.get("Content-Type", "")
            assert "text/html" in content_type
            html_body = resp.read().decode("utf-8")
            # Verify <script> was properly HTML escaped
            assert "&lt;script&gt;alert(&#x27;pwn&#x27;)&lt;/script&gt;" in html_body or "&lt;script&gt;alert('pwn')&lt;/script&gt;" in html_body
            assert "<script>alert('pwn')</script>" not in html_body
            # Verify all four sections are present in HTML
            assert "Procedures" in html_body
            assert "Evidence" in html_body
            assert "Acceptance" in html_body
            assert "Sources" in html_body

        # 2. GET /api/procedures matches CLI JSON
        with urllib.request.urlopen(f"{base_url}/api/procedures") as resp:
            assert resp.status == 200
            content_type = resp.headers.get("Content-Type", "")
            assert "application/json" in content_type
            http_procedures_json = json.loads(resp.read().decode("utf-8"))
            assert http_procedures_json == cli_procedures_json

        # 3. GET /api/acceptance
        with urllib.request.urlopen(f"{base_url}/api/acceptance") as resp:
            assert resp.status == 200
            acc_json = json.loads(resp.read().decode("utf-8"))
            assert "proposals" in acc_json
            assert len(acc_json["proposals"]) == 1

        # 4. POST returns 405 Method Not Allowed
        req_post = urllib.request.Request(f"{base_url}/", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_post)
        assert exc_info.value.code == 405

        req_post_api = urllib.request.Request(f"{base_url}/api/procedures", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc_info_api:
            urllib.request.urlopen(req_post_api)
        assert exc_info_api.value.code == 405

        # 5. Unknown path returns 404
        with pytest.raises(urllib.error.HTTPError) as exc_info_404:
            urllib.request.urlopen(f"{base_url}/unknown/path")
        assert exc_info_404.value.code == 404

    finally:
        server.shutdown()
        server.server_close()


def test_report_rebuilt_per_request(tmp_path: Path) -> None:
    """Web server rebuilds the report dynamically on every request."""
    state_root = tmp_path / "dynamic_root"
    state_root.mkdir()

    server, port = create_server(state_root, host="127.0.0.1", port=0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    base_url = f"http://127.0.0.1:{port}"
    try:
        # Initially empty store -> "missing"
        with urllib.request.urlopen(f"{base_url}/api/procedures") as resp:
            assert json.loads(resp.read().decode("utf-8")) == "missing"

        # Now create learning store and populate one procedure
        db_file = state_root / "learning.sqlite"
        store = LearningStore(str(db_file))
        supp_receipt = make_receipt()
        reader = DictReceiptReader({supp_receipt.receipt_id: supp_receipt})
        attr = make_attribution()
        lesson = Lesson(
            title="Dynamically Added Procedure",
            steps=("step 1",),
            claims=(Claim(text="Dynamically added", receipt_ids=(supp_receipt.receipt_id,)),),
        )
        create_proposal(
            store,
            unit_id=uuid4(),
            lesson=lesson,
            attribution=attr,
            reader=reader,
        )

        # Subsequent request immediately reflects the new procedure
        with urllib.request.urlopen(f"{base_url}/api/procedures") as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert isinstance(data, list)
            assert len(data) == 1
            assert data[0]["title"] == "Dynamically Added Procedure"

    finally:
        server.shutdown()
        server.server_close()
