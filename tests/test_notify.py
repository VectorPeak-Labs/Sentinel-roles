"""Outbound alert channel (sentinel.notify.Notifier).

Uses httpx.MockTransport to capture what the Notifier would POST, without any
network. The invariants that matter: disabled when unconfigured, Slack-compatible
payload shape when enabled, and *never* raises on a failing/slow endpoint.
"""

import asyncio
import json

import httpx

from sentinel.notify import Notifier


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_disabled_notifier_is_noop():
    n = Notifier(webhook_url="")
    assert n.enabled is False
    assert asyncio.run(n.notify("agent_escalation", "hi", ticket="SENT-1")) is False
    assert n._client is None            # never even builds an HTTP client


def test_notify_posts_slack_compatible_payload():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, text="ok")

    n = Notifier("https://hooks.example.com/abc",
                 jira_base_url="https://jira.example.com/")
    n._client = _client(handler)
    ok = asyncio.run(n.notify("agent_escalation", "SENT-1 needs help",
                              ticket="SENT-1", reason="boom"))
    assert ok is True
    body = captured["json"]
    assert captured["url"] == "https://hooks.example.com/abc"
    assert body["text"] == "SENT-1 needs help"      # Slack renders this
    assert body["event"] == "agent_escalation"
    assert body["ticket"] == "SENT-1"
    assert body["url"] == "https://jira.example.com/browse/SENT-1"
    assert body["reason"] == "boom"
    asyncio.run(n.close())


def test_notify_omits_empty_and_absent_fields():
    captured = {}

    def handler(request):
        captured["json"] = json.loads(request.content)
        return httpx.Response(200)

    n = Notifier("https://hooks.example.com/abc")     # no jira base configured
    n._client = _client(handler)
    asyncio.run(n.notify("pipeline_paused", "paused", by="alice", reason=None))
    body = captured["json"]
    assert body["by"] == "alice"
    assert "url" not in body and "ticket" not in body  # no ticket/base -> no link
    assert "reason" not in body                        # None fields dropped


def test_notify_swallows_http_error():
    n = Notifier("https://hooks.example.com/abc")
    n._client = _client(lambda request: httpx.Response(500))
    assert asyncio.run(n.notify("e", "t")) is False    # 5xx -> False, no raise


def test_notify_swallows_transport_exception():
    def handler(request):
        raise httpx.ConnectError("endpoint down")

    n = Notifier("https://hooks.example.com/abc")
    n._client = _client(handler)
    assert asyncio.run(n.notify("e", "t")) is False    # exception caught, no raise
