"""The Orchestrator (docs/01-orchestrator.md): pure traffic control.

Watches the board (webhooks + a full sweep every 15 minutes as the safety net),
dispatches role agents per the status table, enforces leases / WIP limits /
the rework loop-breaker, validates handoff payloads on agent transitions, and
escalates anything it cannot repair mechanically. It performs zero content work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from .agent import AgentRunner
from .audit import AuditLog
from .config import RoleConfig, Settings
from .jira import (JiraClient, JiraError, PROP_LEASE, PROP_REMINDED, PROP_RETRIES,
                   PROP_REWORK, PROP_WAITING)
from .lease import LeaseManager
from .llm import LLM
from .metrics import Metrics
from .notify import Notifier
from .payloads import find_payload, validate_handoff

log = logging.getLogger("sentinel.orchestrator")


def _parse_ts(value: str | None) -> datetime | None:
    """Parse both Jira ('2026-07-12T08:00:00.000+0000') and ISO timestamps."""
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z",):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class Orchestrator:
    def __init__(self, settings: Settings, jira: JiraClient, llm: LLM, audit: AuditLog,
                 notifier: Notifier | None = None, metrics: Metrics | None = None):
        self.settings = settings
        self.jira = jira
        self.llm = llm
        self.audit = audit
        # Disabled Notifier when none supplied, so `.notify(...)` is always safe.
        self.notifier = notifier or Notifier()
        self.metrics = metrics or Metrics()
        self.agent_user: str = ""
        self.leases: LeaseManager | None = None
        self.runner: AgentRunner | None = None
        # (role_id, ticket_key-or-None) -> (task, status_name)
        self.running: dict[tuple[str, str | None], tuple[asyncio.Task, str]] = {}
        self._queue_last_run: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._stopped = asyncio.Event()
        # Webhook debounce: bursts of events (batch edits, comment storms) coalesce
        # into one evaluation pass instead of one full queue re-search per event.
        self.webhook_debounce_seconds = 2.0
        self._pending_keys: set[str] = set()
        self._flush_task: asyncio.Task | None = None
        # /health diagnostics
        self.last_sweep_at: str | None = None
        self.sweep_count = 0
        self.consecutive_sweep_failures = 0
        self.last_sweep_error: str | None = None
        # Board snapshot refreshed each sweep — pipeline backlog for /metrics.
        self.board_state: dict = {"by_status": {}, "needs_human": 0,
                                  "handoff_invalid": 0, "total": 0}
        # Global pause (operational kill-switch): when set, no NEW agents are
        # dispatched (ticket or queue); in-flight runs drain to completion. The
        # state is persisted to disk so a container restart mid-incident does not
        # silently resume the pipeline.
        self.paused = False
        self.paused_at: str | None = None
        self.pause_reason: str | None = None

    async def start(self) -> None:
        me = await self.jira.myself()
        self.agent_user = me.get("name") or me.get("key") or "sentinel"
        self.leases = LeaseManager(self.jira, self.agent_user,
                                   self.settings.label("leased"),
                                   self.settings.lease_timeout)
        self.runner = AgentRunner(self.settings, self.jira, self.llm,
                                  self.leases, self.audit, self.agent_user,
                                  self.notifier, self.metrics)
        self._load_pause_state()
        log.info("orchestrator started as Jira user '%s', project %s, %d roles%s",
                 self.agent_user, self.settings.jira_project, len(self.settings.roles),
                 " (PAUSED)" if self.paused else "")
        self.audit.record("orchestrator_start", agent_user=self.agent_user,
                          paused=self.paused)

    # -- global pause (operational kill-switch) -----------------------------

    def _pause_file(self):
        return self.settings.data_dir / "pause.json"

    def _load_pause_state(self) -> None:
        """Restore a pause set before a restart — an incident freeze must survive
        a container bounce, never silently resume dispatching."""
        try:
            data = json.loads(self._pause_file().read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return
        if data.get("paused"):
            self.paused = True
            self.paused_at = data.get("at")
            self.pause_reason = data.get("reason")

    def _persist_pause_state(self, by: str) -> None:
        try:
            self._pause_file().parent.mkdir(parents=True, exist_ok=True)
            self._pause_file().write_text(json.dumps(
                {"paused": True, "reason": self.pause_reason,
                 "at": self.paused_at, "by": by}), encoding="utf-8")
        except OSError as e:
            # Persistence is best-effort: the in-memory pause still takes effect,
            # but warn loudly because a restart would then resume unexpectedly.
            log.warning("could not persist pause state to %s: %s", self._pause_file(), e)

    async def pause(self, reason: str = "", by: str = "operator") -> None:
        self.paused = True
        self.paused_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.pause_reason = reason.strip() or None
        self._persist_pause_state(by)
        log.warning("pipeline PAUSED by %s: %s", by, self.pause_reason or "(no reason given)")
        self.audit.record("pipeline_paused", by=by, reason=self.pause_reason)
        await self.notifier.notify(
            "pipeline_paused",
            f"⏸️ {self.settings.jira_project} pipeline PAUSED by {by}"
            + (f": {self.pause_reason}" if self.pause_reason else ""),
            by=by, reason=self.pause_reason)

    async def resume(self, by: str = "operator") -> None:
        self.paused = False
        self.paused_at = None
        self.pause_reason = None
        try:
            self._pause_file().unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("could not clear pause file %s: %s", self._pause_file(), e)
        log.warning("pipeline RESUMED by %s", by)
        self.audit.record("pipeline_resumed", by=by)
        await self.notifier.notify(
            "pipeline_resumed",
            f"▶️ {self.settings.jira_project} pipeline RESUMED by {by}", by=by)

    async def stop(self) -> None:
        self._stopped.set()
        running = list(self.running.items())
        tasks = [task for _, (task, _) in running]
        for task in tasks:
            task.cancel()
        # Let each cancelled agent run its own cleanup (release the ticket lease it
        # holds *and* any tickets a queue role self-claimed) before the HTTP clients
        # close in the lifespan shutdown. Bounded so one stuck agent can't hang exit.
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=self.settings.shutdown_grace_seconds)
            except Exception:
                log.warning("error while draining agents on shutdown")
        # Fallback for ticket-scoped roles whose cleanup did not finish in time
        # (release is idempotent, so double-releasing a freed lease is harmless).
        for (role_id, ticket), (task, _) in running:
            if ticket and self.leases:
                try:
                    await self.leases.release(ticket)
                except Exception:
                    log.warning("could not release %s on shutdown", ticket)

    # -- main loop -------------------------------------------------------

    async def run_forever(self) -> None:
        # Jira may still be starting up alongside us — retry with backoff, never die.
        delay = 5
        while not self._stopped.is_set():
            try:
                await self.start()
                break
            except Exception as e:
                log.error("startup failed (Jira unreachable?): %s — retrying in %ds", e, delay)
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    delay = min(delay * 2, 300)
        while not self._stopped.is_set():
            await self._sweep_safely()
            try:
                await asyncio.wait_for(self._stopped.wait(),
                                       timeout=self.settings.sweep_interval)
            except asyncio.TimeoutError:
                pass

    async def _sweep_safely(self) -> None:
        """Run one sweep, tracking consecutive failures for /health (an expired
        PAT or a down Jira must surface as 'degraded', not silent log spam)."""
        try:
            await self.sweep()
            self.consecutive_sweep_failures = 0
            self.last_sweep_error = None
        except JiraError as e:
            # 01 failure path: Jira unavailable -> halt dispatching, resume with full sweep
            self.consecutive_sweep_failures += 1
            self.last_sweep_error = str(e)
            self.metrics.inc("sweep_failures_total")
            log.error("sweep failed (Jira unavailable?): %s", e)
            self.audit.record("sweep_failed", error=str(e))
        except Exception as e:
            self.consecutive_sweep_failures += 1
            self.last_sweep_error = f"{type(e).__name__}: {e}"
            self.metrics.inc("sweep_failures_total")
            log.exception("sweep crashed")
            self.audit.record("sweep_failed", error=self.last_sweep_error)

    # -- sweep -------------------------------------------------------------

    async def sweep(self) -> None:
        self._gc_running()
        statuses = ", ".join(f'"{s}"' for s in self.settings.agent_statuses)
        issues = await self.jira.search(
            f"project = {self.settings.jira_project} AND status in ({statuses}) "
            f"ORDER BY updated ASC", max_results=500)
        log.info("sweep: %d ticket(s) in agent-owned statuses, %d agent(s) running%s",
                 len(issues), len(self.running), " [PAUSED]" if self.paused else "")
        self.last_sweep_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.sweep_count += 1
        self._compute_board_state(issues)
        async with self._lock:
            for issue in issues:
                try:
                    await self._evaluate_ticket(issue)
                except Exception:
                    log.exception("evaluation failed for %s", issue.get("key"))
            await self._evaluate_queues(issues)
        # Re-surface escalations a human has left frozen (ORC-1: nothing silently stuck).
        try:
            await self._remind_stale_escalations(issues)
        except Exception:
            log.exception("stale-escalation scan failed")

    async def _remind_stale_escalations(self, issues: list[dict]) -> None:
        """Escalation alerts fire once when a ticket freezes; if a human never
        acts, the ticket sits `needs-human` indefinitely with no further signal.
        Re-alert for tickets frozen and untouched beyond `stale_escalation_hours`,
        deduped per ticket to at most one reminder per window (sentinel.reminded)."""
        hours = self.settings.stale_escalation_hours
        if hours <= 0:
            return
        threshold = timedelta(hours=hours)
        now = datetime.now(timezone.utc)
        nh = self.settings.label("needs_human")
        hi = self.settings.label("handoff_invalid")
        for issue in issues:
            key = issue.get("key")
            fields = issue.get("fields", {})
            labels = fields.get("labels") or []
            if nh not in labels and hi not in labels:
                continue
            updated = _parse_ts(fields.get("updated"))
            if updated and (now - updated) < threshold:
                continue  # a human touched it recently — not abandoned
            reminded = await self.jira.get_property(key, PROP_REMINDED) or {}
            last = _parse_ts(reminded.get("at"))
            if last and (now - last) < threshold:
                continue  # already reminded within this window — don't spam
            await self.jira.set_property(
                key, PROP_REMINDED, {"at": now.isoformat(timespec="seconds")})
            self.metrics.inc("stale_escalation_reminders_total")
            self.audit.record("stale_escalation_reminder", ticket=key, hours=hours,
                              updated=fields.get("updated"))
            await self.notifier.notify(
                "stale_escalation",
                f"⏰ {self.settings.jira_project} {key} has been frozen awaiting a human "
                f"for over {int(hours)}h and nobody has acted. Remove `{nh}` to resume "
                f"or take the decision the ticket is blocked on.",
                ticket=key, hours=hours)

    def _compute_board_state(self, issues: list[dict]) -> None:
        """Snapshot the board for /metrics: per-status queue depth plus how many
        tickets are frozen (needs-human) or flagged (handoff-invalid)."""
        by_status: dict[str, int] = {}
        needs_human = handoff_invalid = 0
        nh = self.settings.label("needs_human")
        hi = self.settings.label("handoff_invalid")
        for issue in issues:
            fields = issue.get("fields", {})
            status = (fields.get("status") or {}).get("name") or "unknown"
            by_status[status] = by_status.get(status, 0) + 1
            labels = fields.get("labels") or []
            if nh in labels:
                needs_human += 1
            if hi in labels:
                handoff_invalid += 1
        self.board_state = {"by_status": by_status, "needs_human": needs_human,
                            "handoff_invalid": handoff_invalid, "total": len(issues)}

    def _gc_running(self) -> None:
        for key, (task, _) in list(self.running.items()):
            if task.done():
                del self.running[key]

    def _running_count_for_status(self, status: str) -> int:
        return sum(1 for _, s in self.running.values() if s.lower() == status.lower())

    # -- per-ticket dispatch ------------------------------------------------

    async def _evaluate_ticket(self, issue: dict) -> None:
        if self.paused:
            return  # global pause: dispatch nothing, take no side effects (drain)
        key = issue["key"]
        fields = issue.get("fields", {})
        status = (fields.get("status") or {}).get("name", "")
        labels = set(fields.get("labels", []))

        if self.settings.label("needs_human") in labels or \
           self.settings.label("handoff_invalid") in labels:
            return

        roles = [r for r in self.settings.roles_for_status(status) if r.trigger_type == "ticket"]
        if not roles:
            return
        role = roles[0]

        if role.require_label and self.settings.label(role.require_label) not in labels:
            return
        if (role.role_id, key) in self.running:
            return

        # Lease enforcement (ORC-1/ORC-2): active lease -> skip; stale -> reclaim + retry once
        lease = await self.jira.get_property(key, PROP_LEASE)
        if lease:
            if not self.leases.is_stale(lease):
                return
            await self.leases.reclaim(
                key, f"no heartbeat since {lease.get('heartbeat')} "
                     f"(timeout {self.settings.lease_timeout}s)")
            retries = await self.jira.get_property(key, PROP_RETRIES) or {"count": 0}
            retries["count"] = int(retries.get("count", 0)) + 1
            await self.jira.set_property(key, PROP_RETRIES, retries)
            self.metrics.inc("lease_reclaims_total")
            self.audit.record("lease_reclaimed", ticket=key, role=lease.get("role"),
                              retries=retries["count"])
            # fall through: the generic retry check below decides retry vs escalate

        # Retry limit (covers stale-lease reclaims, agent crashes and turn-cap aborts,
        # all of which bump sentinel.retries): retry once, then escalate (01 §failure paths).
        retries = await self.jira.get_property(key, PROP_RETRIES) or {}
        if int(retries.get("count", 0)) > 1:
            # Reset the counter so removing needs-human grants a fresh retry budget
            # instead of re-escalating on the next sweep.
            await self.jira.delete_property(key, PROP_RETRIES)
            await self._escalate(key, "Two consecutive agent runs on this stage failed "
                                      "(crash, timeout or lost heartbeat). A human should "
                                      "inspect the ticket and the sentinel logs, then remove "
                                      "the needs-human label to resume.")
            return

        # Rework loop-breaker safety net (ORC-4)
        if role.role_id.startswith("13-"):
            rework = await self.jira.get_property(key, PROP_REWORK) or {}
            if int(rework.get("count", 0)) > self.settings.rework_limit:
                await self._escalate(
                    key, f"rework_count is {rework.get('count')} (> {self.settings.rework_limit}). "
                         f"Loop-breaker: this ticket has a systemic problem another rework "
                         f"iteration won't fix. Bounce history is in the "
                         f"sentinel.rework property and the ticket comments.")
                return

        # Waiting marker: don't re-dispatch a ticket that is parked on a human
        waiting = await self.jira.get_property(key, PROP_WAITING)
        if waiting:
            updated = _parse_ts(fields.get("updated"))
            since = _parse_ts(waiting.get("since"))
            wake_at = _parse_ts(waiting.get("wake_at"))
            now = datetime.now(timezone.utc)
            fresh_activity = updated and since and updated > since
            wake_due = wake_at and now >= wake_at
            if not fresh_activity and not wake_due:
                return
            await self.jira.delete_property(key, PROP_WAITING)

        # WIP limit on concurrent agent dispatch per status (ORC-3)
        limit = self.settings.wip_limit(status)
        if limit is not None and self._running_count_for_status(status) >= limit:
            log.info("WIP limit for '%s' reached; %s waits", status, key)
            return

        kickoff = (f"You are activated for ticket **{key}**, currently in status "
                   f"**{status}**. Start by calling get_ticket(\"{key}\"), then follow "
                   f"your role document. End with exactly one terminal action.")
        self._spawn(role, key, status, kickoff)

    # -- queue roles ----------------------------------------------------------

    async def _evaluate_queues(self, issues: list[dict]) -> None:
        if self.paused:
            return  # global pause: no queue-role singletons while frozen
        by_status: dict[str, list[dict]] = {}
        for issue in issues:
            status = ((issue.get("fields") or {}).get("status") or {}).get("name", "")
            by_status.setdefault(status.lower(), []).append(issue)

        for role in self.settings.roles.values():
            if role.trigger_type != "queue" or (role.role_id, None) in self.running:
                continue
            queue = [i for s in role.statuses for i in by_status.get(s.lower(), [])]
            actionable = [
                i for i in queue
                if self.settings.label("needs_human") not in (i["fields"].get("labels") or [])
                and self.settings.label("handoff_invalid") not in (i["fields"].get("labels") or [])
            ]
            if not actionable:
                continue
            if not await self._queue_condition_met(role, actionable):
                continue

            listing = "\n".join(
                f"- {i['key']} [{(i['fields'].get('status') or {}).get('name')}] "
                f"{i['fields'].get('summary')} (labels: {', '.join(i['fields'].get('labels') or []) or '-'})"
                for i in actionable)
            kickoff = (f"Queue trigger fired for your role. Tickets currently in your "
                       f"input status(es):\n{listing}\n\n"
                       f"Claim each ticket before acting on it. Follow your role document; "
                       f"end with finish_run when the queue is handled (transitions release "
                       f"their own leases).")
            self._queue_last_run[role.role_id] = time.monotonic()
            self._spawn(role, None, role.statuses[0], kickoff)

    async def _queue_condition_met(self, role: RoleConfig, queue: list[dict]) -> bool:
        if role.condition == "capacity_in_progress":
            limit = self.settings.wip_limit("In Progress")
            if limit is not None:
                in_progress = await self.jira.search(
                    f'project = {self.settings.jira_project} AND status = "In Progress"',
                    max_results=limit + 1, fields="summary")
                if len(in_progress) >= limit:
                    return False
            return True
        if role.condition == "release_window":
            # Production deploys never fire just because the queue is non-empty (role 12):
            # a human opens the window with the release label.
            window = self.settings.label("release_window")
            return any(window in (i["fields"].get("labels") or []) for i in queue)
        if role.min_interval_seconds:
            force = self.settings.label("force_deploy")
            if any(force in (i["fields"].get("labels") or []) for i in queue):
                return True
            last = self._queue_last_run.get(role.role_id, 0.0)
            return time.monotonic() - last >= role.min_interval_seconds
        return True

    # -- dispatch / escalate ---------------------------------------------------

    def _spawn(self, role: RoleConfig, ticket: str | None, status: str, kickoff: str) -> None:
        task = asyncio.create_task(self.runner.run(role, ticket, kickoff),
                                   name=f"{role.role_id}:{ticket or 'queue'}")
        self.running[(role.role_id, ticket)] = (task, status)
        self.metrics.inc("dispatches_total")
        self.audit.record("dispatch_scheduled", role=role.role_id, ticket=ticket, status=status)
        log.info("dispatched %s on %s", role.role_id, ticket or "queue")

    async def _escalate(self, key: str, reason: str) -> None:
        await self.jira.update_labels(key, add=[self.settings.label("needs_human")])
        await self.jira.add_comment(
            key, f"[sentinel] ORCHESTRATOR ESCALATION\n\n{reason}\n\n"
                 f"Remove the `{self.settings.label('needs_human')}` label to resume the pipeline.")
        self.metrics.inc("escalations_total")
        self.audit.record("orchestrator_escalation", ticket=key, reason=reason)
        await self.notifier.notify(
            "orchestrator_escalation",
            f"🚨 {self.settings.jira_project} {key} frozen by the orchestrator — needs a human. "
            f"{reason}",
            ticket=key, source="orchestrator", reason=reason)

    # -- webhook handling ---------------------------------------------------

    async def handle_webhook(self, event: dict) -> None:
        if self.runner is None:  # event arrived before start() finished
            return
        issue = event.get("issue") or {}
        key = issue.get("key")
        if not key or not key.startswith(self.settings.jira_project + "-"):
            return
        actor = (event.get("user") or {}).get("name", "")

        changelog_items = (event.get("changelog") or {}).get("items", [])
        status_change = next((i for i in changelog_items if i.get("field") == "status"), None)

        if status_change:
            await self._on_status_change(key, actor, status_change)

        if event.get("comment") and actor and actor != self.agent_user:
            # A human commented — possibly the reply an agent is waiting for.
            log.info("human comment on %s by %s", key, actor)

        # Any activity: queue this ticket for a debounced evaluation pass
        # (webhooks are the fast path; the sweep remains the safety net).
        self._pending_keys.add(key)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_pending(),
                                                   name="webhook-flush")

    async def _flush_pending(self) -> None:
        """Drain the debounced webhook queue: one evaluation pass per burst."""
        while True:
            await asyncio.sleep(self.webhook_debounce_seconds)
            keys = set(self._pending_keys)
            self._pending_keys.clear()
            if not keys:
                return
            try:
                async with self._lock:
                    self._gc_running()
                    for key in keys:
                        try:
                            fresh = await self.jira.get_issue(key, with_comments=False)
                            await self._evaluate_ticket(fresh)
                        except JiraError as e:
                            log.error("webhook re-evaluation of %s failed: %s", key, e)
                    statuses = ", ".join(f'"{s}"' for s in self.settings.agent_statuses)
                    queue_issues = await self.jira.search(
                        f"project = {self.settings.jira_project} AND status in ({statuses})",
                        max_results=500)
                    await self._evaluate_queues(queue_issues)
            except Exception:
                log.exception("webhook flush failed")
            if not self._pending_keys:
                return

    async def _on_status_change(self, key: str, actor: str, change: dict) -> None:
        to_status = change.get("toString", "")
        from_status = change.get("fromString", "")

        if actor and actor != self.agent_user:
            # Universal rule 6: a human transition is logged, honored, never reverted.
            self.audit.record("human_transition", ticket=key, actor=actor,
                              from_status=from_status, to_status=to_status)
            return

        # ORC-5: an agent transition must carry a valid handoff payload.
        comments = await self.jira.get_comments(key)
        payload = None
        for comment in reversed(comments[-10:]):
            candidate = find_payload(comment.get("body") or "", "agent_handoff")
            if candidate and str(candidate.get("to_status", "")).lower() == to_status.lower():
                payload = candidate
                break
        if payload is None or not validate_handoff(payload).ok:
            self.metrics.inc("handoff_invalid_total")
            await self.jira.update_labels(key, add=[self.settings.label("handoff_invalid")])
            await self._escalate(
                key, f"Agent transition '{from_status}' -> '{to_status}' has no valid "
                     f"agent_handoff payload in the recent comments (ORC-5). The transition "
                     f"was NOT reverted; a human must review it.")
        else:
            # Clean handoff observed — reset the crash-retry counter for the new stage.
            self.metrics.inc("transitions_validated_total")
            await self.jira.delete_property(key, PROP_RETRIES)
            self.audit.record("agent_transition_validated", ticket=key,
                              from_status=from_status, to_status=to_status,
                              role=payload.get("role"), verdict=payload.get("verdict"))
