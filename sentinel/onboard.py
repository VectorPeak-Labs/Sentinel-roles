"""Guided onboarding: ``python -m sentinel.onboard``.

Takes a first-time operator from a fresh clone to a runnable configuration by
generating a project-specific ``.env`` (from ``.env.example``) and filling the
``commands:`` block of ``config/pipeline.yml`` — the one piece of per-project
setup Sentinel cannot ship a safe default for.

Design rules (issue P1):

- **Never print secret values.** The PAT, LiteLLM key, and webhook secret are
  read with :func:`getpass.getpass` and only ever shown masked in the summary.
- **Non-destructive.** ``--dry-run`` writes nothing; an existing file is not
  overwritten without ``--force`` (or an interactive confirmation).
- **No Jira/LiteLLM connectivity required.** This writes a *draft*; ``doctor``
  validates it (and onboarding can hand off to it directly).
- **Human-readable and diffable output** — comments in both files are preserved.

The module is split into pure builders (:func:`build_env_text`,
:func:`build_config_text`, :func:`command_warnings`) and the I/O orchestration
(:func:`run_onboarding`, :func:`collect_interactive`, :func:`main`) so behavior
is testable without a terminal.
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Env keys the flow collects, in prompt order. Secrets are a subset (below).
ENV_COLLECT_KEYS: tuple[str, ...] = (
    "JIRA_BASE_URL",
    "JIRA_PROJECT_KEY",
    "JIRA_PAT",
    "WEBHOOK_SECRET",
    "LITELLM_BASE_URL",
    "LITELLM_API_KEY",
    "SENTINEL_DEFAULT_MODEL",
    "SENTINEL_REVIEWER_MODEL",
    "SENTINEL_ALERT_WEBHOOK_URL",
)

# Values that must never reach stdout/logs — shown masked, read without echo.
ENV_SECRET_KEYS: frozenset[str] = frozenset({"JIRA_PAT", "LITELLM_API_KEY", "WEBHOOK_SECRET"})

# The project-command contract shell roles are told about (config/pipeline.yml).
COMMAND_KEYS: tuple[str, ...] = (
    "clone", "test", "deploy_test", "deploy_staging",
    "deploy_production", "smoke_test", "rollback",
)

# What a blank command means at runtime — used to warn the operator explicitly
# rather than letting a role silently escalate on first use.
_COMMAND_IMPACT: dict[str, str] = {
    "clone": "every shell role (Implementer 07 / Reviewer 08 / Deploy 09 / QA 10 / "
             "Release 12) has no way to check out the workspace",
    "test": "Implementer (07) and Code Reviewer (08) cannot run the test suite",
    "deploy_test": "Deployment (09) will escalate for Test deploys",
    "deploy_staging": "Deployment (09) will escalate for Staging deploys",
    "deploy_production": "Release (12) will escalate for every production release",
    "smoke_test": "Deployment (09) / QA (10) have no post-deploy smoke verification",
    "rollback": "Deployment / Release have no rollback command to verify recovery",
}


# --------------------------------------------------------------------------- #
# Pure builders (no I/O) — the parts worth unit-testing directly.
# --------------------------------------------------------------------------- #

def build_env_text(example_text: str, values: dict[str, str]) -> str:
    """Return ``.env`` content from ``.env.example``, overriding ``KEY=`` lines
    for every key present in ``values`` and leaving comments/other keys intact."""
    key_line = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
    out: list[str] = []
    for line in example_text.splitlines():
        m = key_line.match(line)
        if m and m.group(1) in values:
            out.append(f"{m.group(1)}={values[m.group(1)]}")
        else:
            out.append(line)
    text = "\n".join(out)
    if example_text.endswith("\n"):
        text += "\n"
    return text


def env_summary_lines(values: dict[str, str]) -> list[str]:
    """Human-readable, **secret-masked** summary of collected env values."""
    lines: list[str] = []
    for key in ENV_COLLECT_KEYS:
        if key not in values:
            continue
        value = values[key]
        if key in ENV_SECRET_KEYS:
            shown = "•••••• (set)" if value else "(empty)"
        else:
            shown = value if value else "(empty)"
        lines.append(f"{key} = {shown}")
    return lines


def _yaml_double_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _split_value_comment(rest: str) -> tuple[str, str]:
    """Split a YAML scalar's ``rest`` (text after ``key:``) into (value, comment),
    honoring quotes so a ``#`` inside a quoted value is not mistaken for a comment."""
    quote: str | None = None
    for i, ch in enumerate(rest):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return rest[:i], rest[i:]
    return rest, ""


