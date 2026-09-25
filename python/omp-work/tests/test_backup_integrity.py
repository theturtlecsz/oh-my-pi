"""Object verification and durable WAL evidence without any external calls."""
import json
from hashlib import sha256
from pathlib import Path

import pytest

from omp_work.operations import backup
from omp_work.operations.config import OperationsConfig


@pytest.fixture
def archive(tmp_path, monkeypatch):
    config = OperationsConfig(config_dir=tmp_path / "config", state_dir=tmp_path / "state", data_dir=tmp_path / "data")
    spool = config.data_dir / "wal"
    spool.mkdir(parents=True)
    segment = spool / ("0" * 23 + "1")
    segment.write_bytes(b"wal fixture")
    objects, receipts = {}, []

    def encrypt(source, destination, _):
        destination.write_bytes(source.read_bytes())
        return sha256(destination.read_bytes()).hexdigest()

    def aws(_, arguments):
        key = arguments[arguments.index("--key") + 1]
        if arguments[1] == "put-object":
            body = Path(arguments[arguments.index("--body") + 1]).read_bytes()
            objects[key] = {"ContentLength": len(body), "Metadata": {"sha256": sha256(body).hexdigest()}}
            return b"{}"
        assert arguments[1] == "head-object"
        return json.dumps(objects[key]).encode()

    monkeypatch.setattr(backup, "encrypt_file", encrypt)
    monkeypatch.setattr(backup, "_aws", aws)
    monkeypatch.setattr(backup, "_record_evidence", lambda *args, **kwargs: receipts.append(kwargs))
    return config, segment, receipts, aws


def test_wal_retirement_requires_verified_object_and_evidence(archive):
    config, segment, receipts, _ = archive
    assert backup.upload_wal(config) == 1
    assert not segment.exists()
    assert receipts[-1]["kind"] == "wal_upload" and receipts[-1]["outcome"] == "passed"
    assert receipts[-1]["byte_count"] == len(b"wal fixture")
    assert backup.upload_wal(config) == 0
    assert receipts[-1]["outcome"] == "idle"


@pytest.mark.parametrize("failure", ["metadata", "empty", "bare", "evidence"])
def test_wal_failure_retains_source_for_retry(archive, monkeypatch, failure):
    config, segment, receipts, aws = archive
    if failure in {"metadata", "empty", "bare"}:
        responses = {
            "metadata": b'{"ContentLength":11,"Metadata":{"sha256":"bad"}}',
            "empty": b"",
            "bare": b"{}",
        }
        def bad_metadata(config, arguments):
            if arguments[1] == "head-object":
                return responses[failure]
            return aws(config, arguments)
        monkeypatch.setattr(backup, "_aws", bad_metadata)
    else:
        def unavailable(*args, **kwargs):
            raise RuntimeError("evidence unavailable")
        monkeypatch.setattr(backup, "_record_evidence", unavailable)
    with pytest.raises(RuntimeError):
        backup.upload_wal(config)
    assert segment.read_bytes() == b"wal fixture"
    assert not list(segment.parent.glob("*.gpg"))
    if failure != "evidence":
        assert receipts[-1]["outcome"] == "failed"


def test_download_rejects_corrupted_ciphertext_before_decryption(tmp_path, monkeypatch):
    config = OperationsConfig(config_dir=tmp_path, state_dir=tmp_path, data_dir=tmp_path)
    def corrupt(_, arguments):
        Path(arguments[-1]).write_bytes(b"corrupted")
        return b"{}"
    monkeypatch.setattr(backup, "_aws", corrupt)
    monkeypatch.setattr(backup, "decrypt_file", lambda *args: pytest.fail("decryption must not run"))
    with pytest.raises(RuntimeError, match="ciphertext hash mismatch"):
        backup._download_decrypt(config, "fixture/key", tmp_path / "dump", expected_sha256="0" * 64)
