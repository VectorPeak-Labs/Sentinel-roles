"""increment_rework (role 13): counting, idempotency on retry, loop-breaker signal."""

import asyncio
import json

import pytest
import yaml

from sentinel.audit import AuditLog
from sentinel.config import load_settings
from sentinel.jira import PROP_REWORK
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


def rejection_comment(finding_id="F-1"):
    return "rejected\n```yaml\n" + yaml.safe_dump({"rework": {
        "rejected_from": "tech_review",
        "findings": [{"id": finding_id, "severity": "major", "criterion_ref": "AC-1",
                      "location": "a.py:1", "description": "broken",
                      "required_action": "fix it", "evidence": "url"}],
    }}) + "```"


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    settings = load_settings("config/pipeline.yml")
    jira = FakeJira()
    return ToolContext(
        jira=jira, llm=FakeLLM(),
        leases=LeaseManager(jira, "sentinel-bot", "agent-leased", 1800),
        settings=settings, audit=AuditLog(tmp_path / "audit.jsonl"),
        role=settings.roles["13-rework-router"], ticket="SENT-1",
        workspace=tmp_path)


def increment(ctx):
    result = asyncio.run(dispatch(ctx, "increment_rework", {"key": "SENT-1"}))
    return result, (json.loads(result.content) if not result.content.startswith("ERROR")
                    else None)


def test_first_rejection_counts_once(ctx):
    ctx.jira.comments["SENT-1"] = [rejection_comment()]
    _, data = increment(ctx)
    assert data["rework_count"] == 1
    assert data["limit_exceeded"] is False
    assert data["already_counted"] is False
    assert len(data["bounce_history"]) == 1


def test_retry_after_crash_is_idempotent(ctx):
    ctx.jira.comments["SENT-1"] = [rejection_comment()]
    increment(ctx)
    # router crashed before transitioning; orchestrator retried the run
    _, data = increment(ctx)
    assert data["rework_count"] == 1          # NOT double-counted
    assert data["already_counted"] is True
    assert len(data["bounce_history"]) == 1


def test_new_rejection_counts_again(ctx):
    ctx.jira.comments["SENT-1"] = [rejection_comment("F-1")]
    increment(ctx)
    ctx.jira.comments["SENT-1"].append("some human chatter")
    ctx.jira.comments["SENT-1"].append(rejection_comment("F-2"))
    _, data = increment(ctx)
    assert data["rework_count"] == 2
    assert [h["findings"][0]["id"] for h in data["bounce_history"]] == ["F-1", "F-2"]


def test_loop_breaker_signal_past_limit(ctx):
    ctx.jira.properties[("SENT-1", PROP_REWORK)] = {
        "count": 2, "history": [], "last_counted_comment": "old"}
    ctx.jira.comments["SENT-1"] = [rejection_comment("F-3")]
    _, data = increment(ctx)
    assert data["rework_count"] == 3
    assert data["limit_exceeded"] is True


def test_missing_payload_is_an_error(ctx):
    ctx.jira.comments["SENT-1"] = ["just a chat comment, no payload"]
    result, _ = increment(ctx)
    assert result.content.startswith("ERROR")
    assert "handoff-invalid" in result.content
    assert ("SENT-1", PROP_REWORK) not in ctx.jira.properties
