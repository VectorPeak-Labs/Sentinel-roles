"""Tests for the guided onboarding flow (sentinel/onboard.py).

No terminal, no network: the pure builders are exercised directly, and the
interactive collector is driven with injected prompt/getpass callables.
"""

from pathlib import Path

import pytest

import sentinel.onboard as onboard
from sentinel.config import load_settings
from sentinel.onboard import (
    COMMAND_KEYS,
    build_config_text,
    build_env_text,
    collect_interactive,
    command_warnings,
    env_summary_lines,
    main,
    run_onboarding,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- build_env_text -------------------------------------------------------- #

def test_build_env_fills_values_and_preserves_comments():
    example = REPO_ROOT.joinpath(".env.example").read_text()
    out = build_env_text(example, {
        "JIRA_BASE_URL": "https://jira.example.com",
        "JIRA_PROJECT_KEY": "SENT",
        "JIRA_PAT": "pat-secret",
    })
    assert "JIRA_BASE_URL=https://jira.example.com" in out
    assert "JIRA_PROJECT_KEY=SENT" in out
    assert "JIRA_PAT=pat-secret" in out
    # Comments and untouched keys survive.
    assert "# --- Jira (self-hosted Server / Data Center) ---" in out
    assert "SENTINEL_DEFAULT_MODEL=gpt-4o" in out


def test_build_env_leaves_unknown_keys_untouched():
    out = build_env_text("JIRA_BASE_URL=\nUNMANAGED=keep-me\n", {"JIRA_BASE_URL": "x"})
    assert "JIRA_BASE_URL=x" in out
    assert "UNMANAGED=keep-me" in out


# --- secrets never surfaced ------------------------------------------------ #

def test_secret_values_never_in_summary():
    values = {
        "JIRA_BASE_URL": "https://jira.example.com",
        "JIRA_PAT": "super-secret-pat",
        "LITELLM_API_KEY": "sk-super-secret",
        "WEBHOOK_SECRET": "hook-secret",
    }
    summary = "\n".join(env_summary_lines(values))
    assert "super-secret-pat" not in summary
    assert "sk-super-secret" not in summary
    assert "hook-secret" not in summary
    # Non-secret values are shown; secret keys are marked as set.
    assert "https://jira.example.com" in summary
    assert "JIRA_PAT = •••••• (set)" in summary


def test_run_onboarding_output_hides_secrets(tmp_path, capsys):
    _seed_repo(tmp_path)
    run_onboarding(
        env_values={"JIRA_PAT": "top-secret-token", "JIRA_BASE_URL": "https://j"},
        commands={}, root=tmp_path, dry_run=True,
    )
    printed = capsys.readouterr().out
    assert "top-secret-token" not in printed
    # ...but it does land in the generated .env text (that is the file's job).
    result = run_onboarding(
        env_values={"JIRA_PAT": "top-secret-token"}, commands={}, root=tmp_path,
        dry_run=True,
    )
    assert "JIRA_PAT=top-secret-token" in result.env_text


# --- build_config_text ----------------------------------------------------- #

def test_build_config_fills_commands_and_preserves_comments():
    src = REPO_ROOT.joinpath("config", "pipeline.yml").read_text()
    out = build_config_text(src, {"clone": "git clone https://x/repo.git .",
                                  "test": "npm ci && npm test"})
    assert 'clone: "git clone https://x/repo.git ."' in out
    assert 'test: "npm ci && npm test"' in out
    # The explanatory comments on those lines are preserved.
    assert "# e.g. git clone" in out
    # Untouched command keys keep their empty value.
    assert 'deploy_production: ""' in out
    # Nothing outside the commands block was rewritten (a real role still there).
    assert "07-implementer:" in out


def test_build_config_quotes_special_characters():
    src = 'commands:\n  clone: ""   # c\n'
    out = build_config_text(src, {"clone": 'a "b" \\c'})
    assert r'clone: "a \"b\" \\c"' in out


def test_build_config_ignores_test_key_outside_commands_block():
    # A `test:`-like key above the commands block must not be rewritten.
    src = "roles:\n  x:\n    test: keepme\ncommands:\n  test: \"\"\n"
    out = build_config_text(src, {"test": "pytest"})
    assert "test: keepme" in out
    assert 'test: "pytest"' in out


# --- command warnings ------------------------------------------------------ #

def test_missing_ship_commands_warn():
    warnings = command_warnings({"clone": "git clone", "test": "pytest"})
    joined = "\n".join(warnings)
    assert "commands.deploy_production is empty" in joined
    assert "Release (12)" in joined
    # Provided commands do not warn.
    assert "commands.clone" not in joined
    assert "commands.test" not in joined


def test_no_warnings_when_all_commands_present():
    assert command_warnings({k: f"cmd-{k}" for k in COMMAND_KEYS}) == []


# --- run_onboarding writing ------------------------------------------------ #

def _seed_repo(root: Path):
    """Copy the real .env.example and pipeline.yml into a temp repo root."""
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / ".env.example").write_text(REPO_ROOT.joinpath(".env.example").read_text())
    (root / "config" / "pipeline.yml").write_text(
        REPO_ROOT.joinpath("config", "pipeline.yml").read_text())


