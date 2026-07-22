"""Tests for the evidence bundle standard (sentinel/evidence.py) and the
`check_evidence` tool that validates bundles before handoff."""

import asyncio
import json

import pytest
import yaml

from sentinel.audit import AuditLog
from sentinel.config import load_settings
from sentinel.lease import LeaseManager
from sentinel.tools import ToolContext, dispatch, tools_for_role
from sentinel import evidence
from sentinel.evidence import (
    BUNDLES,
    DEPLOY_ENVIRONMENTS,
    bundle_template,
    bundles_for_role,
    catalog_text,
    expected_filename,
    resolve_kind,
    validate_bundle,
)

from fakes import FakeJira, FakeLLM

REQUIRED_ENV = {
    "JIRA_BASE_URL": "https://jira.example.com",
    "JIRA_PAT": "pat",
    "JIRA_PROJECT_KEY": "SENT",
    "LITELLM_BASE_URL": "https://llm.example.com",
    "LITELLM_API_KEY": "sk",
}


# --- registry integrity ---------------------------------------------------- #

def test_registry_specs_are_well_formed():
    for kind, spec in BUNDLES.items():
        assert spec.kind == kind
        assert spec.fmt in ("markdown", "text", "json", "yaml")
        assert spec.required, f"{kind} has no required sections"
        assert spec.produced_by, f"{kind} has no producing role"