def build_config_text(config_text: str, commands: dict[str, str]) -> str:
    """Return ``config/pipeline.yml`` with the ``commands:`` block values filled
    from ``commands`` (only keys present are touched), preserving every comment."""
    lines = config_text.splitlines()
    out: list[str] = []
    in_commands = False
    entry = re.compile(r"^(?P<indent>\s+)(?P<key>\w+):(?P<rest>.*)$")
    for line in lines:
        if not in_commands:
            out.append(line)
            if re.match(r"^commands:\s*(#.*)?$", line):
                in_commands = True
            continue
        # A non-indented, non-blank line ends the block.
        if line and not line[0].isspace():
            in_commands = False
            out.append(line)
            continue
        m = entry.match(line)
        if m and m.group("key") in commands:
            _, comment = _split_value_comment(m.group("rest"))
            comment = ("  " + comment.rstrip()) if comment.strip() else ""
            out.append(f"{m.group('indent')}{m.group('key')}: "
                       f"{_yaml_double_quote(commands[m.group('key')])}{comment}")
        else:
            out.append(line)
    text = "\n".join(out)
    if config_text.endswith("\n"):
        text += "\n"
    return text


def command_warnings(commands: dict[str, str]) -> list[str]:
    """One warning per blank project command, naming the role impact."""
    warnings: list[str] = []
    for key in COMMAND_KEYS:
        if not (commands.get(key) or "").strip():
            warnings.append(f"commands.{key} is empty — {_COMMAND_IMPACT[key]}.")
    return warnings


# --------------------------------------------------------------------------- #
# Orchestration + I/O
# --------------------------------------------------------------------------- #

@dataclass
class OnboardingResult:
    env_path: Path
    config_path: Path
    env_text: str
    config_text: str
    wrote_env: bool
    wrote_config: bool
    warnings: list[str] = field(default_factory=list)


def _write_file(path: Path, text: str, out: Callable[[str], None], label: str, *,
                guard: bool, force: bool) -> bool:
    """Write ``text`` to ``path``. Returns True iff the file was written.

    ``guard`` protects a file we must not silently clobber (the secret-bearing
    ``.env``): an existing, differing guarded file is left alone unless ``force``.
    The config is an intended *update* target, so it is written whenever its
    content changes (comment-preserving and git-diffable)."""
    if path.exists() and path.read_text() == text:
        out(f"  {label}: unchanged")
        return False
    if path.exists() and guard and not force:
        out(f"  {label}: already exists — not overwriting (use --force to replace)")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    out(f"  {label}: written ({path})")
    return True


def run_onboarding(*, env_values: dict[str, str], commands: dict[str, str],
                   root: Path | str = ".", env_example_path: Path | str | None = None,
                   config_path: Path | str | None = None,
                   env_out_path: Path | str | None = None,
                   dry_run: bool = False, force: bool = False,
                   out: Callable[[str], None] = print) -> OnboardingResult:
    """Build ``.env`` and ``config/pipeline.yml`` from collected answers, print a
    secret-masked summary + warnings, and (unless ``dry_run``) write the files."""
    root = Path(root)
    env_example_path = Path(env_example_path or root / ".env.example")
    config_path = Path(config_path or root / "config" / "pipeline.yml")
    env_out_path = Path(env_out_path or root / ".env")

    env_text = build_env_text(env_example_path.read_text(), env_values)
    config_text = build_config_text(config_path.read_text(), commands)
    warnings = command_warnings(commands)

    out("")
    out("Configuration summary (secrets hidden):")
    for line in env_summary_lines(env_values):
        out("  " + line)

    if warnings:
        out("")
        out("Shell-role commands left blank — these roles will escalate (needs-human) "
            "instead of guessing:")
        for w in warnings:
            out("  ! " + w)

    wrote_env = wrote_config = False
    if dry_run:
        out("\n[dry-run] no files written.")
    else:
        out("")
        wrote_env = _write_file(env_out_path, env_text, out, ".env",
                                guard=True, force=force)
        wrote_config = _write_file(config_path, config_text, out, "config/pipeline.yml",
                                   guard=False, force=force)

    return OnboardingResult(
        env_path=env_out_path, config_path=config_path, env_text=env_text,
        config_text=config_text, wrote_env=wrote_env, wrote_config=wrote_config,
        warnings=warnings,
    )


