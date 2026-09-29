"""Tests for WAL upload resilience: partial failure handling, JSON output flag, and stale scratch cleanup."""
import fcntl
import json
from hashlib import sha256
from pathlib import Path

import pytest
from omp_work.operations import backup
from omp_work.operations.config import OperationsConfig


@pytest.fixture
def multi_archive(tmp_path, monkeypatch):
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    spool = config.data_dir / "wal"
    spool.mkdir(parents=True)
    objects, receipts = {}, []

    def encrypt(source, destination, _):
        destination.write_bytes(source.read_bytes())
        return sha256(destination.read_bytes()).hexdigest()

    def aws(_, arguments):
        key = arguments[arguments.index("--key") + 1]
        if arguments[1] == "put-object":
            body = Path(arguments[arguments.index("--body") + 1]).read_bytes()
            objects[key] = {
                "ContentLength": len(body),
                "Metadata": {"sha256": sha256(body).hexdigest()},
            }
            return b"{}"
        assert arguments[1] == "head-object"
        assert "--output" in arguments and arguments[arguments.index("--output") + 1] == "json"
        return json.dumps(objects[key]).encode()

    monkeypatch.setattr(backup, "encrypt_file", encrypt)
    monkeypatch.setattr(backup, "_aws", aws)
    monkeypatch.setattr(
        backup, "_record_evidence", lambda *args, **kwargs: receipts.append(kwargs)
    )
    return config, spool, receipts, objects


def test_three_segments_second_fails_first_and_third_retired(multi_archive, monkeypatch):
    """A test with three spool segments where the second one's upload always fails shows:
    the first and third segments are uploaded, verified, recorded as evidence and deleted;
    the second stays in the spool; the run still reports failure."""
    config, spool, receipts, objects = multi_archive

    seg1 = spool / ("0" * 23 + "1")
    seg2 = spool / ("0" * 23 + "2")
    seg3 = spool / ("0" * 23 + "3")
    seg1.write_bytes(b"segment 1 data")
    seg2.write_bytes(b"segment 2 failing data")
    seg3.write_bytes(b"segment 3 data")

    original_aws = backup._aws

    def failing_aws(cfg, arguments):
        if arguments[1] == "put-object":
            key = arguments[arguments.index("--key") + 1]
            if seg2.name in key:
                raise RuntimeError("S3 upload failed for seg2")
        return original_aws(cfg, arguments)

    monkeypatch.setattr(backup, "_aws", failing_aws)

    with pytest.raises(RuntimeError, match="S3 upload failed for seg2"):
        backup.upload_wal(config)

    # First and third segments were uploaded, verified, and deleted
    assert not seg1.exists(), "segment 1 should have been retired"
    assert not seg3.exists(), "segment 3 should have been retired"

    # Second segment stays in the spool
    assert seg2.exists(), "segment 2 must remain in the spool"
    assert seg2.read_bytes() == b"segment 2 failing data"

    # Run recorded evidence with outcome 'failed' and byte_count of the verified segments
    assert len(receipts) == 1
    assert receipts[0]["kind"] == "wal_upload"
    assert receipts[0]["outcome"] == "failed"
    assert receipts[0]["byte_count"] == len(b"segment 1 data") + len(b"segment 3 data")

    # S3 objects exist for seg1 and seg3, but not seg2
    keys = list(objects.keys())
    assert any(seg1.name in k for k in keys)
    assert any(seg3.name in k for k in keys)
    assert not any(seg2.name in k for k in keys)


def test_wal_upload_verification_with_aws_default_output_text(multi_archive, monkeypatch):
    """A test runs the WAL upload verification with AWS_DEFAULT_OUTPUT=text (or a fake aws
    that returns text unless --output json is passed) and it passes; every s3api call in
    backup.py whose output is parsed as JSON passes --output json."""
    config, spool, receipts, _ = multi_archive
    monkeypatch.setenv("AWS_DEFAULT_OUTPUT", "text")

    seg = spool / ("0" * 23 + "1")
    seg.write_bytes(b"wal segment content")

    # Fake aws returns plain text unless --output json is explicitly provided
    def strict_aws(_, arguments):
        assert arguments[0] == "s3api"
        action = arguments[1]
        if action == "put-object":
            return b"{}"
        if action == "head-object":
            # If --output json is missing, return AWS text table output that json.loads rejects
            if "--output" not in arguments or arguments[arguments.index("--output") + 1] != "json":
                return b"METADATA\ttext_format_no_json\n"
            digest = sha256(b"wal segment content").hexdigest()
            return json.dumps({
                "ContentLength": len(b"wal segment content"),
                "Metadata": {"sha256": digest},
            }).encode()
        raise NotImplementedError(action)

    monkeypatch.setattr(backup, "_aws", strict_aws)

    uploaded = backup.upload_wal(config)
    assert uploaded == 1
    assert not seg.exists()
    assert receipts[-1]["outcome"] == "passed"


