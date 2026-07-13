"""Audit log rotation & retention.

The audit trail shares the fixed /data volume with agent workspaces and pause
state, and is written from hot paths. These tests pin the bound: the file
rotates at a size limit, keeps only `backup_count` generations, never loses a
recently-written record, and — when rotation is disabled — behaves exactly as
the old unbounded append log.
"""

import json

from sentinel.audit import AuditLog


def _all_lines(path):
    """Every JSONL line across the live file and its rotated generations."""
    files = [path] + [path.with_name(f"{path.name}.{i}") for i in range(1, 50)]
    lines = []
    for f in files:
        if f.exists():
            lines.extend(f.read_text(encoding="utf-8").splitlines())
    return lines


def test_disabled_rotation_keeps_single_file(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path, max_bytes=0)          # unlimited (historical behavior)
    for i in range(200):
        audit.record("dispatch", i=i)
    assert path.exists()
    assert not path.with_name("audit.jsonl.1").exists()
    assert len(path.read_text().splitlines()) == 200


def test_rotates_and_caps_backup_count(tmp_path):
    path = tmp_path / "audit.jsonl"
    # ~60-byte lines; 300-byte cap => rotate roughly every 5 records.
    audit = AuditLog(path, max_bytes=300, backup_count=3)
    for i in range(200):
        audit.record("dispatch", i=i)

    # Only base + 3 generations are retained; the 4th never exists.
    assert path.exists()
    for i in (1, 2, 3):
        assert path.with_name(f"audit.jsonl.{i}").exists(), f"missing .{i}"
    assert not path.with_name("audit.jsonl.4").exists()


def test_no_generation_exceeds_the_limit(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path, max_bytes=400, backup_count=5)
    for i in range(500):
        audit.record("transition", i=i, verdict="pass")
    for f in [path] + [path.with_name(f"audit.jsonl.{i}") for i in range(1, 6)]:
        if f.exists():
            # A generation may exceed the cap by at most its final line.
            assert f.stat().st_size <= 400 + 200


def test_most_recent_records_are_never_lost(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path, max_bytes=300, backup_count=3)
    for i in range(200):
        audit.record("dispatch", i=i)

    lines = _all_lines(path)
    parsed = [json.loads(x) for x in lines]           # every retained line is valid JSON
    indices = {p["i"] for p in parsed}
    # The newest record is in the live file, and the retained window is contiguous
    # down from it (rotation only ever drops the OLDEST generation).
    assert 199 in indices
    assert json.loads(path.read_text().splitlines()[-1])["i"] == 199
    assert max(indices) - min(indices) + 1 == len(indices)


def test_record_shape_unchanged(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    audit.record("escalation", ticket="SENT-1", reason="boom")
    entry = json.loads(path.read_text().splitlines()[0])
    assert entry["event"] == "escalation"
    assert entry["ticket"] == "SENT-1"
    assert entry["reason"] == "boom"
    assert "at" in entry
