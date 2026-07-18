"""Tests for the doctor readiness gate (sentinel/doctor.py).

The classification core (readiness_findings) is pure — no Jira/LiteLLM — so these
run without network using a real Settings built from the shipped pipeline.
"""

import json

import pytest

from sentinel.config import load_settings
from sentinel.doctor import (
    COMMAND_ORDER,
    Report,
    _classify_llm_error,
    readiness_findings,
    render,
)

REQUIRED_ENV = {
    "JIRA_BASE_URL": "https://jira.example.com/",
    "JIRA_PAT": "pat-token",
    "JIRA_PROJECT_KEY": "SENT",
    "LITELLM_BASE_URL": "https://litellm.example.com",
    "LITELLM_API_KEY": "sk-key",
}


@pytest.fixture
def settings(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SENTINEL_REVIEWER_MODEL", raising=False)
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    return load_settings("config/pipeline.yml")


# --- command readiness ----------------------------------------------------- #

def test_blank_commands_are_blockers(settings):
    # Shipped pipeline has all commands blank.
    report = readiness_findings(settings)
    blockers = "\n".join(report.blockers)
    assert "commands.deploy_production is empty" in blockers
    assert "Release (12) will escalate" in blockers
    assert "commands.test is empty" in blockers
    assert "commands.clone is empty" in blockers
    assert report.ready is False


def test_filled_commands_clear_command_blockers(settings):
    settings.commands = {k: f"do-{k}" for k in COMMAND_ORDER}
    settings.webhook_secret = "a-secret"
    report = readiness_findings(settings)
    assert not any("commands." in b for b in report.blockers)
    # Role docs exist and webhook secret is set -> nothing left to block.
    assert report.ready is True


def test_only_needed_commands_block(settings):
    # Fill everything except deploy_production -> exactly one command blocker.
    settings.commands = {k: "x" for k in COMMAND_ORDER}
    settings.commands["deploy_production"] = ""
    command_blockers = [b for b in readiness_findings(settings).blockers if "commands." in b]
    assert command_blockers == [
        "commands.deploy_production is empty — Release (12) will escalate for every "
        "production release"
    ]


# --- security warnings ----------------------------------------------------- #

def test_missing_webhook_secret_warns(settings):
    report = readiness_findings(settings)
    assert any("WEBHOOK_SECRET is empty" in w for w in report.warnings)
    assert any("/health is unauthenticated" in w for w in report.warnings)


def test_webhook_secret_set_no_auth_warning(settings):
    settings.webhook_secret = "s3cret"
    report = readiness_findings(settings)
    assert not any("WEBHOOK_SECRET is empty" in w for w in report.warnings)


# --- reviewer model -------------------------------------------------------- #

def test_reviewer_model_unset_warns(settings):
    assert any("SENTINEL_REVIEWER_MODEL is not set" in w
               for w in readiness_findings(settings).warnings)


def test_reviewer_model_set_no_warning(settings, monkeypatch):
    monkeypatch.setenv("SENTINEL_REVIEWER_MODEL", "claude-sonnet")
    reloaded = load_settings("config/pipeline.yml")
    assert not any("SENTINEL_REVIEWER_MODEL is not set" in w
                   for w in readiness_findings(reloaded).warnings)


# --- Report / render ------------------------------------------------------- #

def test_ready_true_when_no_blockers():
    r = Report()
    assert r.ready is True
    r.warn("just a warning")
    assert r.ready is True
    r.blocker("a blocker")
    assert r.ready is False


def test_render_text_has_sections(settings):
    text = render(readiness_findings(settings), "text")
    assert text.startswith("READY: no")
    assert "BLOCKERS:" in text
    assert "WARNINGS:" in text
    assert "INFO:" in text


def test_render_json_shape(settings):
    payload = json.loads(render(readiness_findings(settings), "json"))
    assert payload["ready"] is False
    assert isinstance(payload["blockers"], list) and payload["blockers"]
    assert set(payload) == {"ready", "blockers", "warnings", "info"}


# --- LLM error classification ---------------------------------------------- #

@pytest.mark.parametrize("status,expected", [
    (401, "authentication failed"),
    (403, "authentication failed"),
    (404, "model not found"),
    (500, "HTTP 500"),
])
def test_classify_llm_error_by_status(status, expected):
    err = RuntimeError("boom")
    err.status_code = status
    assert expected in _classify_llm_error(err)


def test_classify_llm_error_network():
    assert "network/connection error" in _classify_llm_error(RuntimeError("no route"))
