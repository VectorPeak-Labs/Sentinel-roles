"""Tests for the agent tool loop (sentinel/agent.py) with a scripted fake LLM:
lease lifecycle, handoff enforcement, terminal semantics, turn-cap and crash cleanup."""

import asyncio

import pytest
import yaml

from sentinel.agent import AgentRunner
from sentinel.audit import AuditLog
from sentinel.config import load_settings
from sentinel.jira import PROP_LEASE, PROP_RETRIES
from sentinel.lease import LeaseManager

from fakes import FakeJira, FakeLLM, llm_msg, tool_call

REQUIRED_ENV = {
    "JIRA_BASE_URL": "https://jira.example.com",
    "JIRA_PAT": "pat",
    "JIRA_PROJECT_KEY": "SENT",
    "LITELLM_BASE_URL": "https://llm.example.com",
    "LITELLM_API_KEY": "sk",
}


@pytest.fixture
def settings(monkeypatch, tmp_path):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    s = load_settings("config/pipeline.yml")
    s.data_dir = tmp_path
    s.max_agent_turns = 4
    return s


def make_runner(settings, jira, llm, tmp_path):
    leases = LeaseManager(jira, "sentinel-bot", settings.label("leased"),
                          settings.lease_timeout)
    return AgentRunner(settings, jira, llm, leases,
                       AuditLog(tmp_path / "audit.jsonl"), "sentinel-bot")


def seed_ticket(jira, key="SENT-1", status="Business Requirements"):
    jira.issues[key] = {"key": key, "fields": {
        "status": {"name": status}, "labels": [], "summary": "s",
        "description": "", "updated": "2026-07-12T10:00:00.000+0000"}}


def valid_handoff_yaml(key="SENT-1"):
    return yaml.safe_dump({"agent_handoff": {
        "role": "03-business-analyst", "ticket": key,
        "timestamp": "2026-07-12T10:00:00+00:00", "verdict": "pass",
        "from_status": "Business Requirements",
        "to_status": "Technical Requirements",
        "checklist": [{"id": "BIZ-1", "result": "pass", "evidence": "comment-1"}],
        "outputs": {}, "assumptions": [],
    }})


def transition_call(call_id="t1", handoff=None):
    return tool_call(call_id, "transition_with_handoff", {
        "key": "SENT-1", "to_status": "Technical Requirements",
        "summary": "handoff", "handoff_yaml": handoff or valid_handoff_yaml()})


def test_happy_path_transitions_and_releases_lease(settings, tmp_path):
    jira = FakeJira()
    seed_ticket(jira)
    llm = FakeLLM(script=[
        llm_msg(tool_calls=[tool_call("c1", "get_ticket", {"key": "SENT-1"})]),
        llm_msg(tool_calls=[transition_call()]),
    ])
    runner = make_runner(settings, jira, llm, tmp_path)
    role = settings.roles["03-business-analyst"]

    asyncio.run(runner.run(role, "SENT-1", "go"))

    assert jira.transitions == [("SENT-1", "Technical Requirements")]
    assert ("SENT-1", PROP_LEASE) not in jira.properties       # lease released
    assert "agent-leased" not in jira.labels.get("SENT-1", set())
    assert any("agent_handoff" in c for c in jira.comments["SENT-1"])
    assert llm.calls == 2                                       # terminal stopped the loop


def test_invalid_handoff_is_rejected_then_retried(settings, tmp_path):
    jira = FakeJira()
    seed_ticket(jira)
    broken = yaml.safe_load(valid_handoff_yaml())
    broken["agent_handoff"]["checklist"][0].pop("evidence")     # pass without evidence
    llm = FakeLLM(script=[
        llm_msg(tool_calls=[transition_call("t1", yaml.safe_dump(broken))]),
        llm_msg(tool_calls=[transition_call("t2")]),            # fixed payload
    ])
    runner = make_runner(settings, jira, llm, tmp_path)

    asyncio.run(runner.run(settings.roles["03-business-analyst"], "SENT-1", "go"))

    # first attempt refused (no transition happened), second succeeded
    assert jira.transitions == [("SENT-1", "Technical Requirements")]
    assert llm.calls == 2


