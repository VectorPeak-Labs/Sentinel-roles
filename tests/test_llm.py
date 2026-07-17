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


def test_failure_increments_and_records_sanitized_error():
    llm = make_llm([RuntimeError("Bearer sk-secret leaked in body"),
                    RuntimeError("Bearer sk-secret leaked in body")])
    for _ in range(2):
        with pytest.raises(RuntimeError):
            asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
    assert llm.consecutive_failures == 2
    # last_error is exposed on /health and /metrics: only the exception type
    # may appear there, never the message (which can carry secrets/prompts).
    assert llm.last_error == "RuntimeError"
    assert "sk-secret" not in llm.last_error


def test_failure_with_status_code_records_type_and_status_only():
    err = RuntimeError("401 Unauthorized: api key sk-secret rejected")
    err.status_code = 401
    llm = make_llm([err])
    with pytest.raises(RuntimeError):
        asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
    assert llm.last_error == "RuntimeError (HTTP 401)"
    assert "sk-secret" not in llm.last_error


def test_failure_log_carries_sanitized_error_only(caplog):
    # The warning log ships to shared log stores just like /health ships to
    # monitoring: neither may carry the raw exception message.
    llm = make_llm([RuntimeError("Bearer sk-secret leaked in body")])
    with caplog.at_level("WARNING", logger="sentinel.llm"):
        with pytest.raises(RuntimeError):
            asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
    assert "sk-secret" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_success_after_failures_resets_the_signal():
    llm = make_llm([RuntimeError("down"), "ok"])
    with pytest.raises(RuntimeError):
        asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
    assert llm.consecutive_failures == 1
    asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
    assert llm.consecutive_failures == 0
    assert llm.last_error is None
