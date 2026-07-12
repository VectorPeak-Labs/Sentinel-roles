"""Lease protocol (docs/00-overview §Lease protocol).

Claim: assignee = agent identity, label `agent-leased`, lease comment + property.
Heartbeat: refresh the lease property (and comment ref) while working.
Release: remove the label + property on any transition or clean exit.
Reclaim: the Orchestrator clears leases whose heartbeat is older than the timeout.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .jira import JiraClient, JiraError, PROP_LEASE

log = logging.getLogger("sentinel.lease")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


class LeaseError(RuntimeError):
    pass


class LeaseManager:
    def __init__(self, jira: JiraClient, agent_user: str, leased_label: str, timeout_seconds: int):
        self.jira = jira
        self.agent_user = agent_user
        self.leased_label = leased_label
        self.timeout = timedelta(seconds=timeout_seconds)

    def is_stale(self, lease: dict | None) -> bool:
        if not lease:
            return True
        heartbeat = _parse(lease.get("heartbeat", "")) or _parse(lease.get("started", ""))
        if heartbeat is None:
            return True
        return datetime.now(timezone.utc) - heartbeat > self.timeout

    async def claim(self, ticket: str, role_id: str) -> dict:
        """Claim a ticket for a role agent. Raises LeaseError if actively leased elsewhere."""
        existing = await self.jira.get_property(ticket, PROP_LEASE)
        if existing and not self.is_stale(existing):
            raise LeaseError(f"{ticket} is actively leased by {existing.get('role')} "
                             f"since {existing.get('started')}")
        lease = {"agent": self.agent_user, "role": role_id,
                 "started": _now(), "heartbeat": _now()}
        await self.jira.set_property(ticket, PROP_LEASE, lease)
        await self.jira.update_labels(ticket, add=[self.leased_label])
        try:
            await self.jira.assign(ticket, self.agent_user)
        except JiraError as e:
            log.warning("Could not set assignee on %s: %s", ticket, e)
        await self.jira.add_comment(
            ticket, f"[sentinel] lease claimed by {role_id} at {lease['started']}")
        return lease

    async def heartbeat(self, ticket: str, role_id: str) -> None:
        lease = await self.jira.get_property(ticket, PROP_LEASE)
        if not lease or lease.get("role") != role_id:
            raise LeaseError(f"lease on {ticket} lost (now: {lease})")
        lease["heartbeat"] = _now()
        await self.jira.set_property(ticket, PROP_LEASE, lease)

    async def release(self, ticket: str) -> None:
        await self.jira.delete_property(ticket, PROP_LEASE)
        await self.jira.update_labels(ticket, remove=[self.leased_label])

    async def reclaim(self, ticket: str, reason: str) -> None:
        """Orchestrator-side forced release of a dead lease."""
        await self.release(ticket)
        await self.jira.add_comment(ticket, f"[sentinel] lease reclaimed by orchestrator: {reason}")
