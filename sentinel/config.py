"""Configuration: environment settings + the pipeline dispatch table (config/pipeline.yml)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_REF = re.compile(r"\$\{([A-Za-z0-9_]+)(?::([^}]*))?\}")


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
    log_level: str

    rework_limit: int = 2
    split_threshold_points: int = 8
    labels: dict[str, str] = field(default_factory=dict)
    wip_limits: dict[str, int] = field(default_factory=dict)
    roles: dict[str, RoleConfig] = field(default_factory=dict)
    commands: dict[str, str] = field(default_factory=dict)

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
        raise RuntimeError(f"No roles defined in {path}")
    return settings
