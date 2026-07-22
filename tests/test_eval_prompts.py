"""Tests for the role prompt regression suite (sentinel/eval_prompts.py).

The headline test — test_shipped_rubrics_all_pass — IS the regression guard: it
asserts every scenario in evals/role_behaviors.yml still matches the current role
prompts. If a doc edit deletes a load-bearing instruction, this test goes red and
names the role/concept.
"""

import pytest

from sentinel.config import load_settings
from sentinel.eval_prompts import (
    DEFAULT_RUBRIC_PATH,
    Rubric,
    evaluate_prompt,
    load_rubrics,
    main,
    render,
    run_deterministic,
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


@pytest.fixture
def settings(env):
    return load_settings("config/pipeline.yml")


@pytest.fixture
def rubrics():
    return load_rubrics(DEFAULT_RUBRIC_PATH)


# --- rubric fixtures ------------------------------------------------------- #

def test_rubrics_load_and_are_well_formed(rubrics, settings):
    assert len(rubrics) >= 5, "issue #35 requires at least 5 scenarios"
    ids = [r.id for r in rubrics]
    assert len(ids) == len(set(ids)), "scenario ids must be unique"
    for r in rubrics:
        assert r.role in settings.roles, f"{r.id} targets unknown role {r.role}"
        assert r.must_include, f"{r.id} has no must_include groups"
        assert all(group for group in r.must_include), f"{r.id} has an empty group"
        assert r.invariant, f"{r.id} has no invariant description"


# --- the regression guard -------------------------------------------------- #

def test_shipped_rubrics_all_pass_against_current_docs(rubrics, settings):
    results = run_deterministic(rubrics, settings)
    failures = [r for r in results if not r.passed]
    assert not failures, "prompt regression:\n" + "\n".join(
        f"  {f.id} ({f.role}) missing {f.missing}" for f in failures)


def test_every_listed_role_scenario_is_present(rubrics):
    roles = {r.role for r in rubrics}
    # the behaviors the issue enumerates, by role
    for role in ("02-intake-triage", "03-business-analyst", "04-tech-lead-debrief",
                 "07-implementer", "08-code-reviewer", "13-rework-router", "12-release"):
        assert role in roles, f"no scenario covers {role}"


# --- evaluate_prompt unit -------------------------------------------------- #

def _rubric(**kw):
    kw.setdefault("id", "x")
    kw.setdefault("role", "02-intake-triage")
    kw.setdefault("scenario", "s")
    kw.setdefault("invariant", "inv")
    kw.setdefault("must_include", (("alpha",), ("beta", "gamma")))
    kw.setdefault("probe", None)
    return Rubric(**kw)


def test_evaluate_prompt_passes_when_all_groups_present():
    r = evaluate_prompt(_rubric(), "we have ALPHA and gamma here")
    assert r.passed and r.missing == []


def test_evaluate_prompt_reports_missing_group():
    r = evaluate_prompt(_rubric(), "only alpha present")
    assert not r.passed
    assert r.missing == ["beta | gamma"]


def test_evaluate_prompt_is_case_insensitive():
    assert evaluate_prompt(_rubric(must_include=(("TODO(PO)",),)),
                           "leave it as todo(po) please").passed


def test_run_deterministic_flags_unknown_role(settings):
    results = run_deterministic([_rubric(role="99-nope")], settings)
    assert not results[0].passed
    assert "not in config/pipeline.yml" in results[0].missing[0]


# --- a genuine regression is caught ---------------------------------------- #

def test_deleting_an_instruction_would_fail(settings):
    """Sanity: if a role prompt lost 'TODO(PO)', the intake scenario fails —
    demonstrating the guard actually bites."""
    intake = next(r for r in load_rubrics(DEFAULT_RUBRIC_PATH) if r.id == "intake-todo-po")
    good = evaluate_prompt(intake, "keep unknowns as TODO(PO) and post open questions")
    bad = evaluate_prompt(intake, "keep unknowns and post open questions")  # no TODO(PO)
    assert good.passed and not bad.passed


# --- rendering & CLI ------------------------------------------------------- #

def test_render_reports_counts_and_failures():
    from sentinel.eval_prompts import EvalResult
    text = render([EvalResult("a", "02", True),
                   EvalResult("b", "08", False, ["ci green"], invariant="reject red CI")])
    assert "1/2 passed" in text
    assert "[FAIL] b" in text
    assert "ci green" in text


def test_main_exit_zero_on_shipped_docs(env, capsys):
    assert main(["--format", "json"]) == 0
    out = capsys.readouterr().out
    assert '"passed": true' in out


def test_main_exit_nonzero_when_a_rubric_regresses(env, tmp_path, capsys):
    bad = tmp_path / "bad.yml"
    bad.write_text(
        "scenarios:\n"
        "  - id: impossible\n"
        "    role: 02-intake-triage\n"
        "    scenario: s\n"
        "    invariant: never present\n"
        "    must_include:\n"
        "      - ['zzz-not-in-any-prompt-zzz']\n")
    assert main(["--rubrics", str(bad)]) == 1
