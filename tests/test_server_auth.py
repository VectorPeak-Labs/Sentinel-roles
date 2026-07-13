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
