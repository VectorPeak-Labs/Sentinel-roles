"""Role agent runner.

Loading contract (docs/README.md): an agent instance is loaded with
00-overview + 00a-operating-manual + its own role document, in that order,
followed by a runtime preamble describing its identity, tools and hard rules.
The agent then runs an LLM tool loop until it ends its run through a terminal
tool (transition_with_handoff / escalate / finish_run) or hits the turn cap.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import lru_cache
from pathlib import Path

from . import evidence
from .config import RoleConfig, Settings
from .jira import JiraClient, PROP_RETRIES
from .lease import LeaseError, LeaseManager
from .llm import LLM
from .metrics import Metrics
from .notify import Notifier
from .audit import AuditLog
from . import tools as toolsmod
from .tools import ToolContext, dispatch, tools_for_role

log = logging.getLogger("sentinel.agent")

SHARED_DOCS = ("00-overview-and-conventions.md", "00a-operating-manual.md")


@lru_cache(maxsize=32)
def _read_doc(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def build_system_prompt(settings: Settings, role: RoleConfig, agent_user: str) -> str:
    parts = [_read_doc(str(settings.docs_dir / doc)) for doc in SHARED_DOCS]
    parts.append(_read_doc(role.doc))

    commands = {k: v for k, v in settings.commands.items() if v}
    preamble = f"""# Runtime context

You are the **{role.role_id}** agent in the Sentinel pipeline, operating live against Jira.

- Jira project: **{settings.jira_project}**. Your Jira identity (used for leases/assignment): `{agent_user}`.
- All state lives in Jira. Read tickets with `get_ticket` (it includes recent comments and the
  sentinel state: lease, rework count, waiting marker, deployed builds). Search with JQL.
- Status names in this Jira workflow match the pipeline table in 00-overview.
- Labels in use: leased=`{settings.label('leased')}`, escalation=`{settings.label('needs_human')}`,
  invalid handoff=`{settings.label('handoff_invalid')}`, icebox activation=`{settings.label('activate')}`.

## Hard rules (enforced by the tools — do not fight them)
1. The ONLY way to change a ticket's status is `transition_with_handoff` (or `reject_to_rework`
   for rejections). Both validate the payload schemas from 00-overview and refuse incomplete ones.
2. Every run ends with exactly one terminal action: `transition_with_handoff` on your ticket,
   `escalate`, or `finish_run` (when you posted questions and are now waiting on a human, or the
   queue has nothing actionable). Never just stop responding.
3. Humans always win: if the ticket is not in the status you expect, a human moved it — re-read
   and adapt; never counter-transition.
4. Timestamps in payloads: use the current UTC time in ISO 8601.
5. Rework limit: {settings.rework_limit}. Size-gate / split threshold: {settings.split_threshold_points} points.
6. Post human-facing communication (questions, packets, findings) as ticket comments; humans
   reply in comments — when you end a run waiting on a reply, sentinel wakes you when the
   ticket is updated.
"""
    if role.trigger_type == "queue":
        preamble += """
## Queue-role rules
You operate on a queue, not a single ticket. `claim_ticket` each ticket BEFORE acting on it,
and either transition it (releases the lease automatically) or `release_ticket` it. Never leave
leases dangling when you `finish_run`.
"""
    if role.shell:
        cmd_lines = "\n".join(f"- {name}: `{cmd}`" for name, cmd in commands.items()) \
            or "- (none configured — if you need project commands, escalate so a human fills in config/pipeline.yml)"
        preamble += f"""
## Workspace & shell
You have `run_command` in a persistent workspace directory. Project-specific commands configured
by the humans (use these rather than guessing):
{cmd_lines}
"""
        bundles = evidence.catalog_text(role.role_id)
        bundle_block = bundles or ("- (this role produces no standard bundle itself; still put "
                                   "any evidence you attach under `evidence/` and follow the "
                                   "standard in docs/00-overview)")
        preamble += f"""
