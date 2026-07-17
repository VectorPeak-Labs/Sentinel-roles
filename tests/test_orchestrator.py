"""Dispatch-gating tests for the Orchestrator against an in-memory fake Jira.

These cover the highest-risk logic in the platform: when an agent may be
dispatched, when a ticket is skipped, reclaimed, or escalated (ORC-1..4).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml

from sentinel.audit import AuditLog
from sentinel.config import load_settings
from sentinel.jira import (PROP_LEASE, PROP_REMINDED, PROP_RETRIES, PROP_REWORK,
                           PROP_WAITING)
from sentinel.lease import LeaseManager
from sentinel.orchestrator import Orchestrator

REQUIRED_ENV = {
    "JIRA_BASE_URL": "https://jira.example.com",
    "JIRA_PAT": "pat",
    "JIRA_PROJECT_KEY": "SENT",
    "LITELLM_BASE_URL": "https://llm.example.com",
    "LITELLM_API_KEY": "sk",
}


@pytest.fixture
def settings(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    return load_settings("config/pipeline.yml")


from fakes import FakeJira


class FakeRunner:
    def __init__(self):
        self.runs: list[tuple[str, str | None]] = []

    async def run(self, role, ticket, kickoff):
        self.runs.append((role.role_id, ticket))


def make_orchestrator(settings, jira, tmp_path):
    orch = Orchestrator(settings, jira, llm=None,
                        audit=AuditLog(tmp_path / "audit.jsonl"))
    orch.agent_user = "sentinel-bot"
    orch.leases = LeaseManager(jira, "sentinel-bot", settings.label("leased"),
                               settings.lease_timeout)
    orch.runner = FakeRunner()
    return orch


def issue(key, status, labels=(), updated="2026-07-12T10:00:00.000+0000"):
    return {"key": key, "fields": {"status": {"name": status},
                                   "labels": list(labels),
                                   "updated": updated, "summary": "s"}}


def run(coro):
    async def main():
        result = await coro
        await asyncio.sleep(0)  # let _spawn'd runner tasks execute
        return result
    return asyncio.run(main())


def test_dispatches_matching_role(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements")))
    assert orch.runner.runs == [("03-business-analyst", "SENT-1")]


def test_needs_human_blocks_dispatch(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements",
                                    labels=["needs-human"])))
    assert orch.runner.runs == []


def test_intake_requires_activate_label(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    run(orch._evaluate_ticket(issue("SENT-1", "New")))
    assert orch.runner.runs == []
    run(orch._evaluate_ticket(issue("SENT-1", "New", labels=["activate"])))
    assert orch.runner.runs == [("02-intake-triage", "SENT-1")]


def test_active_lease_skipped(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    jira.properties[("SENT-1", PROP_LEASE)] = {
        "role": "03-business-analyst",
        "heartbeat": "2099-01-01T00:00:00+00:00"}
    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements")))
    assert orch.runner.runs == []


def test_stale_lease_reclaimed_and_retried_once(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    jira.properties[("SENT-1", PROP_LEASE)] = {
        "role": "03-business-analyst",
        "heartbeat": "2020-01-01T00:00:00+00:00"}
    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements")))
    # reclaimed: lease gone, retry counter at 1, and re-dispatched once
    assert ("SENT-1", PROP_LEASE) not in jira.properties
    assert jira.properties[("SENT-1", PROP_RETRIES)]["count"] == 1
    assert orch.runner.runs == [("03-business-analyst", "SENT-1")]


def test_second_failure_escalates(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    jira.properties[("SENT-1", PROP_RETRIES)] = {"count": 1}
    jira.properties[("SENT-1", PROP_LEASE)] = {
        "role": "03-business-analyst",
        "heartbeat": "2020-01-01T00:00:00+00:00"}
    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements")))
    assert orch.runner.runs == []
    assert "needs-human" in jira.labels["SENT-1"]
    # retry budget reset so removing the label resumes cleanly
    assert ("SENT-1", PROP_RETRIES) not in jira.properties


def test_rework_loop_breaker_escalates(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    jira.properties[("SENT-1", PROP_REWORK)] = {"count": 3}
    run(orch._evaluate_ticket(issue("SENT-1", "Rework")))
    assert orch.runner.runs == []
    assert "needs-human" in jira.labels["SENT-1"]


def test_waiting_ticket_skipped_until_activity_or_wake(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    jira.properties[("SENT-1", PROP_WAITING)] = {
        "since": "2026-07-12T11:00:00+00:00",
        "wake_at": "2099-01-01T00:00:00+00:00"}
    # updated before `since`, wake far in the future -> parked
    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements",
                                    updated="2026-07-12T10:00:00.000+0000")))
    assert orch.runner.runs == []
    # fresh human activity after `since` -> wakes up, marker cleared
    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements",
                                    updated="2026-07-12T12:00:00.000+0000")))
    assert orch.runner.runs == [("03-business-analyst", "SENT-1")]
    assert ("SENT-1", PROP_WAITING) not in jira.properties


def test_wip_limit_blocks_dispatch(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    limit = settings.wip_limit("In Progress")
    for i in range(limit):
        orch.running[("07-implementer", f"SENT-{i}")] = (None, "In Progress")
    run(orch._evaluate_ticket(issue("SENT-99", "In Progress")))
    assert orch.runner.runs == []


def test_release_queue_gated_on_window_label(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    queue = [issue("SENT-30", "Client Review Accepted")]
    run(orch._evaluate_queues(queue))
    assert orch.runner.runs == []
    queue = [issue("SENT-30", "Client Review Accepted", labels=["release-now"])]
    run(orch._evaluate_queues(queue))
    assert ("12-release", None) in {(r, t) for r, t in orch.runner.runs}


def test_agent_transition_without_payload_flagged(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    run(orch._on_status_change("SENT-1", "sentinel-bot",
                               {"fromString": "Tech Review",
                                "toString": "Tech Review Accepted"}))
    assert "handoff-invalid" in jira.labels["SENT-1"]
    assert "needs-human" in jira.labels["SENT-1"]


def test_agent_transition_with_valid_payload_accepted(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    handoff = {
        "role": "08-code-reviewer", "ticket": "SENT-1",
        "timestamp": "2026-07-12T10:00:00+00:00", "verdict": "pass",
        "from_status": "Tech Review", "to_status": "Tech Review Accepted",
        "checklist": [{"id": "REV-1", "result": "pass", "evidence": "url"}],
        "outputs": {}, "assumptions": [],
    }
    jira.comments["SENT-1"] = [
        "handoff\n```yaml\n" + yaml.safe_dump({"agent_handoff": handoff}) + "```"]
    jira.properties[("SENT-1", PROP_RETRIES)] = {"count": 1}
    run(orch._on_status_change("SENT-1", "sentinel-bot",
                               {"fromString": "Tech Review",
                                "toString": "Tech Review Accepted"}))
    assert "handoff-invalid" not in jira.labels.get("SENT-1", set())
    # clean stage exit resets the crash-retry budget
    assert ("SENT-1", PROP_RETRIES) not in jira.properties


def test_webhook_burst_coalesces_into_one_evaluation(settings, tmp_path):
    jira = FakeJira()
    jira.issues = {"SENT-1": issue("SENT-1", "Business Requirements")}
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.webhook_debounce_seconds = 0.01

    async def main():
        event = {"issue": {"key": "SENT-1"}, "user": {"name": "alice"},
                 "comment": {"body": "hi"}}
        for _ in range(5):  # comment storm
            await orch.handle_webhook(event)
        await orch._flush_task
        await asyncio.sleep(0)

    asyncio.run(main())
    # five events, one dispatch (later events see the ticket already running/leased)
    assert orch.runner.runs == [("03-business-analyst", "SENT-1")]

def test_sweep_failures_tracked_and_reset(settings, tmp_path):
    from sentinel.jira import JiraError

    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)

    async def broken_search(jql, max_results=100, fields=None):
        raise JiraError(401, "PAT expired")

    async def main():
        jira.search = broken_search
        await orch._sweep_safely()
        await orch._sweep_safely()
        assert orch.consecutive_sweep_failures == 2   # /health flips to 'degraded' here
        assert "PAT expired" in orch.last_sweep_error
        jira.search = FakeJira().search               # Jira back up
        await orch._sweep_safely()
        assert orch.consecutive_sweep_failures == 0
        assert orch.last_sweep_error is None
        assert orch.sweep_count == 1

    asyncio.run(main())


def test_human_transition_honored_without_validation(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    run(orch._on_status_change("SENT-1", "alice",
                               {"fromString": "New", "toString": "Done"}))
    assert jira.labels.get("SENT-1", set()) == set()


# -- global pause (operational kill-switch) --------------------------------

def test_pause_suppresses_ticket_dispatch(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.settings.data_dir = tmp_path
    run(orch.pause(reason="incident", by="alice"))
    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements")))
    assert orch.runner.runs == []            # nothing dispatched while paused
    # a paused evaluation takes no side effects (no reclaim/escalate mutations)
    assert jira.labels.get("SENT-1", set()) == set()


def test_pause_suppresses_queue_dispatch(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.settings.data_dir = tmp_path
    run(orch.pause(by="alice"))
    queue = [issue("SENT-30", "Client Review Accepted", labels=["release-now"])]
    run(orch._evaluate_queues(queue))
    assert orch.runner.runs == []


def test_resume_restores_dispatch(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.settings.data_dir = tmp_path
    run(orch.pause(by="alice"))
    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements")))
    assert orch.runner.runs == []
    run(orch.resume(by="alice"))
    assert orch.paused is False
    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements")))
    assert orch.runner.runs == [("03-business-analyst", "SENT-1")]


class RecordingNotifier:
    """Captures notify() calls so tests can assert an alert fired."""
    def __init__(self):
        self.events: list[tuple[str, str | None]] = []

    async def notify(self, event, text, *, ticket=None, **fields):
        self.events.append((event, ticket))
        return True

    async def close(self):
        pass


def test_escalation_fires_notification(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.notifier = RecordingNotifier()
    # count > 1 => retry budget exhausted => escalate
    jira.properties[("SENT-1", PROP_RETRIES)] = {"count": 2}
    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements")))
    assert "needs-human" in jira.labels["SENT-1"]
    assert orch.notifier.events == [("orchestrator_escalation", "SENT-1")]


def test_sweep_computes_board_state(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    board = [
        issue("SENT-1", "In Progress"),
        issue("SENT-2", "In Progress"),
        issue("SENT-3", "Rework", labels=["needs-human"]),
        issue("SENT-4", "Tech Review", labels=["handoff-invalid"]),
    ]

    async def fake_search(jql, max_results=100, fields=None):
        return board

    async def main():
        jira.search = fake_search
        await orch.sweep()

    asyncio.run(main())
    bs = orch.board_state
    assert bs["total"] == 4
    assert bs["by_status"]["In Progress"] == 2
    assert bs["by_status"]["Rework"] == 1
    assert bs["needs_human"] == 1
    assert bs["handoff_invalid"] == 1


def test_metrics_count_dispatch_and_escalation(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    # a clean dispatch
    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements")))
    # a forced escalation on another ticket
    jira.properties[("SENT-2", PROP_RETRIES)] = {"count": 2}
    run(orch._evaluate_ticket(issue("SENT-2", "Business Requirements")))
    snap = orch.metrics.snapshot()
    assert snap["dispatches_total"] == 1
    assert snap["escalations_total"] == 1


def _ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def frozen_issue(key="SENT-1", hours_since_update=48, labels=("needs-human",)):
    return issue(key, "Rework", labels=list(labels), updated=_ago(hours_since_update))


def test_stale_escalation_is_reminded(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.notifier = RecordingNotifier()
    run(orch._remind_stale_escalations([frozen_issue("SENT-1", hours_since_update=48)]))
    assert orch.notifier.events == [("stale_escalation", "SENT-1")]
    assert ("SENT-1", PROP_REMINDED) in jira.properties
    assert orch.metrics.snapshot()["stale_escalation_reminders_total"] == 1


def test_recently_touched_escalation_not_reminded(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.notifier = RecordingNotifier()
    # a human commented an hour ago (updated is recent) -> not abandoned
    run(orch._remind_stale_escalations([frozen_issue("SENT-1", hours_since_update=1)]))
    assert orch.notifier.events == []


def test_reminder_deduped_within_window(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.notifier = RecordingNotifier()
    jira.properties[("SENT-1", PROP_REMINDED)] = {"at": _ago(1)}  # reminded an hour ago
    run(orch._remind_stale_escalations([frozen_issue("SENT-1", hours_since_update=48)]))
    assert orch.notifier.events == []                             # once per window only


def test_non_frozen_ticket_not_reminded(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.notifier = RecordingNotifier()
    run(orch._remind_stale_escalations(
        [issue("SENT-1", "Rework", updated=_ago(72))]))  # old but not needs-human
    assert orch.notifier.events == []


def test_reminders_disabled_when_hours_zero(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.settings.stale_escalation_hours = 0
    orch.notifier = RecordingNotifier()
    run(orch._remind_stale_escalations([frozen_issue("SENT-1", hours_since_update=999)]))
    assert orch.notifier.events == []


def test_pause_and_resume_fire_notifications(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.settings.data_dir = tmp_path
    orch.notifier = RecordingNotifier()
    run(orch.pause(reason="incident", by="alice"))
    run(orch.resume(by="alice"))
    assert [e[0] for e in orch.notifier.events] == ["pipeline_paused", "pipeline_resumed"]


def test_stop_cancels_agents_and_releases_leases(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    jira.properties[("SENT-1", PROP_LEASE)] = {
        "role": "03-business-analyst", "heartbeat": "2099-01-01T00:00:00+00:00"}

    async def main():
        async def blocker():
            await asyncio.Event().wait()
        task = asyncio.create_task(blocker())
        orch.running[("03-business-analyst", "SENT-1")] = (task, "Business Requirements")
        await orch.stop()
        assert task.cancelled()                              # in-flight agent stopped
        assert ("SENT-1", PROP_LEASE) not in jira.properties  # lease freed, not stranded

    asyncio.run(main())


def test_pause_state_survives_restart(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.settings.data_dir = tmp_path
    run(orch.pause(reason="freeze for deploy", by="alice"))
    assert (tmp_path / "pause.json").exists()

    # A fresh orchestrator (simulating a container restart) must reload the freeze.
    revived = make_orchestrator(settings, jira, tmp_path)
    revived.settings.data_dir = tmp_path
    revived._load_pause_state()
    assert revived.paused is True
    assert revived.pause_reason == "freeze for deploy"

    # ...and resume clears the persisted state so the next restart starts clean.
    run(revived.resume(by="alice"))
    assert not (tmp_path / "pause.json").exists()


def test_token_budget_pauses_pipeline(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.settings.data_dir = tmp_path
    orch.notifier = RecordingNotifier()
    orch.settings.llm_daily_token_budget = 1000
    orch.llm = SimpleNamespace(tokens_in_current_window=lambda: 1200,
                               consecutive_failures=0)      # budget blown

    run(orch.sweep())
    assert orch.paused is True
    assert "token budget exhausted" in orch.pause_reason
    assert orch.metrics.snapshot()["token_budget_pauses_total"] == 1
    assert ("pipeline_paused", None) in orch.notifier.events

    # further sweeps while paused do not re-trip the breaker (no alert storm)
    run(orch.sweep())
    assert orch.metrics.snapshot()["token_budget_pauses_total"] == 1

    # dispatch stays suppressed while paused
    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements")))
    assert orch.runner.runs == []


def test_token_budget_disabled_by_default(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.notifier = RecordingNotifier()
    orch.llm = SimpleNamespace(tokens_in_current_window=lambda: 10**9,
                               consecutive_failures=0)      # huge spend, no budget
    assert orch.settings.llm_daily_token_budget == 0
    run(orch.sweep())
    assert orch.paused is False
    assert orch.notifier.events == []


def test_token_budget_under_limit_keeps_dispatching(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.settings.llm_daily_token_budget = 1000
    orch.llm = SimpleNamespace(tokens_in_current_window=lambda: 999,
                               consecutive_failures=0)
    run(orch.sweep())
    assert orch.paused is False
    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements")))
    assert orch.runner.runs == [("03-business-analyst", "SENT-1")]


def test_token_budget_resume_after_midnight_sticks(settings, tmp_path, monkeypatch):
    """Regression: the breaker must not be sticky across days. tokens_today only
    reset on new usage writes, but while paused no LLM call ever happens — so a
    resume after UTC midnight was instantly re-paused by yesterday's count."""
    import sentinel.llm as llm_mod
    from sentinel.llm import LLM

    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.settings.data_dir = tmp_path
    orch.notifier = RecordingNotifier()
    orch.settings.llm_daily_token_budget = 1000

    monkeypatch.setattr(llm_mod, "_utc_today", lambda: "2026-07-17")
    llm = LLM("https://llm.example.com/v1", "key", "gpt-4o")
    llm._record_usage("07-implementer", "gpt-4o",
                      SimpleNamespace(prompt_tokens=900, completion_tokens=300))
    orch.llm = llm

    run(orch.sweep())                                   # day N: budget blown
    assert orch.paused is True

    monkeypatch.setattr(llm_mod, "_utc_today", lambda: "2026-07-18")
    run(orch.resume(by="operator"))                     # human resumes next day
    run(orch.sweep())                                   # must NOT re-pause
    assert orch.paused is False
    assert orch.metrics.snapshot()["token_budget_pauses_total"] == 1

    # new spend crossing the budget on day N+1 trips it again
    llm._record_usage("07-implementer", "gpt-4o",
                      SimpleNamespace(prompt_tokens=1100, completion_tokens=0))
    run(orch.sweep())
    assert orch.paused is True
    assert orch.metrics.snapshot()["token_budget_pauses_total"] == 2


