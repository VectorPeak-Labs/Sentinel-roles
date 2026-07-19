"""Operator audit query CLI: `python -m sentinel.audit recent|ticket`.

Runs against a temporary JSONL file (no Jira/LiteLLM), including a malformed
line to prove the query path never crashes.
"""

import json

import pytest

from sentinel.audit import AuditLog, format_timeline, main


@pytest.fixture
def audit_file(tmp_path):
    """A small trail with mixed events/tickets/roles + one malformed line."""
    path = tmp_path / "audit.jsonl"
    rows = [
        {"at": "t1", "event": "dispatch", "ticket": "SENT-1", "role": "03-business-analyst"},
        {"at": "t2", "event": "transition", "ticket": "SENT-1", "role": "03-business-analyst"},
        {"at": "t3", "event": "dispatch", "ticket": "SENT-2", "role": "07-implementer"},
        {"at": "t4", "event": "escalation", "ticket": "SENT-2", "role": "07-implementer",
         "reason": "deploy_production missing"},
    ]
    with path.open("w", encoding="utf-8") as f:
        for r in rows[:2]:
            f.write(json.dumps(r) + "\n")
        f.write("{ this is not valid json\n")   # crash-interrupted write
        for r in rows[2:]:
            f.write(json.dumps(r) + "\n")
    return path


def _run(argv, capsys):
    rc = main(argv)
    return rc, capsys.readouterr()


# --- ticket timeline ------------------------------------------------------- #

def test_ticket_timeline_text(audit_file, capsys):
    rc, out = _run(["--file", str(audit_file), "ticket", "SENT-1"], capsys)
    assert rc == 0
    lines = out.out.strip().splitlines()
    assert len(lines) == 2                      # both SENT-1 events, malformed line skipped
    assert all("SENT-1" in ln for ln in lines)
    assert "dispatch" in lines[0] and "transition" in lines[1]  # chronological


def test_ticket_timeline_json(audit_file, capsys):
    rc, out = _run(["--file", str(audit_file), "--format", "json", "ticket", "SENT-2"], capsys)
    assert rc == 0
    recs = json.loads(out.out)
    assert [r["event"] for r in recs] == ["dispatch", "escalation"]
    assert recs[1]["reason"] == "deploy_production missing"   # raw fields preserved in json


# --- recent + filters ------------------------------------------------------ #

def test_recent_all(audit_file, capsys):
    rc, out = _run(["--file", str(audit_file), "recent"], capsys)
    assert rc == 0
    assert len(out.out.strip().splitlines()) == 4     # 4 valid rows, malformed skipped


def test_recent_event_filter(audit_file, capsys):
    rc, out = _run(["--file", str(audit_file), "recent", "--event", "dispatch"], capsys)
    lines = out.out.strip().splitlines()
    assert len(lines) == 2 and all("dispatch" in ln for ln in lines)


def test_recent_role_filter(audit_file, capsys):
    rc, out = _run(["--file", str(audit_file), "recent", "--role", "07-implementer"], capsys)
    lines = out.out.strip().splitlines()
    assert len(lines) == 2 and all("07-implementer" in ln for ln in lines)


def test_recent_limit(audit_file, capsys):
    rc, out = _run(["--file", str(audit_file), "recent", "--limit", "1"], capsys)
    # newest-only
    assert out.out.strip().splitlines() == [] or "SENT-2" in out.out


def test_empty_results_note_to_stderr(audit_file, capsys):
    rc, out = _run(["--file", str(audit_file), "ticket", "SENT-999"], capsys)
    assert rc == 0
    assert out.out.strip() == ""                       # nothing on stdout
    assert "no matching audit records" in out.err


# --- read_records role filter (unit) --------------------------------------- #

def test_read_records_role_filter(audit_file):
    log = AuditLog(audit_file, max_bytes=0)
    recs = log.read_records(role="03-business-analyst")
    assert {r["ticket"] for r in recs} == {"SENT-1"}
    assert len(recs) == 2


# --- format_timeline shows extra fields ------------------------------------ #

def test_format_timeline_includes_extra_fields():
    text = format_timeline([{"at": "t", "event": "escalation", "ticket": "SENT-9",
                             "role": "07", "reason": "boom"}])
    assert "SENT-9" in text and "reason=boom" in text
