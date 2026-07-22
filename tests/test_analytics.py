"""Tests for workflow analytics (sentinel/analytics.py) and the audit-log
`since` filter it relies on. The metric core is pure, so these run on a
deterministic in-memory record list — no Jira, no files (except the small
read_records filter test)."""

from datetime import datetime, timedelta, timezone

import pytest

from sentinel.analytics import (
    Analytics,
    compute_analytics,
    parse_since,
    render_text,
)
from sentinel.audit import AuditLog


def _rec(at, event, **fields):
    return {"at": at, "event": event, **fields}


# A small but representative slice of pipeline history for SENT-1 (plus a few
# events on other tickets), crafted so each metric family has something to count.
SAMPLE = [
    _rec("2026-07-20T10:00:00+00:00", "dispatch", role="07-implementer", ticket="SENT-1"),
    _rec("2026-07-20T10:00:00+00:00", "transition", ticket="SENT-1", role="06-sprint-planner",
         from_status="To Do", to_status="In Progress", verdict="pass"),
    _rec("2026-07-20T11:00:00+00:00", "transition", ticket="SENT-1", role="07-implementer",
         from_status="In Progress", to_status="Tech Review", verdict="pass"),   # In Progress 1h
    _rec("2026-07-20T11:30:00+00:00", "transition", ticket="SENT-1", role="08-code-reviewer",
         from_status="Tech Review", to_status="Rework", verdict="reject"),      # Tech Review 30m
    _rec("2026-07-20T11:30:00+00:00", "rejection", ticket="SENT-1", role="08-code-reviewer",
         rejected_from="tech_review", findings=2),
    _rec("2026-07-20T11:31:00+00:00", "rework_incremented", ticket="SENT-1", count=1, exceeded=False),
    _rec("2026-07-20T12:00:00+00:00", "transition", ticket="SENT-1", role="13-rework-router",
         from_status="Rework", to_status="In Progress", verdict="pass"),        # Rework 30m
    _rec("2026-07-20T12:00:00+00:00", "run_finished_waiting", role="03-business-analyst",
         ticket="SENT-2", summary="waiting on PO"),
    _rec("2026-07-20T12:05:00+00:00", "escalation", role="09-deployment", ticket="SENT-3",
         reason="deploy command missing for SENT-3", decision_needed="fill config"),
    _rec("2026-07-20T12:06:00+00:00", "orchestrator_escalation", ticket="SENT-4",
         reason="Two consecutive agent runs failed"),
    _rec("2026-07-20T12:10:00+00:00", "agent_crash", role="07-implementer", ticket="SENT-5", error="boom"),
    _rec("2026-07-20T12:11:00+00:00", "turn_cap_hit", role="07-implementer", ticket="SENT-5"),
    _rec("2026-07-20T12:12:00+00:00", "lease_reclaimed", ticket="SENT-6", role="03-business-analyst", retries=1),
    _rec("2026-07-20T12:13:00+00:00", "transition", ticket="SENT-1", role="07-implementer",
         from_status="In Progress", to_status="Done", verdict="pass"),          # In Progress 13m
]


@pytest.fixture
def analytics():
    return compute_analytics(SAMPLE)


# --- throughput ------------------------------------------------------------ #

def test_throughput_counts(analytics):
    t = analytics.throughput
    assert t["transitions"] == 5
    assert t["completed"] == 1                       # one -> Done
    assert t["by_to_status"]["In Progress"] == 2
    assert t["dispatches"] == 1
    assert t["dispatches_by_role"] == {"07-implementer": 1}


# --- stage durations ------------------------------------------------------- #

def test_stage_durations_pair_entry_and_exit(analytics):
    sd = analytics.stage_durations
    # In Progress was entered twice (1h and 13m); To Do/Done have no entry+exit pair.
    assert sd["In Progress"]["count"] == 2
    assert sd["In Progress"]["max_seconds"] == 3600.0
    assert sd["In Progress"]["avg_seconds"] == pytest.approx(2190.0)
    assert sd["Tech Review"]["count"] == 1 and sd["Tech Review"]["avg_seconds"] == 1800.0
    assert "To Do" not in sd
    assert "Done" not in sd


def test_human_transition_counts_toward_stage_time():
    recs = [
        _rec("2026-07-20T10:00:00+00:00", "transition", ticket="SENT-9",
             from_status="New", to_status="Business Requirements", verdict="pass"),
        _rec("2026-07-20T10:30:00+00:00", "human_transition", ticket="SENT-9",
             actor="alice", from_status="Business Requirements", to_status="On Hold"),
    ]
    sd = compute_analytics(recs).stage_durations
    assert sd["Business Requirements"]["count"] == 1
    assert sd["Business Requirements"]["max_seconds"] == 1800.0


