"""Passive LLM health tracking (sentinel/llm.py).

Every real chat call updates a health signal so /health and /metrics can show a
dead LiteLLM backend instead of reading 'ok' while every agent run crashes.
"""

import asyncio
from types import SimpleNamespace

import pytest

from sentinel.llm import LLM


class FakeCompletions:
    """Replays a behavior list: an Exception raises, anything else returns a
    normal chat-completion message."""
    def __init__(self, behavior):
        self.behavior = list(behavior)
        self.calls = 0

    async def create(self, **kwargs):
        i = min(self.calls, len(self.behavior) - 1)
        self.calls += 1
        item = self.behavior[i]
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])


def make_llm(behavior):
    llm = LLM("https://litellm.example.com/v1", "key", "gpt-4o")
    llm.client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions(behavior)))
    return llm


def test_success_records_last_ok_and_resets_failures():
    llm = make_llm(["ok"])
    msg = asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
    assert msg.content == "ok"
    assert llm.consecutive_failures == 0
    assert llm.last_error is None
    assert llm.last_ok_at is not None


def test_failure_increments_and_records_error():
    llm = make_llm([RuntimeError("connection refused"),
                    RuntimeError("connection refused")])
    for _ in range(2):
        with pytest.raises(RuntimeError):
            asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
    assert llm.consecutive_failures == 2
    assert "connection refused" in llm.last_error


def test_success_after_failures_resets_the_signal():
    llm = make_llm([RuntimeError("down"), "ok"])
    with pytest.raises(RuntimeError):
        asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
    assert llm.consecutive_failures == 1
    asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
    assert llm.consecutive_failures == 0
    assert llm.last_error is None
