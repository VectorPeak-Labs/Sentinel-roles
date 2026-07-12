"""Lease protocol tests (docs/00-overview §Lease protocol)."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from sentinel.jira import PROP_LEASE
from sentinel.lease import LeaseError, LeaseManager

from fakes import FakeJira


def manager(jira, timeout=1800):
    return LeaseManager(jira, "sentinel-bot", "agent-leased", timeout)


def ts(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)) \
        .isoformat(timespec="seconds")


def test_is_stale_boundaries():
    m = manager(FakeJira(), timeout=1800)
    assert m.is_stale(None)                                   # no lease at all
    assert m.is_stale({})                                     # no timestamps
    assert m.is_stale({"heartbeat": "not-a-date"})            # unparseable
    assert not m.is_stale({"heartbeat": ts(60)})              # fresh
    assert m.is_stale({"heartbeat": ts(1801)})                # just past timeout
    assert not m.is_stale({"started": ts(60)})                # falls back to started


def test_claim_sets_property_label_and_comment():
    jira = FakeJira()
    m = manager(jira)
    asyncio.run(m.claim("SENT-1", "03-business-analyst"))
    lease = jira.properties[("SENT-1", PROP_LEASE)]
    assert lease["role"] == "03-business-analyst"
    assert lease["agent"] == "sentinel-bot"
    assert "agent-leased" in jira.labels["SENT-1"]
    assert any("lease claimed" in c for c in jira.comments["SENT-1"])


def test_claim_refused_while_actively_leased():
    jira = FakeJira()
    jira.properties[("SENT-1", PROP_LEASE)] = {"role": "other", "heartbeat": ts(60)}
    with pytest.raises(LeaseError):
        asyncio.run(manager(jira).claim("SENT-1", "03-business-analyst"))


def test_claim_overwrites_stale_lease():
    jira = FakeJira()
    jira.properties[("SENT-1", PROP_LEASE)] = {"role": "other", "heartbeat": ts(9999)}
    asyncio.run(manager(jira).claim("SENT-1", "03-business-analyst"))
    assert jira.properties[("SENT-1", PROP_LEASE)]["role"] == "03-business-analyst"


def test_heartbeat_refreshes_and_detects_loss():
    jira = FakeJira()
    m = manager(jira)

    async def main():
        await m.claim("SENT-1", "03-business-analyst")
        old = jira.properties[("SENT-1", PROP_LEASE)]["heartbeat"]
        await asyncio.sleep(0)
        await m.heartbeat("SENT-1", "03-business-analyst")
        assert jira.properties[("SENT-1", PROP_LEASE)]["heartbeat"] >= old
        # another role took over (or a human cleared it) -> heartbeat must fail loudly
        jira.properties[("SENT-1", PROP_LEASE)] = {"role": "somebody-else"}
        with pytest.raises(LeaseError):
            await m.heartbeat("SENT-1", "03-business-analyst")

    asyncio.run(main())


def test_release_is_idempotent():
    jira = FakeJira()
    m = manager(jira)

    async def main():
        await m.claim("SENT-1", "03-business-analyst")
        await m.release("SENT-1")
        await m.release("SENT-1")  # second release must not raise

    asyncio.run(main())
    assert ("SENT-1", PROP_LEASE) not in jira.properties
    assert "agent-leased" not in jira.labels["SENT-1"]