def test_dry_run_writes_nothing(tmp_path):
    _seed_repo(tmp_path)
    result = run_onboarding(
        env_values={"JIRA_BASE_URL": "https://j"}, commands={"clone": "git clone"},
        root=tmp_path, dry_run=True,
    )
    assert result.wrote_env is False and result.wrote_config is False
    assert not (tmp_path / ".env").exists()


def test_run_onboarding_writes_files(tmp_path):
    _seed_repo(tmp_path)
    result = run_onboarding(
        env_values={"JIRA_BASE_URL": "https://jira", "JIRA_PROJECT_KEY": "SENT"},
        commands={"clone": "git clone https://x .", "test": "pytest"},
        root=tmp_path,
    )
    assert result.wrote_env and result.wrote_config
    env_written = (tmp_path / ".env").read_text()
    assert "JIRA_BASE_URL=https://jira" in env_written
    assert 'clone: "git clone https://x ."' in (tmp_path / "config" / "pipeline.yml").read_text()


def test_existing_env_not_overwritten_without_force(tmp_path):
    _seed_repo(tmp_path)
    (tmp_path / ".env").write_text("PRE-EXISTING")
    result = run_onboarding(
        env_values={"JIRA_BASE_URL": "https://jira"}, commands={}, root=tmp_path,
    )
    assert result.wrote_env is False
    assert (tmp_path / ".env").read_text() == "PRE-EXISTING"
    # With --force it is replaced.
    forced = run_onboarding(
        env_values={"JIRA_BASE_URL": "https://jira"}, commands={}, root=tmp_path, force=True,
    )
    assert forced.wrote_env is True
    assert "JIRA_BASE_URL=https://jira" in (tmp_path / ".env").read_text()


# --- generated config is loadable ------------------------------------------ #

def test_generated_config_loads(tmp_path, monkeypatch):
    _seed_repo(tmp_path)
    env_values = {
        "JIRA_BASE_URL": "https://jira.example.com",
        "JIRA_PROJECT_KEY": "SENT",
        "JIRA_PAT": "pat",
        "LITELLM_BASE_URL": "https://litellm.example.com",
        "LITELLM_API_KEY": "sk-key",
    }
    run_onboarding(env_values=env_values,
                   commands={"clone": "git clone https://x .", "test": "pytest"},
                   root=tmp_path)
    for key, value in env_values.items():
        monkeypatch.setenv(key, value)
    settings = load_settings(tmp_path / "config" / "pipeline.yml")
    assert settings.commands["clone"] == "git clone https://x ."
    assert settings.commands["test"] == "pytest"
    assert settings.commands["deploy_production"] == ""  # left blank, still valid


# --- interactive collection ------------------------------------------------ #

