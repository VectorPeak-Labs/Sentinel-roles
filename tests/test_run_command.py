"""run_command guardrails: shell gating and workspace confinement."""

import asyncio

import pytest

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


def make_ctx(monkeypatch, tmp_path, role_id, workspace):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    settings = load_settings("config/pipeline.yml")
    jira = FakeJira()
    return ToolContext(
        jira=jira, llm=FakeLLM(),
        leases=LeaseManager(jira, "sentinel-bot", "agent-leased", 1800),
        settings=settings, audit=AuditLog(tmp_path / "audit.jsonl"),
        role=settings.roles[role_id], ticket="SENT-1", workspace=workspace)


def run_cmd(ctx, **args):
    return asyncio.run(dispatch(ctx, "run_command", args))


def test_command_runs_in_workspace(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ctx = make_ctx(monkeypatch, tmp_path, "07-implementer", ws)
    result = run_cmd(ctx, command="pwd && echo hi")
    assert "exit_code: 0" in result.content
    assert "hi" in result.content
    assert str(ws) in result.content


def test_parent_traversal_blocked(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ctx = make_ctx(monkeypatch, tmp_path, "07-implementer", ws)
    result = run_cmd(ctx, command="echo escaped", cwd="..")
    assert result.content.startswith("ERROR")
    assert "workspace" in result.content


def test_sibling_prefix_directory_blocked(monkeypatch, tmp_path):
    # regression for the classic startswith() prefix bug: "ws-evil" shares the
    # string prefix of workspace "ws" but is OUTSIDE it
    ws = tmp_path / "ws"
    (tmp_path / "ws-evil").mkdir(parents=True)
    ctx = make_ctx(monkeypatch, tmp_path, "07-implementer", ws)
    result = run_cmd(ctx, command="echo escaped", cwd="../ws-evil")
    assert result.content.startswith("ERROR")


def test_shell_refused_for_non_shell_role(monkeypatch, tmp_path):
    ctx = make_ctx(monkeypatch, tmp_path, "03-business-analyst", tmp_path / "ws")
    result = run_cmd(ctx, command="echo hi")
    assert result.content.startswith("ERROR")
    assert "no shell access" in result.content


def test_nonzero_exit_and_stderr_reported(monkeypatch, tmp_path):
    ctx = make_ctx(monkeypatch, tmp_path, "07-implementer", tmp_path / "ws")
    result = run_cmd(ctx, command="echo oops >&2; exit 3")
    assert "exit_code: 3" in result.content
    assert "oops" in result.content
