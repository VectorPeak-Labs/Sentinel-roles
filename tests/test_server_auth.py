"""Control-plane authentication for the mutating endpoints.

/webhook/jira, /sweep, /pause and /resume can freeze or nudge the whole
pipeline, so they share one guard. These tests pin its contract: constant-time
secret check, three accepted presentation channels (header, bearer, query),
rejection of wrong/missing tokens, and the documented open mode when no secret
is configured.

The server module builds its singletons at import, so env is set before import;
the orchestrator loop is never started (no TestClient lifespan), so nothing
touches the network.
"""

import os
import tempfile

# The server module builds its singletons at import — including an AuditLog that
# mkdir's DATA_DIR (default /data, not writable on CI). Point it at a temp dir and
# set the required Jira/LiteLLM env *before* importing sentinel.server.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("JIRA_BASE_URL", "https://jira.example.com")
os.environ.setdefault("JIRA_PAT", "pat")
os.environ.setdefault("JIRA_PROJECT_KEY", "SENT")
os.environ.setdefault("LITELLM_BASE_URL", "https://llm.example.com")
os.environ.setdefault("LITELLM_API_KEY", "sk")

import asyncio

import pytest
from fastapi import HTTPException

import sentinel.server as srv

SECRET = "s3cret-token"


def ok(**kw):
    return srv._authorized(SECRET, kw.get("token", ""),
                           kw.get("x_sentinel_token"), kw.get("authorization"))


def test_accepts_correct_query_token():
    assert ok(token=SECRET) is True              # Jira-webhook style (URL param)


def test_accepts_x_sentinel_token_header():
    assert ok(x_sentinel_token=SECRET) is True    # keeps the secret out of URLs


def test_accepts_authorization_bearer_header():
    assert ok(authorization=f"Bearer {SECRET}") is True
    assert ok(authorization=f"bearer {SECRET}") is True   # scheme case-insensitive


def test_rejects_wrong_token():
    assert ok(token="wrong") is False


def test_rejects_missing_token():
    assert ok() is False


def test_rejects_wrong_length_token_without_error():
    # hmac.compare_digest handles unequal lengths safely (returns False, no raise).
    assert ok(x_sentinel_token=SECRET + "extra") is False


def test_header_takes_precedence_over_empty_query():
    assert ok(token="", x_sentinel_token=SECRET) is True


def test_open_when_secret_unset():
    assert srv._authorized("", "", None, None) is True          # documented open mode
    assert srv._authorized("", "anything", None, None) is True


def test_require_auth_dependency_raises_403_on_bad_token():
    original = srv.settings.webhook_secret
    srv.settings.webhook_secret = SECRET
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(srv.require_auth(token="wrong",
                                         x_sentinel_token=None, authorization=None))
        assert exc.value.status_code == 403
        # correct token passes cleanly
        assert asyncio.run(srv.require_auth(token=SECRET,
                                            x_sentinel_token=None,
                                            authorization=None)) is None
    finally:
        srv.settings.webhook_secret = original


def test_health_reports_degraded_when_llm_failing():
    srv.orchestrator.agent_user = "bot"          # past 'starting'
    srv.orchestrator.paused = False
    srv.orchestrator.consecutive_sweep_failures = 0
    srv.llm.consecutive_failures = 0
    healthy = asyncio.run(srv.health())
    assert healthy["status"] == "ok"
    assert healthy["llm"]["ok"] is True

    srv.llm.consecutive_failures = srv.LLM_DEGRADED_AFTER   # backend down
    degraded = asyncio.run(srv.health())
    assert degraded["status"] == "degraded"
    assert degraded["llm"]["ok"] is False
    assert degraded["llm"]["consecutive_failures"] == srv.LLM_DEGRADED_AFTER
    srv.llm.consecutive_failures = 0             # restore for other tests


def test_metrics_exposes_llm_token_usage_by_role_and_model():
    srv.llm.usage_totals[("07-implementer", "gpt-4o")] = {
        "calls": 2, "prompt_tokens": 12, "completion_tokens": 5}
    try:
        out = asyncio.run(srv.prometheus_metrics())
        assert "# TYPE sentinel_llm_calls_total counter" in out
        assert ('sentinel_llm_calls_total'
                '{role="07-implementer",model="gpt-4o"} 2') in out
        assert ('sentinel_llm_prompt_tokens_total'
                '{role="07-implementer",model="gpt-4o"} 12') in out
        assert ('sentinel_llm_completion_tokens_total'
                '{role="07-implementer",model="gpt-4o"} 5') in out
    finally:
        srv.llm.usage_totals.clear()             # restore for other tests


def test_audit_endpoint_returns_filtered_records():
    srv.audit.record("dispatch", ticket="SENT-77", role="07-implementer")
    srv.audit.record("escalation", ticket="SENT-77", reason="boom")
    srv.audit.record("dispatch", ticket="SENT-78", role="03-business-analyst")

    out = asyncio.run(srv.audit_query(limit=100, ticket="SENT-77"))
    assert out["count"] == 2
    assert [r["event"] for r in out["records"]] == ["dispatch", "escalation"]

    out = asyncio.run(srv.audit_query(limit=100, ticket="SENT-77", event="escalation"))
    assert out["count"] == 1 and out["records"][0]["reason"] == "boom"


def test_metrics_llm_gauges_healthy():
    # Pin the Prometheus exposition contract for the LLM health gauges (issue #15):
    # below the degraded threshold, llm_up reads 1 and the failure count is exact.
    srv.llm.consecutive_failures = srv.LLM_DEGRADED_AFTER - 1
    try:
        out = asyncio.run(srv.prometheus_metrics())
        assert "sentinel_llm_up 1" in out
        assert f"sentinel_llm_consecutive_failures {srv.LLM_DEGRADED_AFTER - 1}" in out
    finally:
        srv.llm.consecutive_failures = 0         # restore for other tests


def test_metrics_llm_gauges_degraded():
    # At the threshold the same series must flip llm_up to 0 while still
    # exposing the exact failure count.
    srv.llm.consecutive_failures = srv.LLM_DEGRADED_AFTER
    try:
        out = asyncio.run(srv.prometheus_metrics())
        assert "sentinel_llm_up 0" in out
        assert f"sentinel_llm_consecutive_failures {srv.LLM_DEGRADED_AFTER}" in out
    finally:
        srv.llm.consecutive_failures = 0         # restore for other tests