def test_collect_interactive_drives_prompts_and_secrets():
    plain_answers = iter([
        "https://jira.example.com",  # JIRA_BASE_URL
        "SENT",                       # JIRA_PROJECT_KEY
        "https://litellm.example.com",  # LITELLM_BASE_URL
        "",                           # SENTINEL_DEFAULT_MODEL -> default gpt-4o
        "",                           # SENTINEL_REVIEWER_MODEL
        "",                           # SENTINEL_ALERT_WEBHOOK_URL
        "git clone https://x .",      # commands.clone
        "pytest",                     # commands.test
        "", "", "", "", "",           # remaining 5 commands blank
        "y",                          # run doctor?
    ])
    secret_answers = iter(["pat-secret", "", "sk-secret"])  # PAT, webhook(blank), LiteLLM key

    env, commands, run_doctor = collect_interactive(
        ask=lambda _prompt: next(plain_answers),
        getpass_fn=lambda _prompt: next(secret_answers),
        out=lambda _msg: None,
    )

    assert env["JIRA_BASE_URL"] == "https://jira.example.com"
    assert env["JIRA_PAT"] == "pat-secret"
    assert env["WEBHOOK_SECRET"] == ""
    assert env["SENTINEL_DEFAULT_MODEL"] == "gpt-4o"  # default applied
    assert commands["clone"] == "git clone https://x ."
    assert commands["deploy_production"] == ""
    assert run_doctor is True


# --- non-interactive main -------------------------------------------------- #

def test_non_interactive_main_dry_run(tmp_path, monkeypatch, capsys):
    _seed_repo(tmp_path)
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.example.com")
    monkeypatch.setenv("SENTINEL_CMD_CLONE", "git clone https://x .")
    monkeypatch.delenv("JIRA_PAT", raising=False)

    rc = main(["--non-interactive", "--dry-run", "--root", str(tmp_path)])
    assert rc == 0
    assert not (tmp_path / ".env").exists()
    out = capsys.readouterr().out
    assert "[dry-run] no files written." in out
    # deploy_production was not provided -> its escalation warning shows.
    assert "commands.deploy_production is empty" in out


# --- interactive main does not clobber an existing .env -------------------- #

def test_interactive_main_keeps_existing_env_without_force(tmp_path, monkeypatch):
    # Regression: the generic "Write these files now?" confirm must NOT overwrite
    # an existing .env — that is what --force / a dedicated confirm is for.
    _seed_repo(tmp_path)
    (tmp_path / ".env").write_text("EXISTING=secret\n")
    monkeypatch.setattr(onboard, "collect_interactive",
                        lambda **kw: ({"JIRA_BASE_URL": "https://j"},
                                      {"clone": "git clone https://x ."}, False))
    # main() prompts: "Write these files now?" -> y ; "...Overwrite it?" -> n
    replies = iter(["y", "n"])
    monkeypatch.setattr(onboard, "_prompt", lambda *a, **k: next(replies))

    rc = onboard.main(["--root", str(tmp_path)])
    assert rc == 0
    # .env preserved; config still updated in place.
    assert (tmp_path / ".env").read_text() == "EXISTING=secret\n"
    assert 'clone: "git clone https://x ."' in (tmp_path / "config" / "pipeline.yml").read_text()


def test_interactive_main_overwrites_env_when_confirmed(tmp_path, monkeypatch):
    _seed_repo(tmp_path)
    (tmp_path / ".env").write_text("EXISTING=secret\n")
    monkeypatch.setattr(onboard, "collect_interactive",
                        lambda **kw: ({"JIRA_BASE_URL": "https://jira"}, {}, False))
    replies = iter(["y", "y"])  # write? yes ; overwrite existing .env? yes
    monkeypatch.setattr(onboard, "_prompt", lambda *a, **k: next(replies))

    rc = onboard.main(["--root", str(tmp_path)])
    assert rc == 0
    assert "JIRA_BASE_URL=https://jira" in (tmp_path / ".env").read_text()


def test_interactive_main_new_env_written_without_prompt(tmp_path, monkeypatch):
    # No pre-existing .env -> no overwrite question, .env is written on "yes".
    _seed_repo(tmp_path)
    monkeypatch.setattr(onboard, "collect_interactive",
                        lambda **kw: ({"JIRA_BASE_URL": "https://jira"}, {}, False))
    replies = iter(["y"])  # only "Write these files now?" is asked
    monkeypatch.setattr(onboard, "_prompt", lambda *a, **k: next(replies))

    rc = onboard.main(["--root", str(tmp_path)])
    assert rc == 0
    assert "JIRA_BASE_URL=https://jira" in (tmp_path / ".env").read_text()
