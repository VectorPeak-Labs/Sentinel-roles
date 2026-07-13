"""Tools exposed to role agents.

The tool layer is where the pipeline's universal rules are *enforced*, not just
described: a transition is impossible without a valid handoff payload, a move to
Rework is impossible without a valid rejection payload, and every pass-marked
checklist item must carry evidence (validated in sentinel.payloads).
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from .audit import AuditLog
from .config import RoleConfig, Settings
from .jira import (JiraClient, JiraError, PROP_DEPLOYED, PROP_LEASE, PROP_RETRIES,
                   PROP_REWORK, PROP_WAITING)
from .lease import LeaseManager
from .llm import LLM
from .notify import Notifier
from .payloads import find_payload, validate_handoff, validate_rejection

log = logging.getLogger("sentinel.tools")

MAX_TOOL_OUTPUT = 30_000
MAX_COMMENTS = 30
MAX_ATTACHMENT_BYTES = 20_000_000

# MIME types whose content is returned inline by get_attachment (everything else
# is binary: saved to the workspace only).
_TEXT_MIME_EXTRA = {"application/json", "application/xml", "application/yaml",
                    "application/x-yaml", "image/svg+xml"}


def _is_text_mime(mime: str) -> bool:
    return mime.startswith("text/") or mime in _TEXT_MIME_EXTRA


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} chars omitted]"


@dataclass
class ToolContext:
    jira: JiraClient
    llm: LLM
    leases: LeaseManager
    settings: Settings
    audit: AuditLog
    role: RoleConfig
    ticket: str | None            # the leased ticket for ticket-scoped roles, else None
    workspace: Path
    notifier: Notifier | None = None  # outbound alert channel (None = disabled)
    extra_leased: set[str] = field(default_factory=set)  # tickets a queue role leased itself

    @property
    def rework_status(self) -> str:
        router = self.settings.roles.get("13-rework-router")
        return router.statuses[0] if router and router.statuses else "Rework"

    def owns(self, key: str) -> bool:
        return key == self.ticket or key in self.extra_leased


@dataclass
class ToolResult:
    content: str
    terminal: bool = False


# --------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format)
# --------------------------------------------------------------------------

def _tool(name: str, description: str, params: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": params, "required": required},
    }}


_S = {"type": "string"}
_I = {"type": "integer"}
_SL = {"type": "array", "items": {"type": "string"}}

BASE_TOOLS = [
    _tool("get_ticket", "Fetch a ticket: fields, status, labels, links, sentinel state "
          "(lease / rework / waiting / deployed builds) and recent comments.",
          {"key": _S}, ["key"]),
    _tool("search_tickets", "Run a JQL search scoped to the project. Returns key, summary, "
          "status, labels, assignee per match.",
          {"jql": _S, "max_results": _I}, ["jql"]),
    _tool("add_comment", "Post a comment on a ticket (questions to humans, open-question "
          "lists, review packets, audit notes...).",
          {"key": _S, "body": _S}, ["key", "body"]),
    _tool("set_labels", "Add and/or remove labels on a ticket.",
          {"key": _S, "add": _SL, "remove": _SL}, ["key"]),
    _tool("get_attachment", "Download a ticket attachment (ids/filenames are listed by "
          "get_ticket). The file is saved into the workspace under attachments/; text "
          "content (text/*, json, xml, yaml, svg) is additionally returned inline.",
          {"key": _S, "attachment_id": _S}, ["key", "attachment_id"]),
    _tool("attach_file", "Attach a file to a ticket — the evidence channel of universal "
          "rule 5 (screenshots, scan reports, requirement documents, evidence bundles). "
          "Provide EITHER path (an existing file inside the workspace, e.g. produced by "
          "run_command) OR content (inline text) plus a filename.",
          {"key": _S,
           "path": {"type": "string", "description": "Workspace-relative path of the file to upload"},
           "content": {"type": "string", "description": "Inline text content (alternative to path)"},
           "filename": {"type": "string", "description": "Attachment filename (defaults to the path's basename)"}},
          ["key"]),
    _tool("create_ticket", "Create a new ticket in the project (follow-ups, new-scope "
          "requests, split slices). It lands in the icebox (initial status).",
          {"summary": _S, "description": _S, "issue_type": {"type": "string",
           "description": "Bug | Task | Story (must exist in the project)"}, "labels": _SL},
          ["summary", "description"]),
    _tool("link_tickets", "Link two tickets. link_type must exist in Jira (e.g. Relates, "
          "Blocks, Duplicate).",
          {"inward_key": _S, "outward_key": _S, "link_type": _S}, ["inward_key", "outward_key"]),
    _tool("assign_ticket", "Set the assignee of a ticket to a Jira username (e.g. hand a "
          "ticket to the PO). Use null-like empty string to unassign.",
          {"key": _S, "username": _S}, ["key", "username"]),
    _tool("set_deployed_build", "Record the deployed build for an environment on a ticket "
          "(the deployed_build custom field of 00-overview).",
          {"key": _S, "environment": {"type": "string", "description": "test | staging | production"},
           "build": _S}, ["key", "environment", "build"]),
    _tool("transition_with_handoff",
          "THE ONLY WAY to move a ticket to its next status. Validates the agent_handoff "
          "YAML against the 00-overview schema (checklist ids + evidence on every pass, "
          "assumptions list present), posts summary + payload as a comment, performs the "
          "Jira transition and releases the lease. Rejected with the exact validation "
          "errors if the payload is incomplete — fix and retry.",
          {"key": _S, "to_status": _S,
           "summary": {"type": "string", "description": "Human-readable handoff summary, posted above the YAML"},
           "handoff_yaml": {"type": "string", "description": "YAML document with top-level key agent_handoff"}},
          ["key", "to_status", "summary", "handoff_yaml"]),
    _tool("reject_to_rework",
          "Reject a ticket to Rework (review roles 08/10/11 only). Validates BOTH the "
          "rework rejection payload (findings with severity + criterion_ref + "
          "required_action + evidence) and the agent_handoff, posts them, transitions.",
          {"key": _S,
           "summary": {"type": "string", "description": "Human-readable rejection summary"},
           "rejection_yaml": {"type": "string", "description": "YAML document with top-level key rework"},
           "handoff_yaml": {"type": "string", "description": "YAML document with top-level key agent_handoff"}},
          ["key", "summary", "rejection_yaml", "handoff_yaml"]),
    _tool("escalate",
          "Escalation protocol: adds needs-human, posts the reason + specific decision "
          "needed, releases the lease and freezes the ticket until a human acts.",
          {"key": _S, "reason": _S,
           "decision_needed": {"type": "string", "description": "The specific decision a human must make"}},
          ["key", "reason", "decision_needed"]),
    _tool("finish_run",
          "End this run WITHOUT transitioning (e.g. questions posted and now waiting on a "
          "human, or nothing actionable in the queue). Releases the lease. Sentinel "
          "re-dispatches the role when the ticket is updated or after wake_hours.",
          {"summary": {"type": "string", "description": "What was done / what you are waiting for"},
           "wake_hours": {"type": "number", "description": "Re-dispatch after this many hours even without updates (default 24)"}},
          ["summary"]),
]

QUEUE_TOOLS = [
    _tool("claim_ticket", "Lease a ticket before acting on it (queue roles must claim each "
          "ticket they modify). Fails if another agent holds an active lease.",
          {"key": _S}, ["key"]),
    _tool("release_ticket", "Release a lease claimed with claim_ticket without transitioning.",
          {"key": _S}, ["key"]),
]

ROUTER_TOOLS = [
    _tool("increment_rework",
          "Rework Router only: read the ticket's rejection payload from its comments, "
          "increment rework_count, append to bounce history. Returns the new count and "
          "whether the loop-breaker limit is exceeded (if so: do NOT dispatch — escalate).",
          {"key": _S}, ["key"]),
]

SHELL_TOOLS = [
    _tool("run_command",
          "Run a shell command in the agent workspace (git, build, test, scan, deploy and "
          "curl are available). Returns exit code + stdout/stderr. The project-specific "
          "commands from config/pipeline.yml are listed in your instructions.",
          {"command": _S,
           "cwd": {"type": "string", "description": "Relative dir inside the workspace (default '.')"},
           "timeout_seconds": _I},
          ["command"]),
]

ESTIMATOR_TOOLS = [
    _tool("run_estimators",
          "Refinement only: run N independent, blind estimator instances (separate LLM "
          "contexts, no cross-talk). Each returns a story-point estimate with reasoning. "
          "Apply the convergence rule yourself afterwards.",
          {"ticket_context": {"type": "string", "description": "Ticket summary, AC, approach, subtasks"},
           "reference_set": {"type": "string", "description": "5-10 completed tickets with points and actuals"},
           "n": _I},
          ["ticket_context", "reference_set"]),
]


def tools_for_role(role: RoleConfig) -> list[dict]:
    tools = list(BASE_TOOLS)
    if role.trigger_type == "queue":
        tools += QUEUE_TOOLS
    if role.role_id.startswith("13-"):
        tools += ROUTER_TOOLS
    if role.shell:
        tools += SHELL_TOOLS
    if role.role_id.startswith("05-"):
        tools += ESTIMATOR_TOOLS
    return tools


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

async def dispatch(ctx: ToolContext, name: str, args: dict) -> ToolResult:
    handler = _HANDLERS.get(name)
    if handler is None:
        return ToolResult(f"ERROR: unknown tool '{name}'")
    try:
        return await handler(ctx, args)
    except JiraError as e:
        return ToolResult(f"ERROR: {e}")
    except Exception as e:  # tool errors go back to the model, never crash the run
        log.exception("tool %s failed", name)
        return ToolResult(f"ERROR: {type(e).__name__}: {e}")


async def _get_ticket(ctx: ToolContext, args: dict) -> ToolResult:
    key = args["key"]
    issue = await ctx.jira.get_issue(key)
    f = issue.get("fields", {})
    state = {}
    for prop in (PROP_LEASE, PROP_REWORK, PROP_WAITING, PROP_DEPLOYED):
        value = await ctx.jira.get_property(key, prop)
        if value is not None:
            state[prop.split(".", 1)[1]] = value
    comments = (f.get("comment") or {}).get("comments", [])[-MAX_COMMENTS:]
    out = {
        "key": issue.get("key"),
        "summary": f.get("summary"),
        "status": (f.get("status") or {}).get("name"),
        "issue_type": (f.get("issuetype") or {}).get("name"),
        "priority": (f.get("priority") or {}).get("name"),
        "labels": f.get("labels", []),
        "assignee": ((f.get("assignee") or {}).get("name")),
        "reporter": ((f.get("reporter") or {}).get("name")),
        "updated": f.get("updated"),
        "description": _truncate(f.get("description") or "", 8000),
        "links": [
            {"type": (l.get("type") or {}).get("name"),
             "outward": (l.get("outwardIssue") or {}).get("key"),
             "inward": (l.get("inwardIssue") or {}).get("key")}
            for l in f.get("issuelinks", [])
        ],
        "attachments": [
            {"id": a.get("id"), "filename": a.get("filename"), "size": a.get("size"),
             "mime_type": a.get("mimeType"),
             "author": (a.get("author") or {}).get("name"), "created": a.get("created")}
            for a in f.get("attachment") or []
        ],
        "sentinel_state": state,
        "comments": [
            {"author": (c.get("author") or {}).get("name"),
             "created": c.get("created"),
             "body": _truncate(c.get("body") or "", 4000)}
            for c in comments
        ],
    }
    return ToolResult(_truncate(json.dumps(out, ensure_ascii=False, indent=1)))


async def _search_tickets(ctx: ToolContext, args: dict) -> ToolResult:
    jql = args["jql"]
    if ctx.settings.jira_project.lower() not in jql.lower():
        jql = f"project = {ctx.settings.jira_project} AND ({jql})"
    issues = await ctx.jira.search(jql, max_results=min(int(args.get("max_results", 50)), 200))
    rows = [{
        "key": i.get("key"),
        "summary": (i.get("fields") or {}).get("summary"),
        "status": ((i.get("fields") or {}).get("status") or {}).get("name"),
        "labels": (i.get("fields") or {}).get("labels", []),
        "assignee": (((i.get("fields") or {}).get("assignee")) or {}).get("name"),
        "updated": (i.get("fields") or {}).get("updated"),
    } for i in issues]
    return ToolResult(_truncate(json.dumps({"count": len(rows), "issues": rows},
                                           ensure_ascii=False, indent=1)))


async def _add_comment(ctx: ToolContext, args: dict) -> ToolResult:
    comment = await ctx.jira.add_comment(args["key"], args["body"])
    return ToolResult(f"comment posted (id {comment.get('id')})")


async def _set_labels(ctx: ToolContext, args: dict) -> ToolResult:
    protected = {ctx.settings.label("leased")}
    add = [l for l in args.get("add") or [] if l not in protected]
    remove = [l for l in args.get("remove") or [] if l not in protected]
    await ctx.jira.update_labels(args["key"], add=add, remove=remove)
    return ToolResult(f"labels updated: +{add} -{remove}")


async def _get_attachment(ctx: ToolContext, args: dict) -> ToolResult:
    key, att_id = args["key"], str(args["attachment_id"])
    issue = await ctx.jira.get_issue(key, with_comments=False)
    attachments = (issue.get("fields") or {}).get("attachment") or []
    meta = next((a for a in attachments if str(a.get("id")) == att_id), None)
    if meta is None:
        ids = [f"{a.get('id')}:{a.get('filename')}" for a in attachments]
        return ToolResult(f"ERROR: no attachment with id {att_id} on {key}. "
                          f"Available: {ids or 'none'}")
    if int(meta.get("size") or 0) > MAX_ATTACHMENT_BYTES:
        return ToolResult(f"ERROR: attachment is {meta.get('size')} bytes "
                          f"(limit {MAX_ATTACHMENT_BYTES})")
    data = await ctx.jira.download_attachment(meta.get("content") or "")

    # Path(...).name strips any directory components a hostile filename could
    # carry — the file must land inside workspace/attachments, nowhere else.
    filename = Path(meta.get("filename") or f"attachment-{att_id}").name or f"attachment-{att_id}"
    dest_dir = ctx.workspace / "attachments"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    dest.write_bytes(data)

    mime = meta.get("mimeType") or ""
    out = (f"saved '{filename}' ({len(data)} bytes, {mime or 'unknown type'}) "
           f"to attachments/{filename} in the workspace")
    if _is_text_mime(mime):
        try:
            return ToolResult(_truncate(out + "\n--- content ---\n" + data.decode("utf-8")))
        except UnicodeDecodeError:
            pass
    return ToolResult(out)


async def _attach_file(ctx: ToolContext, args: dict) -> ToolResult:
    key = args["key"]
    path_arg, content = args.get("path"), args.get("content")
    if bool(path_arg) == (content is not None):
        return ToolResult("ERROR: provide exactly one of `path` (workspace file) or "
                          "`content` (inline text)")
    if path_arg:
        # Same path-aware containment rule as run_command's cwd.
        src = (ctx.workspace / path_arg).resolve()
        if not src.is_relative_to(ctx.workspace.resolve()):
            return ToolResult("ERROR: path must stay inside the workspace")
        if not src.is_file():
            return ToolResult(f"ERROR: no such file in the workspace: {path_arg}")
        data = src.read_bytes()
        filename = Path(args.get("filename") or src.name).name
    else:
        if not str(args.get("filename") or "").strip():
            return ToolResult("ERROR: filename is required with inline content")
        data = str(content).encode("utf-8")
        filename = Path(args["filename"]).name
    if not filename:
        return ToolResult("ERROR: filename must not be empty")
    if len(data) > MAX_ATTACHMENT_BYTES:
        return ToolResult(f"ERROR: file is {len(data)} bytes (limit {MAX_ATTACHMENT_BYTES})")

    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    await ctx.jira.upload_attachment(key, filename, data, content_type=mime)
    ctx.audit.record("attachment_uploaded", role=ctx.role.role_id, ticket=key,
                     filename=filename, size=len(data))
    return ToolResult(f"attached '{filename}' ({len(data)} bytes, {mime}) to {key}")


async def _create_ticket(ctx: ToolContext, args: dict) -> ToolResult:
    created = await ctx.jira.create_issue(
        ctx.settings.jira_project, args["summary"], args["description"],
        issue_type=args.get("issue_type", "Task"), labels=args.get("labels"))
    ctx.audit.record("ticket_created", by=ctx.role.role_id, key=created.get("key"),
                     summary=args["summary"])
    return ToolResult(f"created {created.get('key')}")


async def _link_tickets(ctx: ToolContext, args: dict) -> ToolResult:
    await ctx.jira.link_issues(args["inward_key"], args["outward_key"],
                               args.get("link_type", "Relates"))
    return ToolResult("linked")


async def _assign_ticket(ctx: ToolContext, args: dict) -> ToolResult:
    username = args.get("username") or None
    await ctx.jira.assign(args["key"], username)
    return ToolResult(f"assignee set to {username}")


async def _set_deployed_build(ctx: ToolContext, args: dict) -> ToolResult:
    key, env, build = args["key"], args["environment"], args["build"]
    deployed = await ctx.jira.get_property(key, PROP_DEPLOYED) or {}
    deployed[env] = {"build": build, "at": _now_iso(), "by": ctx.role.role_id}
    await ctx.jira.set_property(key, PROP_DEPLOYED, deployed)
    return ToolResult(f"deployed_build[{env}] = {build}")


async def _check_transition(ctx: ToolContext, key: str, to_status: str,
                            handoff_yaml: str) -> ToolResult | tuple[dict, str]:
    """Full pre-flight for a transition: ownership, payload schema, ticket/status
    consistency. Returns (payload, current_status) or an error ToolResult."""
    if ctx.role.trigger_type == "queue" and not ctx.owns(key):
        return ToolResult(f"ERROR: claim_ticket({key}) before transitioning it")

    try:
        doc = yaml.safe_load(handoff_yaml)
    except yaml.YAMLError as e:
        return ToolResult(f"ERROR: handoff_yaml does not parse: {e}")
    payload = (doc or {}).get("agent_handoff") if isinstance(doc, dict) else None
    result = validate_handoff(payload)
    if not result.ok:
        return ToolResult("ERROR: handoff payload invalid:\n- " + "\n- ".join(result.errors))
    if payload["ticket"] != key:
        return ToolResult(f"ERROR: handoff.ticket is {payload['ticket']}, expected {key}")
    if payload["to_status"].lower() != to_status.lower():
        return ToolResult("ERROR: handoff.to_status does not match to_status argument")

    issue = await ctx.jira.get_issue(key, with_comments=False)
    current = ((issue.get("fields") or {}).get("status") or {}).get("name", "")
    if payload["from_status"].lower() != current.lower():
        return ToolResult(f"ERROR: ticket is in '{current}' but handoff.from_status says "
                          f"'{payload['from_status']}' — re-read the ticket, someone moved it")

    # Verify the workflow actually has an edge to the target BEFORE posting anything:
    # otherwise the handoff comment lands on a ticket that then fails to move,
    # leaving an orphaned payload (once per retry).
    transitions = await ctx.jira.list_transitions(key)
    targets = [(t.get("to") or {}).get("name", "") for t in transitions]
    if to_status.lower() not in (t.lower() for t in targets):
        return ToolResult(f"ERROR: the Jira workflow has no transition from '{current}' "
                          f"to '{to_status}' for {key}. Available targets: {targets}. "
                          f"If the pipeline requires this transition, escalate — the "
                          f"workflow configuration is missing an edge.")
    return payload, current


async def _transition_with_handoff(ctx: ToolContext, args: dict) -> ToolResult:
    key, to_status = args["key"], args["to_status"]
    checked = await _check_transition(ctx, key, to_status, args["handoff_yaml"])
    if isinstance(checked, ToolResult):
        return checked
    payload, current = checked

    body = f"{args['summary']}\n\n{{code:yaml}}\n{yaml.safe_dump({'agent_handoff': payload}, sort_keys=False)}{{code}}"
    await ctx.jira.add_comment(key, body)
    await ctx.jira.transition_to(key, to_status)
    await ctx.jira.delete_property(key, PROP_WAITING)
    await ctx.jira.delete_property(key, PROP_RETRIES)  # clean stage exit resets crash retries
    await ctx.leases.release(key)
    ctx.extra_leased.discard(key)
    ctx.audit.record("transition", role=ctx.role.role_id, ticket=key,
                     from_status=current, to_status=to_status, verdict=payload["verdict"])
    terminal = ctx.role.trigger_type == "ticket" and key == ctx.ticket
    return ToolResult(f"{key} transitioned '{current}' -> '{to_status}', lease released",
                      terminal=terminal)


async def _reject_to_rework(ctx: ToolContext, args: dict) -> ToolResult:
    key = args["key"]
    try:
        rework_doc = yaml.safe_load(args["rejection_yaml"])
    except yaml.YAMLError as e:
        return ToolResult(f"ERROR: rejection_yaml does not parse: {e}")
    rework = (rework_doc or {}).get("rework") if isinstance(rework_doc, dict) else None
    rejection = validate_rejection(rework)
    if not rejection.ok:
        return ToolResult("ERROR: rejection payload invalid:\n- " + "\n- ".join(rejection.errors))

    # Pre-flight the handoff too BEFORE posting anything: otherwise an invalid handoff
    # would leave an orphaned rework payload on a ticket that never moved (and a
    # duplicate payload on the retry, which the Rework Router then parses).
    checked = await _check_transition(ctx, key, ctx.rework_status, args["handoff_yaml"])
    if isinstance(checked, ToolResult):
        return checked

    # Post the rejection payload first (the Rework Router's input), then handoff+transition
    body = (f"{args['summary']}\n\n{{code:yaml}}\n"
            f"{yaml.safe_dump({'rework': rejection.payload}, sort_keys=False)}{{code}}")
    await ctx.jira.add_comment(key, body)
    ctx.audit.record("rejection", role=ctx.role.role_id, ticket=key,
                     rejected_from=rejection.payload["rejected_from"],
                     findings=len(rejection.payload["findings"]))
    return await _transition_with_handoff(ctx, {
        "key": key, "to_status": ctx.rework_status,
        "summary": f"Rejected to Rework ({len(rejection.payload['findings'])} finding(s), "
                   f"see rejection payload above).",
        "handoff_yaml": args["handoff_yaml"],
    })


async def _escalate(ctx: ToolContext, args: dict) -> ToolResult:
    key = args["key"]
    await ctx.jira.update_labels(key, add=[ctx.settings.label("needs_human")])
    await ctx.jira.add_comment(
        key,
        f"[sentinel] ESCALATION from {ctx.role.role_id}\n\n"
        f"*Reason:* {args['reason']}\n\n"
        f"*Decision needed from a human:* {args['decision_needed']}\n\n"
        f"The ticket is frozen (label {ctx.settings.label('needs_human')}) until a human "
        f"acts and removes the label.")
    await ctx.leases.release(key)
    ctx.extra_leased.discard(key)
    ctx.audit.record("escalation", role=ctx.role.role_id, ticket=key, reason=args["reason"],
                     decision_needed=args["decision_needed"])
    if ctx.notifier is not None:
        await ctx.notifier.notify(
            "agent_escalation",
            f"🚨 {ctx.settings.jira_project} {key} escalated by {ctx.role.role_id} — "
            f"needs a human. {args['reason']} → {args['decision_needed']}",
            ticket=key, source=ctx.role.role_id, reason=args["reason"],
            decision_needed=args["decision_needed"])
    terminal = ctx.role.trigger_type == "ticket" and key == ctx.ticket
    return ToolResult(f"{key} escalated and frozen", terminal=terminal)


async def _finish_run(ctx: ToolContext, args: dict) -> ToolResult:
    wake_hours = float(args.get("wake_hours") or 24)
    if ctx.ticket:
        wake_at = (datetime.now(timezone.utc) + timedelta(hours=wake_hours)) \
            .isoformat(timespec="seconds")
        await ctx.jira.set_property(ctx.ticket, PROP_WAITING, {
            "since": _now_iso(), "reason": args["summary"],
            "role": ctx.role.role_id, "wake_at": wake_at,
        })
        await ctx.leases.release(ctx.ticket)
    for key in list(ctx.extra_leased):
        await ctx.leases.release(key)
        ctx.extra_leased.discard(key)
    ctx.audit.record("run_finished_waiting", role=ctx.role.role_id, ticket=ctx.ticket,
                     summary=args["summary"])
    return ToolResult("run ended without transition", terminal=True)


async def _claim_ticket(ctx: ToolContext, args: dict) -> ToolResult:
    key = args["key"]
    await ctx.leases.claim(key, ctx.role.role_id)
    ctx.extra_leased.add(key)
    return ToolResult(f"lease claimed on {key}")


async def _release_ticket(ctx: ToolContext, args: dict) -> ToolResult:
    key = args["key"]
    await ctx.leases.release(key)
    ctx.extra_leased.discard(key)
    return ToolResult(f"lease released on {key}")


async def _increment_rework(ctx: ToolContext, args: dict) -> ToolResult:
    key = args["key"]
    comments = await ctx.jira.get_comments(key)
    rejection = None
    rejection_comment_id = None
    for comment in reversed(comments):
        rejection = find_payload(comment.get("body") or "", "rework")
        if rejection:
            rejection_comment_id = str(comment.get("id", ""))
            break
    if rejection is None:
        return ToolResult("ERROR: no `rework` rejection payload found in the ticket's "
                          "comments — bounce the ticket back to the rejecting role with "
                          "the handoff-invalid label (role 13 input validation)")
    validated = validate_rejection(rejection)
    if not validated.ok:
        return ToolResult("ERROR: rejection payload malformed:\n- " + "\n- ".join(validated.errors)
                          + "\nBounce back to the rejecting role with handoff-invalid.")

    state = await ctx.jira.get_property(key, PROP_REWORK) or {"count": 0, "history": []}

    # Idempotency: a crashed/retried router run must not count the same rejection
    # twice — phantom bounces would trip the loop-breaker early.
    already_counted = (rejection_comment_id and
                       state.get("last_counted_comment") == rejection_comment_id)
    if not already_counted:
        state["count"] = int(state.get("count", 0)) + 1
        state["rejected_from"] = rejection["rejected_from"]
        state["last_counted_comment"] = rejection_comment_id
        state.setdefault("history", []).append({
            "at": _now_iso(), "rejected_from": rejection["rejected_from"],
            "findings": [{"id": f.get("id"), "severity": f.get("severity"),
                          "criterion_ref": f.get("criterion_ref"),
                          "description": f.get("description")} for f in rejection["findings"]],
        })
        await ctx.jira.set_property(key, PROP_REWORK, state)
    exceeded = state["count"] > ctx.settings.rework_limit
    ctx.audit.record("rework_incremented", ticket=key, count=state["count"],
                     exceeded=exceeded, already_counted=already_counted)
    return ToolResult(json.dumps({
        "rework_count": state["count"], "limit": ctx.settings.rework_limit,
        "limit_exceeded": exceeded, "rejected_from": state["rejected_from"],
        "already_counted": bool(already_counted),
        "findings": len(rejection["findings"]),
        "bounce_history": state["history"],
    }, ensure_ascii=False, indent=1))


async def _run_command(ctx: ToolContext, args: dict) -> ToolResult:
    if not ctx.role.shell:
        return ToolResult("ERROR: this role has no shell access")
    cwd = (ctx.workspace / (args.get("cwd") or ".")).resolve()
    # Path-aware containment check — a string prefix comparison would accept
    # sibling directories that share the prefix (workspace "07" vs "07-evil").
    if not cwd.is_relative_to(ctx.workspace.resolve()):
        return ToolResult("ERROR: cwd must stay inside the workspace")
    cwd.mkdir(parents=True, exist_ok=True)
    timeout = min(int(args.get("timeout_seconds") or 600), 1800)
    proc = await asyncio.create_subprocess_shell(
        args["command"], cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ToolResult(f"ERROR: command timed out after {timeout}s")
    out = (f"exit_code: {proc.returncode}\n"
           f"--- stdout ---\n{stdout.decode(errors='replace')}\n"
           f"--- stderr ---\n{stderr.decode(errors='replace')}")
    return ToolResult(_truncate(out))


_ESTIMATOR_PROMPT = """You are an independent story-point estimator in a blind planning-poker round.
Estimate the ticket below on the project's point scale, calibrated against the reference set.
You see no other estimator's work. Price the risky work, not just the visible work.

