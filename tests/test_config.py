import pytest

from sentinel.config import load_settings

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
    monkeypatch.setenv("SENTINEL_REVIEWER_MODEL", "claude-sonnet")
    return load_settings("config/pipeline.yml")


def test_all_thirteen_roles_minus_orchestrator_loaded(settings):
    # Roles 02-13 are dispatchable agents; 01 (orchestrator) is the loop itself.
    assert len(settings.roles) == 12
    assert "01-orchestrator" not in settings.roles


def test_urls_normalized(settings):
    assert settings.jira_base_url == "https://jira.example.com"
    assert settings.litellm_base_url == "https://litellm.example.com/v1"


def test_env_expansion_in_role_model(settings):
    assert settings.roles["08-code-reviewer"].model == "claude-sonnet"


def test_dispatch_table_matches_pipeline(settings):
    assert settings.roles_for_status("Business Requirements")[0].role_id == "03-business-analyst"
    assert settings.roles_for_status("Rework")[0].role_id == "13-rework-router"
    deploy = settings.roles["09-deployment"]
    assert deploy.trigger_type == "queue"
    assert deploy.watches_status("Tech Review Accepted")
    assert deploy.watches_status("Internal Review Accepted")


def test_intake_requires_activate_label(settings):
    intake = settings.roles["02-intake-triage"]
    assert intake.require_label == "activate"
    assert intake.watches_status("New") and intake.watches_status("On Hold")


def test_release_gated_on_window(settings):
    release = settings.roles["12-release"]
    assert release.condition == "release_window"


def test_every_role_doc_exists(settings):
    from pathlib import Path
    for role in settings.roles.values():
        assert Path(role.doc).exists(), role.doc


def test_missing_env_raises(monkeypatch):
    for key in REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError):
        load_settings("config/pipeline.yml")