def test_prose_without_tools_gets_reminder_not_termination(settings, tmp_path):
    jira = FakeJira()
    seed_ticket(jira)
    llm = FakeLLM(script=[
        llm_msg(content="Let me think about this ticket..."),   # no tool call
        llm_msg(tool_calls=[transition_call()]),
    ])
    runner = make_runner(settings, jira, llm, tmp_path)

    asyncio.run(runner.run(settings.roles["03-business-analyst"], "SENT-1", "go"))

    assert jira.transitions == [("SENT-1", "Technical Requirements")]
    assert llm.calls == 2


def test_turn_cap_releases_lease_and_bumps_retries(settings, tmp_path):
    jira = FakeJira()
    seed_ticket(jira)
    llm = FakeLLM(default=llm_msg(content="still thinking..."))  # never terminal
    runner = make_runner(settings, jira, llm, tmp_path)

    asyncio.run(runner.run(settings.roles["03-business-analyst"], "SENT-1", "go"))

    assert llm.calls == settings.max_agent_turns
    assert ("SENT-1", PROP_LEASE) not in jira.properties
    assert jira.properties[("SENT-1", PROP_RETRIES)]["count"] == 1
    assert any("turn cap" in c for c in jira.comments["SENT-1"])
    assert jira.transitions == []


def test_llm_crash_releases_lease_and_bumps_retries(settings, tmp_path):
    jira = FakeJira()
    seed_ticket(jira)
    llm = FakeLLM(script=[RuntimeError("LiteLLM 502")])
    runner = make_runner(settings, jira, llm, tmp_path)

    asyncio.run(runner.run(settings.roles["03-business-analyst"], "SENT-1", "go"))

    assert ("SENT-1", PROP_LEASE) not in jira.properties
    assert jira.properties[("SENT-1", PROP_RETRIES)]["count"] == 1
    assert jira.transitions == []


def planner_handoff_yaml(key):
    return yaml.safe_dump({"agent_handoff": {
        "role": "06-sprint-planner", "ticket": key,
        "timestamp": "2026-07-12T10:00:00+00:00", "verdict": "pass",
        "from_status": "To Do", "to_status": "In Progress",
        "checklist": [{"id": "PLN-1", "result": "pass", "evidence": "comment-1"}],
        "outputs": {}, "assumptions": [],
    }})


def planner_transition(call_id, key):
    return tool_call(call_id, "transition_with_handoff", {
        "key": key, "to_status": "In Progress", "summary": "planned",
        "handoff_yaml": planner_handoff_yaml(key)})


def test_queue_role_claims_and_transitions_multiple_tickets(settings, tmp_path):
    jira = FakeJira()
    seed_ticket(jira, "SENT-1", "To Do")
    seed_ticket(jira, "SENT-2", "To Do")
    llm = FakeLLM(script=[
        llm_msg(tool_calls=[tool_call("c1", "claim_ticket", {"key": "SENT-1"}),
                            tool_call("c2", "claim_ticket", {"key": "SENT-2"})]),
        # transitions are NOT terminal for queue roles — the run continues
        llm_msg(tool_calls=[planner_transition("t1", "SENT-1")]),
        llm_msg(tool_calls=[planner_transition("t2", "SENT-2")]),
        llm_msg(tool_calls=[tool_call("f1", "finish_run", {"summary": "queue drained"})]),
    ])
    runner = make_runner(settings, jira, llm, tmp_path)

    asyncio.run(runner.run(settings.roles["06-sprint-planner"], None, "queue kickoff"))

    assert jira.transitions == [("SENT-1", "In Progress"), ("SENT-2", "In Progress")]
    assert llm.calls == 4                               # finish_run ended the loop
    # no lease residue anywhere
    assert not any(prop == PROP_LEASE for _, prop in jira.properties)
    assert "agent-leased" not in jira.labels.get("SENT-1", set())
    assert "agent-leased" not in jira.labels.get("SENT-2", set())