def test_every_producing_role_is_a_real_pipeline_role(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    settings = load_settings("config/pipeline.yml")
    for spec in BUNDLES.values():
        for role_id in spec.produced_by:
            assert role_id in settings.roles, f"{spec.kind} produced by unknown role {role_id}"


# --- templates round-trip through validation ------------------------------- #

@pytest.mark.parametrize("kind", list(BUNDLES))
def test_bundle_template_passes_its_own_validation(kind):
    result = validate_bundle(kind, bundle_template(kind))
    assert result.ok, result.errors


def test_deploy_template_env_is_substituted():
    text = bundle_template("deploy-record", env="staging")
    assert "staging" in text
    assert validate_bundle("deploy-record", text).ok


# --- validation failures --------------------------------------------------- #

def test_markdown_missing_section_is_reported():
    # qa-report without a Failures section
    content = "# QA report\n## Environment\nx\n## Build\n1\n## Acceptance\nAC-1 pass\n## Screenshots\ns"
    result = validate_bundle("qa-report", content)
    assert not result.ok
    assert any("failures" in e for e in result.errors)


def test_structured_missing_key_is_reported():
    data = {"tool": "pip-audit", "command": "pip-audit", "timestamp": "t", "result": "pass",
            "findings": []}  # no "dismissed"
    result = validate_bundle("dependency-scan", json.dumps(data))
    assert not result.ok
    assert any("dismissed" in e for e in result.errors)


def test_structured_empty_required_value_is_reported():
    data = {"tool": "", "command": "c", "timestamp": "t", "result": "pass",
            "findings": [], "dismissed": []}
    result = validate_bundle("dependency-scan", json.dumps(data))
    assert any("tool" in e and "empty" in e for e in result.errors)


def test_empty_findings_list_is_acceptable():
    # A clean scan (empty findings/dismissed lists) is valid — absence of a key is not.
    data = {"tool": "bandit", "command": "bandit -r .", "timestamp": "t", "result": "pass",
            "findings": [], "dismissed": []}
    assert validate_bundle("dependency-scan", json.dumps(data)).ok


def test_structured_invalid_yaml_is_reported():
    result = validate_bundle("release-manifest", "version: [unclosed")
    assert not result.ok
    assert any("not valid yaml" in e for e in result.errors)


def test_unknown_kind_is_the_only_error():
    result = validate_bundle("nope", "whatever")
    assert result.errors == [
        "unknown evidence bundle kind 'nope' (known: " + ", ".join(sorted(BUNDLES)) + ")"]


# --- lookups --------------------------------------------------------------- #

def test_resolve_kind_handles_names_prefixes_and_deploy_env():
    assert resolve_kind("qa-report.md") == "qa-report"
    assert resolve_kind("evidence/qa-report.md") == "qa-report"
    assert resolve_kind("secrets-scan.txt") == "secrets-scan"
    for env in DEPLOY_ENVIRONMENTS:
        assert resolve_kind(f"deploy-{env}.md") == "deploy-record"
    assert resolve_kind("random.md") is None


def test_bundles_for_role():
    assert {s.kind for s in bundles_for_role("08-code-reviewer")} == {
        "sast", "dependency-scan", "secrets-scan"}
    assert {s.kind for s in bundles_for_role("10-qa")} == {"qa-report"}
    assert {s.kind for s in bundles_for_role("12-release")} == {
        "deploy-record", "release-manifest", "rollback-verification"}
    assert bundles_for_role("03-business-analyst") == []


def test_expected_filename_deploy_env():
    assert expected_filename("deploy-record", "production") == "deploy-production.md"
    assert expected_filename("qa-report") == "qa-report.md"


def test_catalog_text_scopes_to_role_and_lists_filenames():
    qa = catalog_text("10-qa")
    assert "evidence/qa-report.md" in qa
    assert "deploy-" not in qa                       # only this role's bundle
    assert catalog_text("03-business-analyst") == ""  # non-producing role
    assert "release-manifest.yaml" in catalog_text()  # full catalog


# --- the check_evidence tool ----------------------------------------------- #

def make_ctx(monkeypatch, tmp_path, role_id="10-qa"):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    settings = load_settings("config/pipeline.yml")
    jira = FakeJira()
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(
        jira=jira, llm=FakeLLM(),
        leases=LeaseManager(jira, "sentinel-bot", "agent-leased", 1800),
        settings=settings, audit=AuditLog(tmp_path / "audit.jsonl"),
        role=settings.roles[role_id], ticket="SENT-1", workspace=ws)
    return ctx, ws


def call(ctx, name, **args):
    return asyncio.run(dispatch(ctx, name, args))


def test_check_evidence_tool_only_for_shell_roles(monkeypatch, tmp_path):
    shell_ctx, _ = make_ctx(monkeypatch, tmp_path, role_id="10-qa")
    names = {t["function"]["name"] for t in tools_for_role(shell_ctx.role)}
    assert "check_evidence" in names
    nonshell_ctx, _ = make_ctx(monkeypatch, tmp_path, role_id="03-business-analyst")
    names = {t["function"]["name"] for t in tools_for_role(nonshell_ctx.role)}
    assert "check_evidence" not in names


def test_check_evidence_accepts_a_valid_bundle(monkeypatch, tmp_path):
    ctx, ws = make_ctx(monkeypatch, tmp_path, role_id="10-qa")
    (ws / "evidence").mkdir()
    (ws / "evidence" / "qa-report.md").write_text(bundle_template("qa-report"))
    out = call(ctx, "check_evidence", path="evidence/qa-report.md")
    assert out.content.startswith("OK:")
    assert not out.terminal


def test_check_evidence_reports_missing_sections(monkeypatch, tmp_path):
    ctx, ws = make_ctx(monkeypatch, tmp_path, role_id="10-qa")
    (ws / "qa-report.md").write_text("# QA report\n## Environment\nx\n")
    out = call(ctx, "check_evidence", path="qa-report.md")
    assert "does not yet meet" in out.content
    assert "failures" in out.content


def test_check_evidence_infers_kind_from_deploy_filename(monkeypatch, tmp_path):
    ctx, ws = make_ctx(monkeypatch, tmp_path, role_id="09-deployment")
    (ws / "deploy-staging.md").write_text(bundle_template("deploy-record", env="staging"))
    out = call(ctx, "check_evidence", path="deploy-staging.md")
    assert out.content.startswith("OK:")


def test_check_evidence_missing_file_errors(monkeypatch, tmp_path):
    ctx, _ = make_ctx(monkeypatch, tmp_path, role_id="10-qa")
    out = call(ctx, "check_evidence", path="evidence/qa-report.md")
    assert "no such file" in out.content


def test_check_evidence_rejects_escape_from_workspace(monkeypatch, tmp_path):
    ctx, _ = make_ctx(monkeypatch, tmp_path, role_id="10-qa")
    out = call(ctx, "check_evidence", path="../secret.md")
    assert "must stay inside the workspace" in out.content


def test_check_evidence_unrecognized_filename_needs_kind(monkeypatch, tmp_path):
    ctx, ws = make_ctx(monkeypatch, tmp_path, role_id="10-qa")
    (ws / "notes.md").write_text("# whatever")
    out = call(ctx, "check_evidence", path="notes.md")
    assert "not a recognized evidence bundle filename" in out.content
