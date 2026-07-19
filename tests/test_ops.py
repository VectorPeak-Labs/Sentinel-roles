"""GET /ops.json — the operator status snapshot.

The server module builds its singletons at import, so env is set before import;
the orchestrator loop is never started, so nothing touches the network. The
snapshot builder is exercised with fake orchestrator/LLM state, and the wired
endpoint is called directly against the (idle) module singletons.
"""

import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("JIRA_BASE_URL", "https://jira.example.com")
os.environ.setdefault("JIRA_PAT", "pat")
os.environ.setdefault("JIRA_PROJECT_KEY", "SENT")
os.environ.setdefault("LITELLM_BASE_URL", "https://llm.example.com")
os.environ.setdefault("LITELLM_API_KEY", "sk")

import asyncio

import sentinel.server as srv


def _fake_orch(**over):
    base = dict(
        agent_user="sentinel-bot",
        paused=False, paused_at=None, pause_reason=None,
        last_sweep_at="2026-07-19T03:00:00+00:00", last_sweep_error=None,
        consecutive_sweep_failures=0, sweep_count=7,
        _pending_keys={"SENT-1"},
        llm_gated=False,
        running={("07-implementer", "SENT-42"): object()},
        board_state={"by_status": {"In Progress": 2, "Rework": 1}, "needs_human": 1,
                     "handoff_invalid": 0, "total": 3},
    )
    base.update(over)
    return SimpleNamespace(**base)


def _fake_llm(consecutive_failures=0):
    return SimpleNamespace(
        consecutive_failures=consecutive_failures,
        last_error=None, last_ok_at="2026-07-19T03:00:00+00:00",
        tokens_in_current_window=lambda: 1234,
    )


# --- overall status -------------------------------------------------------- #

def test_status_starting_when_no_agent_user():
    assert srv._overall_status(_fake_orch(agent_user=""), True) == "starting"


def test_status_paused_wins():
    assert srv._overall_status(_fake_orch(paused=True), True) == "paused"


def test_status_degraded_on_sweep_failures_or_llm():
    assert srv._overall_status(_fake_orch(consecutive_sweep_failures=2), True) == "degraded"
    assert srv._overall_status(_fake_orch(), False) == "degraded"


def test_status_ok():
    assert srv._overall_status(_fake_orch(), True) == "ok"


# --- snapshot shape -------------------------------------------------------- #

def test_snapshot_has_expected_sections_and_no_secrets():
    snap = srv.build_ops_snapshot(_fake_orch(), srv.settings, _fake_llm(), [])
    assert snap["status"] == "ok"
    assert snap["running_agents"] == [{"role": "07-implementer", "ticket": "SENT-42"}]
    assert snap["board"]["by_status"] == {"In Progress": 2, "Rework": 1}
    assert snap["board"]["needs_human"] == 1
    assert snap["board"]["sampled_at"] == "2026-07-19T03:00:00+00:00"
    assert snap["pending_webhook_evaluations"] == 1
    assert snap["llm"]["tokens_today"] == 1234
    assert "note" in snap["leases"]  # lease enumeration deferred, documented
    # No PAT / API key material leaks into the snapshot.
    blob = repr(snap)
    assert srv.settings.jira_pat not in blob
    assert srv.settings.litellm_api_key not in blob


# --- recent escalations: filter + sanitize --------------------------------- #

def test_recent_escalations_filters_and_sanitizes():
    records = [  # oldest-first, as read_records returns
        {"at": "t1", "event": "dispatch", "ticket": "SENT-1", "role": "03"},
        {"at": "t2", "event": "escalation", "ticket": "SENT-2", "role": "07",
         "reason": "deploy_production missing", "error": "secret-ish detail"},
        {"at": "t3", "event": "run_complete", "ticket": "SENT-2"},
        {"at": "t4", "event": "orchestrator_escalation", "ticket": "SENT-3", "role": "01"},
    ]
    out = srv._recent_escalations(records)
    # Newest-first, only attention events.
    assert [e["event"] for e in out] == ["orchestrator_escalation", "escalation"]
    assert out[0]["ticket"] == "SENT-3"
    # Only the safe subset of keys — no reason/error strings.
    assert set(out[1]) == {"at", "event", "ticket", "role"}
    assert all("error" not in e and "reason" not in e for e in out)


def test_recent_escalations_limit():
    records = [{"at": str(i), "event": "escalation", "ticket": f"S-{i}", "role": "07"}
               for i in range(25)]
    assert len(srv._recent_escalations(records, limit=10)) == 10


# --- the wired endpoint (idle singletons, no network) ---------------------- #

def test_ops_endpoint_smoke():
    snap = asyncio.run(srv.ops_status())
    # Loop never started in tests -> agent_user empty -> "starting".
    assert snap["status"] == "starting"
    for key in ("pause", "sweep", "llm", "board", "running_agents",
                "recent_escalations", "leases"):
        assert key in snap
    assert snap["recent_escalations"] == []  # empty audit log
