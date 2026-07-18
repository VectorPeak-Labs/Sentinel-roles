"""Pre-flight readiness gate: ``python -m sentinel.doctor [--format json] [--no-network]``.

Answers one operator question — *is this deployment safe and useful to run?* — by
classifying findings into three buckets and reporting ``READY: yes|no``:

- **BLOCKERS** — the pipeline will not run productively as configured (a missing
  role document, a blank shell-role command whose role would escalate on first
  use, an unreachable Jira/LiteLLM, a workflow status that does not exist).
- **WARNINGS** — it will run, but with a known risk (unauthenticated endpoints,
  the Code Reviewer sharing the default model).
- **INFO** — confirmations and deferred checks.

The command-, config-, and security-readiness classification lives in the pure,
network-free :func:`readiness_findings`; Jira/LiteLLM reachability is layered on
top. Exit code is ``0`` iff ready (no blockers).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from .config import Settings, load_settings
from .jira import JiraClient, JiraError
from .llm import LLM

# The project-command contract, and which shell roles need each command. A blank
# command whose owning role is enabled is a readiness blocker (that role escalates).
COMMAND_ORDER: tuple[str, ...] = (
    "clone", "test", "deploy_test", "deploy_staging",
    "deploy_production", "smoke_test", "rollback",
)
ROLE_COMMAND_NEEDS: dict[str, tuple[str, ...]] = {
    "07-implementer": ("clone", "test"),
    "08-code-reviewer": ("clone", "test"),
    "09-deployment": ("clone", "deploy_test", "deploy_staging", "smoke_test", "rollback"),
    "10-qa": ("clone", "smoke_test"),
    "12-release": ("clone", "deploy_production", "smoke_test", "rollback"),
}
_COMMAND_IMPACT: dict[str, str] = {
    "clone": "shell roles cannot check out the workspace",
    "test": "Implementer (07) and Code Reviewer (08) cannot run the test suite",
    "deploy_test": "Deployment (09) will escalate for Test deploys",
    "deploy_staging": "Deployment (09) will escalate for Staging deploys",
    "deploy_production": "Release (12) will escalate for every production release",
    "smoke_test": "Deployment (09) / QA (10) have no post-deploy smoke verification",
    "rollback": "Deployment (09) / Release (12) have no rollback command",
}

_SHARED_DOCS = ("00-overview-and-conventions.md", "00a-operating-manual.md")


@dataclass
class Report:
    """Accumulates classified findings; ``ready`` is true iff no blockers."""

    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    def blocker(self, msg: str) -> None:
        self.blockers.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.info.append(msg)

    @property
    def ready(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict:
        return {"ready": self.ready, **asdict(self)}


def _required_commands(settings: Settings) -> set[str]:
    """Commands at least one *enabled shell* role needs."""
    needed: set[str] = set()
    for role_id, cmds in ROLE_COMMAND_NEEDS.items():
        role = settings.roles.get(role_id)
        if role and role.shell:
            needed.update(cmds)
    return needed


def readiness_findings(settings: Settings) -> Report:
    """Pure, network-free readiness classification: role docs, project commands,
    reviewer model, and endpoint-security defaults."""
    r = Report()

    # Role documents (these ARE the agent prompts — a missing one breaks the role).
    for role in settings.roles.values():
        if not Path(role.doc).exists():
            r.blocker(f"role {role.role_id}: missing role document {role.doc}")
    for shared in _SHARED_DOCS:
        if not (settings.docs_dir / shared).exists():
            r.blocker(f"missing shared document {settings.docs_dir / shared}")

    # Project-command readiness, with role-specific impact.
    needed = _required_commands(settings)
    for cmd in COMMAND_ORDER:
        if cmd in needed and not (settings.commands.get(cmd) or "").strip():
            r.blocker(f"commands.{cmd} is empty — {_COMMAND_IMPACT[cmd]}")

    # Reviewer model override (role 08 recommends a different model than the Implementer).
    reviewer = settings.roles.get("08-code-reviewer")
    if reviewer and not reviewer.model:
        r.warn("SENTINEL_REVIEWER_MODEL is not set — the Code Reviewer (08) runs on the "
               "default model in a separate context")

    # Endpoint-security defaults.
    if not settings.webhook_secret:
        r.warn("WEBHOOK_SECRET is empty — /webhook/jira, /sweep, /pause and /resume are "
               "UNAUTHENTICATED; set it before exposing the service")
    r.warn("/health is unauthenticated and may expose operational detail — keep the service "
           "behind a reverse proxy / private network")
    r.note("token auth via ?token= is for Jira webhooks / development only — prefer the "
           "X-Sentinel-Token or Authorization: Bearer header so it stays out of access logs")

    # Confirmations.
    r.note(f"{len(settings.roles)} roles configured and dispatch table validated")
    r.note(f"default model: {settings.default_model}")
    r.note("alert webhook configured" if settings.alert_webhook_url
           else "alert webhook not set — Jira comment + needs-human label are the only channel")
    return r


def _classify_llm_error(e: Exception) -> str:
    """Distinguish auth / missing-model / network from an LLM call failure."""
    status = getattr(e, "status_code", None)
    if not isinstance(status, int):
        status = getattr(e, "status", None)
    name = type(e).__name__
    if isinstance(status, int):
        if status in (401, 403):
            return f"authentication failed (HTTP {status}) — check LITELLM_API_KEY"
        if status == 404:
            return f"model not found (HTTP {status}) — check the model name in LiteLLM"
        return f"{name} (HTTP {status})"
    return f"{name} (network/connection error)"


async def check_jira(settings: Settings, r: Report) -> None:
    jira = JiraClient(settings.jira_base_url, settings.jira_pat)
    try:
        me = await jira.myself()
        r.note(f"Jira reachable at {settings.jira_base_url} as "
               f"'{me.get('name')}' ({me.get('displayName')})")
        statuses = (await jira._request(
            "GET", f"/project/{settings.jira_project}/statuses")).json()
        known = {s["name"].lower() for it in statuses for s in it.get("statuses", [])}
        r.note(f"project {settings.jira_project}: {len(known)} workflow status(es) found")
        for status in settings.agent_statuses:
            if status.lower() not in known:
                r.blocker(f"workflow status '{status}' not found in project "
                          f"{settings.jira_project} — fix the Jira workflow or config/pipeline.yml")
        r.note("Jira write-permission probes (labels / properties / comments / attachments / "
               "assignee) are deferred — a readiness check does not mutate a live project; "
               "run a smoke ticket to confirm the service account's rights")
    except (JiraError, httpx.HTTPError) as e:
        r.blocker(f"Jira unreachable at {settings.jira_base_url}: {type(e).__name__}: {e}")
    finally:
        await jira.close()


async def _probe_model(settings: Settings, model: str, label: str, r: Report) -> None:
    llm = LLM(settings.litellm_base_url, settings.litellm_api_key, model)
    try:
        await llm.chat([{"role": "user", "content": "Reply with the single word: ok"}])
        r.note(f"LiteLLM reachable at {settings.litellm_base_url}, {label} '{model}' answered")
    except Exception as e:  # noqa: BLE001 — surface every backend failure as a blocker
        r.blocker(f"LiteLLM {label} '{model}' unreachable: {_classify_llm_error(e)}")
    finally:
        await llm.close()


async def check_llm(settings: Settings, r: Report) -> None:
    await _probe_model(settings, settings.default_model, "default model", r)
    reviewer = settings.roles.get("08-code-reviewer")
    if reviewer and reviewer.model and reviewer.model != settings.default_model:
        await _probe_model(settings, reviewer.model, "reviewer model", r)


def render(r: Report, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(r.as_dict(), indent=2)
    lines = [f"READY: {'yes' if r.ready else 'no'}"]
    for title, items in (("BLOCKERS", r.blockers), ("WARNINGS", r.warnings), ("INFO", r.info)):
        if items:
            lines.append(f"\n{title}:")
            lines.extend(f"  - {m}" for m in items)
    return "\n".join(lines)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sentinel.doctor",
        description="Readiness gate: classify config/command/Jira/LiteLLM findings.")
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="output format (default: text)")
    parser.add_argument("--no-network", action="store_true",
                        help="skip Jira/LiteLLM checks (config/command/security readiness only)")
    # Called programmatically (argv is None) parses no flags; the CLI passes sys.argv[1:].
    args = parser.parse_args(argv if argv is not None else [])

    try:
        settings = load_settings()
    except Exception as e:  # noqa: BLE001 — a bad config is itself a readiness blocker
        report = Report()
        report.blocker(f"configuration failed to load: {e}")
        print(render(report, args.format))
        return 1

    report = readiness_findings(settings)
    if not args.no_network:
        await check_jira(settings, report)
        await check_llm(settings, report)

    print(render(report, args.format))
    return 0 if report.ready else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
