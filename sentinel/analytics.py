"""Workflow analytics CLI: ``python -m sentinel.analytics --since 7d``.

Turns the append-only audit trail into a delivery-lead view of the pipeline:
throughput, where work waits (stage bottlenecks + explicit waits), how often it
bounces (rework), how often agents escalate, and reliability (crashes, turn-cap
aborts, lease reclaims). It reuses :meth:`AuditLog.read_records` and touches no
external service, so it runs offline like ``python -m sentinel.audit`` / doctor.

Data source and limitations
---------------------------
Everything here is **derived from the audit JSONL** — the single source Sentinel
already writes on its hot paths — so it needs no Jira calls and no new database
(both explicit constraints of the feature). Consequences worth stating plainly:

- **Stage durations are audit-derived approximations**: the time a ticket spent
  in a status is measured between the ``transition`` that entered it and the
  ``transition`` that left it. A status a ticket was *parked* in by a human, or
  that predates the window, has no entry event and is not counted. This is not a
  substitute for Jira status-history; it is a cheap, always-available proxy.
- **Live board state is out of scope here** — current queue depth, the current
  count/age of ``needs-human`` tickets, and token/gate state are point-in-time
  and belong to ``/ops.json`` and ``/metrics``. Analytics is the *historical*,
  flow-over-a-window view; the ops surfaces are the *right now* view.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .audit import DEFAULT_BACKUP_COUNT, AuditLog, _default_audit_path

_DURATION = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

# Escalation reasons carry a ticket key + free text; strip the noisiest bits so
# frequency counting groups like-with-like instead of treating every ticket as
# unique.
_TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


def parse_since(spec: str, *, now: datetime | None = None) -> datetime:
    """Resolve ``--since`` to a UTC datetime. Accepts a relative duration
    (``7d`` / ``24h`` / ``30m`` / ``90s`` / ``2w``) or an ISO-8601 date/datetime."""
    now = now or datetime.now(timezone.utc)
    m = _DURATION.match(spec)
    if m:
        return now - timedelta(seconds=int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()])
    try:
        dt = datetime.fromisoformat(spec)
    except ValueError as e:
        raise ValueError(f"invalid --since '{spec}': use e.g. 7d, 24h, 30m, or an ISO date") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _summarize_seconds(seconds: list[float]) -> dict:
    n = len(seconds)
    ordered = sorted(seconds)
    p50 = ordered[n // 2] if n else 0.0
    return {"count": n,
            "avg_seconds": round(sum(seconds) / n, 1) if n else 0.0,
            "p50_seconds": round(p50, 1),
            "max_seconds": round(max(seconds), 1) if n else 0.0}


@dataclass
class Analytics:
    records: int = 0
    since: str | None = None
    until: str | None = None
    throughput: dict = field(default_factory=dict)
    stage_durations: dict = field(default_factory=dict)   # status -> summary
    waits: dict = field(default_factory=dict)
    escalations: dict = field(default_factory=dict)
    rework: dict = field(default_factory=dict)
    reliability: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "window": {"since": self.since, "until": self.until, "records": self.records},
            "throughput": self.throughput,
            "stage_durations": self.stage_durations,
            "waits": self.waits,
            "escalations": self.escalations,
            "rework": self.rework,
            "reliability": self.reliability,
        }


def compute_analytics(records: list[dict], done_status: str = "Done") -> Analytics:
    """Fold audit records (any order) into the workflow report. Pure: no I/O."""
    a = Analytics(records=len(records))
    ats = [r.get("at") for r in records if r.get("at")]
    if ats:
        a.since, a.until = min(ats), max(ats)

    transitions = [r for r in records if r.get("event") == "transition"]
    dispatches = [r for r in records if r.get("event") == "dispatch"]

    # -- throughput --------------------------------------------------------
    by_to_status: Counter = Counter(r.get("to_status") or "?" for r in transitions)
    a.throughput = {
        "transitions": len(transitions),
        "completed": by_to_status.get(done_status, 0),
        "by_to_status": dict(by_to_status.most_common()),
        "dispatches": len(dispatches),
        "dispatches_by_role": dict(Counter(r.get("role") or "?" for r in dispatches).most_common()),
    }

    # -- stage durations (bottlenecks) ------------------------------------
    # Per ticket, pair the entry into a status (to_status) with the later exit
    # (from_status) and accumulate the gap. Both agent and human transitions
    # count, so a ticket parked by a human between two agent moves is included.
    per_status: dict[str, list[float]] = {}
    trans_by_ticket: dict[str, list[dict]] = {}
    for r in transitions + [r for r in records if r.get("event") == "human_transition"]:
        key = r.get("ticket")
        if key:
            trans_by_ticket.setdefault(key, []).append(r)
    for events in trans_by_ticket.values():
        events.sort(key=lambda r: r.get("at") or "")
        entered: dict[str, datetime] = {}
        for r in events:
            at = _parse_at(r.get("at"))
            if at is None:
                continue
            frm, to = r.get("from_status"), r.get("to_status")
            if frm and frm in entered:
                delta = (at - entered.pop(frm)).total_seconds()
                if delta >= 0:
                    per_status.setdefault(frm, []).append(delta)
            if to:
                entered[to] = at
    a.stage_durations = {status: _summarize_seconds(secs)
                         for status, secs in sorted(per_status.items())}

    # -- waits -------------------------------------------------------------
    waits = [r for r in records if r.get("event") == "run_finished_waiting"]
    a.waits = {"total": len(waits),
               "by_role": dict(Counter(r.get("role") or "?" for r in waits).most_common())}

    # -- escalations -------------------------------------------------------
    agent_esc = [r for r in records if r.get("event") == "escalation"]
    orch_esc = [r for r in records if r.get("event") == "orchestrator_escalation"]
    reasons: Counter = Counter()
    for r in agent_esc + orch_esc:
        reason = _TICKET_RE.sub("<ticket>", str(r.get("reason") or "")).strip()
        if reason:
            reasons[reason[:120]] += 1
    a.escalations = {
        "total": len(agent_esc) + len(orch_esc),
        "by_source": {"agent": len(agent_esc), "orchestrator": len(orch_esc)},
        "by_role": dict(Counter(r.get("role") or "?" for r in agent_esc).most_common()),
        "top_reasons": [{"reason": reason, "count": n} for reason, n in reasons.most_common(5)],
    }

    # -- rework ------------------------------------------------------------
    rejections = [r for r in records if r.get("event") == "rejection"]
    increments = [r for r in records if r.get("event") == "rework_incremented"]
    a.rework = {
        "rejections": len(rejections),
        "by_rejected_from": dict(Counter(r.get("rejected_from") or "?"
                                          for r in rejections).most_common()),
        "increments": sum(1 for r in increments if not r.get("already_counted")),
        "loop_breaker_hits": sum(1 for r in increments if r.get("exceeded")),
    }

    # -- reliability -------------------------------------------------------
    a.reliability = {
        "agent_crashes": sum(1 for r in records if r.get("event") == "agent_crash"),
        "turn_cap_hits": sum(1 for r in records if r.get("event") == "turn_cap_hit"),
        "lease_reclaims": sum(1 for r in records if r.get("event") == "lease_reclaimed"),
        "sweep_failures": sum(1 for r in records if r.get("event") == "sweep_failed"),
    }
    return a


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def _kv_lines(counter: dict, indent: str = "    ") -> list[str]:
    return [f"{indent}{k}: {v}" for k, v in counter.items()] or [f"{indent}(none)"]


def render_text(a: Analytics) -> str:
    t, r = a.throughput, a.reliability
    lines = [
        "SENTINEL WORKFLOW ANALYTICS",
        f"window: {a.since or '-'}  ->  {a.until or '-'}   ({a.records} audit record(s))",
        "",
        "THROUGHPUT",
        f"  transitions: {t['transitions']}   completed (-> Done): {t['completed']}   "
        f"dispatches: {t['dispatches']}",
        "  dispatches by role:",
        *_kv_lines(t["dispatches_by_role"], "    "),
        "",
        "STAGE DURATIONS (time-in-status, audit-derived; bottlenecks first)",
    ]
    ranked = sorted(a.stage_durations.items(),
                    key=lambda kv: kv[1]["avg_seconds"], reverse=True)
    if ranked:
        for status, s in ranked:
            lines.append(f"  {status:<24} n={s['count']:<4} avg={_fmt_duration(s['avg_seconds'])}"
                         f"  p50={_fmt_duration(s['p50_seconds'])}  max={_fmt_duration(s['max_seconds'])}")
    else:
        lines.append("    (no paired transitions in window)")
    lines += [
        "",
        f"WAITS (runs ended waiting on a human): {a.waits['total']}",
        *_kv_lines(a.waits["by_role"], "    "),
        "",
        f"ESCALATIONS: {a.escalations['total']}  "
        f"(agent={a.escalations['by_source']['agent']}, "
        f"orchestrator={a.escalations['by_source']['orchestrator']})",
        "  by role:",
        *_kv_lines(a.escalations["by_role"], "    "),
        "  top reasons:",
        *([f"    {e['count']}x  {e['reason']}" for e in a.escalations["top_reasons"]]
          or ["    (none)"]),
        "",
        f"REWORK: {a.rework['rejections']} rejection(s), "
        f"{a.rework['increments']} increment(s), "
        f"{a.rework['loop_breaker_hits']} loop-breaker hit(s)",
        "  by rejected_from:",
        *_kv_lines(a.rework["by_rejected_from"], "    "),
        "",
        "RELIABILITY",
        f"  crashes: {r['agent_crashes']}   turn-cap aborts: {r['turn_cap_hits']}   "
        f"lease reclaims: {r['lease_reclaims']}   sweep failures: {r['sweep_failures']}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sentinel.analytics",
        description="Workflow analytics derived from the Sentinel audit trail "
                    "(throughput, stage bottlenecks, waits, rework, escalations, reliability).")
    parser.add_argument("--since", default="7d",
                        help="time window: a duration (7d, 24h, 30m, 2w) or an ISO date "
                             "(default: 7d)")
    parser.add_argument("--file", type=Path, default=None,
                        help="audit.jsonl path (default: ${DATA_DIR:-/data}/audit.jsonl)")
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="output format (default: text)")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        since_dt = parse_since(args.since)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    path = args.file or _default_audit_path()
    log_ = AuditLog(path, max_bytes=0, backup_count=DEFAULT_BACKUP_COUNT)
    # A high limit + the since filter returns every in-window record (bounded,
    # single-project deployment — the audit log is size-rotated regardless).
    records = log_.read_records(limit=1_000_000,
                                since=since_dt.isoformat(timespec="seconds"))
    analytics = compute_analytics(records)

    if args.format == "json":
        print(json.dumps(analytics.as_dict(), indent=2, default=str))
    else:
        print(render_text(analytics))
    return 0


if __name__ == "__main__":
    sys.exit(main())
