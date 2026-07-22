"""Deterministic end-to-end demo: ``python -m sentinel.demo``.

Runs a representative ticket through the Sentinel pipeline **without Jira,
LiteLLM, project commands, or any secret** — so a new contributor can watch the
workflow operate end to end in a few seconds, and CI can guard the happy path
from regressing.

What is real vs. faked
----------------------
The demo drives the **real** control plane: the actual :class:`Orchestrator`
sweep + dispatch gating, the real :class:`AgentRunner` tool loop, the real
handoff-payload validation in :mod:`sentinel.payloads`, the real
:class:`LeaseManager`, and the real :class:`AuditLog`. Only the two external
dependencies are replaced by in-memory stand-ins:

- :class:`InMemoryJira` — a dict-backed board supporting just the calls the
  pipeline makes (properties, labels, comments, transitions, JQL-ish search).
- :class:`ScriptedLLM` — plays a fixed, per-role script of tool calls
  (``get_ticket`` → ``transition_with_handoff`` with a schema-valid handoff),
  so every transition still has to pass the same validation a live model would.

Because dispatch, validation, leasing, and audit are the real code, a green demo
is meaningful: if a change breaks the handoff schema, the dispatch table, or the
lease/transition contract, the demo (and its test) fails.

The scripted flow walks one ticket from the icebox to a reviewed change:

    New (activate) → Business Requirements → Technical Requirements →
    Technical Refinement → To Do → In Progress → Tech Review →
    Tech Review Accepted

covering both ticket roles and the queue-based Sprint Planner (06), plus the two
shell roles (Implementer 07, Code Reviewer 08).
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

from .audit import AuditLog, format_timeline
from .config import Settings, load_settings
from .metrics import Metrics
from .orchestrator import Orchestrator
from .payloads import find_payload

# The demo ticket's journey: (role_id, from_status, to_status). Each entry is one
# dispatch → transition. The Orchestrator picks the role from the status, so this
# table only has to agree with config/pipeline.yml (the test asserts it does).
FLOW: tuple[tuple[str, str, str], ...] = (
    ("02-intake-triage",         "New",                    "Business Requirements"),
    ("03-business-analyst",      "Business Requirements",  "Technical Requirements"),
    ("04-tech-lead-debrief",     "Technical Requirements", "Technical Refinement"),
    ("05-refinement-estimation", "Technical Refinement",   "To Do"),
    ("06-sprint-planner",        "To Do",                  "In Progress"),
    ("07-implementer",           "In Progress",            "Tech Review"),
    ("08-code-reviewer",         "Tech Review",            "Tech Review Accepted"),
)
TARGET_STATUS = FLOW[-1][2]

# A short, human-readable checklist evidence line per role, so the handoff
# payloads read like a real (if terse) pipeline rather than filler.
_EVIDENCE: dict[str, str] = {
    "02-intake-triage": "triage complete: type/priority set, open questions posted",
    "03-business-analyst": "acceptance criteria written and confirmed with the PO",
    "04-tech-lead-debrief": "technical approach and subtasks recorded",
    "05-refinement-estimation": "blind planning-poker converged on 3 points",
    "06-sprint-planner": "pulled into the sprint; In Progress has capacity",
    "07-implementer": "change implemented; project test suite green",
    "08-code-reviewer": "review passed: AC mapped, CI green, scans clean",
}

_ESTIMATOR_MARKER = "You are an independent story-point estimator"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tool_call(call_id: str, name: str, args: dict) -> SimpleNamespace:
    import json
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _assistant(content: str | None = None, tool_calls: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(content=content, tool_calls=tool_calls or None)


# --------------------------------------------------------------------------- #
# In-memory Jira board
# --------------------------------------------------------------------------- #

class InMemoryJira:
    """Dict-backed Jira supporting exactly the calls the pipeline makes.

    Deliberately self-contained (no dependency on the test-suite fakes) so the
    demo runs as a plain module. ``search`` interprets the pipeline's JQL by the
    only feature it relies on — the quoted status name(s) present in the query.
    """

    # Superset of the pipeline statuses; list_transitions offers them all, so the
    # workflow-edge check in the transition tool always finds the target.
    PIPELINE_STATUSES = [
        "New", "On Hold", "Business Requirements", "Technical Requirements",
        "Technical Refinement", "To Do", "In Progress", "Tech Review",
        "Tech Review Accepted", "Internal Review", "Internal Review Accepted",
        "Client Review", "Client Review Accepted", "Rework", "Done"]

    def __init__(self, agent_user: str = "sentinel-bot"):
        self.agent_user = agent_user
        self.issues: dict[str, dict] = {}
        self.comments: dict[str, list[dict]] = {}
        self.properties: dict[tuple[str, str], object] = {}
        self.transitions: list[tuple[str, str]] = []

    # -- seeding -----------------------------------------------------------
    def seed(self, key: str, *, summary: str, status: str, labels: list[str]) -> None:
        self.issues[key] = {"key": key, "fields": {
            "summary": summary, "status": {"name": status},
            "issuetype": {"name": "Story"}, "labels": list(labels),
            "reporter": {"name": "product-owner"},
            "updated": _now_iso()}}

    def status_of(self, key: str) -> str:
        return self.issues[key]["fields"]["status"]["name"]

    def labels_of(self, key: str) -> list[str]:
        return list(self.issues[key]["fields"].get("labels", []))

    # -- identity / search -------------------------------------------------
    async def myself(self) -> dict:
        return {"name": self.agent_user}

    async def search(self, jql: str, max_results: int = 100, fields=None) -> list[dict]:
        out = []
        for key, issue in self.issues.items():
            status = issue["fields"]["status"]["name"]
            if f'"{status}"'.lower() in jql.lower():
                out.append(copy.deepcopy(issue))
        return out[:max_results]

    # -- issue / comments --------------------------------------------------
    async def get_issue(self, key: str, with_comments: bool = True) -> dict:
        issue = copy.deepcopy(self.issues[key])
        if with_comments:
            issue["fields"]["comment"] = {"comments": [
                {"author": {"name": c["author"]}, "created": c["at"], "body": c["body"]}
                for c in self.comments.get(key, [])]}
        return issue

    async def add_comment(self, key: str, body: str) -> dict:
        lst = self.comments.setdefault(key, [])
        lst.append({"author": self.agent_user, "at": _now_iso(), "body": body})
        self.issues[key]["fields"]["updated"] = _now_iso()
        return {"id": str(len(lst))}

    async def get_comments(self, key: str) -> list[dict]:
        return [{"id": str(i + 1), "body": c["body"]}
                for i, c in enumerate(self.comments.get(key, []))]

    async def assign(self, key: str, username) -> None:
        self.issues[key]["fields"]["assignee"] = {"name": username} if username else None

    async def update_labels(self, key: str, add=None, remove=None) -> None:
        labels = self.issues[key]["fields"].setdefault("labels", [])
        for label in add or []:
            if label not in labels:
                labels.append(label)
        for label in remove or []:
            if label in labels:
                labels.remove(label)

    # -- properties (sentinel state) --------------------------------------
    async def get_property(self, key: str, prop: str):
        return self.properties.get((key, prop))

    async def set_property(self, key: str, prop: str, value) -> None:
        self.properties[(key, prop)] = value

    async def delete_property(self, key: str, prop: str) -> None:
        self.properties.pop((key, prop), None)

    # -- transitions -------------------------------------------------------
    async def list_transitions(self, key: str) -> list[dict]:
        return [{"id": str(i + 1), "to": {"name": s}}
                for i, s in enumerate(self.PIPELINE_STATUSES)]

    async def transition_to(self, key: str, target_status: str) -> None:
        self.issues[key]["fields"]["status"]["name"] = target_status
        self.issues[key]["fields"]["updated"] = _now_iso()
        self.transitions.append((key, target_status))


# --------------------------------------------------------------------------- #
# Scripted LLM
# --------------------------------------------------------------------------- #

class ScriptedLLM:
    """Plays a fixed per-role tool-call script; makes no network calls.

    The role agent loop calls :meth:`chat` once per turn. We key the response on
    the dispatched ``role`` and the turn index (number of assistant messages so
    far), returning the next scripted tool call. Refinement (05) additionally
    fans out to blind estimator sub-calls, which arrive here with the estimator
    system prompt and no tools — those get a canned point estimate.
    """

    def __init__(self, project: str):
        self.project = project
        self.calls = 0
        self.consecutive_failures = 0   # read by the orchestrator's LLM gate
        self._key_re = re.compile(rf"{re.escape(project)}-\d+")

    def _steps(self, role: str) -> list[str]:
        if role.startswith("06-"):                      # queue role
            return ["claim", "transition", "finish"]
        if role.startswith("05-"):                      # refinement runs estimators
            return ["get_ticket", "estimate", "transition"]
        return ["get_ticket", "transition"]

    def _ticket_key(self, messages: list[dict]) -> str:
        for m in messages:
            if m.get("role") == "user":
                match = self._key_re.search(m.get("content") or "")
                if match:
                    return match.group(0)
        return f"{self.project}-1"

    async def chat(self, messages, tools=None, model=None, temperature=None,
                   role=None):
        self.calls += 1
        system = messages[0]["content"] if messages else ""
        if _ESTIMATOR_MARKER in system:
            return _assistant(content="ESTIMATE: 3\nREASONING: Demo blind estimate; "
                                      "risk is modest and it matches a 3-point reference "
                                      "ticket in the calibration set.")

        key = self._ticket_key(messages)
        frm, to = self._transition_for(role)
        turn = sum(1 for m in messages if m.get("role") == "assistant")
        steps = self._steps(role)
        step = steps[min(turn, len(steps) - 1)]
        cid = f"call-{role}-{turn}"

        if step == "get_ticket":
            return _assistant(content=f"Reading {key}.",
                              tool_calls=[_tool_call(cid, "get_ticket", {"key": key})])
        if step == "claim":
            return _assistant(content=f"Claiming {key} from the queue.",
                              tool_calls=[_tool_call(cid, "claim_ticket", {"key": key})])
        if step == "estimate":
            return _assistant(
                content="Running blind planning-poker estimators.",
                tool_calls=[_tool_call(cid, "run_estimators", {
                    "ticket_context": f"{key}: add CSV export to the reports page.",
                    "reference_set": "SENT-REF-1 (2pts), SENT-REF-2 (3pts), SENT-REF-3 (5pts)",
                    "n": 3})])
        if step == "finish":
            return _assistant(
                content="Queue handled.",
                tool_calls=[_tool_call(cid, "finish_run",
                                       {"summary": f"{key} pulled into the sprint."})])

        # step == "transition"
        summary = f"{role}: {frm} → {to} (demo)."
        return _assistant(
            content=f"Handing off {key} to {to}.",
            tool_calls=[_tool_call(cid, "transition_with_handoff", {
                "key": key, "to_status": to, "summary": summary,
                "handoff_yaml": _handoff_yaml(role, key, frm, to)})])

    def _transition_for(self, role: str) -> tuple[str, str]:
        for role_id, frm, to in FLOW:
            if role_id == role:
                return frm, to
        raise KeyError(f"no scripted transition for role {role}")


def _handoff_yaml(role: str, key: str, frm: str, to: str) -> str:
    """A schema-valid ``agent_handoff`` document (validated by the real tools)."""
    code = role.split("-", 1)[0]
    payload = {"agent_handoff": {
        "role": role, "ticket": key, "timestamp": _now_iso(), "verdict": "pass",
        "from_status": frm, "to_status": to,
        "checklist": [{"id": f"{code}-1", "result": "pass",
                       "evidence": _EVIDENCE.get(role, "demo evidence")}],
        "assumptions": [],
        "outputs": {"note": f"{role} completed the {frm} stage in the demo run"}}}
    return yaml.safe_dump(payload, sort_keys=False)


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

@dataclass
class DemoResult:
    ticket_key: str
    summary: str
    final_status: str
    transitions: list[tuple[str, str, str]] = field(default_factory=list)  # (role, from, to)
    handoffs: list[dict] = field(default_factory=list)                     # agent_handoff payloads
    audit_records: list[dict] = field(default_factory=list)
    escalated: bool = False


async def run_demo(*, settings: Settings | None = None, max_rounds: int = 12) -> DemoResult:
    """Run the scripted ticket through the real pipeline and return the outcome.

    Sets placeholder (non-secret) env vars if the required ones are unset, so the
    demo is runnable from a clean checkout. All state lives in a throwaway temp
    directory that is removed before returning.
    """
    for name, value in (("JIRA_BASE_URL", "https://demo.invalid"),
                        ("JIRA_PAT", "demo"), ("JIRA_PROJECT_KEY", "SENT"),
                        ("LITELLM_BASE_URL", "https://demo.invalid"),
                        ("LITELLM_API_KEY", "demo")):
        os.environ.setdefault(name, value)

    settings = settings or load_settings("config/pipeline.yml")
    workdir = Path(tempfile.mkdtemp(prefix="sentinel-demo-"))
    project = settings.jira_project
    key = f"{project}-1"
    summary = "Add CSV export to the reports page"
    try:
        settings.data_dir = workdir
        jira = InMemoryJira()
        jira.seed(key, summary=summary, status="New", labels=[settings.label("activate")])
        audit = AuditLog(workdir / "audit.jsonl", max_bytes=0)
        llm = ScriptedLLM(project)
        orch = Orchestrator(settings, jira, llm=llm, audit=audit, metrics=Metrics())
        await orch.start()

        transitions: list[tuple[str, str, str]] = []
        for _ in range(max_rounds):
            before = jira.status_of(key)
            await orch.sweep()
            dispatched = [role_id for (role_id, _t) in orch.running]
            tasks = [task for task, _s in orch.running.values()]
            if tasks:
                await asyncio.gather(*tasks)
            orch._gc_running()
            after = jira.status_of(key)
            if after != before and dispatched:
                transitions.append((dispatched[0], before, after))
            if after == TARGET_STATUS:
                break
            if after == before and not tasks:
                break  # nothing moved and nothing running — pipeline is quiescent

        records = audit.read_records(limit=1000)
        handoffs: list[dict] = []
        for c in jira.comments.get(key, []):
            payload = find_payload(c["body"], "agent_handoff")
            if payload:
                handoffs.append(payload)
        escalated = settings.label("needs_human") in jira.labels_of(key)
        return DemoResult(ticket_key=key, summary=summary,
                          final_status=jira.status_of(key), transitions=transitions,
                          handoffs=handoffs, audit_records=records, escalated=escalated)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render_transcript(result: DemoResult) -> str:
    lines = [
        "=== Sentinel end-to-end demo ===",
        f"Ticket {result.ticket_key}: \"{result.summary}\"",
        "Seeded in status 'New' with the 'activate' label; no Jira, LiteLLM, or secrets used.",
        "",
        "Pipeline walk (each step is a real dispatch + validated handoff):",
    ]
    prev = "New"
    for i, (role, frm, to) in enumerate(result.transitions, 1):
        lines.append(f"  {i}. {role:<26} {frm:<24} -> {to}")
        prev = to
    lines += [
        "",
        f"Final status: {prev}"
        + ("  (reached target)" if result.final_status == TARGET_STATUS else ""),
        f"Handoff payloads posted: {len(result.handoffs)}"
        + ("  — all schema-valid (the tools reject invalid ones)" if result.handoffs else ""),
        f"Escalations: {'yes' if result.escalated else 'none'}",
        "",
        "Audit timeline:",
        format_timeline(result.audit_records) or "  (empty)",
    ]
    return "\n".join(lines)


def render_markdown(result: DemoResult) -> str:
    lines = [
        "# Sentinel end-to-end demo",
        "",
        f"**Ticket {result.ticket_key}** — {result.summary}  ",
        "Run entirely in-memory: no Jira, LiteLLM, project commands, or secrets.",
        "",
        "## Pipeline walk",
        "",
        "| # | Role | From | To |",
        "| - | ---- | ---- | -- |",
    ]
    for i, (role, frm, to) in enumerate(result.transitions, 1):
        lines.append(f"| {i} | `{role}` | {frm} | {to} |")
    lines += [
        "",
        f"**Final status:** {result.final_status}  ",
        f"**Handoff payloads (schema-validated):** {len(result.handoffs)}  ",
        f"**Escalations:** {'yes' if result.escalated else 'none'}",
        "",
        "## Audit timeline",
        "",
        "```",
        format_timeline(result.audit_records) or "(empty)",
        "```",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sentinel.demo",
        description="Run a deterministic end-to-end pipeline demo (no Jira/LiteLLM/secrets).")
    parser.add_argument("--markdown", type=Path, default=None,
                        help="also write a Markdown transcript to this path")
    parser.add_argument("--quiet", action="store_true",
                        help="print only the final status line")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    result = asyncio.run(run_demo())

    if args.markdown:
        args.markdown.write_text(render_markdown(result), encoding="utf-8")
    if args.quiet:
        print(f"final status: {result.final_status} "
              f"({'ok' if result.final_status == TARGET_STATUS else 'INCOMPLETE'})")
    else:
        print(render_transcript(result))
        if args.markdown:
            print(f"\nMarkdown transcript written to {args.markdown}")

    # Exit non-zero if the demo did not complete its scripted journey — makes it
    # usable as a CI smoke check as well as a human-facing walkthrough.
    return 0 if result.final_status == TARGET_STATUS and not result.escalated else 1


if __name__ == "__main__":
    sys.exit(main())
