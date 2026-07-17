"""LLM access. Every AI call in the platform goes through the LiteLLM deployment
(OpenAI-compatible chat completions API)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from openai import AsyncOpenAI

log = logging.getLogger("sentinel.llm")


def _utc_today() -> str:
    """Current UTC date — the daily token-budget window (own function so tests
    can roll the day over)."""
    return datetime.now(timezone.utc).date().isoformat()


def _safe_error(e: Exception) -> str:
    """Sanitized error label for the health signal. Exception messages can carry
    request/response bodies (prompts, API keys in headers), and last_error is
    exposed on /health and /metrics — so keep only the type and HTTP status."""
    for attr in ("status_code", "status"):
        status = getattr(e, attr, None)
        if isinstance(status, int):
            return f"{type(e).__name__} (HTTP {status})"
    return type(e).__name__


class LLM:
    def __init__(self, base_url: str, api_key: str, default_model: str):
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, max_retries=3)
        self.default_model = default_model
        # Passive health signal (no extra calls): every real chat updates these so
        # /health and /metrics can show a dead LiteLLM backend instead of reading
        # 'ok' while every agent run silently crashes and escalates.
        self.consecutive_failures = 0
        self.last_error: str | None = None
        self.last_ok_at: str | None = None
        # Cumulative token usage per (role, model), fed to /metrics. Every agent
        # action is a billed LLM call; without this a runaway loop burns budget
        # invisibly. Only touched from the event loop — no lock needed.
        self.usage_totals: dict[tuple[str, str], dict[str, int]] = {}
        # Rolling one-UTC-day token total, feeding the orchestrator's daily
        # budget circuit breaker (SENTINEL_LLM_DAILY_TOKEN_BUDGET).
        self.tokens_today = 0
        self._usage_day: str | None = None

    def _record_usage(self, role: str | None, model: str, usage) -> None:
        key = (role or "unattributed", model)
        totals = self.usage_totals.setdefault(
            key, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        totals["calls"] += 1
        # Some OpenAI-compatible backends omit usage; count the call regardless.
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        totals["prompt_tokens"] += prompt
        totals["completion_tokens"] += completion
        today = _utc_today()
        if today != self._usage_day:
            self._usage_day = today
            self.tokens_today = 0
        self.tokens_today += prompt + completion

    def usage_snapshot(self) -> list[tuple[dict[str, str], dict[str, int]]]:
        """[(labels, totals), ...] for the /metrics exposition, in stable order."""
        return [({"role": role, "model": model}, dict(totals))
                for (role, model), totals in sorted(self.usage_totals.items())]

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   model: str | None = None, temperature: float | None = None,
                   role: str | None = None):
        """One chat-completions call; returns the first choice's message object.
        `role` attributes the call's token usage to a pipeline role."""
        kwargs: dict = {"model": model or self.default_model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if temperature is not None:
            kwargs["temperature"] = temperature
        try:
            response = await self.client.chat.completions.create(**kwargs)
        except Exception as e:
            self.consecutive_failures += 1
            self.last_error = _safe_error(e)
            # Log the sanitized label, not the exception: client errors can carry
            # request/response bodies (prompts, bearer tokens), and app logs are
            # often shipped to shared stores.
            log.warning("chat completion failed: %s", self.last_error)
            raise
        self.consecutive_failures = 0
        self.last_error = None
        self.last_ok_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._record_usage(role, kwargs["model"], getattr(response, "usage", None))
        return response.choices[0].message

    async def close(self) -> None:
        await self.client.close()
