"""reject_to_rework ordering: nothing may be posted before BOTH payloads validate."""

import asyncio

import pytest
import yaml

from sentinel.audit import AuditLog
from sentinel.config import load_settings
from sentinel.lease import LeaseManager
from sentinel.tools import ToolContext, dispatch

from fakes import FakeJira, FakeLLM

REQUIRED_ENV = {
    "JIRA_BASE_URL": "https://jira.example.com",
    "JIRA_PAT": "pat",
    "JIRA_PROJECT_KEY": "SENT",
    "LITELLM_BASE_URL": "https://llm.example.com",
    "LITELLM_API_KEY": "sk",
}


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    settings = load_settings("config/pipeline.yml")
    jira = FakeJira()
    jira.issues["SENT-1"] = {"key": "SENT-1", "fields": {
        "status": {"name": "Tech Review"}, "labels": [], "summary": "s"}}
    return ToolContext(
        jira=jira, llm=FakeLLM(),
        leases=LeaseManager(jira, "sentinel-bot", "agent-leased", 1800),
        settings=settings, audit=AuditLog(tmp_path / "audit.jsonl"),
        role=settings.roles["08-code-reviewer"], ticket="SENT-1",
        workspace=tmp_path)


def rejection_yaml():
    return yaml.safe_dump({"rework": {
        "rejected_from": "tech_review",
        "findings": [{"id": "F-1", "severity": "blocker", "criterion_ref": "SEC-1",
                      "location": "a.py:1", "description": "authz missing",
                      "required_action": "enforce role check", "evidence": "url"}],
    }})


def handoff_yaml(from_status="Tech Review"):
    return yaml.safe_dump({"agent_handoff": {
        "role": "08-code-reviewer", "ticket": "SENT-1",
        "timestamp": "2026-07-12T10:00:00+00:00", "verdict": "reject",
        "from_status": from_status, "to_status": "Rework",
        "checklist": [{"id": "REV-1", "result": "fail"}],
        "outputs": {}, "assumptions": [],
    }})


def test_valid_rejection_posts_both_payloads_and_transitions(ctx):
    result = asyncio.run(dispatch(ctx, "reject_to_rework", {
        "key": "SENT-1", "summary": "rejected",
        "rejection_yaml": rejection_yaml(), "handoff_yaml": handoff_yaml()}))
    assert result.terminal
    assert ctx.jira.transitions == [("SENT-1", "Rework")]
    bodies = ctx.jira.comments["SENT-1"]
    assert any("rework:" in b for b in bodies)
    assert any("agent_handoff:" in b for b in bodies)


def test_invalid_handoff_posts_nothing(ctx):
    # from_status contradicts the ticket's actual status -> pre-flight must fail
    result = asyncio.run(dispatch(ctx, "reject_to_rework", {
        "key": "SENT-1", "summary": "rejected",
        "rejection_yaml": rejection_yaml(),
        "handoff_yaml": handoff_yaml(from_status="In Progress")}))
    assert not result.terminal
    assert result.content.startswith("ERROR")
    assert ctx.jira.transitions == []
    # the load-bearing assertion: no orphaned rework payload was posted
    assert ctx.jira.comments.get("SENT-1", []) == []


def test_invalid_rejection_posts_nothing(ctx):
    broken = yaml.safe_load(rejection_yaml())
    broken["rework"]["findings"][0].pop("criterion_ref")
    result = asyncio.run(dispatch(ctx, "reject_to_rework", {
        "key": "SENT-1", "summary": "rejected",
        "rejection_yaml": yaml.safe_dump(broken), "handoff_yaml": handoff_yaml()}))
    assert result.content.startswith("ERROR")
    assert "criterion_ref" in result.content
    assert ctx.jira.comments.get("SENT-1", []) == []
    assert ctx.jira.transitions == []
