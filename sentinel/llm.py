"""LLM access. Every AI call in the platform goes through the LiteLLM deployment
(OpenAI-compatible chat completions API)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from openai import AsyncOpenAI

log = logging.getLogger("sentinel.llm")


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

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   model: str | None = None, temperature: float | None = None):
        """One chat-completions call; returns the first choice's message object."""
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
            log.warning("chat completion failed: %s", e)
            raise
        self.consecutive_failures = 0
        self.last_error = None
        self.last_ok_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return response.choices[0].message

    async def close(self) -> None:
        await self.client.close()
