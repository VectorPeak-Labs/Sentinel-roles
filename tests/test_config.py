import dataclasses

import pytest

from sentinel.config import ConfigError, load_settings, validate_config

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


# --- dispatch-table validation (validate_config / ConfigError) --------------

def _replace_role(settings, role_id, **changes):
    """Return a copy of settings.roles with one role's fields overridden."""
    roles = dict(settings.roles)
    roles[role_id] = dataclasses.replace(roles[role_id], **changes)
    return roles


def test_shipped_config_passes_validation(settings):
    assert validate_config(settings) == []


def test_unknown_trigger_type_flagged(settings):
    settings.roles = _replace_role(settings, "03-business-analyst", trigger_type="tickets")
    problems = validate_config(settings)
    assert any("trigger.type 'tickets'" in p for p in problems)


def test_empty_statuses_flagged(settings):
    settings.roles = _replace_role(settings, "03-business-analyst", statuses=())
    assert any("watches no status" in p for p in validate_config(settings))


def test_undefined_require_label_flagged(settings):
    # A typo'd require_label resolves to a literal label no human sets -> the role
    # silently never dispatches. This is the highest-value check.
    settings.roles = _replace_role(settings, "02-intake-triage", require_label="activ")
    problems = validate_config(settings)
    assert any("require_label 'activ'" in p for p in problems)


def test_unknown_condition_flagged(settings):
    settings.roles = _replace_role(settings, "06-sprint-planner", condition="capacity")
    assert any("condition 'capacity'" in p for p in validate_config(settings))


def test_condition_on_ticket_role_flagged(settings):
    # A condition on a ticket role is dead config — conditions are only evaluated
    # for queue roles, so the intended gate silently never applies.
    settings.roles = _replace_role(settings, "03-business-analyst",
                                   condition="capacity_in_progress")
    assert any("only evaluated for queue roles" in p for p in validate_config(settings))


def test_duplicate_ticket_role_on_status_flagged(settings):
    settings.roles = _replace_role(settings, "04-tech-lead-debrief",
                                   statuses=("Business Requirements",))
    assert any("multiple ticket roles" in p for p in validate_config(settings))


def test_wip_limit_on_unwatched_status_flagged(settings):
    settings.wip_limits = dict(settings.wip_limits)
    settings.wip_limits["Nonexistent Status"] = 3
    assert any("Nonexistent Status" in p for p in validate_config(settings))


def test_validation_aggregates_all_problems(settings):
    # One pass reports every problem, so an operator fixes them together.
    settings.roles = _replace_role(settings, "03-business-analyst",
                                   trigger_type="tickets", statuses=())
    assert len(validate_config(settings)) >= 2


def test_load_settings_raises_configerror_on_invalid_table(tmp_path, monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    bad = tmp_path / "pipeline.yml"
    bad.write_text(
        "labels: {activate: activate}\n"
        "roles:\n"
        "  02-intake:\n"
        "    doc: docs/02-intake-triage.md\n"
        "    trigger: { type: ticket, statuses: [\"New\"], require_label: activ }\n")
    with pytest.raises(ConfigError) as exc:
        load_settings(str(bad))
    assert "require_label 'activ'" in str(exc.value)