def _prompt(label: str, *, default: str = "", secret: bool = False,
            ask: Callable[[str], str] = input,
            getpass_fn: Callable[[str], str] | None = None) -> str:
    if secret:
        import getpass as _getpass
        reader = getpass_fn or _getpass.getpass
        return reader(f"{label}: ").strip()
    suffix = f" [{default}]" if default else ""
    raw = ask(f"{label}{suffix}: ").strip()
    return raw or default


def collect_interactive(*, ask: Callable[[str], str] = input,
                        getpass_fn: Callable[[str], str] | None = None,
                        out: Callable[[str], None] = print,
                        ) -> tuple[dict[str, str], dict[str, str], bool]:
    """Prompt for every field. Returns (env_values, commands, run_doctor)."""
    out("Sentinel onboarding — Jira, LiteLLM, and project commands.")
    out("Secrets are read without echo and never printed back.\n")

    env: dict[str, str] = {}
    env["JIRA_BASE_URL"] = _prompt("Jira base URL (no trailing slash)", ask=ask)
    env["JIRA_PROJECT_KEY"] = _prompt("Jira project key (e.g. SENT)", ask=ask)
    env["JIRA_PAT"] = _prompt("Jira Personal Access Token", secret=True, getpass_fn=getpass_fn)
    env["WEBHOOK_SECRET"] = _prompt("Webhook secret (optional; guards mutating endpoints)",
                                    secret=True, getpass_fn=getpass_fn)
    env["LITELLM_BASE_URL"] = _prompt("LiteLLM base URL", ask=ask)
    env["LITELLM_API_KEY"] = _prompt("LiteLLM API key", secret=True, getpass_fn=getpass_fn)
    env["SENTINEL_DEFAULT_MODEL"] = _prompt("Default model", default="gpt-4o", ask=ask)
    env["SENTINEL_REVIEWER_MODEL"] = _prompt("Code Reviewer model override (optional)", ask=ask)
    env["SENTINEL_ALERT_WEBHOOK_URL"] = _prompt("Alert webhook URL (optional)", ask=ask)

    out("\nProject commands for shell roles — leave blank to have that role escalate:")
    commands: dict[str, str] = {}
    for key in COMMAND_KEYS:
        commands[key] = _prompt(f"  commands.{key}", ask=ask)

    run_doctor = _prompt("\nRun doctor after writing? [y/N]", default="n",
                         ask=ask).lower().startswith("y")
    return env, commands, run_doctor


def _run_doctor_with(env_values: dict[str, str], config_path: Path) -> int:
    """Load the just-collected settings into the environment and run doctor."""
    import asyncio

    from . import doctor

    for key, value in env_values.items():
        if value:
            os.environ[key] = value
    os.environ["SENTINEL_CONFIG"] = str(config_path)
    return asyncio.run(doctor.main())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sentinel.onboard",
        description="Generate a project-specific .env and config/pipeline.yml.")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be written without touching any file")
    parser.add_argument("--force", action="store_true",
                        help="overwrite existing .env / config/pipeline.yml")
    parser.add_argument("--non-interactive", action="store_true",
                        help="take values from the environment instead of prompting "
                             "(commands from SENTINEL_CMD_<NAME>); useful for CI/dry-run")
    parser.add_argument("--run-doctor", action="store_true",
                        help="run doctor after writing the files")
    parser.add_argument("--root", default=".", help="repo root (default: cwd)")
    args = parser.parse_args(argv)

    root = Path(args.root)
    force = args.force
    if args.non_interactive:
        env_values = {k: os.environ.get(k, "") for k in ENV_COLLECT_KEYS}
        commands = {k: os.environ.get(f"SENTINEL_CMD_{k.upper()}", "") for k in COMMAND_KEYS}
        run_doctor = args.run_doctor
    else:
        env_values, commands, run_doctor = collect_interactive()
        run_doctor = run_doctor or args.run_doctor
        if not args.dry_run:
            # A final confirm authorizes writing. It updates config/pipeline.yml
            # (an intended, comment-preserving update), but must NOT silently
            # clobber a secret-bearing .env — overwriting an existing one needs
            # --force or its own dedicated confirmation.
            proceed = _prompt("Write these files now? [y/N]", default="n").lower()
            if not proceed.startswith("y"):
                args.dry_run = True
            elif (root / ".env").exists() and not force:
                overwrite = _prompt("An .env already exists. Overwrite it? [y/N]",
                                    default="n").lower()
                force = overwrite.startswith("y")

    result = run_onboarding(env_values=env_values, commands=commands, root=root,
                            dry_run=args.dry_run, force=force)

    if run_doctor and not args.dry_run:
        print("\nRunning doctor against the new configuration...\n")
        return _run_doctor_with(env_values, result.config_path)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
