"""Async client for self-hosted Jira (Server / Data Center, REST API v2, PAT bearer auth).

Machine state (leases, rework counters, deployed builds, waiting markers) is stored in
Jira issue *properties* — no custom-field administration required. Labels carry the
human-visible flags (agent-leased, needs-human, ...).
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

log = logging.getLogger("sentinel.jira")

# Issue property keys (the "custom fields" of 00-overview, without Jira admin work)
PROP_LEASE = "sentinel.lease"
PROP_REWORK = "sentinel.rework"          # {"count": n, "rejected_from": "...", "history": [...]}
PROP_WAITING = "sentinel.waiting"        # {"since": iso, "reason": str, "wake_at": iso|null}
PROP_DEPLOYED = "sentinel.deployed"      # {"<env>": {"build": str, "at": iso}}
PROP_RETRIES = "sentinel.retries"        # {"count": n} — orchestrator crash/reclaim retries


class JiraError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"Jira API error {status_code}: {message}")
        self.status_code = status_code


ISSUE_FIELDS = "summary,description,status,labels,assignee,issuetype,priority,updated,created,issuelinks,reporter"


# Transient server/proxy conditions that mean "the request was NOT processed":
# safe to retry for any method. Jira Server/DC emits these under load, during GC
# pauses, reindexing, or reverse-proxy failover.
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
# Methods with no side effect (or naturally idempotent): safe to retry even when a
# network error leaves the outcome unknown. A mutating POST (comment, transition,
# create) is NOT retried on an ambiguous transport error, to avoid duplicates.
IDEMPOTENT_METHODS = frozenset({"GET", "PUT", "DELETE"})


class JiraClient:
    def __init__(self, base_url: str, pat: str, timeout: float = 30.0,
                 max_retries: int = 3, backoff_base: float = 0.5,
                 backoff_cap: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/rest/api/2",
            headers={"Authorization": f"Bearer {pat}", "Accept": "application/json"},
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        """Sleep before a retry: honor a server Retry-After if given, else
        exponential backoff, both with jitter and capped."""
        delay: float | None = None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = None
        if delay is None:
            delay = self.backoff_base * (2 ** attempt)
        delay = min(delay, self.backoff_cap)
        delay += random.uniform(0, delay * 0.25)  # jitter to avoid thundering herd
        if delay > 0:
            await asyncio.sleep(delay)

    async def _request(self, method: str, path: str, *,
                       idempotent: bool | None = None, **kwargs) -> httpx.Response:
        if idempotent is None:
            idempotent = method.upper() in IDEMPOTENT_METHODS
        attempt = 0
        while True:
            try:
                resp = await self._client.request(method, path, **kwargs)
            except httpx.TransportError as e:
                # Network/timeout: only retry when the call is safe to repeat — a
                # mutating POST may already have taken effect server-side.
                if idempotent and attempt < self.max_retries:
                    log.warning("Jira %s %s transport error (%s), retry %d/%d",
                                method, path, e, attempt + 1, self.max_retries)
                    await self._backoff(attempt, None)
                    attempt += 1
                    continue
                raise JiraError(0, f"transport error: {e}") from e
            if resp.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                log.warning("Jira %s %s returned %d, retry %d/%d", method, path,
                            resp.status_code, attempt + 1, self.max_retries)
                await self._backoff(attempt, resp.headers.get("Retry-After"))
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise JiraError(resp.status_code, resp.text[:2000])
            return resp

    # -- identity ------------------------------------------------------------

    async def myself(self) -> dict:
        return (await self._request("GET", "/myself")).json()

    # -- issues --------------------------------------------------------------

    async def get_issue(self, key: str, with_comments: bool = True) -> dict:
        # Single-issue reads include attachments (evidence files); searches stay light.
        fields = ISSUE_FIELDS + ",attachment" + (",comment" if with_comments else "")
        return (await self._request("GET", f"/issue/{key}", params={"fields": fields})).json()

    async def search(self, jql: str, max_results: int = 100, fields: str = ISSUE_FIELDS) -> list[dict]:
        issues: list[dict] = []
        start = 0
        while True:
            payload = {"jql": jql, "startAt": start, "maxResults": min(max_results - len(issues), 50),
                       "fields": fields.split(",")}
            # /search is a read expressed as POST — safe to retry on transport errors.
            data = (await self._request("POST", "/search", json=payload, idempotent=True)).json()
            page = data.get("issues", [])
            issues.extend(page)
            if not page or len(issues) >= min(max_results, data.get("total", 0)):
                return issues
            start = len(issues)

    async def create_issue(self, project: str, summary: str, description: str,
                           issue_type: str = "Task", labels: list[str] | None = None) -> dict:
        payload = {"fields": {
            "project": {"key": project},
            "summary": summary,
            "description": description,
            "issuetype": {"name": issue_type},
            **({"labels": labels} if labels else {}),
        }}
        return (await self._request("POST", "/issue", json=payload)).json()

    # -- transitions ---------------------------------------------------------

    async def list_transitions(self, key: str) -> list[dict]:
        data = (await self._request("GET", f"/issue/{key}/transitions")).json()
        return data.get("transitions", [])

    async def transition_to(self, key: str, target_status: str) -> None:
        """Transition an issue to the workflow status whose name matches target_status."""
        transitions = await self.list_transitions(key)
        for t in transitions:
            if t.get("to", {}).get("name", "").lower() == target_status.lower():
                await self._request("POST", f"/issue/{key}/transitions",
                                    json={"transition": {"id": t["id"]}})
                return
        available = [t.get("to", {}).get("name") for t in transitions]
        raise JiraError(400, f"No transition from current status of {key} to '{target_status}'. "
                             f"Available targets: {available}")

    # -- comments ------------------------------------------------------------

    async def add_comment(self, key: str, body: str) -> dict:
        return (await self._request("POST", f"/issue/{key}/comment", json={"body": body})).json()

    async def get_comments(self, key: str) -> list[dict]:
        data = (await self._request("GET", f"/issue/{key}/comment",
                                    params={"maxResults": 200})).json()
        return data.get("comments", [])

    # -- attachments (evidence files) ------------------------------------------

    async def download_attachment(self, content_url: str) -> bytes:
        """Fetch attachment binary content. The URL comes from the issue's
        attachment metadata and must stay on this Jira host (the PAT rides on
        every request from this client — never send it elsewhere)."""
        if not content_url.startswith(self.base_url + "/"):
            raise JiraError(400, f"attachment URL is not on {self.base_url}: {content_url}")
        # Absolute URL (outside the /rest/api/2 base); GET, so it retries like any read.
        resp = await self._request("GET", content_url, follow_redirects=True)
        return resp.content

    async def upload_attachment(self, key: str, filename: str, data: bytes,
                                content_type: str = "application/octet-stream") -> list[dict]:
        # X-Atlassian-Token: no-check is required to pass Jira's XSRF guard on
        # multipart uploads.
        resp = await self._request(
            "POST", f"/issue/{key}/attachments",
            headers={"X-Atlassian-Token": "no-check"},
            files={"file": (filename, data, content_type)})
        return resp.json()

    # -- labels / assignee ---------------------------------------------------

    async def update_labels(self, key: str, add: list[str] | None = None,
                            remove: list[str] | None = None) -> None:
        update = [{"add": l} for l in (add or [])] + [{"remove": l} for l in (remove or [])]
        if update:
            await self._request("PUT", f"/issue/{key}", json={"update": {"labels": update}})

    async def assign(self, key: str, username: str | None) -> None:
        # Jira Server/DC uses "name"; null unassigns
        await self._request("PUT", f"/issue/{key}/assignee", json={"name": username})

    # -- issue links ---------------------------------------------------------

    async def link_issues(self, inward_key: str, outward_key: str, link_type: str = "Relates") -> None:
        await self._request("POST", "/issueLink", json={
            "type": {"name": link_type},
            "inwardIssue": {"key": inward_key},
            "outwardIssue": {"key": outward_key},
        })

    # -- issue properties (machine state) -------------------------------------

    async def get_property(self, key: str, prop: str) -> Any | None:
        try:
            data = (await self._request("GET", f"/issue/{key}/properties/{prop}")).json()
            return data.get("value")
        except JiraError as e:
            if e.status_code == 404:
                return None
            raise

    async def set_property(self, key: str, prop: str, value: Any) -> None:
        await self._request("PUT", f"/issue/{key}/properties/{prop}", json=value)

    async def delete_property(self, key: str, prop: str) -> None:
        try:
            await self._request("DELETE", f"/issue/{key}/properties/{prop}")
        except JiraError as e:
            if e.status_code != 404:
                raise
