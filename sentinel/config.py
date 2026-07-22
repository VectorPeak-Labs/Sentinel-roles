"""Configuration: environment settings + the pipeline dispatch table (config/pipeline.yml)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

_ENV_REF = re.compile(r"\$\{([A-Za-z0-9_]+)(?::([^}]*))?\}")

# Dispatch-table vocabulary the Orchestrator actually understands. A trigger type
# or condition outside these sets is silently non-functional at runtime, so the
# validator rejects it at load time (see validate_config).
KNOWN_TRIGGER_TYPES = frozenset({"ticket", "queue"})
KNOWN_CONDITIONS = frozenset({"capacity_in_progress", "release_window"})

# Application-security baselines the policy pack understands. `none` disables the
# named-standard check (the pipeline still runs the per-ticket SEC-* checklist).
KNOWN_SECURITY_BASELINES = ("owasp-asvs-l1", "owasp-asvs-l2", "owasp-asvs-l3", "none")


class ConfigError(RuntimeError):
    """Raised for a malformed pipeline configuration (bad env or dispatch table)."""


def _expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} / ${VAR:default} references in a YAML tree."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set (see .env.example)")
    return value


@dataclass(frozen=True)
class RoleConfig:
    role_id: str
    doc: str
    trigger_type: str                 # "ticket" | "queue"
    statuses: tuple[str, ...]
    require_label: str | None = None  # label-config key, resolved to the actual label
    condition: str | None = None      # "capacity_in_progress" | "release_window"
    shell: bool = False
    model: str | None = None
    estimators: int = 3
    min_interval_seconds: int = 0

    def watches_status(self, status: str) -> bool:
        return status.lower() in {s.lower() for s in self.statuses}


# --------------------------------------------------------------------------- #
# Project policy pack (config/policy.yml)
#
# Externalizes the security / review / QA / release rules that were previously
# hardcoded assumptions in the role documents, so a project can set them without
# hand-editing prompts. Simple typed config, NOT a policy engine (per issue #33):
# the defaults below are equivalent to Sentinel's built-in behavior, so an absent
# or partial policy.yml changes nothing.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SecurityPolicy:
    baseline: str = "owasp-asvs-l2"
    require_dependency_scan: bool = True
    require_secrets_scan: bool = True


@dataclass(frozen=True)
class ReviewPolicy:
    require_ci_green: bool = True
    allow_minor_followups: bool = True


@dataclass(frozen=True)
class QaPolicy:
    require_visual_evidence: bool = True
    require_screenshots: bool = True


@dataclass(frozen=True)
class ReleasePolicy:
    require_human_notes_approval: bool = True
    soak_minutes: int = 30
    require_reversible_migrations: bool = True


@dataclass(frozen=True)
class Policy:
    security: SecurityPolicy = field(default_factory=SecurityPolicy)
    review: ReviewPolicy = field(default_factory=ReviewPolicy)
    qa: QaPolicy = field(default_factory=QaPolicy)
    release: ReleasePolicy = field(default_factory=ReleasePolicy)

    def summary_lines(self) -> list[str]:
        """Compact, human/prompt-readable rendering of the active policy."""
        s, r, q, rel = self.security, self.review, self.qa, self.release
        def yn(v: bool) -> str:
            return "required" if v else "not required"
        return [
            f"Security baseline: {s.baseline}. Dependency scan {yn(s.require_dependency_scan)}; "
            f"secrets scan {yn(s.require_secrets_scan)} (role 08).",
            f"Review: CI green {yn(r.require_ci_green)}; minor-only findings may become a "
            f"follow-up ticket: {'yes' if r.allow_minor_followups else 'no'} (role 08).",
            f"QA: visual evidence {yn(q.require_visual_evidence)}; screenshots "
            f"{yn(q.require_screenshots)} (role 10).",
            f"Release: human-approved notes {yn(rel.require_human_notes_approval)}; "
            f"soak {rel.soak_minutes} min; reversible migrations "
            f"{yn(rel.require_reversible_migrations)} (role 12).",
        ]


_SECTION_TYPES = {"security": SecurityPolicy, "review": ReviewPolicy,
                  "qa": QaPolicy, "release": ReleasePolicy}


def _coerce_section(name: str, cls, raw: object, problems: list[str]) -> dict:
    """Overlay a raw YAML section onto its typed defaults, type-checking each key."""
    values: dict[str, object] = {}
    if raw is None:
        return values
    if not isinstance(raw, dict):
        problems.append(f"policy.{name} must be a mapping")
        return values
    known = {f.name: f.type for f in fields(cls)}
    for key, value in raw.items():
        if key not in known:
            problems.append(f"policy.{name}.{key} is not a recognized policy key "
                            f"(known: {', '.join(sorted(known))})")
            continue
        if known[key] == "bool":
            if not isinstance(value, bool):
                problems.append(f"policy.{name}.{key} must be true or false, got {value!r}")
                continue
        elif known[key] == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                problems.append(f"policy.{name}.{key} must be an integer, got {value!r}")
                continue
            if value < 0:
                problems.append(f"policy.{name}.{key} must be >= 0, got {value}")
                continue
        values[key] = value
    return values


def build_policy(raw: dict | None) -> tuple[Policy, list[str]]:
    """Build a Policy from a raw mapping, overlaying onto defaults. Returns the
    policy (defaults for anything invalid/absent) plus a list of problems."""
    problems: list[str] = []
    raw = raw or {}
    if not isinstance(raw, dict):
        return Policy(), ["policy.yml must be a mapping of sections"]
    for section in raw:
        if section not in _SECTION_TYPES:
            problems.append(f"policy.{section} is not a recognized section "
                            f"(known: {', '.join(sorted(_SECTION_TYPES))})")
    sections = {name: cls(**_coerce_section(name, cls, raw.get(name), problems))
                for name, cls in _SECTION_TYPES.items()}
    policy = Policy(**sections)
    if policy.security.baseline not in KNOWN_SECURITY_BASELINES:
        problems.append(f"policy.security.baseline '{policy.security.baseline}' is not one of "
                        f"{list(KNOWN_SECURITY_BASELINES)}")
    return policy, problems


def load_policy(path: str | os.PathLike | None = None) -> tuple[Policy, list[str]]:
    """Load the policy pack. A missing file yields built-in defaults (behavior
    unchanged); a present-but-invalid file yields defaults plus problems (the
    caller — load_settings — turns problems into a ConfigError, failing loudly)."""
    path = Path(path or os.environ.get("SENTINEL_POLICY", "config/policy.yml"))
    if not path.exists():
        return Policy(), []
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        return Policy(), [f"policy file {path} is not valid YAML: {e}"]
    return build_policy(_expand_env(raw) if isinstance(raw, dict) else raw)


@dataclass
class Settings:
    jira_base_url: str
    jira_pat: str
    jira_project: str
    litellm_base_url: str
    litellm_api_key: str
    default_model: str
    webhook_secret: str
    alert_webhook_url: str
    data_dir: Path
    docs_dir: Path
    sweep_interval: int
    lease_timeout: int
    heartbeat_interval: int
    max_agent_turns: int
    jira_max_retries: int
    audit_max_bytes: int
    audit_backup_count: int
    shutdown_grace_seconds: float
    stale_escalation_hours: float
    log_level: str

    llm_daily_token_budget: int = 0
    workspace_max_bytes: int = 0
    rework_limit: int = 2
    split_threshold_points: int = 8
    labels: dict[str, str] = field(default_factory=dict)
    wip_limits: dict[str, int] = field(default_factory=dict)
    roles: dict[str, RoleConfig] = field(default_factory=dict)
    commands: dict[str, str] = field(default_factory=dict)
    policy: Policy = field(default_factory=Policy)

    def label(self, key: str) -> str:
        return self.labels.get(key, key)

    def wip_limit(self, status: str) -> int | None:
        for name, limit in self.wip_limits.items():
            if name.lower() == status.lower():
                return int(limit)
        return None

    def roles_for_status(self, status: str) -> list[RoleConfig]:
        return [r for r in self.roles.values() if r.watches_status(status)]

    @property
    def agent_statuses(self) -> list[str]:
        seen: dict[str, str] = {}
        for role in self.roles.values():
            for status in role.statuses:
                seen.setdefault(status.lower(), status)
        return list(seen.values())


def validate_config(settings: "Settings") -> list[str]:
    """Static consistency checks on the dispatch table. Returns a list of
    human-readable problems (empty ⇒ valid).

    Every check here catches a mistake that is otherwise **silent** at runtime —
    a role that never dispatches, a gate that never applies, a WIP limit that is
    never enforced — precisely the "nothing silently stuck" class of failure the
    platform guards against everywhere else, but at the config layer. `load_settings`
    raises `ConfigError` on any of them so a bad `pipeline.yml` fails loudly at
    startup instead of quietly stranding tickets in production.
    """
    problems: list[str] = []
    label_keys = set(settings.labels)
    ticket_owners: dict[str, list[str]] = {}   # status (lower) -> ticket role ids

    for role_id, role in settings.roles.items():
        if role.trigger_type not in KNOWN_TRIGGER_TYPES:
            problems.append(
                f"role '{role_id}': trigger.type '{role.trigger_type}' is not one of "
                f"{sorted(KNOWN_TRIGGER_TYPES)} — it matches neither dispatch path and "
                f"would never run")
        if not role.statuses:
            problems.append(
                f"role '{role_id}': trigger.statuses is empty — it watches no status and "
                f"would never dispatch")
        if role.require_label and role.require_label not in label_keys:
            problems.append(
                f"role '{role_id}': require_label '{role.require_label}' is not a key in "
                f"labels: {sorted(label_keys)} — the role would wait for a label no human "
                f"ever applies and never dispatch")
        if role.condition is not None:
            if role.condition not in KNOWN_CONDITIONS:
                problems.append(
                    f"role '{role_id}': condition '{role.condition}' is not one of "
                    f"{sorted(KNOWN_CONDITIONS)} — an unrecognized condition is silently "
                    f"ignored, so the gate you intended would not apply")
            if role.trigger_type != "queue":
                problems.append(
                    f"role '{role_id}': condition '{role.condition}' is only evaluated for "
                    f"queue roles, but this role is '{role.trigger_type}' — the gate would "
                    f"be silently ignored")
        if role.trigger_type == "ticket":
            for status in role.statuses:
                ticket_owners.setdefault(status.lower(), []).append(role_id)

    for status_lc, owners in sorted(ticket_owners.items()):
        if len(owners) > 1:
            problems.append(
                f"status '{status_lc}' is watched by multiple ticket roles "
                f"({sorted(owners)}) — dispatch picks one nondeterministically; give each "
                f"status a single ticket role")

    watched = {s.lower() for role in settings.roles.values() for s in role.statuses}
    for status in settings.wip_limits:
        if status.lower() not in watched:
            problems.append(
                f"wip_limits names status '{status}' that no role watches — the limit is "
                f"never enforced (typo in the status name?)")

    return problems


def load_settings(config_path: str | os.PathLike | None = None) -> Settings:
    litellm_url = _require("LITELLM_BASE_URL").rstrip("/")
    if not litellm_url.endswith("/v1"):
        litellm_url += "/v1"

    settings = Settings(
        jira_base_url=_require("JIRA_BASE_URL").rstrip("/"),
        jira_pat=_require("JIRA_PAT"),
        jira_project=_require("JIRA_PROJECT_KEY"),
        litellm_base_url=litellm_url,
        litellm_api_key=_require("LITELLM_API_KEY"),
        default_model=os.environ.get("SENTINEL_DEFAULT_MODEL", "gpt-4o"),
        webhook_secret=os.environ.get("WEBHOOK_SECRET", ""),
        alert_webhook_url=os.environ.get("SENTINEL_ALERT_WEBHOOK_URL", "").strip(),
        data_dir=Path(os.environ.get("DATA_DIR", "/data")),
        docs_dir=Path(os.environ.get("DOCS_DIR", "docs")),
        sweep_interval=int(os.environ.get("SENTINEL_SWEEP_INTERVAL", "900")),
        lease_timeout=int(os.environ.get("SENTINEL_LEASE_TIMEOUT", "1800")),
        heartbeat_interval=int(os.environ.get("SENTINEL_HEARTBEAT_INTERVAL", "600")),
        max_agent_turns=int(os.environ.get("SENTINEL_MAX_AGENT_TURNS", "80")),
        jira_max_retries=int(os.environ.get("SENTINEL_JIRA_MAX_RETRIES", "3")),
        audit_max_bytes=int(os.environ.get("SENTINEL_AUDIT_MAX_BYTES", "50000000")),
        audit_backup_count=int(os.environ.get("SENTINEL_AUDIT_BACKUP_COUNT", "5")),
        shutdown_grace_seconds=float(os.environ.get("SENTINEL_SHUTDOWN_GRACE", "10")),
        stale_escalation_hours=float(os.environ.get("SENTINEL_STALE_ESCALATION_HOURS", "24")),
        llm_daily_token_budget=int(os.environ.get("SENTINEL_LLM_DAILY_TOKEN_BUDGET", "0")),
        workspace_max_bytes=int(os.environ.get("SENTINEL_WORKSPACE_MAX_BYTES", "0")),
        log_level=os.environ.get("SENTINEL_LOG_LEVEL", "INFO"),
    )

    path = Path(config_path or os.environ.get("SENTINEL_CONFIG", "config/pipeline.yml"))
    raw = _expand_env(yaml.safe_load(path.read_text()))

    settings.rework_limit = int(raw.get("rework_limit", 2))
    settings.split_threshold_points = int(raw.get("split_threshold_points", 8))
    settings.labels = dict(raw.get("labels", {}))
    settings.wip_limits = dict(raw.get("wip_limits", {}))
    settings.commands = {k: (v or "").strip() for k, v in dict(raw.get("commands", {})).items()}

    for role_id, spec in dict(raw.get("roles", {})).items():
        trigger = spec.get("trigger", {})
        settings.roles[role_id] = RoleConfig(
            role_id=role_id,
            doc=spec["doc"],
            trigger_type=trigger.get("type", "ticket"),
            statuses=tuple(trigger.get("statuses", [])),
            require_label=trigger.get("require_label"),
            condition=trigger.get("condition"),
            shell=bool(spec.get("shell", False)),
            model=(spec.get("model") or "").strip() or None,
            estimators=int(spec.get("estimators", 3)),
            min_interval_seconds=int(spec.get("min_interval_seconds", 0)),
        )

    if not settings.roles:
        raise ConfigError(f"No roles defined in {path}")
    problems = validate_config(settings)
    if problems:
        raise ConfigError(
            f"Invalid pipeline configuration in {path}:\n  - " + "\n  - ".join(problems))

    # Project policy pack: a missing file yields defaults; an invalid one fails
    # loudly at startup, exactly like a malformed dispatch table.
    settings.policy, policy_problems = load_policy()
    if policy_problems:
        raise ConfigError(
            "Invalid project policy (config/policy.yml):\n  - " + "\n  - ".join(policy_problems))
    return settings
