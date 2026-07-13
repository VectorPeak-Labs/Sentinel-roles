"""LLM access. Every AI call in the platform goes through the LiteLLM deployment
(OpenAI-compatible chat completions API)."""

from __future__ import annotations

import logging

from openai import AsyncOpenAI

log = logging.getLogger("sentinel.llm")


class LLM:
    def __init__(self, base_url: str, api_key: str, default_model: str):
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, max_retries=3)
        self.default_model = default_model

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   model: str | None = None, temperature: float | None = None):
        """One chat-completions call; returns the first choice's message object."""
        kwargs: dict = {"model": model or self.default_model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if temperature is not None:
            kwargs["temperature"] = temperature
        response = await self.client.chat.completions.create(**kwargs)
        return response.choices[0].message

    async def close(self) -> None:
        await self.client.close()