Respond with exactly:
ESTIMATE: <points>
REASONING: <3-6 sentences: main cost drivers, risks priced in, closest reference ticket>"""


async def _run_estimators(ctx: ToolContext, args: dict) -> ToolResult:
    n = min(int(args.get("n") or ctx.role.estimators), 5)
    prompt = (f"## Ticket\n{args['ticket_context']}\n\n"
              f"## Reference set (completed tickets: points and actuals)\n{args['reference_set']}")

    async def one(i: int) -> dict:
        try:
            msg = await ctx.llm.chat(
                [{"role": "system", "content": _ESTIMATOR_PROMPT},
                 {"role": "user", "content": prompt}],
                model=ctx.role.model, temperature=1.0)
            return {"estimator": i + 1, "response": (msg.content or "").strip()}
        except Exception as e:
            return {"estimator": i + 1, "error": str(e)}

    results = await asyncio.gather(*(one(i) for i in range(n)))
    return ToolResult(_truncate(json.dumps({"estimates": list(results)},
                                           ensure_ascii=False, indent=1)))


_HANDLERS = {
    "get_ticket": _get_ticket,
    "search_tickets": _search_tickets,
    "add_comment": _add_comment,
    "set_labels": _set_labels,
    "get_attachment": _get_attachment,
    "attach_file": _attach_file,
    "create_ticket": _create_ticket,
    "link_tickets": _link_tickets,
    "assign_ticket": _assign_ticket,
    "set_deployed_build": _set_deployed_build,
    "transition_with_handoff": _transition_with_handoff,
    "reject_to_rework": _reject_to_rework,
    "escalate": _escalate,
    "finish_run": _finish_run,
    "claim_ticket": _claim_ticket,
    "release_ticket": _release_ticket,
    "increment_rework": _increment_rework,
    "run_command": _run_command,
    "run_estimators": _run_estimators,
}
