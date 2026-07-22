"""Tests for the deterministic end-to-end demo (sentinel/demo.py).

These are the CI guard for the demo: they run the real orchestrator/agent/tool
path against the in-memory board + scripted LLM and assert the ticket completes
its scripted journey with valid handoffs and no escalation. They also pin the
demo flow to config/pipeline.yml so a dispatch-table change can't silently
desync the walkthrough.
"""

import asyncio

import pytest

from sentinel.config import load_settings
from sentinel.demo import (
    FLOW,
    TARGET_STATUS,
    InMemoryJira,
    ScriptedLLM,
    render_markdown,
    render_transcript,
    run_demo,
)
from sentinel.payloads import validate_handoff

REQUIRED_ENV = {
    "JIRA_BASE_URL": "https://demo.invalid",
    "JIRA_PAT": "demo",
    "JIRA_PROJECT_KEY": "SENT",
    "LITELLM_BASE_URL": "https://demo.invalid",
    "LITELLM_API_KEY": "demo",
}


@pytest.fixture
def env(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def result(env):
    return asyncio.run(run_demo())


# --- end-to-end outcome ---------------------------------------------------- #

def test_demo_reaches_target_status(result):
    assert result.final_status == TARGET_STATUS
    assert result.escalated is False


def test_demo_walks_every_scripted_role_in_order(result):
    walked = [(role, frm, to) for role, frm, to in result.transitions]
    assert walked == list(FLOW)


def test_demo_posts_a_valid_handoff_per_transition(result):
    # One handoff payload per FLOW step, and every one passes the real schema.
    assert len(result.handoffs) == len(FLOW)
    for payload in result.handoffs:
        assert validate_handoff(payload).ok, payload


def test_demo_records_dispatch_and_transition_audit_events(result):
    events = [r["event"] for r in result.audit_records]
    assert events.count("dispatch") == len(FLOW)
    assert events.count("transition") == len(FLOW)
    assert "orchestrator_start" in events
    # the queue role (06) ends with finish_run, recorded as run_finished_waiting
    assert "run_finished_waiting" in events


def test_demo_is_deterministic(env):
    a = asyncio.run(run_demo())
    b = asyncio.run(run_demo())
    assert a.transitions == b.transitions
    assert a.final_status == b.final_status
    assert len(a.handoffs) == len(b.handoffs)


def test_demo_leaves_no_dangling_lease(result):
    # Every role released its lease on transition/finish; nothing frozen.
    assert result.escalated is False
    # a completed run must not leave needs-human / handoff-invalid on the ticket
    assert not any(r["event"] == "orchestrator_escalation" for r in result.audit_records)


# --- flow / config agreement ---------------------------------------------- #

def test_flow_matches_pipeline_dispatch_table(env):
    """Each scripted (role, from_status) must be exactly how the shipped
    pipeline routes that status — otherwise the demo would drift from reality."""
    settings = load_settings("config/pipeline.yml")
    for role_id, from_status, _to in FLOW:
        owners = [r.role_id for r in settings.roles_for_status(from_status)]
        assert role_id in owners, (
            f"{role_id} does not watch '{from_status}' in pipeline.yml (owners: {owners})")


# --- rendering ------------------------------------------------------------- #

def test_render_transcript_contains_walk_and_timeline(result):
    text = render_transcript(result)
    assert "Sentinel end-to-end demo" in text
    assert TARGET_STATUS in text
    for role_id, _f, _t in FLOW:
        assert role_id in text
    assert "Audit timeline:" in text


def test_render_markdown_is_a_table(result):
    md = render_markdown(result)
    assert md.startswith("# Sentinel end-to-end demo")
    assert "| # | Role | From | To |" in md
    assert "`08-code-reviewer`" in md


# --- scripted LLM units ---------------------------------------------------- #

def test_scripted_llm_estimator_subcall_returns_estimate():
    llm = ScriptedLLM("SENT")
    msg = asyncio.run(llm.chat(
        [{"role": "system", "content": "You are an independent story-point estimator ..."},
         {"role": "user", "content": "estimate this"}]))
    assert msg.tool_calls is None
    assert "ESTIMATE:" in msg.content


def test_scripted_llm_ticket_key_extraction():
    llm = ScriptedLLM("SENT")
    key = llm._ticket_key([{"role": "user", "content": "activated for ticket **SENT-42**"}])
    assert key == "SENT-42"


# --- in-memory Jira units -------------------------------------------------- #

def test_in_memory_jira_search_matches_quoted_status():
    jira = InMemoryJira()
    jira.seed("SENT-1", summary="s", status="In Progress", labels=[])
    jira.seed("SENT-2", summary="s", status="To Do", labels=[])
    hits = asyncio.run(jira.search('project = SENT AND status = "In Progress"'))
    assert [i["key"] for i in hits] == ["SENT-1"]


def test_in_memory_jira_transition_updates_status():
    jira = InMemoryJira()
    jira.seed("SENT-1", summary="s", status="To Do", labels=[])
    asyncio.run(jira.transition_to("SENT-1", "In Progress"))
    assert jira.status_of("SENT-1") == "In Progress"
