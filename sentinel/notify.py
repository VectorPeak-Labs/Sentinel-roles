"""Outbound alerting: push critical events (escalations, freezes) to a webhook.

The pipeline freezes tickets for a human constantly — missing project commands,
ambiguity, rework loops, repeated crashes, invalid transitions. Until now the only
signal was a Jira comment plus the `needs-human` label, so *someone had to be
watching the board*. That is the gap this closes: the Notifier POSTs a compact
JSON message to a configured webhook the moment the pipeline needs attention.

The payload is **Slack-compatible** (an incoming-webhook consumer renders the
`text` field) and **generic** (structured `event` / `ticket` / `url` / extra
fields ride alongside for anything else — Slack ignores the extras).

Design:
- **Disabled by default.** With no `SENTINEL_ALERT_WEBHOOK_URL` configured the
  Notifier is a no-op, keeping the platform generic until a project opts in.
- **Best-effort and non-blocking.** A slow or failing alert endpoint must never
  block or crash the orchestrator/agent that raised the event — every failure is
  caught and logged, and the HTTP call is bounded by a short timeout.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("sentinel.notify")


class Notifier:
    def __init__(self, webhook_url: str = "", jira_base_url: str = "",
                 timeout: float = 10.0):
        self.webhook_url = (webhook_url or "").strip()
        self.jira_base_url = (jira_base_url or "").rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def _issue_url(self, ticket: str | None) -> str | None:
        if ticket and self.jira_base_url:
            return f"{self.jira_base_url}/browse/{ticket}"
        return None

    async def notify(self, event: str, text: str, *, ticket: str | None = None,
                     **fields) -> bool:
        """Send one alert. Returns True if it was delivered (2xx), False otherwise
        (including when the Notifier is disabled). Never raises."""
        if not self.enabled:
            return False
        payload: dict = {"text": text, "event": event}
        if ticket:
            payload["ticket"] = ticket
        url = self._issue_url(ticket)
        if url:
            payload["url"] = url
        payload.update({k: v for k, v in fields.items() if v is not None})
        try:
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=self.timeout)
            resp = await self._client.post(self.webhook_url, json=payload)
            if resp.status_code >= 400:
                log.warning("alert webhook returned %s for event '%s'",
                            resp.status_code, event)
                return False
            return True
        except Exception as e:  # network error, timeout, bad URL — never propagate
            log.warning("alert webhook failed for event '%s': %s", event, e)
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