class OutageFakeLLM:
    """Simulates a failing LiteLLM backend: probes fail until recover_on_probe
    is set, at which point a probe succeeds and resets the failure counter
    (mirroring the real LLM.chat behavior)."""
    def __init__(self, failures):
        self.consecutive_failures = failures
        self.last_error = "APIConnectionError"
        self.probes = 0
        self.recover_on_probe = False

    async def chat(self, messages, tools=None, model=None, temperature=None,
                   role=None):
        self.probes += 1
        if self.recover_on_probe:
            self.consecutive_failures = 0
            return SimpleNamespace(content="ok")
        self.consecutive_failures += 1
        raise RuntimeError("down")


def test_llm_gate_suspends_dispatch_during_outage(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.notifier = RecordingNotifier()
    orch.llm = OutageFakeLLM(failures=3)

    run(orch.sweep())
    assert orch.llm_gated is True
    assert orch.llm.probes == 1                                  # probed for recovery
    assert orch.notifier.events == [("llm_outage", None)]
    assert orch.metrics.snapshot()["llm_gate_engagements_total"] == 1

    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements")))
    assert orch.runner.runs == []                                # dispatch suppressed

    run(orch.sweep())                                            # still down
    assert orch.notifier.events == [("llm_outage", None)]        # no alert storm
    assert orch.llm.probes == 2                                  # keeps probing


def test_llm_gate_lifts_itself_when_probe_succeeds(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.notifier = RecordingNotifier()
    orch.llm = OutageFakeLLM(failures=3)

    run(orch.sweep())
    assert orch.llm_gated is True

    orch.llm.recover_on_probe = True                             # backend comes back
    run(orch.sweep())
    assert orch.llm_gated is False
    assert orch.notifier.events == [("llm_outage", None), ("llm_recovered", None)]

    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements")))
    assert orch.runner.runs == [("03-business-analyst", "SENT-1")]   # dispatch resumed


def test_llm_gate_not_engaged_below_threshold(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    orch.notifier = RecordingNotifier()
    orch.llm = OutageFakeLLM(failures=2)                         # under the threshold

    run(orch.sweep())
    assert orch.llm_gated is False
    assert orch.llm.probes == 0                                  # healthy: no probe calls
    assert orch.notifier.events == []
    run(orch._evaluate_ticket(issue("SENT-1", "Business Requirements")))
    assert orch.runner.runs == [("03-business-analyst", "SENT-1")]
