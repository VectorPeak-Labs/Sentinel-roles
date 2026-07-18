"""Passive LLM health tracking (sentinel/llm.py).

Every real chat call updates a health signal so /health and /metrics can show a
dead LiteLLM backend instead of reading 'ok' while every agent run crashes.
"""

import asyncio
from types import SimpleNamespace

import pytest

from sentinel.llm import LLM


class FakeCompletions:
    """Replays a behavior list: an Exception raises, a (prompt, completion)
    tuple returns a message with that token usage, anything else returns a
    normal chat-completion message without a usage block (some backends omit it)."""
    def __init__(self, behavior):
        self.behavior = list(behavior)
        self.calls = 0

    async def create(self, **kwargs):
        i = min(self.calls, len(self.behavior) - 1)
        self.calls += 1
        item = self.behavior[i]
        if isinstance(item, Exception):
            raise item
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
        if isinstance(item, tuple):
            response.usage = SimpleNamespace(prompt_tokens=item[0],
                                             completion_tokens=item[1])
        return response


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


def test_usage_accumulates_per_role_and_model():
    llm = make_llm([(7, 3), (5, 2), (11, 4)])
    msgs = [{"role": "user", "content": "hi"}]
    asyncio.run(llm.chat(msgs, role="07-implementer"))
    asyncio.run(llm.chat(msgs, role="07-implementer"))
    asyncio.run(llm.chat(msgs, role="08-code-reviewer", model="gpt-5o"))
    assert llm.usage_totals[("07-implementer", "gpt-4o")] == {
        "calls": 2, "prompt_tokens": 12, "completion_tokens": 5}
    assert llm.usage_totals[("08-code-reviewer", "gpt-5o")] == {
        "calls": 1, "prompt_tokens": 11, "completion_tokens": 4}
    # snapshot returns Prometheus-ready label dicts in stable (sorted) order
    labels = [l for l, _ in llm.usage_snapshot()]
    assert labels == [{"role": "07-implementer", "model": "gpt-4o"},
                      {"role": "08-code-reviewer", "model": "gpt-5o"}]


def test_usage_counts_calls_even_when_backend_omits_usage():
    llm = make_llm(["ok"])   # response has no usage block
    asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))   # no role either
    assert llm.usage_totals[("unattributed", "gpt-4o")] == {
        "calls": 1, "prompt_tokens": 0, "completion_tokens": 0}


def test_failed_calls_record_no_usage():
    llm = make_llm([RuntimeError("down")])
    with pytest.raises(RuntimeError):
        asyncio.run(llm.chat([{"role": "user", "content": "hi"}], role="07-implementer"))
    assert llm.usage_totals == {}


def test_tokens_today_accumulates_and_resets_on_day_rollover(monkeypatch):
    import sentinel.llm as llm_mod
    monkeypatch.setattr(llm_mod, "_utc_today", lambda: "2026-07-17")
    llm = make_llm([(7, 3), (5, 2), (1, 1)])
    msgs = [{"role": "user", "content": "hi"}]
    asyncio.run(llm.chat(msgs, role="07-implementer"))
    asyncio.run(llm.chat(msgs, role="08-code-reviewer"))
    assert llm.tokens_today == 17                       # 7+3 + 5+2, across roles

    monkeypatch.setattr(llm_mod, "_utc_today", lambda: "2026-07-18")
    asyncio.run(llm.chat(msgs, role="07-implementer"))  # new UTC day
    assert llm.tokens_today == 2                        # window reset, 1+1