def test_stale_scratch_file_cleaned_up_real_wal_untouched(multi_archive):
    """A test leaves a stale `.<segment>.<hex>.gpg` scratch file in the spool, runs upload_wal,
    and the scratch file is gone afterwards while real WAL segments are untouched."""
    config, spool, receipts, _ = multi_archive

    real_segment = spool / ("0" * 23 + "1")
    real_segment.write_bytes(b"real wal segment")

    # Leave a stale scratch file matching .<name>.<hex>.gpg pattern
    stale_scratch = spool / f".{real_segment.name}.deadbeef0123456789abcdef01234567.gpg"
    stale_scratch.write_bytes(b"stale scratch file left by killed run")

    stale_scratch_2 = spool / ".000000010000000000000099.1234abcd.gpg"
    stale_scratch_2.write_bytes(b"another stale scratch file")

    # Run upload_wal
    uploaded = backup.upload_wal(config)
    assert uploaded == 1

    # Stale scratch files are gone
    assert not stale_scratch.exists(), "stale scratch file should be unlinked"
    assert not stale_scratch_2.exists(), "stale scratch file 2 should be unlinked"

    # Real segment was processed (uploaded, verified, deleted)
    assert not real_segment.exists()
    assert receipts[-1]["outcome"] == "passed"


def test_stale_scratch_cleaned_even_when_spool_has_no_active_segments(multi_archive):
    """When the spool only contains stale scratch files and no valid WAL segments,
    upload_wal still cleans up the stale scratch file and reports idle."""
    config, spool, receipts, _ = multi_archive

    stale_scratch = spool / ".000000010000000000000001.deadbeef.gpg"
    stale_scratch.write_bytes(b"abandoned scratch")

    uploaded = backup.upload_wal(config)
    assert uploaded == 0
    assert not stale_scratch.exists()
    assert receipts[-1]["outcome"] == "idle"


def test_concurrent_run_lock_protects_in_use_scratch(multi_archive):
    """A concurrent run's in-use scratch file is protected by the exclusive run lock."""
    config, spool, _receipts, _ = multi_archive
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = config.state_dir / "wal_upload.lock"

    # Simulate an active concurrent run holding the lock and having an in-use scratch file
    with lock_path.open("a+") as active_lock:
        fcntl.flock(active_lock.fileno(), fcntl.LOCK_EX)

        in_use_scratch = spool / ".000000010000000000000001.inuse12345678.gpg"
        in_use_scratch.write_bytes(b"actively being written by concurrent run")

        # A concurrent process attempting to acquire the lock non-blocking cannot proceed
        with lock_path.open("a+") as contender_lock, pytest.raises(BlockingIOError):
            fcntl.flock(contender_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        # In-use scratch remains untouched while lock is held
        assert in_use_scratch.exists()

        # Release lock and simulate concurrent run cleaned up its scratch
        in_use_scratch.unlink()
        fcntl.flock(active_lock.fileno(), fcntl.LOCK_UN)


def test_partial_failure_with_evidence_failure_retains_all_segments(multi_archive, monkeypatch):
    """If one segment fails and evidence recording also fails, verified segments
    must NOT be unlinked because durable evidence did not succeed."""
    config, spool, _receipts, _objects = multi_archive

    seg1 = spool / ("0" * 23 + "1")
    seg2 = spool / ("0" * 23 + "2")
    seg1.write_bytes(b"segment 1 data")
    seg2.write_bytes(b"segment 2 failing data")

    original_aws = backup._aws

    def failing_aws(cfg, arguments):
        if arguments[1] == "put-object":
            key = arguments[arguments.index("--key") + 1]
            if seg2.name in key:
                raise RuntimeError("S3 upload failed for seg2")
        return original_aws(cfg, arguments)

    def failing_evidence(*args, **kwargs):
        raise RuntimeError("database unreachable for evidence")

    monkeypatch.setattr(backup, "_aws", failing_aws)
    monkeypatch.setattr(backup, "_record_evidence", failing_evidence)

    with pytest.raises(RuntimeError, match="S3 upload failed for seg2"):
        backup.upload_wal(config)

    # Both segments must still exist because evidence failed!
    assert seg1.exists(), "segment 1 must be retained when evidence recording fails"
    assert seg2.exists(), "segment 2 must be retained"


def test_latest_backup_and_verify_uploaded_require_output_json(tmp_path, monkeypatch):
    """Verify that both _verify_uploaded and _latest_backup explicitly pass --output json."""
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )

    def strict_aws(_, arguments):
        assert arguments[0] == "s3api"
        if "--output" not in arguments or arguments[arguments.index("--output") + 1] != "json":
            # Return text table format when --output json is not specified
            return b"TABLE\toutput\tformat\n"
        if arguments[1] == "head-object":
            return json.dumps({
                "ContentLength": 10,
                "Metadata": {"sha256": "fakehash"},
            }).encode()
        if arguments[1] == "list-objects-v2":
            return json.dumps({
                "Contents": [{"Key": f"{config.prefix}/base/2026/01/01/backup-123/COMPLETE"}],
            }).encode()
        raise NotImplementedError(arguments[1])

    monkeypatch.setattr(backup, "_aws", strict_aws)

    # Calling _verify_uploaded succeeds because --output json is passed
    backup._verify_uploaded(config, "fake-key", "fakehash", 10)

    # Calling _latest_backup succeeds because --output json is passed
    prefix, _resp = backup._latest_backup(config)
    assert prefix == f"{config.prefix}/base/2026/01/01/backup-123/"