## Evidence bundles (universal rule 5 — evidence over assertion)
Write evidence into an `evidence/` directory in your workspace using the project's standard
names/schemas, then `check_evidence` each file BEFORE `attach_file` so the next role and the
audit trail can find and read it. Bundles you are responsible for:
{bundle_block}
"""
    return "\n\n---\n\n".join(parts) + "\n\n---\n\n" + preamble


class AgentRunner:
    def __init__(self, settings: Settings, jira: JiraClient, llm: LLM,
                 leases: LeaseManager, audit: AuditLog, agent_user: str,
                 notifier: "Notifier | None" = None, metrics: "Metrics | None" = None):
        self.settings = settings
        self.jira = jira
        self.llm = llm
        self.leases = leases
        self.audit = audit
        self.agent_user = agent_user
        self.notifier = notifier
        self.metrics = metrics

    async def run(self, role: RoleConfig, ticket: str | None, kickoff: str) -> None:
        """Run one role agent instance. `ticket` is set for ticket-scoped roles."""
        workspace = self.settings.data_dir / "workspace" / role.role_id
        workspace.mkdir(parents=True, exist_ok=True)
        ctx = ToolContext(jira=self.jira, llm=self.llm, leases=self.leases,
                          settings=self.settings, audit=self.audit, role=role,
                          ticket=ticket, workspace=workspace, notifier=self.notifier,
                          metrics=self.metrics)

        if ticket:
            try:
                await self.leases.claim(ticket, role.role_id)
            except LeaseError as e:
                log.info("dispatch of %s on %s aborted: %s", role.role_id, ticket, e)
                return

        # Queue roles claim tickets mid-run (claim_ticket), so the heartbeat task
        # runs for every role — otherwise a long deploy/release batch would have
        # its leases reclaimed by the sweep while still working.
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(ctx))
        self.audit.record("dispatch", role=role.role_id, ticket=ticket)
        try:
            await self._loop(ctx, kickoff)
        except asyncio.CancelledError:
            # Shutdown / redeploy: release every lease this run holds so the ticket
            # is free immediately instead of frozen until the stale-lease timeout
            # (~30 min). This is NOT a failure, so do not bump the retry counter.
            for key in ([ticket] if ticket else []) + list(ctx.extra_leased):
                try:
                    await self.leases.release(key)
                except Exception:
                    log.warning("could not release lease on %s during shutdown", key)
            self.audit.record("agent_cancelled", role=role.role_id, ticket=ticket)
            raise
        except Exception as e:
            log.exception("agent run %s/%s crashed", role.role_id, ticket)
            self.audit.record("agent_crash", role=role.role_id, ticket=ticket, error=str(e))
            # Release cleanly and bump the retry counter so the Orchestrator retries
            # once, then escalates — a deterministic crash must not loop forever.
            for key in ([ticket] if ticket else []) + list(ctx.extra_leased):
                try:
                    await self._bump_retries(key)
                    await self.leases.release(key)
                except Exception:
                    log.warning("could not release lease on %s after crash", key)
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()

    async def _heartbeat_loop(self, ctx: ToolContext) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_interval)
            await self._heartbeat_once(ctx)

    async def _heartbeat_once(self, ctx: ToolContext) -> None:
        """Refresh every lease this run holds: the role's own ticket plus any
        tickets a queue role claimed with claim_ticket."""
        for key in ([ctx.ticket] if ctx.ticket else []) + list(ctx.extra_leased):
            try:
                await self.leases.heartbeat(key, ctx.role.role_id)
            except LeaseError:
                # Humans win: someone took the lease — stop touching that ticket.
                log.warning("lease on %s lost (human or orchestrator intervened)", key)
                ctx.extra_leased.discard(key)
            except Exception:
                log.exception("heartbeat failed for %s", key)

    async def _loop(self, ctx: ToolContext, kickoff: str) -> None:
        role = ctx.role
        system = build_system_prompt(self.settings, role, self.agent_user)
        messages: list[dict] = [{"role": "system", "content": system},
                                {"role": "user", "content": kickoff}]
        tools = tools_for_role(role)

        for turn in range(self.settings.max_agent_turns):
            msg = await self.llm.chat(messages, tools=tools, model=role.model,
                                      role=role.role_id)
            tool_calls = msg.tool_calls or []
            # Serialize only the canonical tool-call shape — model_dump() can carry
            # provider-specific extras that other LiteLLM backends reject on replay.
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                **({"tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name,
                                  "arguments": tc.function.arguments}}
                    for tc in tool_calls]} if tool_calls else {}),
            })

            if not tool_calls:
                # Model produced prose without a terminal action — remind it once per occurrence.
                messages.append({"role": "user", "content":
                                 "Reminder: every run must end with exactly one terminal tool "
                                 "call (transition_with_handoff / escalate / finish_run). "
                                 "Continue working or end the run now."})
                continue

            terminal = False
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError as e:
                    result = toolsmod.ToolResult(f"ERROR: arguments are not valid JSON: {e}")
                else:
                    result = await dispatch(ctx, tc.function.name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": result.content})
                log.debug("%s/%s tool %s -> %s", role.role_id, ctx.ticket,
                          tc.function.name, result.content[:200])
                if result.terminal:
                    terminal = True
            if terminal:
                self.audit.record("run_complete", role=role.role_id, ticket=ctx.ticket,
                                  turns=turn + 1)
                return

        # Turn cap hit: fail safe — release everything and record it.
        log.error("agent %s/%s hit the turn cap (%d) without a terminal action",
                  role.role_id, ctx.ticket, self.settings.max_agent_turns)
        self.audit.record("turn_cap_hit", role=role.role_id, ticket=ctx.ticket)
        for key in ([ctx.ticket] if ctx.ticket else []) + list(ctx.extra_leased):
            try:
                await self._bump_retries(key)
                await self.leases.release(key)
                await self.jira.add_comment(
                    key, f"[sentinel] {role.role_id} run aborted at turn cap without a "
                         f"terminal action; lease released. The orchestrator will retry once.")
            except Exception:
                log.warning("cleanup failed for %s", key)

    async def _bump_retries(self, ticket: str) -> None:
        retries = await self.jira.get_property(ticket, PROP_RETRIES) or {"count": 0}
        retries["count"] = int(retries.get("count", 0)) + 1
        await self.jira.set_property(ticket, PROP_RETRIES, retries)
