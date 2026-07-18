"""Transient-failure resilience for the Jira client.

Jira Server/DC intermittently returns 429/503 (rate limiting, GC pauses,
reindexing, proxy failover). A single blip must not fail an agent action or
flip /health to 'degraded'. These tests drive JiraClient over an httpx
MockTransport (no network, no real sleeps) to pin the retry contract:

- retry 429/502/503/504 for any method (the server did not process the request);
- retry transport errors only for idempotent methods (a mutating POST may already
  have taken effect — retrying could double-post);
- never retry ordinary 4xx;
- give up after max_retries and raise.
"""

import asyncio

import httpx
import pytest

from sentinel.jira import JiraClient, JiraError


def make_client(handler, max_retries=3):
    client = JiraClient("https://jira.example.com", "pat", max_retries=max_retries,
                        backoff_base=0.0, backoff_cap=0.0)   # no real sleeping
    client._client = httpx.AsyncClient(
        base_url="https://jira.example.com/rest/api/2",
        transport=httpx.MockTransport(handler))
    return client


def responder(statuses):
    """Return a handler that yields the given status codes in order, then 200s,
    counting how many times it was called."""
    calls = {"n": 0}
    seq = list(statuses)

    def handler(request):
        i = calls["n"]
        calls["n"] += 1
        code = seq[i] if i < len(seq) else 200
        if code == 200:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(code, text="transient", headers={"Retry-After": "0"})

    return handler, calls


def test_retries_transient_status_then_succeeds():
    handler, calls = responder([503, 503])
    client = make_client(handler)
    # get_property returns the body's "value" key; the 200 stub has none -> None.
    result = asyncio.run(client.get_property("SENT-1", "sentinel.lease"))
    assert result is None
    assert calls["n"] == 3            # two failures + one success


def test_gives_up_after_max_retries():
    handler, calls = responder([503, 503, 503, 503, 503])
    client = make_client(handler, max_retries=2)
    with pytest.raises(JiraError) as exc:
        asyncio.run(client.myself())
    assert exc.value.status_code == 503
    assert calls["n"] == 3            # initial try + 2 retries


def test_no_retry_on_ordinary_4xx():
    handler, calls = responder([400])
    client = make_client(handler)
    with pytest.raises(JiraError) as exc:
        asyncio.run(client.myself())
    assert exc.value.status_code == 400
    assert calls["n"] == 1            # 4xx is a real error — fail fast


def test_429_is_retried():
    handler, calls = responder([429])
    client = make_client(handler)
    asyncio.run(client.myself())
    assert calls["n"] == 2            # rate-limit backed off and retried


def test_transport_error_retried_for_idempotent_get():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("connection reset")
        return httpx.Response(200, json={"displayName": "Bot"})

    client = make_client(handler)
    me = asyncio.run(client.myself())      # GET — safe to retry
    assert me["displayName"] == "Bot"
    assert calls["n"] == 3


def test_transport_error_not_retried_for_mutating_post():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("connection reset")

    client = make_client(handler)
    # add_comment is a mutating POST — a lost response might mean it WAS posted,
    # so we must not silently retry and risk a duplicate comment.
    with pytest.raises(JiraError):
        asyncio.run(client.add_comment("SENT-1", "hi"))
    assert calls["n"] == 1


def test_search_post_is_retried_as_idempotent():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("blip")
        return httpx.Response(200, json={"issues": [], "total": 0})

    client = make_client(handler)
    issues = asyncio.run(client.search("project = SENT"))   # POST /search, read-only
    assert issues == []
    assert calls["n"] == 2
