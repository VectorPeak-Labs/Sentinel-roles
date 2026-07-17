"""Prometheus metrics — the observability the platform was missing.

`/health` is a point-in-time snapshot; it cannot tell you the dispatch rate, how
often the pipeline is escalating to humans, or whether sweeps are quietly
failing over the last hour. For an autonomous system meant to run unattended,
that trend visibility is what makes it monitorable and alertable.

This is a tiny hand-rolled registry (no new dependency): monotonic **counters**
incremented from the orchestrator/agents at the same points they already audit,
plus live **gauges** sampled from orchestrator state at scrape time. `render()`
emits the standard Prometheus text exposition format.
"""

from __future__ import annotations

import threading

# name (without the sentinel_ prefix) -> HELP text. TYPE is counter.
COUNTERS: dict[str, str] = {
    "dispatches_total": "Role-agent dispatches scheduled by the orchestrator.",
    "escalations_total": "Tickets frozen with needs-human (orchestrator + agents).",
    "lease_reclaims_total": "Stale leases reclaimed by the orchestrator.",
    "sweep_failures_total": "Board sweeps that failed (Jira unreachable, etc.).",
    "transitions_validated_total": "Agent transitions accepted with a valid handoff payload.",
    "handoff_invalid_total": "Agent transitions rejected for a missing/invalid handoff payload.",
    "stale_escalation_reminders_total": "Reminders sent for tickets left frozen awaiting a human.",
}


class Metrics:
    """Thread-safe monotonic counters (the audit log writes from a thread lock too)."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {name: 0 for name in COUNTERS}
        self._lock = threading.Lock()

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            if name in self._counts:              # unknown names are ignored, never raise
                self._counts[name] += amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


def _fmt_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    inner = ",".join(
        f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
        for k, v in labels.items())
    return "{" + inner + "}"


def _line(name: str, value, labels: str = "") -> str:
    return f"sentinel_{name}{labels} {value}"


def render(counters: dict[str, int], gauges: dict[str, tuple[str, float | int]],
           labeled_gauges: dict[str, tuple[str, list]] | None = None,
           labeled_counters: dict[str, tuple[str, list]] | None = None) -> str:
    """Prometheus text exposition.

    `gauges` maps name -> (help, value). `labeled_gauges` and `labeled_counters`
    map name -> (help, [(labels_dict, value), ...]) for multi-series metrics such
    as per-status queue depth (`sentinel_tickets_in_status{status="In Progress"}`)
    or per-role token usage (`sentinel_llm_prompt_tokens_total{role=...,model=...}`).
    """
    out: list[str] = []
    for name, help_text in COUNTERS.items():
        out.append(f"# HELP sentinel_{name} {help_text}")
        out.append(f"# TYPE sentinel_{name} counter")
        out.append(_line(name, counters.get(name, 0)))
    for name, (help_text, series) in (labeled_counters or {}).items():
        out.append(f"# HELP sentinel_{name} {help_text}")
        out.append(f"# TYPE sentinel_{name} counter")
        for labels, value in series:
            out.append(_line(name, value, _fmt_labels(labels)))
    for name, (help_text, value) in gauges.items():
        out.append(f"# HELP sentinel_{name} {help_text}")
        out.append(f"# TYPE sentinel_{name} gauge")
        out.append(_line(name, value))
    for name, (help_text, series) in (labeled_gauges or {}).items():
        out.append(f"# HELP sentinel_{name} {help_text}")
        out.append(f"# TYPE sentinel_{name} gauge")
        for labels, value in series:
            out.append(_line(name, value, _fmt_labels(labels)))
    return "\n".join(out) + "\n"