def test_queue_role_transition_refused_without_claim(settings, tmp_path):
    jira = FakeJira()
    seed_ticket(jira, "SENT-1", "To Do")
    llm = FakeLLM(script=[
        llm_msg(tool_calls=[planner_transition("t1", "SENT-1")]),  # no claim first
        llm_msg(tool_calls=[tool_call("f1", "finish_run", {"summary": "done"})]),
    ])
    runner = make_runner(settings, jira, llm, tmp_path)

    asyncio.run(runner.run(settings.roles["06-sprint-planner"], None, "queue kickoff"))

    assert jira.transitions == []


def test_heartbeat_covers_queue_claimed_tickets(settings, tmp_path):
    from sentinel.tools import ToolContext

    jira = FakeJira()
    runner = make_runner(settings, jira, FakeLLM(), tmp_path)
    ctx = ToolContext(jira=jira, llm=runner.llm, leases=runner.leases,
                      settings=settings, audit=runner.audit,
                      role=settings.roles["06-sprint-planner"], ticket=None,
                      workspace=tmp_path)

    async def main():
        await runner.leases.claim("SENT-1", "06-sprint-planner")
        ctx.extra_leased.add("SENT-1")
        jira.properties[("SENT-1", PROP_LEASE)]["heartbeat"] = "2020-01-01T00:00:00+00:00"
        await runner._heartbeat_once(ctx)
        assert jira.properties[("SENT-1", PROP_LEASE)]["heartbeat"] > "2020-01-02"
        # lease taken over by someone else -> dropped from this run's holdings
        jira.properties[("SENT-1", PROP_LEASE)] = {"role": "somebody-else"}
        await runner._heartbeat_once(ctx)
        assert "SENT-1" not in ctx.extra_leased

    asyncio.run(main())


def test_dispatch_aborts_if_ticket_actively_leased(settings, tmp_path):
    jira = FakeJira()
    seed_ticket(jira)
    jira.properties[("SENT-1", PROP_LEASE)] = {
        "role": "other", "heartbeat": "2099-01-01T00:00:00+00:00"}
    llm = FakeLLM()  # must never be called
    runner = make_runner(settings, jira, llm, tmp_path)

    asyncio.run(runner.run(settings.roles["03-business-analyst"], "SENT-1", "go"))

    assert llm.calls == 0
    assert jira.transitions == []


def test_cancellation_releases_all_held_leases(settings, tmp_path):
    """On shutdown/redeploy the agent run is cancelled; it must release the ticket
    lease AND any tickets a queue role self-claimed, so nothing stays frozen until
    the stale-lease timeout. It must NOT bump the retry counter (not a failure)."""
    jira = FakeJira()
    seed_ticket(jira, "SENT-1")
    runner = make_runner(settings, jira, FakeLLM(), tmp_path)
    started = asyncio.Event()

    async def fake_loop(ctx, kickoff):
        await ctx.leases.claim("SENT-2", ctx.role.role_id)   # queue-style extra claim
        ctx.extra_leased.add("SENT-2")
        started.set()
        await asyncio.Event().wait()                          # block until cancelled

    runner._loop = fake_loop
    role = settings.roles["03-business-analyst"]

    async def main():
        task = asyncio.create_task(runner.run(role, "SENT-1", "go"))
        await started.wait()
        assert ("SENT-1", PROP_LEASE) in jira.properties
        assert ("SENT-2", PROP_LEASE) in jira.properties
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # both leases freed immediately, and no retry breadcrumb left behind
        assert ("SENT-1", PROP_LEASE) not in jira.properties
        assert ("SENT-2", PROP_LEASE) not in jira.properties
        assert ("SENT-1", PROP_RETRIES) not in jira.properties

    asyncio.run(main())
