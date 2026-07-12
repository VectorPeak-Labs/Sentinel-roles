"""Dispatch-gating tests for the Orchestrator against an in-memory fake Jira.

These cover the highest-risk logic in the platform: when an agent may be
dispatched, when a ticket is skipped, reclaimed, or escalated (ORC-1..4).
"""

import asyncio

import pytest
import yaml

from sentinel.audit import AuditLog
from sentinel.config import load_settings
from sentinel.jira import PROP_LEASE, PROP_RETRIES, PROP_REWORK, PROP_WAITING
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

def test_human_transition_honored_without_validation(settings, tmp_path):
    jira = FakeJira()
    orch = make_orchestrator(settings, jira, tmp_path)
    run(orch._on_status_change("SENT-1", "alice",
                               {"fromString": "New", "toString": "Done"}))
    assert jira.labels.get("SENT-1", set()) == set()
