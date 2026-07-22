"""Tests for the project policy pack (config/policy.yml → sentinel.config.Policy)
and its two tested effects: policy-driven prompt injection and fail-fast policy
validation at load time."""

import pytest

from sentinel.agent import build_system_prompt
from sentinel.config import (
    ConfigError,
    Policy,
    build_policy,
    load_policy,
    load_settings,
)

REQUIRED_ENV = {
    "JIRA_BASE_URL": "https://jira.example.com",
    "JIRA_PAT": "pat",
    "JIRA_PROJECT_KEY": "SENT",
    "LITELLM_BASE_URL": "https://llm.example.com",
    "LITELLM_API_KEY": "sk",
}


@pytest.fixture
def env(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


# --- defaults == current behavior ------------------------------------------ #

def test_defaults_match_shipped_behavior():
    p = Policy()
    assert p.security.baseline == "owasp-asvs-l2"
    assert p.security.require_dependency_scan and p.security.require_secrets_scan
    assert p.review.require_ci_green and p.review.allow_minor_followups
    assert p.qa.require_visual_evidence and p.qa.require_screenshots
    assert p.release.require_human_notes_approval
    assert p.release.soak_minutes == 30
    assert p.release.require_reversible_migrations


def test_shipped_policy_file_loads_to_defaults():
    policy, problems = load_policy("config/policy.yml")
    assert problems == []
    assert policy == Policy()   # the shipped file is exactly the defaults


def test_missing_policy_file_yields_defaults(tmp_path):
    policy, problems = load_policy(tmp_path / "nope.yml")
    assert policy == Policy() and problems == []


def test_settings_carry_policy(env):
    settings = load_settings("config/pipeline.yml")
    assert settings.policy == Policy()


# --- overlay + coercion ---------------------------------------------------- #

def test_partial_policy_overlays_onto_defaults():
    policy, problems = build_policy({"release": {"soak_minutes": 60},
                                     "security": {"baseline": "owasp-asvs-l3"}})
    assert problems == []
    assert policy.release.soak_minutes == 60
    assert policy.security.baseline == "owasp-asvs-l3"
    # untouched keys keep defaults
    assert policy.release.require_reversible_migrations is True
    assert policy.qa.require_screenshots is True


def test_none_baseline_is_allowed():
    policy, problems = build_policy({"security": {"baseline": "none"}})
    assert problems == [] and policy.security.baseline == "none"


# --- validation (fail-fast) ------------------------------------------------ #

def test_unknown_baseline_is_a_problem():
    _policy, problems = build_policy({"security": {"baseline": "pci-dss"}})
    assert any("baseline 'pci-dss'" in p for p in problems)


def test_negative_soak_is_a_problem():
    _policy, problems = build_policy({"release": {"soak_minutes": -5}})
    assert any("soak_minutes" in p and ">= 0" in p for p in problems)


def test_non_bool_flag_is_a_problem():
    _policy, problems = build_policy({"security": {"require_secrets_scan": "yes"}})
    assert any("require_secrets_scan" in p and "true or false" in p for p in problems)


def test_non_int_soak_is_a_problem():
    _policy, problems = build_policy({"release": {"soak_minutes": True}})
    assert any("soak_minutes" in p and "integer" in p for p in problems)


def test_unknown_key_and_section_are_problems():
    _policy, problems = build_policy({"security": {"bogus": 1}, "sekurity": {}})
    assert any("security.bogus" in p for p in problems)
    assert any("sekurity" in p and "section" in p for p in problems)


def test_load_settings_raises_on_invalid_policy(env, monkeypatch, tmp_path):
    bad = tmp_path / "policy.yml"
    bad.write_text("security:\n  baseline: not-a-standard\n")
    monkeypatch.setenv("SENTINEL_POLICY", str(bad))
    with pytest.raises(ConfigError) as exc:
        load_settings("config/pipeline.yml")
    assert "policy" in str(exc.value).lower()


def test_env_expansion_in_policy(env, monkeypatch, tmp_path):
    monkeypatch.setenv("SEC_BASELINE", "owasp-asvs-l1")
    pol = tmp_path / "policy.yml"
    pol.write_text("security:\n  baseline: ${SEC_BASELINE}\n")
    monkeypatch.setenv("SENTINEL_POLICY", str(pol))
    settings = load_settings("config/pipeline.yml")
    assert settings.policy.security.baseline == "owasp-asvs-l1"


# --- prompt injection (agent instructions change with policy) -------------- #

def test_prompt_includes_active_policy(env):
    settings = load_settings("config/pipeline.yml")
    role = settings.roles["08-code-reviewer"]
    prompt = build_system_prompt(settings, role, "sentinel-bot")
    assert "Project policy" in prompt
    assert "owasp-asvs-l2" in prompt


def test_changing_a_policy_value_changes_the_prompt(env):
    settings = load_settings("config/pipeline.yml")
    role = settings.roles["12-release"]
    before = build_system_prompt(settings, role, "sentinel-bot")
    assert "soak 30 min" in before

    settings.policy = build_policy({"release": {"soak_minutes": 90},
                                    "security": {"baseline": "owasp-asvs-l3"}})[0]
    after = build_system_prompt(settings, role, "sentinel-bot")
    assert "soak 90 min" in after
    assert "owasp-asvs-l3" in after
    assert before != after
