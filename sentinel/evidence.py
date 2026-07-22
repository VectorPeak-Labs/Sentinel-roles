"""Evidence bundle standard (docs/00-overview §Evidence bundle standard).

Universal rule 5 is *evidence over assertion*: every `pass` on a checklist item
carries evidence, exchanged as Jira attachments. This module standardizes the
**names and minimal schemas** of those attachments so a downstream role — or an
operator reading a ticket — can reliably find and interpret them, instead of
every agent inventing its own filenames.

It is deliberately pure (no Jira, no network, no I/O): a registry of
:class:`BundleSpec` plus helpers to validate a bundle's content, render a
skeleton template, and describe what a role must produce. The `check_evidence`
tool (sentinel/tools.py) and the role prompts (sentinel/agent.py) are the only
callers that turn this into runtime behavior — validation stays *advisory* (it
reports what's missing, it never rejects an attachment), matching the issue's
"add enforcement only where straightforward" guidance and keeping Jira
attachments the transport, not replacing them.

Canonical layout (workspace-relative, mirrored onto the ticket as attachments)::

    evidence/
      sast-summary.md            # role 08 — static analysis (SAST) summary
      dependency-scan.json       # role 08 — dependency/CVE scan
      secrets-scan.txt           # role 08 — secrets scan
      qa-report.md               # role 10 — functional + visual + a11y QA
      deploy-<env>.md            # roles 09/12 — one per environment deployed
      release-manifest.yaml      # role 12 — production release manifest
      rollback-verification.md   # roles 09/12 — rollback trigger + verification
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

EVIDENCE_DIR = "evidence"

# Environments a deploy record may target (deploy-<env>.md).
DEPLOY_ENVIRONMENTS = ("test", "staging", "production")


@dataclass(frozen=True)
class BundleSpec:
    """One evidence artifact: its canonical filename, format, required
    sections/keys, and which role(s) produce it."""

    kind: str
    filename: str            # canonical name; the deploy record uses "deploy-<env>.md"
    fmt: str                 # "markdown" | "text" | "json" | "yaml"
    produced_by: tuple[str, ...]
    required: tuple[str, ...]   # section names (markdown/text) or top-level keys (json/yaml)
    description: str

    @property
    def structured(self) -> bool:
        return self.fmt in ("json", "yaml")


# The scan bundles (SAST / dependency / secrets) share one minimal schema.
_SCAN_FIELDS = ("tool", "command", "timestamp", "result", "findings", "dismissed")

BUNDLES: dict[str, BundleSpec] = {
    "sast": BundleSpec(
        "sast", "sast-summary.md", "markdown", ("08-code-reviewer",), _SCAN_FIELDS,
        "Static analysis (SAST) scan summary"),
    "dependency-scan": BundleSpec(
        "dependency-scan", "dependency-scan.json", "json", ("08-code-reviewer",),
        _SCAN_FIELDS, "Dependency / known-vulnerability (CVE) scan"),
    "secrets-scan": BundleSpec(
        "secrets-scan", "secrets-scan.txt", "text", ("08-code-reviewer",), _SCAN_FIELDS,
        "Secrets scan (credentials/keys leaked into the tree)"),
    "qa-report": BundleSpec(
        "qa-report", "qa-report.md", "markdown", ("10-qa",),
        ("environment", "build", "acceptance", "screenshots", "failures"),
        "Functional + visual + accessibility QA report"),
    "deploy-record": BundleSpec(
        "deploy-record", "deploy-<env>.md", "markdown",
        ("09-deployment", "12-release"),
        ("environment", "build", "command", "output", "smoke"),
        "Deployment record for one environment"),
    "release-manifest": BundleSpec(
        "release-manifest", "release-manifest.yaml", "yaml", ("12-release",),
        ("version", "tickets", "build_ids", "migration_order", "rollback_path"),
        "Production release manifest"),
    "rollback-verification": BundleSpec(
        "rollback-verification", "rollback-verification.md", "markdown",
        ("09-deployment", "12-release"),
        ("trigger", "rollback", "verification"),
        "Rollback trigger, command, and verification result"),
}


@dataclass
class BundleValidation:
    kind: str
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# --------------------------------------------------------------------------- #
# Lookup helpers
# --------------------------------------------------------------------------- #

def _deploy_filename(env: str) -> str:
    return f"deploy-{env}.md"


def resolve_kind(filename: str) -> str | None:
    """Map an evidence filename to its bundle kind, or None if unrecognized.

    Handles the environment-parameterized deploy record (``deploy-test.md`` …
    ``deploy-production.md`` → ``deploy-record``) and ignores any directory
    prefix (``evidence/qa-report.md`` resolves the same as ``qa-report.md``)."""
    name = Path(filename).name.lower()
    for env in DEPLOY_ENVIRONMENTS:
        if name == _deploy_filename(env):
            return "deploy-record"
    for spec in BUNDLES.values():
        if spec.filename != "deploy-<env>.md" and name == spec.filename.lower():
            return spec.kind
    return None


def bundles_for_role(role_id: str) -> list[BundleSpec]:
    """The bundles a given role is responsible for producing (registry order)."""
    return [spec for spec in BUNDLES.values() if role_id in spec.produced_by]


def expected_filename(kind: str, env: str | None = None) -> str:
    spec = BUNDLES[kind]
    if spec.filename == "deploy-<env>.md":
        return _deploy_filename(env or "test")
    return spec.filename


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _section_present(content: str, name: str) -> bool:
    """True if `name` appears as a Markdown heading (``## Findings``) or a
    labelled line (``tool: bandit`` / ``**Tool:**``) — case-insensitive. Kept
    lenient on purpose: this is an advisory nudge toward the standard layout,
    not a strict parser."""
    token = re.escape(name)
    heading = re.compile(rf"(?im)^\s*#{{1,6}}\s+.*\b{token}\b")
    # A labelled line: optional list bullet + optional bold, the token, then a
    # colon — matches "tool: bandit", "- **Environment:** …", "**Result:** pass".
    labelled = re.compile(rf"(?im)^\s*(?:[-*+]\s+)?\**\s*{token}\**\s*:")
    return bool(heading.search(content) or labelled.search(content))


def validate_bundle(kind: str, content: str) -> BundleValidation:
    """Check a bundle's content against its schema. Reports missing sections/keys;
    never raises. An unknown ``kind`` is itself the single reported error."""
    result = BundleValidation(kind=kind)
    spec = BUNDLES.get(kind)
    if spec is None:
        result.errors.append(f"unknown evidence bundle kind '{kind}' "
                             f"(known: {', '.join(sorted(BUNDLES))})")
        return result

    if spec.structured:
        try:
            data = json.loads(content) if spec.fmt == "json" else yaml.safe_load(content)
        except (json.JSONDecodeError, yaml.YAMLError) as e:
            result.errors.append(f"{spec.filename} is not valid {spec.fmt}: {e}")
            return result
        if not isinstance(data, dict):
            result.errors.append(f"{spec.filename} must be a {spec.fmt} mapping "
                                 f"with keys: {', '.join(spec.required)}")
            return result
        for key in spec.required:
            value = data.get(key)
            # Present-but-empty is as bad as absent for evidence — an empty
            # findings list is fine (it means "clean"), but a missing key is not.
            if key not in data:
                result.errors.append(f"missing required key '{key}'")
            elif value is None or (isinstance(value, str) and not value.strip()):
                result.errors.append(f"required key '{key}' is empty")
    else:
        for name in spec.required:
            if not _section_present(content, name):
                result.errors.append(f"missing required section '{name}'")
    return result


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #

_MD_TEMPLATES: dict[str, str] = {
    "sast": (
        "# SAST summary\n\n"
        "- **Tool:** <scanner name + version>\n"
        "- **Command:** `<exact command run>`\n"
        "- **Timestamp:** <ISO 8601 UTC>\n"
        "- **Result:** pass | fail\n\n"
        "## Findings\n<none, or one bullet per finding: severity, location, rule>\n\n"
        "## Dismissed findings\n<none, or finding + why it is a false positive / accepted risk>\n"),
    "qa-report": (
        "# QA report\n\n"
        "- **Environment:** <test URL / build under test>\n"
        "- **Build:** <deployed_build id>\n\n"
        "## Acceptance criteria checks\n<AC-id: pass/fail + evidence ref per criterion>\n\n"
        "## Screenshots\n<breakpoint: file under evidence/screenshots/>\n\n"
        "## Failures\n<none, or each failure with severity and criterion_ref>\n"),
    "deploy-record": (
        "# Deploy record\n\n"
        "- **Environment:** <test | staging | production>\n"
        "- **Build:** <artifact / build id deployed>\n"
        "- **Command:** `<deploy command run>`\n\n"
        "## Output\n<summary of deploy output / logs>\n\n"
        "## Smoke\n<smoke-test command + result>\n"),
    "rollback-verification": (
        "# Rollback verification\n\n"
        "## Trigger\n<what prompted the rollback>\n\n"
        "## Rollback\n`<rollback command run>`\n\n"
        "## Verification\n<how the rolled-back state was confirmed healthy>\n"),
}

_STRUCTURED_TEMPLATES: dict[str, dict] = {
    "dependency-scan": {
        "tool": "<scanner name + version>", "command": "<exact command>",
        "timestamp": "<ISO 8601 UTC>", "result": "pass",
        "findings": [], "dismissed": []},
    "release-manifest": {
        "version": "<release version / tag>", "tickets": ["<KEY-123>"],
        "build_ids": {"production": "<build id>"},
        "migration_order": ["<migration step, in order>"],
        "rollback_path": "<how to roll this release back>"},
}

_SECRETS_TEMPLATE = (
    "tool: <scanner name + version>\n"
    "command: <exact command run>\n"
    "timestamp: <ISO 8601 UTC>\n"
    "result: pass\n"
    "findings: none\n"
    "dismissed: none\n")


def bundle_template(kind: str, env: str | None = None) -> str:
    """A fillable skeleton for a bundle that already satisfies validate_bundle."""
    spec = BUNDLES.get(kind)
    if spec is None:
        raise KeyError(f"unknown evidence bundle kind '{kind}'")
    if kind == "secrets-scan":
        return _SECRETS_TEMPLATE
    if spec.fmt == "json":
        return json.dumps(_STRUCTURED_TEMPLATES[kind], indent=2) + "\n"
    if spec.fmt == "yaml":
        return yaml.safe_dump(_STRUCTURED_TEMPLATES[kind], sort_keys=False)
    template = _MD_TEMPLATES[kind]
    if kind == "deploy-record" and env:
        template = template.replace("<test | staging | production>", env)
    return template


# --------------------------------------------------------------------------- #
# Human/prompt-readable catalog
# --------------------------------------------------------------------------- #

def catalog_text(role_id: str | None = None) -> str:
    """A concise description of the expected bundles — for a role's runtime
    prompt (``role_id`` set) or for documentation (``role_id`` None → all)."""
    specs = bundles_for_role(role_id) if role_id else list(BUNDLES.values())
    if not specs:
        return ""
    lines = []
    for spec in specs:
        lines.append(f"- `{EVIDENCE_DIR}/{spec.filename}` ({spec.fmt}) — {spec.description}; "
                     f"required: {', '.join(spec.required)}")
    return "\n".join(lines)