# --- waits / escalations / rework / reliability ---------------------------- #

def test_waits(analytics):
    assert analytics.waits["total"] == 1
    assert analytics.waits["by_role"] == {"03-business-analyst": 1}


def test_escalations_group_by_source_role_and_reason(analytics):
    e = analytics.escalations
    assert e["total"] == 2
    assert e["by_source"] == {"agent": 1, "orchestrator": 1}
    assert e["by_role"] == {"09-deployment": 1}
    reasons = {r["reason"] for r in e["top_reasons"]}
    # ticket key is normalized out so like reasons would group
    assert "deploy command missing for <ticket>" in reasons
    assert "Two consecutive agent runs failed" in reasons


def test_rework(analytics):
    assert analytics.rework["rejections"] == 1
    assert analytics.rework["by_rejected_from"] == {"tech_review": 1}
    assert analytics.rework["increments"] == 1
    assert analytics.rework["loop_breaker_hits"] == 0


def test_rework_idempotent_increment_and_loop_breaker():
    recs = [
        _rec("t1", "rework_incremented", ticket="SENT-1", count=1, exceeded=False),
        _rec("t2", "rework_incremented", ticket="SENT-1", count=1, exceeded=False, already_counted=True),
        _rec("t3", "rework_incremented", ticket="SENT-1", count=3, exceeded=True),
    ]
    rw = compute_analytics(recs).rework
    assert rw["increments"] == 2          # the already_counted replay is not double-counted
    assert rw["loop_breaker_hits"] == 1


def test_reliability(analytics):
    r = analytics.reliability
    assert r == {"agent_crashes": 1, "turn_cap_hits": 1, "lease_reclaims": 1, "sweep_failures": 0}


def test_window_bounds(analytics):
    assert analytics.since == "2026-07-20T10:00:00+00:00"
    assert analytics.until == "2026-07-20T12:13:00+00:00"
    assert analytics.records == len(SAMPLE)


# --- empty input ----------------------------------------------------------- #

def test_empty_records_is_all_zero():
    a = compute_analytics([])
    assert a.throughput["transitions"] == 0
    assert a.stage_durations == {}
    assert a.escalations["total"] == 0
    assert "no paired transitions" in render_text(a)


# --- parse_since ----------------------------------------------------------- #

@pytest.mark.parametrize("spec,seconds", [
    ("90s", 90), ("30m", 1800), ("24h", 86400), ("7d", 604800), ("2w", 1209600)])
def test_parse_since_durations(spec, seconds):
    now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    assert parse_since(spec, now=now) == now - timedelta(seconds=seconds)


def test_parse_since_iso_date_is_utc():
    assert parse_since("2026-07-01") == datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_parse_since_invalid_raises():
    with pytest.raises(ValueError):
        parse_since("lastweek")


# --- rendering ------------------------------------------------------------- #

def test_render_text_has_sections_and_ranks_bottleneck_first(analytics):
    text = render_text(analytics)
    for header in ("THROUGHPUT", "STAGE DURATIONS", "WAITS", "ESCALATIONS", "REWORK", "RELIABILITY"):
        assert header in text
    # In Progress (avg 36.5m) is the worst stage, so it lands above Tech Review (30m).
    assert text.index("In Progress") < text.index("Tech Review")


def test_as_dict_shape(analytics):
    d = analytics.as_dict()
    assert set(d) == {"window", "throughput", "stage_durations", "waits",
                      "escalations", "rework", "reliability"}
    assert d["window"]["records"] == len(SAMPLE)


# --- audit read_records `since` filter ------------------------------------- #

def test_read_records_since_filters_by_timestamp(tmp_path, monkeypatch):
    log = AuditLog(tmp_path / "audit.jsonl", max_bytes=0)
    # Freeze timestamps by patching the module clock the record path uses.
    import sentinel.audit as auditmod

    class _Clock:
        now_value = "2026-07-01T00:00:00+00:00"

        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(cls.now_value)

    monkeypatch.setattr(auditmod, "datetime", _Clock)
    _Clock.now_value = "2026-07-01T00:00:00+00:00"
    log.record("dispatch", ticket="SENT-1")
    _Clock.now_value = "2026-07-10T00:00:00+00:00"
    log.record("dispatch", ticket="SENT-2")

    recent = log.read_records(since="2026-07-05T00:00:00+00:00")
    assert [r["ticket"] for r in recent] == ["SENT-2"]
    allrecs = log.read_records()
    assert {r["ticket"] for r in allrecs} == {"SENT-1", "SENT-2"}
